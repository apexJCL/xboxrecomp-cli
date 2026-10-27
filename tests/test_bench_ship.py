"""BENCH_BUILD=local (bench/ship.py): this machine builds, the host only
runs. The bench runs in-process against test_bench_cli's scratch tree, with
its fake ssh and rsync first on PATH and the local build replaced by one
that writes an exe, so each case sees exactly what went to the host: no
build.sh, the exe and its files rsynced to runs/<sha12>/, the run there.

  uv run pytest tests/test_bench_ship.py
"""

import hashlib
import os
import shutil
import subprocess
import tempfile

import pytest
from test_bench_cli import FAKE_RSYNC, FAKE_SSH, SKIP, scratch, split_script

from xboxrecomp_cli import build as build_mod
from xboxrecomp_cli import manifest
from xboxrecomp_cli.bench import Bench
from xboxrecomp_cli.bench import main as bench_main
from xboxrecomp_cli.bench import ship as ship_mod
from xboxrecomp_cli.bench.config import Config, ConfigError
from xboxrecomp_cli.bench.remote import HOST_DIR, host_text

EXE_BODY = b"MZ exe"
EXE_SHA = hashlib.sha256(EXE_BODY).hexdigest()

pytestmark = pytest.mark.skipif(bool(SKIP), reason=SKIP or "")

# test_bench_cli's fake, which also answers integrate_show.sh's sha256sum
# with FAKE_EXE_SHA.
FAKE_SSH_SHA = FAKE_SSH.replace(
    "exit 0\n",
    'if grep -qF \'sha256sum "$EXE_REL"\' "$f.stdin"; then\n'
    '    echo "${FAKE_EXE_SHA:-none}  build-win/cat_recomp.exe"; echo "built: ..."\n'
    "fi\nexit 0\n",
)
# test_bench_cli's fake rsync, which also lists a directory source (the
# staged set, gone once ship returns) and hashes its exe.
FAKE_RSYNC_LS = FAKE_RSYNC.replace(
    "exit 0\n",
    'for a in "$@"; do case "$a" in /*/) [ -d "$a" ] && { ls -A "$a"; '
    'shasum -a 256 "$a"*.exe 2>/dev/null | cut -d" " -f1; } '
    '> "$FAKE_REC/$(printf %04d "$n")-rsync.ls";; esac; done\nexit 0\n',
)


@pytest.fixture
def tree(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        cat, tk, bindir, digest = scratch(tmp)
        for name, body in (("ssh", FAKE_SSH_SHA), ("rsync", FAKE_RSYNC_LS)):
            with open(os.path.join(bindir, name), "w") as f:
                f.write(body)
        rec = os.path.join(tmp, "rec")
        os.makedirs(rec)
        for k in list(os.environ):
            if k.startswith(("BENCH_", "XBOXRECOMP", "LLVM_MINGW", "PROTONPATH", "FAKE_")):
                monkeypatch.delenv(k)
        monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("BENCH_GAME_DIR", cat)
        monkeypatch.setenv("XBOXRECOMP_DIR", tk)
        monkeypatch.setenv("FAKE_REC", rec)
        monkeypatch.setenv("FAKE_GEN_DIGEST", digest)
        builds = []

        def fake_build(target, cmake_args=(), stale_ok=False, jobs=0, **kw):
            assert target == "windows"
            g = manifest.current()
            assert os.path.realpath(g.root) == os.path.realpath(cat)
            assert os.environ["XBOXRECOMP_DIR"] == tk
            builds.append((list(cmake_args), stale_ok, jobs))
            bdir = g.build_dir("windows")
            os.makedirs(bdir, exist_ok=True)
            exe = g.exe(bdir, "windows")
            with open(exe, "wb") as f:
                f.write(EXE_BODY)
            for n, body in (("cat_recomp.pdb", b"pdb"), ("config.default.toml", b"[x]\n")):
                with open(os.path.join(bdir, n), "wb") as f:
                    f.write(body)
            if t.get("touch_gen"):
                with open(os.path.join(g.gen, "recomp_0001.c"), "a") as f:
                    f.write("/* regenerated */\n")
            return exe

        monkeypatch.setattr(build_mod, "build", fake_build)
        t = {"cat": cat, "tk": tk, "rec": rec, "builds": builds, "game": manifest.load(cat)}
        yield t


def calls(t):
    """[(kind, argv, stdin)] since the last call, and forget them."""
    rec = t["rec"]
    out = []
    for n in sorted(x for x in os.listdir(rec) if x.endswith(".argv")):
        kind = n.split("-")[1].split(".")[0]
        argv = open(os.path.join(rec, n), "rb").read().decode().split("\0")[:-1]
        stdin = None
        ls = os.path.join(rec, n[: -len(".argv")] + ".ls")
        if os.path.exists(ls):
            stdin = open(ls).read()
        if kind == "ssh":
            stdin = open(os.path.join(rec, n[: -len(".argv")] + ".stdin"), "rb").read().decode()
        out.append((kind, argv, stdin))
    shutil.rmtree(rec)
    os.makedirs(rec)
    return out


def bodies(cs):
    names = {host_text(n): n for n in os.listdir(HOST_DIR) if n.endswith(".sh")}
    out = []
    for kind, _argv, stdin in cs:
        if kind == "ssh" and stdin:
            out.append(names.get(split_script(stdin)[1], "?"))
        else:
            out.append(kind)
    return out


def run(t, *argv):
    return bench_main(list(argv), t["game"])


def exe_sha(t):
    with open(os.path.join(t["cat"], "build-win", "cat_recomp.exe"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_build_ships_and_never_compiles_on_host(tree, capsys):
    t = tree
    assert run(t, "build", "-DX=1") == 0
    assert t["builds"] == [(["-DX=1"], False, 0)]
    cs = calls(t)
    assert bodies(cs) == ["ship_prep.sh", "rsync", "rsync"], bodies(cs)
    assert not any(c[2] and "cmake --build" in c[2] for c in cs)
    sha = exe_sha(t)
    run_dir = "~/bench-test/blinx2/runs/" + sha[:12]
    assert "REMOTE_GAME=%s\n" % run_dir in cs[0][2]
    # The exe, its PDB and config, and the provenance, staged in one
    # directory and mirrored into the run's build-win/ with --delete, so it
    # holds exactly that set.
    argv = cs[1][1]
    assert "--checksum" in argv and "-i" in argv and "--delete" in argv
    assert argv[-1] == "fakehost:%s/build-win/" % run_dir
    assert argv[-3] == "--delete" and argv[-2].endswith("/") and os.path.isabs(argv[-2])
    listing = cs[1][2].split()
    assert listing[:-1] == [
        "cat_recomp.exe",
        "cat_recomp.pdb",
        "config.default.toml",
        "provenance.txt",
    ], listing
    assert listing[-1] == sha  # the snapshot shipped is the exe hashed
    assert cs[2][1][-2].endswith("running_game.py")
    prov = open(os.path.join(t["cat"], "build-win", ship_mod.LOCAL_PROVENANCE)).read()
    assert prov.startswith("built: ") and "(BENCH_BUILD=local)" in prov, prov
    assert "\ngen: sha256 %s (2 files)\n" % os.environ["FAKE_GEN_DIGEST"] in prov, prov
    assert prov.endswith("exe: %s cat_recomp.exe\n" % sha), prov
    assert "cat: " in prov and "toolkit: " in prov


def test_run_ships_once_then_runs_in_the_run_dir(tree, capsys):
    t = tree
    assert run(t, "build") == 0
    calls(t)
    run(t, "run")
    cs = calls(t)
    b = bodies(cs)
    assert b[:4] == ["ship_prep.sh", "rsync", "rsync", "run_game.sh"], b
    assert b.count("ship_prep.sh") == 1
    run_dir = "~/bench-test/blinx2/runs/" + exe_sha(t)[:12]
    assert "REMOTE_GAME=%s\n" % run_dir in cs[3][2]
    pulls = [c[1] for c in cs[4:] if c[0] == "rsync"]
    assert pulls and all(p[-2] == "fakehost:%s/bench-logs/" % run_dir for p in pulls), pulls


def test_bench_run_names_the_run_dir(tree, monkeypatch):
    t = tree
    monkeypatch.setenv("BENCH_RUN", "pb-kick")
    assert run(t, "build") == 0
    cs = calls(t)
    assert "REMOTE_GAME=~/bench-test/blinx2/runs/pb-kick\n" in cs[0][2]
    # logs and symbolize find it without shipping.
    run(t, "symbolize")
    cs = calls(t)
    assert bodies(cs) == ["symbolize.sh"] and "runs/pb-kick\n" in cs[0][2]


def test_ship_without_exe_refuses(tree, capsys):
    assert run(tree, "sync") == 1
    assert "no build-win/cat_recomp.exe here: build it first" in capsys.readouterr().err
    assert calls(tree) == []


def test_integrate_checks_host_sha_and_gen(tree, monkeypatch, capsys):
    t = tree
    # The fake host answers with another exe: integrate fails.
    monkeypatch.setenv("FAKE_EXE_SHA", "0" * 64)
    assert run(t, "integrate") == 1
    err = capsys.readouterr().err
    assert "the host's exe is %s, not the one built here (%s)" % ("0" * 64, EXE_SHA) in err
    b = bodies(calls(t))
    assert "build.sh" not in b and "gen_digest.sh" not in b and b[-1] == "integrate_show.sh", b
    assert t["builds"][-1][1] is True  # stale_ok: integrate checked it first
    # The host has the exe built here: integrate passes.
    monkeypatch.setenv("FAKE_EXE_SHA", EXE_SHA)
    assert run(t, "integrate") == 0
    out = capsys.readouterr().out
    digest = os.environ["FAKE_GEN_DIGEST"]
    assert "gen: 2 files, sha256 %s (unchanged during the build)" % digest in out, out


def test_integrate_fails_when_gen_changes_during_build(tree, capsys):
    tree["touch_gen"] = True
    assert run(tree, "integrate") == 1
    assert "gen/ changed during the build" in capsys.readouterr().err
    assert not [b for b in bodies(calls(tree)) if b != "?"]


def test_tests_refused_and_golden_nobuild(tree, capsys):
    t = tree
    assert run(t, "tests") == 1
    assert "BENCH_BUILD=local: the toolkit's Proton ctests compile on the host" in (
        capsys.readouterr().err
    )
    assert calls(t) == []
    b = Bench(Config(t["game"]))
    assert b.tests_or_skip() == (0, "nobuild")
    assert b.tests_or_skip(force=True) == (1, "FAIL")
    assert calls(t) == []


def test_host_mode_maps_no_build_refusal(tree, monkeypatch, capsys):
    monkeypatch.setenv("BENCH_BUILD", "host")
    monkeypatch.setenv("FAKE_EXIT_RE", "EXTRA_ARGS=")
    monkeypatch.setenv("FAKE_EXIT_RC", "78")
    assert run(tree, "build") == 1
    err = capsys.readouterr().err
    assert "build: fakehost is a no-build host (~/.recomp-no-build)" in err, err


def test_host_scripts_refuse_on_marker():
    for name in ("build.sh", "tests.sh"):
        text = host_text(name)
        guard = text.index(".recomp-no-build")
        assert guard < text.index("cmake"), name
        assert "exit 78" in text[guard : text.index("\n", guard)], name


@pytest.mark.parametrize(
    "env,msg",
    [
        ({"BENCH_BUILD": "remote"}, "BENCH_BUILD=remote: use one of local, host"),
        ({"BENCH_RUN": "../x"}, "BENCH_RUN=../x: letters, digits"),
        ({"BENCH_RUN": ".."}, "BENCH_RUN=..: letters, digits"),
    ],
)
def test_config_values(tree, env, msg):
    e = {"BENCH_GAME_DIR": tree["cat"], "XBOXRECOMP_DIR": tree["tk"], "HOME": "/h"}
    e.update(env)
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(")):
        Config(tree["game"], e)


def test_config_default_is_local(tree):
    e = {"BENCH_GAME_DIR": tree["cat"], "XBOXRECOMP_DIR": tree["tk"], "HOME": "/h"}
    c = Config(tree["game"], e)
    # The base is the game's (its slug), shared by every checkout of it.
    assert c.local_build and c.remote_game == c.base == "~/bench-test/blinx2"
    e["BENCH_BUILD"] = "host"
    h = Config(tree["game"], e)
    assert not h.local_build and h.base == "~/bench-test/cat"


GNU_CP = (
    shutil.which("cp") and subprocess.run(["cp", "--version"], capture_output=True).returncode == 0
)
# ship_prep.sh runs on a Linux host with GNU cp. Elsewhere (the Mac) a shim
# takes its one form, cp -p --reflink=auto -t DIR FILE..., so the seed's
# find and its dot-file rule are tested on every gate.
CP_SHIM = """#!/bin/bash
[ "$1 $2 $3" = "-p --reflink=auto -t" ] || { echo "cp shim: $*" >&2; exit 2; }
d=$4; shift 4; exec /bin/cp -p "$@" "$d"
"""


def ship_prep(tmp_path, script):
    env = dict(os.environ)
    if not GNU_CP:
        shim = tmp_path / "shim"
        shim.mkdir(exist_ok=True)
        (shim / "cp").write_text(CP_SHIM)
        (shim / "cp").chmod(0o755)
        env["PATH"] = "%s%s%s" % (shim, os.pathsep, env["PATH"])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_ship_prep_seeds_from_newest_run(tmp_path):
    base = tmp_path / "game"
    gf = tmp_path / "gf"
    (gf).mkdir()
    (gf / "default.xbe").write_bytes(b"xbe")
    old = base / "runs" / "old" / "build-win"
    old.mkdir(parents=True)
    (old / "cat_recomp.exe").write_bytes(b"old exe")
    (old / "cat_recomp.pdb").write_bytes(b"old pdb")
    (old / "sub").mkdir()
    (old / ".cat_recomp.exe.aB3x9z").write_bytes(b"rsync temp")  # an interrupted ship
    script = (
        "set -euo pipefail\nREMOTE_GAME=%s\nCLI_DIR=%s\nEXE=cat_recomp.exe\n"
        "GAME_FILES=game_files\nXBE=game_files/default.xbe\nDEST=build-win\nSRC=%s\n%s"
        % (base / "runs" / "new", tmp_path / "cli", gf, host_text("ship_prep.sh"))
    )
    r = ship_prep(tmp_path, script)
    assert r.returncode == 0, r.stderr
    new = base / "runs" / "new"
    assert (new / "build-win" / "cat_recomp.exe").read_bytes() == b"old exe"
    # Files only, and no dot-files: the rsync temp file stays behind.
    assert sorted(os.listdir(new / "build-win")) == ["cat_recomp.exe", "cat_recomp.pdb"]
    assert os.readlink(new / "game_files") == str(gf)
    assert (new / "bench-logs").is_dir()
    assert "ship: seeded from ../old" in r.stdout
    # Again: the run has its exe, nothing is copied over it.
    (new / "build-win" / "cat_recomp.exe").write_bytes(b"new exe")
    r = ship_prep(tmp_path, script)
    assert r.returncode == 0 and "seeded" not in r.stdout
    assert (new / "build-win" / "cat_recomp.exe").read_bytes() == b"new exe"
    assert "is writable; runs share it" in r.stderr
    # Saves in the shared copy: refused, as every run would read them.
    for d in ("UDATA", "TDATA"):
        (gf / d).mkdir()
        r = ship_prep(tmp_path, script)
        assert r.returncode == 1 and "%s: saves in the shared game files" % d in r.stderr
        (gf / d).rmdir()
    r = ship_prep(tmp_path, script)
    assert r.returncode == 0, r.stderr


def test_every_run_checks_the_shipped_exe(tree, monkeypatch, capsys):
    t = tree
    assert run(t, "build") == 0
    calls(t)
    run(t, "run")
    cs = calls(t)
    rg = [c for c in cs if c[0] == "ssh" and bodies([c]) == ["run_game.sh"]]
    assert rg and "EXPECT_SHA=%s\n" % exe_sha(t) in rg[0][2]
    # Another ship replaced the run dir's exe before the lock: nothing ran.
    monkeypatch.setenv("FAKE_EXIT_RE", "EXPECT_SHA=")
    monkeypatch.setenv("FAKE_EXIT_RC", str(ship_mod.EXE_CHANGED_RC))
    assert run(t, "run") == 1
    assert "is no longer the one this command shipped" in capsys.readouterr().err


def test_run_game_refuses_another_exe():
    text = host_text("run_game.sh")
    check = text.index('if [ -n "${EXPECT_SHA:-}" ]')
    assert text.index("flock -w 3600 9") < check < text.index("RECOMP_BENCH_RUN="), "under the lock"
    assert "exit 76" in text[check : text.index("\nfi\n", check)]


def test_ship_refuses_exe_changed_since_build(tree, capsys):
    t = tree
    assert run(t, "build") == 0
    calls(t)
    b = Bench(Config(t["game"]))
    exe = os.path.join(t["cat"], "build-win", "cat_recomp.exe")
    with pytest.raises(ship_mod.BenchError, match="changed since it was built"):
        ship_mod.ship(b, exe, "0" * 64)
    assert calls(t) == []


def test_build_refuses_while_gen_regenerates(tree, capsys):
    t = tree
    open(t["game"].regen_marker, "w").close()
    assert run(t, "build") == 1
    assert "gen/ is mid-regeneration" in capsys.readouterr().err
    assert t["builds"] == [] and calls(t) == []


def test_setup_local_makes_no_box(tree):
    t = tree
    assert run(t, "setup") == 0
    cs = calls(t)
    assert bodies(cs) == ["check_umu.sh", "setup_mingw.sh"], bodies(cs)
    # check_umu.sh gets the prologue: its PATH is where umu-run is found.
    assert 'PATH="$PATH:$HOME/.local/bin"' in cs[0][2], cs[0][2]
    assert not any("distrobox" in " ".join(c[1]) + (c[2] or "") for c in cs)


def test_setup_host_mode_refused_on_marker(tree, monkeypatch, capsys):
    monkeypatch.setenv("BENCH_BUILD", "host")
    monkeypatch.setenv("FAKE_EXIT_RE", "recomp-no-build.*never builds; set BENCH_BUILD")
    monkeypatch.setenv("FAKE_EXIT_RC", "78")
    assert run(tree, "setup") == 1
    assert "setup: fakehost is a no-build host" in capsys.readouterr().err
    text = fill_box()
    assert text.index(".recomp-no-build") < text.index("distrobox create")


def fill_box():
    from xboxrecomp_cli.bench.remote import fill

    return fill("setup_box.sh.in", BENCH_IMAGE="x")


def test_game_files_default_under_the_base(tree):
    e = {"BENCH_GAME_DIR": tree["cat"], "XBOXRECOMP_DIR": tree["tk"], "HOME": "/h"}
    assert Config(tree["game"], e)["BENCH_GAME_FILES"] == "~/bench-test/blinx2/game_files"
    e["BENCH_BUILD"] = "host"
    assert Config(tree["game"], e)["BENCH_GAME_FILES"] == "~/xbox-recomp/cat/game_files"


def test_sync_game_files_left_read_only(tree, monkeypatch):
    from xboxrecomp_cli.bench import sync as sync_mod

    b = Bench(Config(tree["game"]))
    cmds = []

    def command(cmd, capture=False):
        cmds.append(cmd)
        if cmd.startswith("realpath"):
            return 0, "/srv/u/bench-test/blinx2\n/srv/u/bench-test/blinx2\n"
        return 0, "0\n"  # writable before the copy

    monkeypatch.setattr(b.r, "command", command)
    monkeypatch.setattr(sync_mod, "rsync", lambda *a: 0)
    assert sync_mod.sync_game_files(b, "link") == 0
    assert cmds[-1] == "chmod -R a-w ~/bench-test/blinx2/game_files", cmds


def test_first_game_files_sync_needs_no_exe(tree, capsys):
    t = tree
    assert run(t, "sync", "--game-files") == 0
    assert "nothing shipped" in capsys.readouterr().out
    assert "ship_prep.sh" not in bodies(calls(t))


def test_one_exe_per_command(tree, capsys):
    """Two scenarios of one command: the second refuses an exe rebuilt in
    between, instead of shipping and running it."""
    t = tree
    assert run(t, "build") == 0
    calls(t)
    b = Bench(Config(t["game"]))
    b.run_game([])
    with open(os.path.join(t["cat"], "build-win", "cat_recomp.exe"), "ab") as f:
        f.write(b"rebuilt")
    with pytest.raises(ship_mod.BenchError, match="changed during this command"):
        b.run_game([])
    assert bodies(calls(t)).count("run_game.sh") == 1


def test_integrate_pins_its_build_for_the_golden(tree, monkeypatch):
    t = tree
    monkeypatch.setenv("FAKE_EXE_SHA", EXE_SHA)
    b = Bench(Config(t["game"]))
    assert b.integrate_local(False, []) == 0
    assert b._pinned == EXE_SHA
    # build-win/ changes between integrate's check and its golden.
    with open(os.path.join(t["cat"], "build-win", "cat_recomp.exe"), "ab") as f:
        f.write(b"rebuilt")
    calls(t)
    with pytest.raises(ship_mod.BenchError, match="changed during this command"):
        b.run_game([])
    assert calls(t) == []


@pytest.mark.parametrize("windows_dir,exe_dir", [(".", ""), ("", ""), ("build-win", ".")])
def test_ship_refuses_the_run_root_as_dest(tree, windows_dir, exe_dir):
    t = tree
    assert run(t, "build") == 0
    calls(t)
    b = Bench(Config(t["game"]))
    b.cfg.game.m["build"]["windows_dir"] = windows_dir
    b.cfg.game.m["build"]["exe_dir"] = exe_dir
    with pytest.raises(ship_mod.BenchError, match="rsync --delete would empty the run"):
        ship_mod.ship(b)
    assert calls(t) == []
