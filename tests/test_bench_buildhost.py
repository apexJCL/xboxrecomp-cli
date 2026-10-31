"""BENCH_BUILD_HOST (bench/buildhost.py): a native Windows machine compiles
what BENCH_BUILD=local would compile here. The bench runs in-process against
test_bench_windows' tree and fakes; the fake ssh answers build.ps1's state
phase with FAKE_STATE and its build phase with FAKE_BUILD_OUT (exit
FAKE_BUILD_RC; the first build with FAKE_BUILD_FIRST and its exit
FAKE_BUILD_FIRST_RC when set), so each case sees what was sent and what
came back. The script itself runs only on the PC.

  uv run pytest tests/test_bench_buildhost.py
"""

import hashlib
import json
import os
import subprocess
import tarfile

import pytest
from test_bench_ship import run, tree  # noqa: F401 (the fixture)
from test_bench_windows import calls, config, win  # noqa: F401 (the fixture)

from xboxrecomp_cli.bench import Bench, buildhost
from xboxrecomp_cli.bench import ship as ship_mod
from xboxrecomp_cli.bench.config import ConfigError
from xboxrecomp_cli.bench.remote import BenchError

# What the fake sftp writes for every file it gets.
PULLED_SHA = hashlib.sha256(b"x\n").hexdigest()

BUILD_SSH = r"""
case "$*" in
    *build.ps1*)
        p=$(ls "$FAKE_REC"/*-scp.json | tail -1)
        if grep -q '"phase": "state"' "$p"; then printf '%b' "${FAKE_STATE:-}"; exit 0; fi
        if [ -n "${FAKE_BUILD_FIRST:-}" ] && [ ! -e "$FAKE_REC/built" ]; then
            : > "$FAKE_REC/built"; printf '%b' "$FAKE_BUILD_FIRST"; exit "${FAKE_BUILD_FIRST_RC:-0}"
        fi
        printf '%b' "${FAKE_BUILD_OUT:-}"; exit "${FAKE_BUILD_RC:-0}" ;;
esac
"""

HOST = {"BENCH_BUILD_HOST": "pc", "BENCH_BUILD_DIR": "D:/xbr/build-bench"}


@pytest.fixture
def bh(win, monkeypatch):  # noqa: F811
    """test_bench_windows' tree with the build host's ssh answers, and a
    POSIX run host (BENCH_HOST_OS unset): the build host is independent of
    where the exe runs."""
    ssh = [
        os.path.join(p, "ssh")
        for p in os.environ["PATH"].split(os.pathsep)
        if os.path.isfile(os.path.join(p, "rsync"))
    ][0]
    body = open(ssh).read()
    with open(ssh, "w") as f:
        f.write(body.replace('case "$*" in\n', BUILD_SSH.lstrip("\n") + 'case "$*" in\n', 1))
    monkeypatch.delenv("BENCH_HOST_OS")
    for k, v in HOST.items():
        monkeypatch.setenv(k, v)
    with open(win["bench_env"], "w") as f:
        f.write(win["bench_env_text"])
    return win


def files_out(*names):
    return "".join("@xbr-file %s %s\n" % (n, PULLED_SHA) for n in names)


def git(d, *args):
    return subprocess.run(
        ["git", "-C", d] + list(args), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize(
    "env,msg",
    [
        ({"BENCH_BUILD_HOST": "pc"}, "BENCH_BUILD_DIR is not set: a build host"),
        (dict(HOST, BENCH_BUILD="host"), "BENCH_BUILD_HOST=pc needs BENCH_BUILD=local"),
        (dict(HOST, BENCH_BUILD_DIR="~/b"), "BENCH_BUILD_DIR=~/b: a build host takes a drive"),
        (dict(HOST, BENCH_BUILD_LOCK="/tmp/l"), "BENCH_BUILD_LOCK=/tmp/l: a build host"),
        (dict(HOST, BENCH_BUILD_GAME_FILES="g"), "BENCH_BUILD_GAME_FILES=g: a build host"),
    ],
)
def test_config_refusals(bh, env, msg):
    with pytest.raises(ConfigError, match=msg):
        config(bh, **env)


def test_config_paths_and_lock(bh):
    c = config(bh, **dict(HOST, BENCH_BUILD_DIR="D:\\xbr\\build-bench\\"))
    assert c.build_host == "pc" and c["BENCH_BUILD_DIR"] == "D:/xbr/build-bench"
    # The lock the PC's runs take, beside the build trees.
    assert c["BENCH_BUILD_LOCK"] == "D:/xbr/.bench-lock"
    assert not config(bh, BENCH_BUILD_HOST="").build_host
    # A build tree off the usual root still takes the PC's one lock,
    # unless BENCH_BUILD_LOCK names another.
    for d in ("D:/xbr/scratch/x-build", "E:/build"):
        c = config(bh, **dict(HOST, BENCH_BUILD_DIR=d))
        assert c["BENCH_BUILD_LOCK"] == "D:/xbr/.bench-lock"
    c = config(bh, **dict(HOST, BENCH_BUILD_LOCK="D:/l/.lock"))
    assert c["BENCH_BUILD_LOCK"] == "D:/l/.lock"


def test_snapshot_clean_dirty_and_untracked(bh):
    d = bh["cat"]
    head = git(d, "rev-parse", "HEAD")
    assert buildhost.snapshot(d) == (head, "")
    with open(os.path.join(d, "game.toml"), "a") as f:
        f.write("# local edit\n")
    with open(os.path.join(d, "new.txt"), "w") as f:
        f.write("x")
    commit, note = buildhost.snapshot(d)
    # The edit travels as a commit of its own; HEAD, the tree and the stash
    # list stay as they were; the untracked file is named, not sent.
    assert commit != head and "# local edit" in git(d, "show", commit + ":game.toml")
    assert git(d, "rev-parse", "HEAD") == head and git(d, "stash", "list") == ""
    assert note == "1 untracked file(s) in %s are not sent" % d
    # The same edits, the same commit: the host is not sent it again.
    assert buildhost.snapshot(d)[0] == commit


def test_bundle_full_then_incremental(bh, tmp_path):
    d = bh["cat"]
    first = git(d, "rev-parse", "HEAD")
    assert buildhost.bundle(d, first, first, str(tmp_path / "none")) is None
    full = buildhost.bundle(d, first, "", str(tmp_path / "full.bundle"))
    host = str(tmp_path / "host")
    os.makedirs(host)
    git(host, "init", "-q")
    git(host, "fetch", "-q", full, "+%s:refs/xbr/in" % buildhost.REF)
    assert git(host, "rev-parse", "refs/xbr/in") == first
    with open(os.path.join(d, "game.toml"), "a") as f:
        f.write("# second\n")
    git(d, "commit", "-qam", "second")
    second = git(d, "rev-parse", "HEAD")
    inc = buildhost.bundle(d, second, first, str(tmp_path / "inc.bundle"))
    # Only what the host lacks, and it fetches into the host's repository.
    v = subprocess.run(["git", "-C", host, "bundle", "verify", inc], capture_output=True, text=True)
    assert v.returncode == 0 and first in v.stdout + v.stderr
    git(host, "fetch", "-q", inc, "+%s:refs/xbr/in" % buildhost.REF)
    assert git(host, "rev-parse", "refs/xbr/in") == second
    # The ref is gone again: bundles never leave one behind.
    assert subprocess.run(["git", "-C", d, "rev-parse", "-q", "--verify", buildhost.REF]).returncode
    # The host is ahead (a feature branch built before): git would refuse
    # an empty bundle, and the host already has every object.
    assert buildhost.bundle(d, first, second, str(tmp_path / "anc.bundle")) is None
    assert not os.path.exists(tmp_path / "anc.bundle")
    # A host head this repository does not know: the full bundle again.
    other = "1" * 40
    assert buildhost.bundle(d, first, other, str(tmp_path / "unk.bundle"))


def test_gen_state_follows_gen_and_its_key(bh, tmp_path):
    g = config(bh, **HOST).game
    a, n = buildhost.gen_state(g)
    assert n == 2
    with open(g.gen_key, "a") as f:
        f.write(" ")
    b, _ = buildhost.gen_state(g)
    with open(os.path.join(g.gen, "recomp_0001.c"), "a") as f:
        f.write("/* x */\n")
    c, _ = buildhost.gen_state(g)
    assert len({a, b, c}) == 3
    t = buildhost.gen_tarball(g, str(tmp_path / "gen.tar.gz"))
    names = tarfile.open(t).getnames()
    assert g.rel(g.gen_key) in names and g.rel(g.gen) + "/recomp_0001.c" in names


def test_build_sends_builds_and_pulls(bh, monkeypatch, capsys):
    t = bh
    monkeypatch.setenv(
        "FAKE_BUILD_OUT", "compiling\\n" + files_out("cat_recomp.exe", "config.default.toml")
    )
    assert run(t, "buildhost", "build", "-DX=1") == 0
    out = capsys.readouterr().out
    assert "compiling" in out and "@xbr-file" not in out
    cs = calls(t)
    sent = [a for k, argv, _ in cs if k == "scp" for a in argv if a.endswith((".bundle", ".gz"))]
    assert sorted(os.path.basename(a) for a in sent) == [
        "game.bundle",
        "gen.tar.gz",
        "xboxrecomp-cli.bundle",
        "xboxrecomp.bundle",
    ]
    jsons = [e for k, _, e in cs if k == "scp" and e]
    state, build = jsons[0], jsons[-1]
    assert state["phase"] == "state" and build["phase"] == "build"
    assert build["root"] == "D:/xbr/build-bench/blinx2" and build["lock"] == "D:/xbr/.bench-lock"
    assert build["args"] == ["build", "windows", "--stale-gen-ok", "--", "-DX=1"]
    assert build["exe_rel"] == "build-win/cat_recomp.exe" and build["gen"]["tar"] == "gen.tar.gz"
    assert build["gen"]["marker"].endswith(".gen-regenerating")
    # The host checks the tarball, and drops the windows build dir's gen/
    # objects when it unpacks one.
    tar = [a for k, argv, _ in cs if k == "scp" for a in argv if a.endswith(".gz")][0]
    assert build["gen"]["sha256"] and build["gen"]["build"] == "build-win"
    # Each call uploads to its own .in/<call> and pulls from its .out/<call>.
    call = build["call"]
    assert [argv for k, argv, _ in cs if k == "scp" and tar in argv][0][-1] == (
        "pc:D:/xbr/build-bench/blinx2/.in/%s/" % call
    )
    assert (
        "/D:/xbr/build-bench/blinx2/.out/%s/cat_recomp.exe" % call
        in ([e for k, _, e in cs if k == "sftp"][-1])
    )
    # The bundles' ref is the call's own.
    assert build["ref"] == "%s/%s" % (buildhost.REF, call)
    assert build["game_files"]["path"] is None and build["lock_held"] is False
    assert build["owner"].startswith("bench build blinx2 ")
    assert [r[0] for r in build["repos"]] == list(buildhost.REPOS)
    assert build["repos"][0][2] == git(t["cat"], "rev-parse", "HEAD")
    # The pulled files are the exe's directory; ship and run use them.
    c = config(t, **HOST)
    exe = ship_mod.local_exe(c)
    assert exe == os.path.join(t["cat"], "build-win", "build-host", "cat_recomp.exe")
    assert sorted(os.listdir(os.path.dirname(exe))) == [
        "bench-provenance.txt",
        "cat_recomp.exe",
        "config.default.toml",
    ]
    prov = open(os.path.join(os.path.dirname(exe), ship_mod.LOCAL_PROVENANCE)).read()
    assert "build host: pc D:/xbr/build-bench/blinx2" in prov and "\nsent: game " in prov
    # Never built here.
    assert t["builds"] == []


def test_build_sends_only_what_the_host_lacks(bh, monkeypatch):
    t = bh
    monkeypatch.setenv("FAKE_BUILD_OUT", files_out("cat_recomp.exe"))
    c = config(t, **HOST)
    b = Bench(c)
    h = buildhost.BuildHost(b)
    heads = {n: buildhost.snapshot(d)[0] for n, d in h.trees()}
    gen = buildhost.gen_state(c.game)[0]
    monkeypatch.setenv(
        "FAKE_STATE", "".join("head %s %s\\n" % kv for kv in heads.items()) + "gen %s\\n" % gen
    )
    calls(t)
    assert run(t, "buildhost", "build") == 0
    cs = calls(t)
    assert not [
        a for k, argv, _ in cs if k == "scp" for a in argv if a.endswith((".bundle", ".gz"))
    ]
    build = [e for k, _, e in cs if k == "scp" and e][-1]
    assert [r[1] for r in build["repos"]] == ["", "", ""] and build["gen"]["tar"] == ""


def test_build_cli_override(bh, monkeypatch, tmp_path):
    other = str(tmp_path / "cli")
    os.makedirs(other)
    git(other, "init", "-q")
    git(other, "commit", "-q", "--allow-empty", "-m", "pinned cli")
    h = buildhost.BuildHost(Bench(config(bh, BENCH_BUILD_CLI=other, **HOST)))
    assert dict(h.trees())["xboxrecomp-cli"] == other


@pytest.mark.parametrize(
    "rc,out,msg",
    [
        (75, "@xbr-status lock-timeout\\n", "D:/xbr/.bench-lock was held for an hour"),
        # The CLI's own 75 is a failed build, not the lock.
        (75, "", "build: failed on pc (exit 75)"),
        (1, "", "build: failed on pc (exit 1)"),
        (0, "", "pc listed no files for build-win/cat_recomp.exe"),
        (0, "@xbr-file cat_recomp.exe %s\\n" % ("0" * 64), "came back as sha256"),
        # A CLI line that merely starts with "file " is output, not a file.
        (0, "file x.exe %s\\n" % PULLED_SHA, "pc listed no files"),
    ],
)
def test_build_failures(bh, monkeypatch, capsys, rc, out, msg):
    monkeypatch.setenv("FAKE_BUILD_RC", str(rc))
    monkeypatch.setenv("FAKE_BUILD_OUT", out)
    assert run(bh, "buildhost", "build") == 1
    assert msg in capsys.readouterr().err


def test_bench_build_ships_the_pulled_exe(bh, monkeypatch, capsys):
    t = bh
    monkeypatch.setenv("FAKE_BUILD_OUT", files_out("cat_recomp.exe"))
    assert run(t, "build") == 0
    assert t["builds"] == []
    # The posix run host gets the pulled exe, in the run directory its
    # sha256 names, as it gets a local one.
    assert (
        "ship: build-win/build-host/cat_recomp.exe -> fakehost:~/bench-test/blinx2/runs/%s"
        % PULLED_SHA[:12]
    ) in capsys.readouterr().out


def test_run_verb_pulls_paths(bh, monkeypatch):
    t = bh
    monkeypatch.setenv("FAKE_BUILD_OUT", "@xbr-pulled blinx2-patch\\n")
    assert run(t, "buildhost", "run", "package", "windows", "--pull", "dist/*-patch") == 0
    cs = calls(t)
    build = [e for k, _, e in cs if k == "scp" and e][-1]
    # package keeps the gen/ it was sent (the host has no analysis), and
    # build.ps1 copies the pulled paths out under the lock.
    assert build["args"] == ["package", "windows", "--stale-gen-ok"] and build["exe_rel"] is None
    assert build["pull"] == ["dist/*-patch"]
    batch = [e for k, _, e in cs if k == "sftp"][-1]
    assert batch.startswith(
        'get -R "/D:/xbr/build-bench/blinx2/.out/%s/blinx2-patch" ' % build["call"]
    )
    # And the call's .out is removed on the host afterwards.
    assert any(
        k == "ssh"
        and ".out/%s" % build["call"] in " ".join(argv)
        and "Remove-Item" in " ".join(argv)
        for k, argv, _ in cs
    )


@pytest.mark.parametrize(
    "path",
    [
        "../y",
        "a/../y",
        "a\\..\\y",
        "/abs",
        "\\abs",
        "C:/x",
        "game_files",
        "game_files/x.iso",
        # A glob that matches the link, another case, or the tree itself.
        "*",
        "g*",
        "*/default.xbe",
        "Game_Files",
        "GAME_FILES/x.iso",
        ".",
        "./game_files",
    ],
)
def test_run_verb_refuses_pulls_outside_the_tree(bh, capsys, path):
    t = bh
    c = config(t, **HOST)
    path = path.replace("game_files", c.game.m["data"]["game_files"].strip("/"))
    assert run(t, "buildhost", "run", "x", "--pull", path) == 1
    assert "a path inside the game tree" in capsys.readouterr().err
    assert not [k for k, _, _ in calls(t) if k == "scp"]


def test_run_verb_package_keeps_the_local_gen_guards(bh, capsys):
    t = bh
    g = config(t, **HOST).game
    # Mid-regeneration here: nothing is sent.
    open(g.regen_marker, "w").close()
    assert run(t, "buildhost", "run", "package", "windows") == 1
    assert "gen/ is mid-regeneration" in capsys.readouterr().err
    assert calls(t) == []
    os.remove(g.regen_marker)
    # Stale against its key: refused as a local package refuses it.
    with open(g.gen_key, "w") as f:
        f.write("not a key")
    assert run(t, "buildhost", "run", "package", "windows") == 1
    err = capsys.readouterr().err
    assert "is stale: no key" in err and "--stale-gen-ok" in err
    assert not [k for k, _, _ in calls(t) if k == "scp"]
    # The caller's own --stale-gen-ok sends it anyway, once.
    assert run(t, "buildhost", "run", "package", "windows", "--stale-gen-ok") == 0
    build = [e for k, _, e in calls(t) if k == "scp" and e][-1]
    assert build["args"] == ["package", "windows", "--stale-gen-ok"]


def test_run_verb_refuses_gen_changed_while_sent(bh, monkeypatch, capsys):
    t = bh
    g = config(t, **HOST).game
    real = buildhost.BuildHost.run_once

    def edit_then_run(self, *a):
        # recomp rewrites gen/ here while the call is under way.
        with open(os.path.join(g.gen, "recomp_0001.c"), "a") as f:
            f.write("/* later */\n")
        return real(self, *a)

    monkeypatch.setattr(buildhost.BuildHost, "run_once", edit_then_run)
    assert run(t, "buildhost", "run", "package", "windows") == 1
    assert "changed here while it was sent" in capsys.readouterr().err


def test_gen_moved_on_the_host_sends_gen_again(bh, monkeypatch, capsys):
    """Another controller unpacked its gen/ between this call's state
    probe and its lock: build.ps1 refuses, and the call goes again with
    gen/ whatever the host says."""
    t = bh
    c = config(t, **HOST)
    gen = buildhost.gen_state(c.game)[0]
    monkeypatch.setenv("FAKE_STATE", "gen %s\\n" % gen)
    monkeypatch.setenv("FAKE_BUILD_FIRST", "@xbr-status gen-moved\\n")
    monkeypatch.setenv("FAKE_BUILD_FIRST_RC", "76")
    monkeypatch.setenv("FAKE_BUILD_OUT", files_out("cat_recomp.exe"))
    assert run(t, "buildhost", "build") == 0
    assert "changed since the probe; sending it again" in capsys.readouterr().out
    builds = [e for k, _, e in calls(t) if k == "scp" and e and e["phase"] == "build"]
    assert [b["gen"]["tar"] for b in builds] == ["", "gen.tar.gz"]
    assert builds[0]["gen"]["digest"] == builds[1]["gen"]["digest"] == gen


def test_gen_moved_twice_fails(bh, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_BUILD_OUT", "@xbr-status gen-moved\\n")
    monkeypatch.setenv("FAKE_BUILD_RC", "76")
    assert run(bh, "buildhost", "build") == 1
    assert "gen/ on pc kept changing; nothing built" in capsys.readouterr().err


@pytest.mark.parametrize(
    "env,held",
    [
        # A Deck's flock (the run host's BENCH_LOCK) is not the PC's lock.
        ({"BENCH_LOCK_HELD": "1"}, False),
        # The PC as run host (bench.env's BENCH_HOST, fakehost), its lock
        # the build lock: held by the caller.
        (
            {
                "BENCH_LOCK_HELD": "1",
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_LOCK": "d:/xbr/.bench-lock",
            },
            True,
        ),
        # The same file with Windows' slashes.
        (
            {
                "BENCH_LOCK_HELD": "1",
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_LOCK": "D:\\xbr\\.bench-lock",
            },
            True,
        ),
        # The same PC with only another BENCH_LOCK: the build lock follows
        # it, so the PC's runs and builds still queue on one file.
        (
            {
                "BENCH_LOCK_HELD": "1",
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_LOCK": "D:/other/.bench-lock",
            },
            True,
        ),
        # An explicit BENCH_BUILD_LOCK of another file: the build takes it.
        (
            {
                "BENCH_LOCK_HELD": "1",
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_LOCK": "D:/other/.bench-lock",
                "BENCH_BUILD_LOCK": "D:/xbr/.bench-lock",
            },
            False,
        ),
        # Bench and build trees on two drives: still the PC's one lock.
        (
            {
                "BENCH_LOCK_HELD": "1",
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_DIR": "E:/bench",
            },
            True,
        ),
        (
            {
                "BENCH_HOST_OS": "windows",
                "BENCH_BUILD_HOST": "fakehost",
                "BENCH_LOCK": "D:/xbr/.bench-lock",
            },
            False,
        ),
    ],
)
def test_lock_held_only_for_the_build_hosts_own_lock(bh, env, held):
    if env.get("BENCH_HOST_OS"):
        env = dict({"BENCH_DIR": "D:/xbr/bench", "BENCH_GAME_FILES": "D:/xbr/gf"}, **env)
    c = config(bh, **dict(HOST, **env))
    assert buildhost.BuildHost(Bench(c)).params("build")["lock_held"] is held


def test_ship_warns_when_the_exe_here_is_newer(bh, monkeypatch, capsys):
    t = bh
    monkeypatch.setenv("FAKE_BUILD_OUT", files_out("cat_recomp.exe"))
    assert run(t, "buildhost", "build") == 0
    here = os.path.join(t["cat"], "build-win", "cat_recomp.exe")
    os.makedirs(os.path.dirname(here), exist_ok=True)
    with open(here, "w") as f:
        f.write("mac\n")
    later = os.path.getmtime(ship_mod.local_exe(config(t, **HOST))) + 60
    os.utime(here, (later, later))
    capsys.readouterr()
    assert run(t, "run", "--no-build") in (0, 1)
    assert "is newer than the build host's" in capsys.readouterr().out


def test_buildhost_needs_the_host(win, capsys):  # noqa: F811
    assert run(win, "buildhost", "build") == 1
    assert "buildhost: set BENCH_BUILD_HOST" in capsys.readouterr().err


def test_params_json_is_what_build_ps1_reads(bh):
    h = buildhost.BuildHost(Bench(config(bh, **HOST)))
    p = h.params("build", repos=[], gen={}, args=[], exe_rel=None)
    assert set(p) >= {"phase", "root", "slug", "ref", "mingw", "lock", "lock_held", "owner", "call"}
    assert p["mingw"] == config(bh, **HOST).game.m["toolchain"]["llvm_mingw"]
    assert p["game_files"]["path"] is None
    json.dumps(p)


def test_game_files_linked_only_for_a_run(bh, monkeypatch):
    t = bh
    env = dict(HOST, BENCH_BUILD_GAME_FILES="D:\\xbr\\BLiNX2\\game_files\\")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    c = config(t, **env)
    assert run(t, "buildhost", "run", "package", "windows") == 0
    build = [e for k, _, e in calls(t) if k == "scp" and e][-1]
    assert build["game_files"] == {
        "path": "D:/xbr/BLiNX2/game_files",
        "rel": c.game.m["data"]["game_files"],
    }
    # A build reads none, so none is linked.
    monkeypatch.setenv("FAKE_BUILD_OUT", files_out("cat_recomp.exe"))
    assert run(t, "buildhost", "build") == 0
    build = [e for k, _, e in calls(t) if k == "scp" and e][-1]
    assert build["game_files"]["path"] is None


def test_unknown_verb(bh, capsys):
    with pytest.raises(BenchError):
        Bench(config(bh, **HOST)).cmd_buildhost(["nope"])


def test_run_verb_pulls_beside_the_game_files(bh):
    t = bh
    assert run(t, "buildhost", "run", "x", "--pull", "build-win/*.json", "--pull", "./dist") == 0


def test_bundle_clears_a_bare_ref_left_by_an_older_call(bh, tmp_path):
    d = str(tmp_path / "r")
    os.makedirs(d)
    for a in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", d, *a], check=True
        )
    head = subprocess.run(
        ["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", d, "update-ref", buildhost.REF, head], check=True)
    out = str(tmp_path / "b.bundle")
    assert buildhost.bundle(d, head, "", out, buildhost.REF + "/1-2") == out
    left = subprocess.run(
        ["git", "-C", d, "for-each-ref", "refs/worktree/"], capture_output=True, text=True
    )
    assert left.stdout == ""
