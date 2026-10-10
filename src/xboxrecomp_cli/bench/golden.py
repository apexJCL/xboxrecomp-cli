"""bench golden: run each scenario of the game's golden.json (game.toml
golden.json) with frame dumps on, pull the frames it names, and compare
them (the compare engine, xboxrecomp_cli.golden, as a subprocess, as
bench.sh ran scripts/golden.py). Exit 1 on a regression, a missing frame,
a present mismatch, a crash or a test failure; 3 when a run was slow on a
busy host (the compare is printed but proves nothing)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

from .checks import check_present_mismatch, check_run_end
from .remote import BenchError, quote_words
from .sync import rsync


def engine_argv(game, module):
    """`python -m xboxrecomp_cli.<module>` with the game's golden paths."""
    return [sys.executable, "-m", "xboxrecomp_cli." + module] + golden_args(game)


def golden_args(game):
    if not game.golden_json:
        raise BenchError("game.toml sets no golden.json: no golden scenarios for this game")
    from ..golden import enhance_args

    return [
        "--golden-json",
        game.golden_json,
        "--golden-frames",
        game.golden_frames,
        "--game-root",
        game.root,
    ] + enhance_args(game.m["golden"]["enhance_stock"])


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


STAMP_RE = re.compile(r"^\d{8}-\d{6}$")


def keep_frames(cfg):
    return (cfg.get("BENCH_KEEP_FRAMES") or "0") == "1"


def prune(b, runs, res):
    """After a check: for each scenario that passed (EXACT or CLOSE), whose
    run ended cleanly and presented only the walker's surfaces, keep only
    the images the check read (every frame's plain dump, the verdict
    images, the window's best) and remove the run's frames/ on the host.
    Anything short of a pass keeps every frame, here and there: a NEWVIEW's
    are what `golden reference` records the new view from. res: read_used()'s
    {scen: (rc, paths)}."""
    from ..golden import prune_frames

    if keep_frames(b.cfg):
        b.say("golden: frames kept (BENCH_KEEP_FRAMES=1 or --keep-frames)")
        return
    stamps = []
    for scen, (stamp, frames, clean) in runs.items():
        rc, paths = res.get(scen, (None, []))
        if rc != 0 or not clean:
            continue
        n, size, kept = prune_frames(frames, paths)
        b.say("golden: %s: pruned %d flip dumps (%.1f MB), kept %d" % (scen, n, size / 1e6, kept))
        if STAMP_RE.match(stamp):
            stamps.append(stamp)
    if stamps:
        with b.no_errexit():
            rc = b.r.remote(b.r.ship("prune_frames.sh", "STAMPS=(%s)\n" % quote_words(stamps)))[0]
        if rc != 0:
            b.say("golden: the host's frames/ were not all removed (exit %d)" % rc)


def parse_args(args):
    """(mode, force, tests, only) from bench golden's arguments; only is
    None (every scenario) or a list of names."""
    mode, force, tests, only = "check", False, False, None
    it = iter(args)
    for a in it:
        if a == "--record":
            mode = "record"
        elif a == "--force":
            force = True
        elif a == "--tests":
            tests = True
        elif a == "--only" or a.startswith("--only="):
            v = a[len("--only=") :] if "=" in a else next(it, "")
            names = [x for x in v.split(",") if x]
            if not names:
                raise BenchError("golden: --only takes SCEN[,SCEN]")
            only = (only or []) + names
        else:
            raise BenchError("golden: unknown option %s" % a)
    return mode, force, tests, only


def select_rows(plan, only):
    """golden plan's rows, only the named scenarios (in plan order) when
    only is given; an unknown name is an error."""
    rows = [r for r in plan.rstrip("\n").split("\n") if r]
    if only is None:
        return rows
    names = [r.split("\t", 1)[0] for r in rows]
    bad = [x for x in only if x not in names]
    if bad:
        raise BenchError(
            "golden: --only: no scenario %s (golden.json has %s)"
            % (", ".join(bad), ", ".join(names))
        )
    return [r for r in rows if r.split("\t", 1)[0] in only]


def verdict_word(end_rc, present_rc, compare_rc, text=""):
    """One word for a scenario, the hardest failure first: FAIL-RUN (crash
    or early end), FAIL-PRESENT, REGRESSION, then for check's exit 2 the
    first of INCOMPLETE, MISSING and NEWVIEW its output (text) shows (a hub
    view nobody has recorded yet is not a flaky run), INCONCLUSIVE (slow on
    a busy host), pass."""
    if end_rc not in (0, 3):
        return "FAIL-RUN"
    if present_rc:
        return "FAIL-PRESENT"
    if compare_rc == 1:
        return "REGRESSION"
    if compare_rc:
        tags = {line.split(" ", 1)[0] for line in text.splitlines()}
        return next((t for t in ("INCOMPLETE", "MISSING", "NEWVIEW") if t in tags), "INCOMPLETE")
    if end_rc == 3:
        return "INCONCLUSIVE"
    return "pass"


SESSIONS = "golden-sessions.tsv"


def session_line(kind, tests, words, rc, only=None, now=None):
    """A line of bench-logs/golden-sessions.tsv: time, kind (golden or
    integrate), tests (pass, skip, FAIL, off), only=all or the --only list
    (a one-scenario rerun is not a full pass), scen=stamp:WORD per
    scenario, exit code. What a flake count reads."""
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return (
        "\t".join(
            [t, kind, "tests=" + tests, "only=" + (",".join(only) if only else "all")]
            + ["%s=%s:%s" % (scen, stamp, w) for scen, (stamp, w) in words.items()]
            + ["rc=%d" % rc]
        )
        + "\n"
    )


def write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        print("golden: warning: %s not written (%s)" % (path, e), file=sys.stderr)


def cmd_golden(b, args, bench_env=None, kind="golden"):
    b.need_host()
    mode, force, force_tests, only = parse_args(args)
    check_dump(b.cfg.game)
    check_presets(b.cfg.game)
    if bench_env is None:
        bench_env = b.cfg["BENCH_ENV"]
    rc, plan = golden_py(b, "plan", capture=True)
    if rc != 0:
        raise BenchError("golden: no plan")
    rows = select_rows(plan, only)
    # Missing reference PNGs (gitignored: they are game frames) are named
    # now, not after the runs: a check without them can only FAIL, so it
    # stops here; --record makes them, so there it only warns.
    if golden_py(b, "refs")[0] != 0 and mode != "record":
        raise BenchError("golden: reference PNGs missing (see above)")
    with b.no_errexit():
        trc, tests = b.tests_or_skip(force_tests)
        trc = 1 if trc != 0 else 0
    prc = erc = inc = grc = 0
    dirs = []
    # Per scenario: (stamp, frames dir, whether its run ended cleanly and
    # presented only the walker's surfaces), for the prune after the check;
    # and its run checks' lines and rcs, for its golden.txt.
    runs, notes = {}, {}
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
        lines = []

        def out(msg, lines=lines):
            b.say(msg)
            lines.append(msg)

        e = check_run_end(log, minf, out=out, crash_tag=b.cfg.game.m["bench"]["crash_tag"])
        if e == 3:
            inc = 1
        elif e != 0:
            erc = 1
        pm = check_present_mismatch(os.path.join(log, "game-stdio.log"), out=out)
        if pm != 0:
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
        runs[scen] = (stamp, os.path.join(log, "frames"), e == 0 and pm == 0)
        notes[scen] = (log, lines, e, pm)
    b.step("golden: %s" % mode)
    if mode == "record" and not force and (prc or erc or inc or trc):
        b.say(
            "golden: not recording: a toolkit test failed, or a run crashed, ended early, ran "
            "slow or presented surfaces the walker did not draw (--force to record anyway)"
        )
        return 1
    if mode == "record":
        if golden_py(b, mode, *dirs)[0] != 0:
            grc = 1
    else:
        grc = check_each(b, runs, notes, prune_after=not trc)
        part = " (--only: not a full pass)" if only else ""
        b.say(
            "golden: verdicts: %s%s"
            % (", ".join("%s %s" % (scen, notes[scen][4]) for scen in runs), part)
        )
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
        rc = 1
    elif inc:
        b.say(
            "golden: INCONCLUSIVE: a run was slow on a busy host (see end: above); run again "
            "on a quiet host"
        )
        rc = 3
    else:
        rc = grc
    if mode == "check":
        words = {scen: (runs[scen][0], notes[scen][4]) for scen in runs}
        sessions = os.path.join(b.cfg.game_dir, "bench-logs", SESSIONS)
        try:
            with open(sessions, "a", encoding="utf-8") as f:
                f.write(session_line(kind, tests, words, rc, only))
        except OSError as e:
            print("golden: warning: %s not written (%s)" % (sessions, e), file=sys.stderr)
    return rc


def check_each(b, runs, notes, prune_after):
    """golden check per scenario (the same compare as one call over all of
    them), its output printed and written with the run's own checks to the
    run's golden.txt, ending in its verdict line. Returns 1 on any FAIL,
    else 2 on any INCOMPLETE, else 0; notes[scen] gains the verdict word."""
    from ..golden import read_used

    res, rcs = {}, []
    for scen, (_stamp, frames, _clean) in runs.items():
        fd, used = tempfile.mkstemp(prefix="golden-used-")
        os.close(fd)
        try:
            rc, text = golden_py(b, "check", "--used", used, "%s=%s" % (scen, frames), capture=True)
            try:
                res.update(read_used(used))
            except OSError:
                pass
        finally:
            os.remove(used)
        sys.stdout.write(text)
        sys.stdout.flush()
        log, lines, e, pm = notes[scen]
        word = verdict_word(e, pm, rc, text)
        notes[scen] = (log, lines, e, pm, word)
        write_text(
            os.path.join(log, "golden.txt"),
            "".join(x + "\n" for x in lines) + text + "verdict: %s %s\n" % (scen, word),
        )
        rcs.append(rc)
    if prune_after:
        prune(b, runs, res)
    return 1 if 1 in rcs else 2 if any(rcs) else 0
