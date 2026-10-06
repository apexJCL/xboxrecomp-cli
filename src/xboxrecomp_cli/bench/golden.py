"""bench golden: run each scenario of the game's golden.json (game.toml
golden.json) with frame dumps on, pull the frames it names, and compare
them (the compare engine, xboxrecomp_cli.golden, as a subprocess, as
bench.sh ran scripts/golden.py). Exit 1 on a regression, a missing frame,
a present mismatch, a crash or a test failure; 3 when a run was slow on a
busy host (the compare is printed but proves nothing)."""

import json
import os
import subprocess
import sys

from .checks import check_present_mismatch, check_run_end
from .remote import BenchError
from .sync import rsync


def engine_argv(game, module):
    """`python -m xboxrecomp_cli.<module>` with the game's golden paths."""
    return [sys.executable, "-m", "xboxrecomp_cli." + module] + golden_args(game)


def golden_args(game):
    if not game.golden_json:
        raise BenchError("game.toml sets no golden.json: no golden scenarios for this game")
    return [
        "--golden-json",
        game.golden_json,
        "--golden-frames",
        game.golden_frames,
        "--game-root",
        game.root,
    ]


def check_dump(game):
    """The goldens were recorded on the dumps xbe.sha256 lists: another
    dump's frames mean nothing against them, so golden refuses it before
    any run (an empty list turns the check off). The dump checked is this
    tree's, the one `sync --game-files` copies to the host; a controller
    with no dump of its own (the host's BENCH_GAME_FILES came from
    elsewhere) only warns, as the host's copy is not checked."""
    from .. import host

    known = game.m["xbe"]["sha256"]
    if not known:
        return
    if not os.path.isfile(game.xbe):
        print(
            "golden: warning: no %s here, so the host's dump (BENCH_GAME_FILES) is not "
            "checked against the known dumps (game.toml xbe.sha256)" % game.rel(game.xbe),
            file=sys.stderr,
        )
        return
    sha = host.sha256_path(game.xbe)
    if sha not in known:
        raise BenchError(
            "golden: unknown dump %s (sha256 %s); the references were recorded on %s"
            % (game.rel(game.xbe), sha, ", ".join(known))
        )


def check_presets(game):
    """A scenario's input script must be a preset the game compiles in
    (input.presets): one that names another would run with no input and
    fail its frames for the wrong reason. An empty list: no check."""
    presets, key = game.m["input"]["presets"], game.m["input"]["script_env"]
    if not presets or not game.golden_json:
        return
    with open(game.golden_json) as f:
        scenarios = json.load(f).get("scenarios", {})
    bad = [
        "%s (%s)" % (name, sc["env"][key])
        for name, sc in scenarios.items()
        if sc.get("env", {}).get(key, "").startswith("@") and sc["env"][key] not in presets
    ]
    if bad:
        raise BenchError(
            "golden: %s name no preset in game.toml input.presets (%s)"
            % (", ".join(bad), ", ".join(presets))
        )


def golden_py(b, *args, stdout=None, capture=False):
    sys.stdout.flush()
    argv = engine_argv(b.cfg.game, "golden") + list(args)
    if capture:
        r = subprocess.run(argv, stdout=subprocess.PIPE)
        return r.returncode, r.stdout.decode(errors="replace")
    return subprocess.run(argv, stdout=stdout).returncode, ""


def cmd_golden(b, args, bench_env=None):
    b.need_host()
    mode, force = "check", False
    for a in args:
        if a == "--record":
            mode = "record"
        elif a == "--force":
            force = True
        else:
            raise BenchError("golden: unknown option %s" % a)
    check_dump(b.cfg.game)
    check_presets(b.cfg.game)
    if bench_env is None:
        bench_env = b.cfg["BENCH_ENV"]
    rc, plan = golden_py(b, "plan", capture=True)
    if rc != 0:
        raise BenchError("golden: no plan")
    # Missing reference PNGs (gitignored: they are game frames) are named
    # now, not after the runs: a check without them can only FAIL, so it
    # stops here; --record makes them, so there it only warns.
    if golden_py(b, "refs")[0] != 0 and mode != "record":
        raise BenchError("golden: reference PNGs missing (see above)")
    with b.no_errexit():
        trc = 1 if b.cmd_tests([]) != 0 else 0
    prc = erc = inc = grc = 0
    dirs = []
    rows = plan.rstrip("\n").split("\n")
    for row in rows:
        scen, secs, minf, env = (row.split("\t", 3) + ["", "", "", ""])[:4]
        b.step("golden: %s (%ss)" % (scen, secs))
        # The scenario's env goes last so it wins over BENCH_ENV: the
        # references are D3D11 ones whatever the caller exported. The flips
        # around each frame (golden.py dumpat) are dumped too, so an anchored
        # frame is checked at the same distance from its event as the reference.
        rc, dumpat = golden_py(b, "dumpat", scen, capture=True)
        if rc != 0:
            raise BenchError("golden: no dumpat list for %s" % scen)
        dumpat = dumpat.rstrip("\n")
        stamp = b.run_game(
            [],
            bench_env=(bench_env + " " if bench_env else "")
            + "%s RECOMP_DEBUG=fb_dump_at=%s" % (env, dumpat),
            timeout=secs,
            frames="1",
        )
        log = os.path.join(b.cfg.game_dir, "bench-logs", stamp)
        if minf == "0":
            minf = ""
        e = check_run_end(log, minf, out=b.say)
        if e == 3:
            inc = 1
        elif e != 0:
            erc = 1
        if check_present_mismatch(os.path.join(log, "game-stdio.log"), out=b.say) != 0:
            prc = 1
        os.makedirs(os.path.join(log, "frames"), exist_ok=True)
        _, idxs = golden_py(b, "frames", scen, capture=True)
        c = b.cfg
        for idx in idxs.split():
            if (
                rsync(
                    "-az",
                    "%s:%s/bench-logs/%s/frames/frame_%s.bmp" % (c.host, c.remote_game, stamp, idx),
                    os.path.join(log, "frames") + "/",
                )
                != 0
            ):
                b.say("golden: %s frame_%s.bmp was not dumped" % (scen, idx))
        # Only the flips the check reads (each target +-2, from the run's
        # anchors), not all of dumpat's window (150-230 MB a scenario).
        pulls = os.path.join(log, "frames", "pulls.txt")
        with open(pulls, "w") as f:
            rc, _ = golden_py(b, "pulls", scen, os.path.join(log, "game-stdio.log"), stdout=f)
        if rc != 0:
            open(pulls, "w").close()
        # A directory source with --files-from does not get its leading ~
        # expanded on the host; the remote rsync starts in $HOME anyway.
        rg = c.remote_game[2:] if c.remote_game.startswith("~/") else c.remote_game
        if (
            rsync(
                "-az",
                "--files-from=%s" % pulls,
                "%s:%s/bench-logs/%s/frames/" % (c.host, rg, stamp),
                os.path.join(log, "frames") + "/",
            )
            != 0
        ):
            b.say(
                "golden: %s: some flip dumps were not pulled (the check says INCOMPLETE if it "
                "needed them)" % scen
            )
        dirs.append("%s=%s" % (scen, os.path.join(log, "frames")))
    b.step("golden: %s" % mode)
    if mode == "record" and not force and (prc or erc or inc or trc):
        b.say(
            "golden: not recording: a toolkit test failed, or a run crashed, ended early, ran "
            "slow or presented surfaces the walker did not draw (--force to record anyway)"
        )
        return 1
    if golden_py(b, mode, *dirs)[0] != 0:
        grc = 1
    if erc:
        b.say("golden: FAIL: a run crashed or ended early (see end: above)")
    if prc:
        b.say("golden: FAIL: a run presented surfaces the walker did not draw (see present: above)")
    if trc:
        b.say("golden: FAIL: a toolkit test failed (see tests: above)")
    if mode == "record" and force and (prc or erc or inc or trc):
        b.say("golden: WARNING: recorded with --force from a bad run; re-record after a clean one")
    # A hard failure wins; then a slow run on a busy host; then the compare.
    if prc or erc or trc:
        return 1
    if inc:
        b.say(
            "golden: INCONCLUSIVE: a run was slow on a busy host (see end: above); run again "
            "on a quiet host"
        )
        return 3
    return grc
