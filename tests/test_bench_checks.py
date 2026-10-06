#!/usr/bin/env python3
"""benchlib.checks against fixture run directories: check_run_end and
check_present_mismatch print bench.sh's message and return its code. The
expected text is written out here. Given a full bench.sh (its last version
is `git show 61ea933:scripts/bench.sh`; the script itself is gone) in
BENCH_PARITY_REF, its own bash functions run on the same fixtures and must
agree too. Plain asserts;
runs alone or under pytest.

  uv run python tests/test_bench_checks.py
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
from xboxrecomp_cli.bench import checks  # noqa: E402

REF = os.environ.get("BENCH_PARITY_REF")


def bash_funcs():
    if not REF or not shutil.which("bash") or sys.platform == "win32":
        return None
    text = open(REF).read()
    out = []
    for name in ("check_present_mismatch", "check_run_end"):
        m = re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, text, re.S | re.M)
        assert m, "%s has no %s(): not a full bench.sh" % (REF, name)
        out.append(m.group(0))
    return "".join(out)


FUNCS = bash_funcs()


def info(limit="75", flips="2300", busy="no", load="1.0 1.0 1.0 at start, 2.0 2.0 2.0 at end"):
    lines = [
        "host:   bench  2026-10-06T07:00:00+00:00",
        "proton: GE-Proton  prefix: ~/p",
        "env:    (none)",
        "args:   (none)",
    ]
    lines.append("limit:  %s" % ("none" if limit is None else "%s s (SIGINT)" % limit))
    lines.append("lock:   ~/.recomp-run.lock, waited 0s")
    if flips is not None:
        lines.append("flips:  %s" % flips)
    lines.append("load:   %s (16 cpus)" % load)
    lines.append("busy:   %s" % busy)
    return "\n".join(lines) + "\n"


def make(d, name, rc="124", stdio="[D3D11] flip 1\n", run_info=None, survived=None):
    p = os.path.join(d, name)
    os.makedirs(p)
    if stdio is not None:
        with open(os.path.join(p, "game-stdio.log"), "w") as f:
            f.write(stdio)
    if rc is not None:
        with open(os.path.join(p, "exit-code"), "w") as f:
            f.write(rc + "\n")
    with open(os.path.join(p, "run-info.txt"), "w") as f:
        f.write(info() if run_info is None else run_info)
    if survived is not None:
        with open(os.path.join(p, "survived"), "w") as f:
            f.write(survived)
    return p


# (name, make() keywords, min_flips, expected stdout, expected code)
RUN_END = [
    ("clean", {}, "2275", "end: ran to the 75 s limit\nend: 2300 flips (30.7/s; floor 2275)\n", 0),
    ("clean-nofloor", {}, "", "end: ran to the 75 s limit\n", 0),
    (
        "crash",
        {"stdio": "boot\n[CRASH] SIGSEGV at 0x1234 RIP=0x5\nmore\n"},
        "2275",
        "end: FAIL crashed: [CRASH] SIGSEGV at 0x1234 RIP=0x5\n",
        1,
    ),
    (
        "sigkill",
        {"rc": "137"},
        "2275",
        "end: FAIL ignored SIGINT at the 75 s limit; SIGKILLed\n",
        1,
    ),
    ("early", {"rc": "1"}, "2275", "end: FAIL exit 1 before the 75 s limit\n", 1),
    ("no-exit-code", {"rc": None}, "", "end: FAIL no exit-code in {log} (host script died?)\n", 1),
    (
        "no-limit",
        {"rc": "1", "run_info": info(limit=None)},
        "2275",
        "end: exit 1 (no limit set; not checked)\n",
        0,
    ),
    (
        "slow-busy",
        {"run_info": info(flips="1000", busy="yes (cpu stall 12.5% of the run)")},
        "2275",
        "end: ran to the 75 s limit\nend: INCONCLUSIVE 1000 flips under the floor 2275 (13.3/s): "
        "host busy, yes (cpu stall 12.5% of the run); load 1.0 1.0 1.0 at start, 2.0 2.0 2.0 at end "
        "(16 cpus)\n",
        3,
    ),
    (
        "slow-idle",
        {"run_info": info(flips="1000")},
        "2275",
        "end: ran to the 75 s limit\nend: FAIL 1000 flips under the floor 2275 (13.3/s) on an idle "
        "host: perf regression? load 1.0 1.0 1.0 at start, 2.0 2.0 2.0 at end (16 cpus)\n",
        1,
    ),
    (
        "no-stdio-flips",
        {"run_info": info(flips="?")},
        "2275",
        "end: ran to the 75 s limit\nend: FAIL no game-stdio.log: the game wrote no log, so no flip "
        "count\n",
        1,
    ),
    (
        "no-flips-line",
        {"run_info": info(flips=None)},
        "2275",
        "end: ran to the 75 s limit\nend: FAIL no flips: line in run-info (host script died after the "
        "run?)\n",
        1,
    ),
    (
        "survivor-killed",
        {
            "survived": "survived umu-run exit 124 (after 20 s grace):\n 12 wine\nkilled: none left after "
            "wineserver -k and kill -9\n"
        },
        "2275",
        "end: warning: the game outlived umu-run and was killed; see {log}/survived\n"
        "end: ran to the 75 s limit\nend: 2300 flips (30.7/s; floor 2275)\n",
        0,
    ),
    (
        "survivor-alive",
        {
            "survived": "survived umu-run exit 124 (after 20 s grace):\n 12 wine\nstill alive after "
            "wineserver -k and kill -9:\n 12 wine\n"
        },
        "2275",
        "end: FAIL the game outlived umu-run and survived wineserver -k and kill -9; see "
        "{log}/survived\n",
        1,
    ),
    (
        "survivor-unfinished",
        {"survived": "survived umu-run exit 124 (after 20 s grace):\n"},
        "2275",
        "end: FAIL the survivor sweep did not finish (host script died in it?); see {log}/survived\n",
        1,
    ),
]


def flip(n, p, w):
    return "[D3D11] flip %d batches 12 prog 3 present 0x%X walker 0x%X tail\n" % (n, p, w)


# (name, log text or None for no file, expected stdout, expected code)
PRESENT = [
    ("no-log", None, "", 1),
    (
        "no-flip-lines",
        "[METAL] flip 1 present 0x1 walker 0x1\n",
        "present: no [D3D11] flip lines in {log} (not checked)\n",
        0,
    ),
    (
        "all-match",
        "".join(flip(i, 0x100, 0x100) for i in range(1, 4)) + "noise\n",
        "present: 0/3 flips mismatched\n",
        0,
    ),
    (
        "mismatched",
        flip(1, 0, 0x100)
        + "".join(flip(i, 0x200, 0x100) for i in range(2, 9))
        + flip(9, 0x100, 0x100),
        "present: FAIL 8/9 flips present a surface the walker did not draw; first: 1(0x0/0x100) "
        "2(0x200/0x100) 3(0x200/0x100) 4(0x200/0x100) 5(0x200/0x100)\n",
        1,
    ),
]


def py_run(fn, *args):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = fn(*args, out=lambda m: buf.write(m + "\n"))
    return buf.getvalue(), rc


def sh_run(call, *args):
    script = FUNCS + '\n%s "$@"\necho "rc=$?"\n' % call
    out = subprocess.run(
        ["bash", "-c", script, "_"] + list(args), capture_output=True, text=True
    ).stdout
    body, _, rc = out.rpartition("rc=")
    return body, int(rc)


def test_check_run_end():
    with tempfile.TemporaryDirectory() as d:
        for name, kw, minf, want, code in RUN_END:
            log = make(d, name, **kw)
            want = want.replace("{log}", log)
            got = py_run(checks.check_run_end, log, minf)
            assert got == (want, code), (name, got, want, code)
            if FUNCS:
                assert sh_run("check_run_end", log, minf) == (want, code), name


def test_check_present_mismatch():
    with tempfile.TemporaryDirectory() as d:
        for name, text, want, code in PRESENT:
            log = os.path.join(d, name + ".log")
            if text is not None:
                with open(log, "w") as f:
                    f.write(text)
            want = want.replace("{log}", log)
            got = py_run(checks.check_present_mismatch, log)
            assert got == (want, code), (name, got, want, code)
            if FUNCS:
                assert sh_run("check_present_mismatch", log) == (want, code), name


def test_gen_digest_matches_shasum():
    if not shutil.which("shasum"):
        return
    with tempfile.TemporaryDirectory() as d:
        gen = os.path.join(d, "src", "recomp", "gen")
        os.makedirs(gen)
        for n, t in (("b.c", "b"), ("a.c", "a"), ("Z.h", "z"), (".hidden", "h")):
            with open(os.path.join(gen, n), "w") as f:
                f.write(t)
        want = subprocess.run(
            "shasum -a 256 -- * | LC_ALL=C sort -k2 | shasum -a 256 | cut -d' ' -f1",
            shell=True,
            cwd=gen,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert checks.gen_digest_local(gen) == want


def test_tree_state():
    with tempfile.TemporaryDirectory() as d:
        assert checks.tree_state("x", d) == "x: (not a git checkout: %s)" % d
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", d]
        subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
        open(os.path.join(d, "f"), "w").close()
        subprocess.run(g + ["add", "f"], check=True)
        subprocess.run(g + ["commit", "-qm", "f"], check=True)
        sha = subprocess.run(
            g + ["rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        assert checks.tree_state("cat", d) == "cat: %s main clean" % sha
        open(os.path.join(d, "g"), "w").close()
        with open(os.path.join(d, "f"), "w") as f:
            f.write("x")
        assert checks.tree_state("cat", d) == "cat: %s main dirty (2 paths)" % sha


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   %s" % name)
