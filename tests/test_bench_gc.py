"""bench gc: the protection rules, the reference text over every worktree,
the refusals, and the host plan, on temp stores whose run-info.txt files
are written here (synthetic: placeholder host, paths and shas).

  uv run pytest tests/test_bench_gc.py
"""

import hashlib
import os
import shutil
import subprocess
import time

import pytest

from xboxrecomp_cli import manifest
from xboxrecomp_cli.bench import gc
from xboxrecomp_cli.bench.config import Config
from xboxrecomp_cli.bench.remote import BenchError

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
HOST = os.path.join(os.path.dirname(os.path.abspath(gc.__file__)), "host")
OLD = "20260101-1"  # stamps far older than any --days


def git(d, *args):
    subprocess.run(
        ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"),
    )


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def run_info(exe="build-win/cat_recomp.exe", script="@attract", golden=False, tree="cat"):
    env = "env:    RECOMP_HOST_PAD=0"
    if script:
        env += " RECOMP_INPUT_SCRIPT=" + script
    if golden:
        env += " RECOMP_DEBUG=fb_dump_at=1-5"
    lines = ["host:   benchhost  2026-01-01T00:00:00+00:00", env]
    if exe:
        lines.append("%s  %s" % ("ab" * 32, exe))
    if tree:
        lines.append("built:  %s: %s main clean" % (tree, "c" * 40))
        lines.append("built:  toolkit: %s main clean" % ("d" * 40))
    return "\n".join(lines) + "\n"


def make_run(store, name, info=None, exit_code=True, files=("game-stdio.log",)):
    d = os.path.join(store, name)
    os.makedirs(d)
    if info is not False:
        write(os.path.join(d, "run-info.txt"), info or run_info())
    if exit_code:
        write(os.path.join(d, "exit-code"), "0\n")
    for f in files:
        write(os.path.join(d, f), "x\n")
    return d


@pytest.fixture
def ws(tmp_path):
    """tmp/game (a git repo with game.toml, golden.json, audio.json,
    TASKS.md, openspec/ and a bench-logs link), tmp/store, tmp/notes,
    tmp/xboxrecomp."""
    root = tmp_path / "game"
    root.mkdir()
    with open(os.path.join(GAME, "game.toml")) as f:
        toml = f.read()
    toml += (
        '\n[bench.gc]\nrefs = ["../notes"]\nrefs_exclude = ["../notes/inventory/*"]\n'
        "keep = 1\ndays = 3\n"
    )
    write(str(root / "game.toml"), toml)
    write(str(root / "analysis/golden/golden.json"), '{"scenarios": {}}\n')
    write(str(root / "analysis/golden/audio.json"), "{}\n")
    write(str(root / "TASKS.md"), "# tasks\n")
    write(str(root / "openspec/changes/x/tasks.md"), "- none\n")
    git(str(root), "init", "-q", "-b", "main")
    git(str(root), "add", "-A")
    git(str(root), "commit", "-q", "-m", "x")
    (tmp_path / "store").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "xboxrecomp").mkdir()
    os.symlink("../store", str(root / "bench-logs"))
    return tmp_path


class FakeRemote:
    def __init__(self, listing=""):
        self.listing = listing
        self.sent = []

    def ship(self, name, assigns="", prologue=True):
        return name + "\n" + assigns

    def remote(self, script, capture=False):
        self.sent.append(script)
        return 0, self.listing if capture else ""


class FakeBench:
    def __init__(self, root, listing=""):
        g = manifest.load(str(root))
        self.cfg = Config(g, {"BENCH_HOST": "benchhost"})
        self.r = FakeRemote(listing)
        self.out = []

    def say(self, msg="", end="\n"):
        self.out.append(msg)

    def need_host(self):
        pass

    def text(self):
        return "\n".join(self.out)


def rows_of(b):
    out = {}
    for line in b.out:
        parts = line.split()
        if parts and gc.RUN_RE.match(parts[0]):
            out[parts[0]] = line
    return out


def run_gc(ws, *args, listing=""):
    b = FakeBench(ws / "game", listing)
    rc = gc.cmd_gc(b, list(args))
    return rc, b


def tree_hash(top):
    h = hashlib.sha256()
    for d, dirs, files in sorted(os.walk(top)):
        dirs.sort()
        for n in sorted(files):
            p = os.path.join(d, n)
            h.update(p.encode())
            if not os.path.islink(p):
                with open(p, "rb") as f:
                    h.update(f.read())
    return h.hexdigest()


def test_rules_in_order(ws):
    store = str(ws / "store")
    make_run(store, OLD + "00001", run_info(exe="build-win/other.exe"))
    make_run(store, OLD + "00002", info=False)
    make_run(store, OLD + "00003")  # referenced below
    make_run(store, OLD + "00004", exit_code=False)
    young = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600))
    make_run(store, young, run_info(script="@stage1"))
    make_run(store, OLD + "00005")  # older @attract plain: removable
    make_run(store, OLD + "00006")  # the newest @attract plain (keep 1)
    make_run(store, OLD + "00007", run_info(golden=True))  # its own group
    make_run(store, OLD + "00008-pacing", run_info())
    write(str(ws / "game" / "TASKS.md"), "run %s00003 shows it\n" % OLD)
    rc, b = run_gc(ws)
    assert rc == 0
    r = rows_of(b)
    assert "other game (other.exe)" in r[OLD + "00001"]
    assert "unowned" in r[OLD + "00002"]
    assert "referenced (1 file)" in r[OLD + "00003"]
    assert "incomplete (no exit-code)" in r[OLD + "00004"]
    assert "younger than 3d" in r[young]
    assert r[OLD + "00005"].endswith("removable")
    assert "newest 1 of plain @attract" in r[OLD + "00006"]
    assert "newest 1 of golden @attract" in r[OLD + "00007"]
    assert "newest 1 of pacing @attract" in r[OLD + "00008-pacing"]
    assert "DRY RUN: nothing removed" in b.text()


def test_owner_rules(ws):
    store = str(ws / "store")
    make_run(store, OLD + "00001", run_info(exe="build-win/other.exe"))
    make_run(store, OLD + "00002", run_info(exe="build-win/other.exe"))
    make_run(store, OLD + "00003", run_info(exe=None))
    make_run(store, OLD + "00004", run_info(exe=None))
    make_run(store, OLD + "00005", run_info(tree="irq"))  # an agent folder, our exe
    make_run(store, OLD + "00006", run_info(tree="irq"))
    rc, b = run_gc(ws, "--include-unowned", "--keep", "0")
    r = rows_of(b)
    for n in ("00001", "00002"):
        assert "other game (other.exe)" in r[OLD + n], r[OLD + n]
    for n in ("00003", "00004", "00005", "00006"):
        assert r[OLD + n].endswith("removable"), r[OLD + n]
    assert "ours [irq]" in r[OLD + "00005"]
    rc, b = run_gc(ws, "--keep", "0")
    assert "unowned" in rows_of(b)[OLD + "00003"]


def test_suffixed_and_hhmmss_references(ws):
    store = str(ws / "store")
    make_run(store, OLD + "00001-mac-pad")
    make_run(store, "20260102-123456")
    make_run(store, OLD + "00009")
    make_run(store, OLD + "00010")
    write(str(ws / "notes" / "a" / "RESUME-a.md"), "the pad run %s00001; golden 123456\n" % OLD)
    rc, b = run_gc(ws, "--keep", "0")
    r = rows_of(b)
    assert "referenced" in r[OLD + "00001-mac-pad"]
    assert "referenced" in r["20260102-123456"]
    assert r[OLD + "00009"].endswith("removable")


def test_reference_in_another_worktree(ws):
    store = str(ws / "store")
    make_run(store, OLD + "00001")
    make_run(store, OLD + "00002")
    wt = str(ws / "wt2")
    git(str(ws / "game"), "worktree", "add", "-q", "-b", "side", wt)
    write(os.path.join(wt, "RESUME-side.md"), "evidence: %s00001\n" % OLD)  # untracked
    rc, b = run_gc(ws, "--keep", "0")
    r = rows_of(b)
    assert "referenced" in r[OLD + "00001"] and r[OLD + "00002"].endswith("removable")
    assert "sources: 2 worktrees" in b.text()
    # ../notes from the other worktree does not exist; that is no refusal.
    assert rc == 0


def test_refs_exclude_and_sole_protector(ws):
    store = str(ws / "store")
    for i in range(1, 6):
        make_run(store, OLD + "0000%d" % i)
    inv = "".join("%s0000%d\n" % (OLD, i) for i in range(1, 6))
    write(str(ws / "notes" / "inventory" / "all.txt"), inv)
    rc, b = run_gc(ws, "--keep", "0")
    assert all(v.endswith("removable") for v in rows_of(b).values())
    # Not excluded: it alone protects all five, and the report says so.
    write(str(ws / "notes" / "list.txt"), inv)
    rc, b = run_gc(ws, "--keep", "0")
    assert all("referenced" in v for v in rows_of(b).values())
    assert "%6d %s" % (5, os.path.realpath(str(ws / "notes" / "list.txt"))) in b.text()


def test_refusals(ws):
    shutil.rmtree(str(ws / "notes"))
    with pytest.raises(BenchError, match="reference sources missing"):
        run_gc(ws)
    (ws / "notes").mkdir()
    os.remove(str(ws / "game" / "analysis/golden/golden.json"))
    with pytest.raises(BenchError, match="golden.json in the main checkout"):
        run_gc(ws)
    write(str(ws / "game" / "analysis/golden/golden.json"), "{}\n")
    os.remove(str(ws / "game" / "bench-logs"))
    with pytest.raises(BenchError, match="no store at"):
        run_gc(ws)


def test_worktree_list_refusal(ws, tmp_path):
    with pytest.raises(BenchError, match="cannot list the worktrees"):
        gc.worktrees(str(tmp_path / "not-a-repo"))


def test_big_file_skipped_and_named(ws):
    store = str(ws / "store")
    make_run(store, OLD + "00001")
    make_run(store, OLD + "00002")
    big = str(ws / "notes" / "big.txt")
    with open(big, "w") as f:
        f.write("%s00001\n" % OLD)
        f.write("x" * (gc.MAX_REF_FILE + 1))
    rc, b = run_gc(ws, "--keep", "0")
    assert rows_of(b)[OLD + "00001"].endswith("removable")
    assert "skipped (over 8 MiB): " + big in b.text()


def test_links_loose_files_and_apply(ws):
    store = str(ws / "store")
    victim = ws / "outside"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    d = make_run(store, OLD + "00001")
    os.symlink(str(victim), os.path.join(d, "link"))
    make_run(store, OLD + "00002")
    write(os.path.join(store, "loose.wav"), "x")
    os.makedirs(os.path.join(store, "named-run"))
    os.symlink(str(victim), os.path.join(store, OLD + "00003"))
    before = tree_hash(store)
    rc, b = run_gc(ws, "--keep", "0")
    assert tree_hash(store) == before  # a dry run touches nothing
    assert "not managed (loose files, named dirs; never touched): 3 entries" in b.text()
    rc, b = run_gc(ws, "--keep", "0", "--apply")
    assert rc == 0 and "removed 2 runs, 0 failures" in b.text()
    assert sorted(os.listdir(store)) == [OLD + "00003", "loose.wav", "named-run"]
    assert (victim / "keep.txt").is_file()


def test_remove_local_refuses_odd_names(ws, tmp_path):
    out = []
    store = str(ws / "store")
    os.makedirs(os.path.join(store, "named"))
    assert gc.remove_local(store, ["named", "../x"], out.append) == 2
    assert os.path.isdir(os.path.join(store, "named"))


def listing(rows):
    out = []
    for name, sha, ex, files, info in rows:
        out.append("R\t%s\t100\t%s\t%d\t50" % (name, sha, ex))
        out += ["I\t%s\t%s" % (name, line) for line in info.splitlines()]
        out += ["F\t%s\t%s" % (name, f) for f in files]
    out.append("N\tloose.wav\t4")
    return "\n".join(out) + "\n"


def test_host_plan(ws):
    store = str(ws / "store")
    info = run_info()
    sha = hashlib.sha256(info.encode()).hexdigest()
    host_files = ["run-info.txt", "exit-code", "game-stdio.log", "frames/flip_00001.bmp"]
    host_files += ["steam-default.log", "xbox_kernel.log"]
    # Whole local copy (kernel log compressed in place), one file missing,
    # another run-info, and none at all.
    for n in ("00001", "00002", "00003"):
        make_run(store, OLD + n, info, files=("game-stdio.log",))
    write(os.path.join(store, OLD + "00001", "xbox_kernel.log.zst"), "z")
    write(os.path.join(store, OLD + "00003", "run-info.txt"), info + "extra\n")
    # 00002 has no xbox_kernel.log locally: one file missing.
    lst = listing(
        [(OLD + n, sha, 1, host_files, info) for n in ("00001", "00002", "00003", "00004")]
    )
    rc, b = run_gc(ws, "--host", "--keep", "0", listing=lst)
    r = rows_of(b)
    assert r[OLD + "00001"].endswith("removable"), r
    assert "not in the local store (1 files missing)" in r[OLD + "00002"]
    assert r[OLD + "00003"].endswith("not in the local store")
    assert r[OLD + "00004"].endswith("not in the local store")
    assert "(frames 50.0K)" in r[OLD + "00001"]
    assert b.r.sent == ["gc_list.sh\n"]
    rc, b = run_gc(ws, "--host", "--keep", "0", "--apply", listing=lst)
    assert b.r.sent[1] == "gc_apply.sh\nNAMES=(%s )\n" % (OLD + "00001")


def test_host_scripts(ws):
    """gc_list.sh lists a fake host store; gc_apply.sh removes only run
    directories and refuses other names."""
    game = ws / "hostgame"
    st = game / "bench-logs"
    run = st / (OLD + "00001")
    (run / "frames").mkdir(parents=True)
    (run / "frames" / "flip_00001.bmp").write_text("x")
    (run / "run-info.txt").write_text(run_info())
    (run / "exit-code").write_text("0\n")
    (st / "loose.wav").write_text("x")

    # The host's flock, faked where there is none (macOS): always granted.
    fake = ws / "bin"
    fake.mkdir()
    (fake / "flock").write_text("#!/bin/sh\nexit 0\n")
    (fake / "flock").chmod(0o755)

    def go(name, pre=""):
        with open(os.path.join(HOST, name)) as f:
            body = f.read()
        env = dict(os.environ, HOME=str(ws))
        if not shutil.which("flock"):
            env["PATH"] = str(fake) + os.pathsep + env["PATH"]
        return subprocess.run(
            ["bash", "-c", "REMOTE_GAME=%s\n%s%s" % (game, pre, body)],
            capture_output=True,
            text=True,
            env=env,
        )

    r = go("gc_list.sh")
    assert r.returncode == 0, r.stderr
    runs, unmanaged, files, sha, frames = gc.parse_host_listing(r.stdout, "RECOMP_INPUT_SCRIPT")
    assert [x["name"] for x in runs] == [OLD + "00001"] and runs[0]["exit"]
    assert runs[0]["info"]["exe"] == "build-win/cat_recomp.exe"
    assert runs[0]["info"]["scenario"] == "@attract"
    assert sorted(files[OLD + "00001"]) == ["exit-code", "run-info.txt"]
    assert sha[OLD + "00001"] == hashlib.sha256(run_info().encode()).hexdigest()
    assert [n for n, _ in unmanaged] == ["loose.wav"]
    r = go("gc_apply.sh", "NAMES=(../x loose.wav)\n")
    assert r.returncode != 0 and "refusing" in r.stderr and run.is_dir()
    r = go("gc_apply.sh", "NAMES=(%s)\n" % (OLD + "00001"))
    assert r.returncode == 0, r.stderr
    assert not run.exists() and (st / "loose.wav").is_file()
