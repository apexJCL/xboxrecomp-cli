#!/usr/bin/env python3
"""The host's umu-run in ~/.local/bin is found by what ships to the host:
`<game> bench`'s prologue (prologue.sh.in, placeholder values) then
check_umu.sh, and the head of bench-matrix's DECK_RUN. Each runs here in
bash with a scratch HOME and a PATH without ~/.local/bin, as non-interactive
ssh gives on a Steam Deck whose sshd no longer reads ~/.ssh/environment.
Plain asserts; runs alone or under pytest.

  uv run python tests/test_bench_umu_path.py
  uv run pytest tests/test_bench_umu_path.py
"""

import os
import re
import stat
import subprocess
import sys
import tempfile

import pytest

from xboxrecomp_cli import bench_matrix
from xboxrecomp_cli.bench.remote import HOST_PATH, fill, host_text

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs bash host scripts")

PATH_LINE = 'PATH="$PATH:$HOME/.local/bin"'


def prologue():
    """The prologue with every @NAME@ but HOST_PATH set to a plain word, as
    Remote.prologue fills it."""
    names = set(re.findall(r"@([A-Z_]+)@", host_text("prologue.sh.in"))) - {"HOST_PATH"}
    return fill("prologue.sh.in", HOST_PATH=HOST_PATH, **{n: "x" for n in names})


def deck_run_head():
    """DECK_RUN up to its arguments, then where it finds umu-run."""
    head = bench_matrix.DECK_RUN.split("\nR=$1", 1)[0]
    return head + "\ncommand -v umu-run\n"


def run(home, script):
    # No ~/.local/bin and no rc files: what sshd's non-interactive bash has.
    env = {"HOME": home, "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-s"],
        input=script,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def stub_umu(home):
    bindir = os.path.join(home, ".local", "bin")
    os.makedirs(bindir)
    p = os.path.join(bindir, "umu-run")
    with open(p, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p


def test_prologue_finds_local_bin_umu():
    with tempfile.TemporaryDirectory() as home:
        stub_umu(home)
        p = run(home, prologue() + host_text("check_umu.sh"))
        assert p.returncode == 0, p.stderr
        # Without the prologue's PATH it is not found: the Deck's failure.
        p = run(home, host_text("check_umu.sh"))
        assert p.returncode == 1 and "host has no umu-run" in p.stderr, p.stderr


def test_missing_umu_still_reported():
    with tempfile.TemporaryDirectory() as home:
        p = run(home, prologue() + host_text("check_umu.sh"))
        assert p.returncode == 1 and "host has no umu-run" in p.stderr, p.stderr


def test_deck_run_finds_local_bin_umu():
    with tempfile.TemporaryDirectory() as home:
        umu = stub_umu(home)
        p = run(home, deck_run_head())
        assert p.returncode == 0 and p.stdout.strip() == umu, (p.stdout, p.stderr)


def test_path_line_once_before_the_values():
    """Once per script, before the prologue's first value (and DECK_RUN's
    arguments), and appended: never ahead of the host's or a box's own
    tools."""
    lines = prologue().splitlines()
    assert lines[0] == "set -euo pipefail", lines[:2]
    assert lines.count(PATH_LINE) == 1, lines
    assert lines.index(PATH_LINE) < lines.index("BENCH_DIR=x"), lines
    assert sum("PATH=" in x for x in lines if not x.startswith("#")) == 1, lines
    deck = bench_matrix.DECK_RUN.splitlines()
    assert deck.count(PATH_LINE) == 1 and deck.index(PATH_LINE) < deck.index("R=$1; CAP=$2")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(0)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
