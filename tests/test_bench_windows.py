"""BENCH_HOST_OS=windows (bench/hosts.py): a native Windows host driven over
OpenSSH with PowerShell scripts, scp and sftp. The bench runs in-process
against test_bench_ship's tree, with fake ssh, scp and sftp first on PATH,
so each case sees what went to the host: the params each script got, the
files sent, the pulls asked for. The scripts themselves run only on the PC.

  uv run pytest tests/test_bench_windows.py
"""

import json
import os

import pytest
from test_bench_ship import run, tree  # noqa: F401 (the fixture)

from xboxrecomp_cli.bench.config import Config, ConfigError
from xboxrecomp_cli.bench.hosts import REFUSED, game_env, script_digest, sftp_batch

# ssh: the script check answers FAKE_SCRIPTS_RC (3: not there yet), ship.ps1
# lists FAKE_NEED, Get-FileHash answers FAKE_EXE_SHA, run_game.ps1 exits
# FAKE_RUN_RC.
FAKE_SSH = r"""#!/bin/bash
n=$(cat "$FAKE_REC/n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_REC/n"
printf '%s\0' "$@" > "$FAKE_REC/$(printf %04d "$n")-ssh.argv"
case "$*" in
    *Test-Path*) exit "${FAKE_SCRIPTS_RC:-3}" ;;
    *ship.ps1*) printf '%b' "${FAKE_NEED:-}" ;;
    *Get-FileHash*) echo "${FAKE_EXE_SHA:-none}" ;;
    *run_game.ps1*) exit "${FAKE_RUN_RC:-0}" ;;
esac
exit 0
"""
# scp: a params file is kept beside its argv.
FAKE_SCP = r"""#!/bin/bash
n=$(cat "$FAKE_REC/n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_REC/n"
f="$FAKE_REC/$(printf %04d "$n")-scp"
printf '%s\0' "$@" > "$f.argv"
for a in "$@"; do case "$a" in *.json) cp "$a" "$f.json";; esac; done
exit 0
"""
# sftp: the batch is kept; each get of a name writes the file, unless the
# name matches FAKE_SFTP_MISS; a get of a log dir (*) writes a clean exit.
FAKE_SFTP = r"""#!/bin/bash
n=$(cat "$FAKE_REC/n" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_REC/n"
f="$FAKE_REC/$(printf %04d "$n")-sftp"
printf '%s\0' "$@" > "$f.argv"
while [ $# -gt 1 ]; do [ "$1" = -b ] && cp "$2" "$f.batch"; shift; done
while read -r get r l; do
    r=${r//\"/}; l=${l//\"/}
    case "$r" in *\**) printf 0 > "${l}exit-code"; : > "${l}game-stdio.log"; continue;; esac
    name=${r##*/}
    if [ -n "${FAKE_SFTP_MISS:-}" ] && [[ "$name" =~ $FAKE_SFTP_MISS ]]; then continue; fi
    echo x > "$l$name"
done < "$f.batch"
exit 0
"""

WIN = {"BENCH_HOST_OS": "windows", "BENCH_DIR": "D:/bench/bench", "BENCH_GAME_FILES": "D:/bench/gf"}
# scripts/bench.env is applied over the environment, so the host paths go
# there; the rest of WIN goes in the environment.
FILE_KEYS = ("BENCH_DIR", "BENCH_GAME_FILES")


def bench_env(t, **kv):
    """The scratch's bench.env with KV's host paths (single-quoted, as a
    Windows path needs) after its own lines."""
    with open(t["bench_env"], "w") as f:
        f.write(t["bench_env_text"])
        for k in FILE_KEYS:
            if k in kv:
                f.write("%s='%s'\n" % (k, kv[k]) if kv[k] else "%s=\n" % k)


@pytest.fixture
def win(tree, monkeypatch):  # noqa: F811
    bindir = [
        p for p in os.environ["PATH"].split(os.pathsep) if os.path.isfile(os.path.join(p, "rsync"))
    ][0]
    for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP), ("sftp", FAKE_SFTP)):
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
    tree["bench_env"] = os.path.join(tree["cat"], "scripts", "bench.env")
    tree["bench_env_text"] = open(tree["bench_env"]).read()
    bench_env(tree, **WIN)
    monkeypatch.setenv("BENCH_HOST_OS", "windows")
    return tree


def calls(t):
    """[(kind, argv, extra)] since the last call, and forget them; extra is
    a params file's JSON (scp) or an sftp batch."""
    rec = t["rec"]
    out = []
    for n in sorted(x for x in os.listdir(rec) if x.endswith(".argv")):
        base = n[: -len(".argv")]
        kind = base.split("-")[1]
        argv = open(os.path.join(rec, n), "rb").read().decode().split("\0")[:-1]
        extra = None
        if os.path.exists(os.path.join(rec, base + ".json")):
            extra = json.load(open(os.path.join(rec, base + ".json")))
        if os.path.exists(os.path.join(rec, base + ".batch")):
            extra = open(os.path.join(rec, base + ".batch")).read()
        out.append((kind, argv, extra))
        for x in os.listdir(rec):
            if x.startswith(base):
                os.remove(os.path.join(rec, x))
    if os.path.exists(os.path.join(rec, "n")):
        os.remove(os.path.join(rec, "n"))
    return out


def params(cs, script):
    """The params JSON sent just before the ssh that ran SCRIPT."""
    for i, (kind, argv, _) in enumerate(cs):
        if kind == "ssh" and any(script in a for a in argv):
            return cs[i - 1][2]
    raise AssertionError("%s did not run: %s" % (script, cs))


def config(t, **env):
    """A Config from ENV alone, its host paths written to bench.env."""
    bench_env(t, **{k: env.pop(k) for k in FILE_KEYS if k in env})
    e = {"BENCH_GAME_DIR": t["cat"], "XBOXRECOMP_DIR": t["tk"], "HOME": "/h"}
    e.update(env)
    return Config(t["game"], e)


@pytest.mark.parametrize(
    "env,msg",
    [
        ({"BENCH_HOST_OS": "macos"}, "BENCH_HOST_OS=macos: use one of posix, windows"),
        ({"BENCH_HOST_OS": "windows"}, "is not set: a Windows host"),
        (dict(WIN, BENCH_GAME_FILES=""), "BENCH_GAME_FILES is not set"),
        (dict(WIN, BENCH_DIR="~/xbr"), "BENCH_DIR=~/xbr: a Windows host takes a drive path"),
        (dict(WIN, BENCH_DIR="/srv/x"), "BENCH_DIR=/srv/x: a Windows host takes a drive path"),
        (dict(WIN, BENCH_LOCK='D:/a"b'), 'BENCH_LOCK=D:/a"b: a Windows host'),
        (dict(WIN, BENCH_BUILD="host"), "BENCH_BUILD=host: a Windows host only runs"),
    ],
)
def test_config_refusals(win, env, msg):
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(")):
        config(win, **dict(env))


def test_config_windows_paths(win):
    c = config(win, **dict(WIN, BENCH_DIR="D:\\bench\\bench\\", BENCH_LOCK="E:\\locks\\pc"))
    assert c.windows and c["BENCH_DIR"] == "D:/bench/bench" and c.base == "D:/bench/bench/blinx2"
    assert c["BENCH_LOCK"] == "E:/locks/pc"
    assert config(win, **WIN)["BENCH_LOCK"] == "D:/bench/.bench-lock"
    # The POSIX host is the default and unchanged.
    bench_env(win)
    p = config(win)
    assert not p.windows and p["BENCH_HOST_OS"] == "posix" and p["BENCH_DIR"] == "~/bench-test"


def test_game_env_joins_lists_and_resolves_run():
    env = dict(
        game_env(
            "RECOMP_TRACE=flip RECOMP_DEBUG=a RECOMP_SAVE_DIR=@run 'X=a b' RECOMP_DEBUG=b "
            "RECOMP_FB_DUMP=@run RECOMP_TRACE=pb",
            "D:/bench/bench/g/runs/r/bench-logs/s",
            "D:/bench/gf",
            "1",
            "95",
        )
    )
    log = "D:\\bench\\bench\\g\\runs\\r\\bench-logs\\s"
    assert env["RECOMP_TRACE"] == "flip,pb"
    assert env["RECOMP_DEBUG"] == "a,b,fb_dump=%s\\frames\\f,d3d11_dump=%s\\frames\\" % (log, log)
    assert env["RECOMP_SAVE_DIR"] == log + "\\save" and "RECOMP_FB_DUMP" not in env
    assert env["X"] == "a b"
    assert env["RECOMP_STDIO_LOG"] == log + "\\game-stdio.log"
    assert env["RECOMP_GAME_FILES"] == "D:\\bench\\gf"
    assert env["RECOMP_HDD_DIR"] == log + "\\hdd"
    assert env["RECOMP_WINDOW_QUIT_AFTER"] == "95"
    # No limit, no frames: neither key; a caller's game files win.
    env = dict(game_env("RECOMP_GAME_FILES=E:/g", "D:/l", "D:/gf", "0", ""))
    assert "RECOMP_WINDOW_QUIT_AFTER" not in env and "RECOMP_DEBUG" not in env
    assert env["RECOMP_GAME_FILES"] == "E:/g"


def test_sftp_batch_names_drive_paths():
    assert sftp_batch([("D:/a/frame_1.bmp", "/l/f")]) == '-get "/D:/a/frame_1.bmp" "/l/f/"\n'


def test_sftp_batch_keeps_a_glob_unquoted():
    assert sftp_batch([("D:/a b/logs/*", "/l/s")]) == '-get "/D:/a b/logs/"* "/l/s/"\n'


@pytest.mark.parametrize("cmd", sorted(REFUSED))
def test_verbs_refused(win, capsys, cmd):
    assert run(win, cmd) == 1
    err = capsys.readouterr().err
    assert "%s: not on a Windows host (BENCH_HOST_OS=windows)" % cmd in err, err
    assert calls(win) == []


def test_sync_game_files_refused(win, capsys):
    assert run(win, "sync", "--game-files") == 1
    assert (
        "copy the game files to BENCH_GAME_FILES (D:/bench/gf) by hand" in capsys.readouterr().err
    )


def test_logs_needs_a_stamp(win, capsys):
    assert run(win, "logs") == 1
    assert "name the run's stamp on a Windows host" in capsys.readouterr().err


def test_build_ships_by_scp(win, monkeypatch, capsys):
    t = win
    monkeypatch.setenv(
        "FAKE_NEED", "need build-win/cat_recomp.exe\nneed build-win/provenance.txt\n"
    )
    assert run(t, "build") == 0
    cs = calls(t)
    d = "D:/bench/bench/.xbr/host/%s" % script_digest()
    # The scripts go once per digest, digest.txt last; then ship.ps1's
    # params, ship.ps1, and only the files it named.
    assert [c[0] for c in cs] == ["ssh", "scp", "scp", "scp", "ssh", "scp"], cs
    assert cs[1][1][-1] == "fakehost:%s/" % d and cs[2][1][-2].endswith("digest.txt")
    assert sorted(os.path.basename(a) for a in cs[1][1] if a.endswith(".ps1")) == [
        "build.ps1",
        "launch.ps1",
        "prune_frames.ps1",
        "run_game.ps1",
        "ship.ps1",
    ]
    p = cs[3][2]
    assert p["run_dir"] == "D:/bench/bench/blinx2/runs/" + p["files"][0][2][:12]
    assert p["exact"] == "build-win"
    assert [f[1] for f in p["files"]] == [
        "cat_recomp.exe",
        "cat_recomp.pdb",
        "config.default.toml",
        "provenance.txt",
    ]
    assert cs[3][1][-1] == "fakehost:D:/bench/bench/.xbr/params/"
    assert 'ship.ps1" -Params "D:/bench/bench/.xbr/params/ship-' in cs[4][1][-1]
    sent = cs[5][1]
    assert [os.path.basename(a) for a in sent[-3:-1]] == ["cat_recomp.exe", "provenance.txt"]
    assert sent[-1] == "fakehost:%s/build-win/" % p["run_dir"]


def test_run_sends_env_and_pulls_logs(win, monkeypatch, capsys):
    t = win
    monkeypatch.setenv("BENCH_ENV", "RECOMP_TRACE=flip RECOMP_SAVE_DIR=@run")
    monkeypatch.setenv("BENCH_TIMEOUT", "30")
    assert run(t, "build") == 0
    calls(t)
    monkeypatch.setenv("FAKE_SCRIPTS_RC", "0")
    assert run(t, "run") == 0
    cs = calls(t)
    # The scripts are there and ship.ps1 needs nothing: only params go.
    assert [c for c in cs if c[0] == "scp" and c[2] is None] == []
    p = params(cs, "run_game.ps1")
    env = dict(p["env"])
    assert env["RECOMP_SAVE_DIR"].endswith("\\bench-logs\\%s\\save" % p["stamp"])
    assert env["RECOMP_TRACE"] == "flip" and env["RECOMP_WINDOW_QUIT_AFTER"] == "30"
    assert p["timeout"] == 30 and p["lock"] == "D:/bench/.bench-lock" and not p["lock_held"]
    assert p["expect_sha"] == params(cs, "ship.ps1")["files"][0][2]
    assert p["exe_rel"] == "build-win/cat_recomp.exe" and p["game_files"] == "D:/bench/gf"
    assert p["owner"].startswith("bench blinx2 %s " % p["stamp"])
    assert p["launch"].endswith("/launch.ps1")
    batch = cs[-1][2]
    assert batch == '-get "/%s/"* "%s/"\n' % (
        p["log"],
        os.path.join(t["cat"], "bench-logs", p["stamp"]),
    )


@pytest.mark.parametrize(
    "rc,msg",
    [
        (75, "D:/bench/.bench-lock was held for an hour; nothing ran"),
        (76, "is no longer the one this command shipped"),
        (2, "the host did not start the game (exit 2"),
    ],
)
def test_run_failures_name_the_cause(win, monkeypatch, capsys, rc, msg):
    assert run(win, "build") == 0
    monkeypatch.setenv("FAKE_RUN_RC", str(rc))
    assert run(win, "run") == 1
    assert msg in capsys.readouterr().err


def test_integrate_checks_the_host_sha(win, monkeypatch, capsys):
    from xboxrecomp_cli.bench import Bench
    from xboxrecomp_cli.bench import ship as ship_mod

    t = win
    assert run(t, "build") == 0
    b = Bench(config(t, **WIN))
    ship_mod.resolve(b.cfg)
    monkeypatch.setenv("FAKE_SCRIPTS_RC", "0")
    monkeypatch.setenv("FAKE_EXE_SHA", "abc")
    rc, out = b.host.exe_sha()
    assert rc == 0 and out == "abc  %s/build-win/cat_recomp.exe\n" % b.cfg.remote_game


def test_pull_frames_names_the_missing(win, monkeypatch, tmp_path):
    from xboxrecomp_cli.bench import Bench

    monkeypatch.setenv("FAKE_SFTP_MISS", "^frame_2")
    b = Bench(config(win, **WIN))
    missing = b.host.pull_frames("20261024-120000", ["frame_1.bmp", "frame_2.bmp"], str(tmp_path))
    assert missing == ["frame_2.bmp"] and (tmp_path / "frame_1.bmp").exists()
    assert b.host.pull_frames("s", [], str(tmp_path)) == []


def fake_golden_py(monkeypatch, out, seen):
    """bench.golden's golden_py, answering every call with (0, OUT) and
    recording its arguments in SEEN; a pulls call (stdout=f) writes OUT."""
    from xboxrecomp_cli.bench import golden as bg

    def golden_py(b, *args, stdout=None, capture=False):
        seen.append(args)
        if stdout is not None:
            stdout.write(out)
        return 0, out

    monkeypatch.setattr(bg, "golden_py", golden_py)


def test_check_each_marks_only_windows_and_keeps_known(win, monkeypatch, tmp_path):
    from xboxrecomp_cli.bench import Bench
    from xboxrecomp_cli.bench import golden as bg

    text = "KNOWN    s/f\ngolden: KNOWN s/f: TRACK-42\ngolden: PASS-with-known (1 KNOWN)\n"
    for cfg, host_os in ((config(win, **WIN), ["--host-os", "windows"]), (config(win), [])):
        seen = []
        fake_golden_py(monkeypatch, text, seen)
        log = tmp_path / ("w" if host_os else "p")
        log.mkdir()
        notes = {"s": (str(log), ["end: ok"], 0, 0)}
        assert bg.check_each(Bench(cfg), {"s": ("st", str(log), True)}, notes, False) == 0
        # The Deck's argv is today's: no --host-os, so it ignores the marking.
        assert seen[0][1 : 1 + len(host_os)] == tuple(host_os) and seen[0][0] == "check"
        assert ("--host-os" in seen[0]) == bool(host_os)
        assert notes["s"][4] == "pass-with-known"
        assert (log / "golden.txt").read_text().endswith("verdict: s pass-with-known\n")


def test_known_scenario_is_not_pruned(win, tmp_path):
    from xboxrecomp_cli.bench import Bench
    from xboxrecomp_cli.bench import golden as bg

    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "flip_00080.bmp").write_bytes(b"x")
    b = Bench(config(win, **WIN))
    bg.prune(b, {"s": ("20261024-120000", str(frames), True)}, {"s": (3, [])})
    assert (frames / "flip_00080.bmp").exists()


def test_pull_windows_gets_frames_and_pulls(win, monkeypatch, capsys, tmp_path):
    from xboxrecomp_cli.bench import Bench
    from xboxrecomp_cli.bench import golden as bg

    seen = []
    fake_golden_py(monkeypatch, "flip_00080.bmp\nflip_00081.bmp\n", seen)
    monkeypatch.setenv("FAKE_SFTP_MISS", "^(frame_2|flip_00081)")
    b = Bench(config(win, **WIN))
    log = tmp_path / "log"
    (log / "frames").mkdir(parents=True)
    calls(win)
    bg.pull_windows(b, "s", "20261024-120000", str(log), ["1", "2"])
    out = capsys.readouterr().out
    assert "golden: s frame_2.bmp was not dumped" in out
    assert "golden: s: some flip dumps were not pulled" in out
    assert (log / "frames" / "frame_1.bmp").exists() and (
        log / "frames" / "flip_00080.bmp"
    ).exists()
    assert seen == [("pulls", "s", str(log / "game-stdio.log"))]
    batches = [e for k, _, e in calls(win) if k == "sftp"]
    remote = "/D:/bench/bench/blinx2/bench-logs/20261024-120000/frames"
    assert '-get "%s/frame_1.bmp"' % remote in batches[0]
    assert '-get "%s/flip_00081.bmp"' % remote in batches[1]


def test_game_env_keeps_the_shader_cache_in_the_base():
    env = dict(game_env("", "D:/b/g/runs/r/bench-logs/s", "D:/gf", "0", "", "D:/b/g/shader-cache"))
    assert env["RECOMP_DXBC_CACHE"] == "D:\\b\\g\\shader-cache"
    assert env["RECOMP_HDD_DIR"] == "D:\\b\\g\\runs\\r\\bench-logs\\s\\hdd"
    # A scenario's own setting wins.
    env = dict(game_env("RECOMP_DXBC_CACHE=0", "D:/l", "D:/gf", "0", "", "D:/c"))
    assert env["RECOMP_DXBC_CACHE"] == "0"
