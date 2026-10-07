"""bench: build and run the game's Windows x86-64 build under Proton on a
remote Linux host, driven from this machine over ssh. The controller is
here, standard-library Python; what runs on the host is shell, in host/,
sent on ssh's standard input exactly as the game's old scripts/bench.sh
sent it.

Loaded by main.py only for `<game> bench`.
"""

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time

from . import checks
from .config import Config, ConfigError
from .gc import cmd_gc
from .golden import cmd_golden
from .pacing import cmd_pacing
from .remote import BenchError, Remote, fill, host_text, quote_words, shell_quote
from .sync import cmd_sync, rsync

HELP = """\
usage: @SLUG@ bench <command> [args]

Build and run the Windows x86-64 build of this game under Proton on a remote
x86-64 Linux host (the bench host), driven from this machine over ssh.

Commands:
  setup     create the build distrobox, install cmake/ninja/umu-launcher,
            download llvm-mingw                                (once per host)
  sync      rsync the toolkit (see XBOXRECOMP_DIR) and this project to the host
            --game-files  also set up @GAME_FILES@/ (your own host only): the
                      host keeps ONE copy, BENCH_GAME_FILES. A sync from the
                      tree that owns it copies this machine's @GAME_FILES@/ into
                      it; any other tree gets a symlink to it (nothing copied)
            --game-files=reflink  a btrfs reflink copy instead of the symlink
  build     configure + compile build-win/ on the host     [extra cmake args]
            (holds the host's run lock, shared, so no game runs during a build)
  run       launch @WINDOWS_EXE@ under Proton, log to
            bench-logs/<timestamp>/ on the host, then pull logs   [game args];
            fails if any D3D11 flip presented a surface other than the one the
            walker drew, or on @CRASH_TAG@, or (with a limit) an early exit
  golden    run the golden-frame scenarios (@GOLDEN_JSON@) and
            compare their frames; first runs `tests`. Exit 1 on a regression,
            3 when a run was slow on a busy host (INCONCLUSIVE)
            --record  take this run's frames as the new references (refused
                      after a crash, a present mismatch or a test failure
                      unless --force is also given); to record one frame, run
                      @SLUG@ golden record --only NAME SCEN=DIR
  tests     build and ctest the toolkit's Proton tests, holding the run lock
            exclusively
  pacing    frame-pacing A/B (@SLUG@ pacing-stats): one golden scenario
            under two environments, alternating A B A B ..., all under one
            hold of the run lock; report in bench-logs/<stamp>-pacing/
            [--scen SCEN (@PACING@)] [--runs N (3)] [--gate clock|sleep]
            ["A env" "B env"]   (default "RECOMP_PRESENT_PACING=spin"
            "RECOMP_PRESENT_PACING=sleep"); each run adds
            RECOMP_TRACE=flip,pacing=all, and BENCH_ENV applies to both arms
  logs      symbolize a run's crash reports, then pull bench-logs/ from the
            host into ./bench-logs/              [stamp, default: newest run]
  symbolize name the native addresses in a run's @CRASH_TAG@ reports, into
            bench-logs/<stamp>/crash-symbols.txt  [stamp, default: newest run]
  gc        list bench runs nothing names any more, with their sizes; a dry
            run unless --apply. Only stamp-named run dirs ever go; kept:
            other games' runs, runs named in golden.json, TASKS.md,
            RESUME*.md, openspec/ or game.toml [bench.gc] refs (in every
            worktree), runs without exit-code, young runs and the newest per
            scenario. --host: the host's store, under the run lock; a run
            goes only when this machine holds a whole copy
            [--apply] [--host] [--keep N] [--days D] [--include-unowned]
            [--refs PATH]... [--quiet]
  shell     open an ssh shell in the host's project directory
  all       sync, build, run
  integrate sync + build the integration heads into BENCH_DIR; run from the
            integration checkout (@GAME_NAME@ @MAIN_BRANCH@@TOOLKIT_BRANCH@), clean trees only
            --golden  then run golden once
            --dirty   allow uncommitted changes in either tree
            --stale-gen-ok  sync even when gen/ is stale against its key
                      (run analyze and recomp instead)
  doctor    check the bench without changing anything: ssh, rsync, the
            host's umu-run and distrobox, the run lock's holder, the host's
            bench-provenance.txt

--kill-game (any command that runs the game; or BENCH_KILL_GAME=1): end a
game running outside the bench once the run lock is held. Without it the
run only warns, on stdout and in bench-logs/<stamp>/warnings.txt.

--keep-frames (golden; or BENCH_KEEP_FRAMES=1): keep every frame dump of a
passing scenario. Without it, a scenario that passes (EXACT or CLOSE, its
run clean) keeps only the images its check read: each frame's plain dump,
the verdict images and the window's best flip; its run's frames/ on the
host goes. A run that fails, is INCOMPLETE or INCONCLUSIVE, or --record,
keeps everything.

--proton-log (any command that runs the game or pulls logs; or
BENCH_PROTON_LOG=full): keep and pull Proton's whole log,
bench-logs/<stamp>/steam-default.log. Without it the host keeps its first
and last 8 MiB (a run with +seh can write gigabytes), and logs never pulls
a Proton log larger than that.

The run lock: every command that builds or runs takes the host's run lock
(~/.recomp-run.lock) itself, inside its host script: shared for builds,
exclusive for runs and tests, waiting up to an hour (exit 75). Never wrap
`@SLUG@ bench` in an outer flock. This controller never touches the lock.

Configuration, from the environment or scripts/bench.env (KEY=value lines,
never synced or committed; its lines win over the environment). Quote a
host path that starts with ~ (BENCH_DIR='~/bench'): unquoted, the ~ becomes
this machine's home, as it did when bench.sh sourced the file:

  BENCH_HOST       ssh target, e.g. user@host                    (required)
  BENCH_DIR        project root on the host          (~/xbox-recomp)
  BENCH_BOX        distrobox name                    (xbr-build)
  BENCH_IMAGE      distrobox image                   (fedora:42)
  LLVM_MINGW_ROOT  llvm-mingw install on the host    ($BENCH_DIR/llvm-mingw)
  LLVM_MINGW_TAG   llvm-mingw release to install     (game.toml)
  PROTONPATH       Proton for umu-run                (GE-Proton = latest GE)
  BENCH_PREFIX     Wine prefix on the host           ($BENCH_DIR/prefix)
  BENCH_GAME_FILES the host's single @GAME_FILES@ copy (~/xbox-recomp/<game>/@GAME_FILES@);
                   every other tree links to it; it may be read-only
  BENCH_ENV        space-separated VAR=value pairs for the game, e.g.
                   "RECOMP_AC97_READY=0 WINEDEBUG=+seh"; RECOMP_SAVE_DIR=@run
                   gives the run an empty save dir, bench-logs/<stamp>/save/.
                   RECOMP_TRACE=/RECOMP_DEBUG= entries add up (comma-joined)
                   with the ones a scenario pins
  BENCH_TIMEOUT    stop the game with SIGINT after this many seconds
  BENCH_FRAMES     1: RECOMP_DEBUG=d3d11_dump into bench-logs/<stamp>/frames/
                   on the host (every 60th present; not pulled by logs)
  BENCH_HOLD_MAX   pacing: the most seconds its hold of the run lock lasts if
                   this command dies without releasing it  (14400)
  BENCH_KEEP_FRAMES 1: as --keep-frames
  BENCH_KILL_GAME  1: as --kill-game
  BENCH_PROTON_LOG Proton's log (PROTON_LOG=1) per run: cap (the default:
                   its first and last 8 MiB), full (as --proton-log) or off
                   (not written; symbolize then needs the crash report's
                   own load address)
  BENCH_GAME_DIR   the project tree to drive (default: this checkout)
  XBOXRECOMP_DIR   local toolkit checkout (external/xboxrecomp if present,
                   else ../xboxrecomp)

run-info.txt records the exe's sha256 and, from build-win/provenance.txt, the
@TREE@ and toolkit commits it was built from and whether either tree was dirty
at sync time.

The host always gets the toolkit next to the project, so CMakeLists.txt finds
it at ../xboxrecomp there (external/ is not synced):

  $BENCH_DIR/xboxrecomp   toolkit working tree
  $BENCH_DIR/<game>       this project; build-win/ and bench-logs/ live here
"""


def render_help(game):
    """HELP with the game's names (game.toml), as the old bench.sh header
    read for BLiNX 2."""
    m = game.m
    tkb = m["bench"]["toolkit_branch"]
    scen = m["bench"]["pacing_scenario"] or "the first in golden.json"
    values = {
        "SLUG": game.slug,
        "WINDOWS_EXE": "%s/%s.exe" % (m["build"]["windows_dir"], m["build"]["exe"]),
        "GOLDEN_JSON": m["golden"]["json"] or "game.toml golden.json",
        "PACING": scen,
        "GAME_NAME": m["pipeline"]["game_name"],
        "GAME_FILES": m["data"]["game_files"],
        # The provenance lines' name for the game tree: its remote name.
        "TREE": m["bench"]["remote_name"] or os.path.basename(game.root.rstrip("/")),
        "CRASH_TAG": m["bench"]["crash_tag"],
        "MAIN_BRANCH": m["bench"]["main_branch"],
        # The old text wrapped here; keep its two lines when a branch is named.
        "TOOLKIT_BRANCH": ("; a toolkit off\n            %s only warns" % tkb) if tkb else "",
    }
    text = HELP
    for k, v in values.items():
        text = text.replace("@%s@" % k, v)
    return text


# Bytes of Proton's log kept at each end by default (BENCH_PROTON_LOG=cap).
# The loader trace symbolize reads sits in the first few hundred KB.
PROTON_LOG_CAP = 8 << 20
PROTON_LOG_MODES = ("cap", "full", "off")


def proton_log_mode(cfg):
    mode = cfg.get("BENCH_PROTON_LOG") or "cap"
    if mode not in PROTON_LOG_MODES:
        raise BenchError("BENCH_PROTON_LOG=%s: use one of %s" % (mode, ", ".join(PROTON_LOG_MODES)))
    return mode


class Exit(Exception):
    """set -e: a command failed outside a condition; the script ends with
    its code."""

    def __init__(self, rc):
        Exception.__init__(self, rc)
        self.rc = rc


class Bench:
    def __init__(self, cfg):
        self.cfg = cfg
        self.r = Remote(cfg)
        self.errexit = True

    # bash's echo, step, die and set -e
    def say(self, msg="", end="\n"):
        sys.stdout.write(msg + end)
        sys.stdout.flush()

    def step(self, msg):
        self.say()
        self.say("== %s ==" % msg)

    def ok(self, rc):
        """A command bench.sh ran plainly: under set -e a failure ends the
        whole command, unless a caller ran it in a condition."""
        if rc and self.errexit:
            raise Exit(rc)
        return rc

    @contextlib.contextmanager
    def no_errexit(self):
        """`f || ...`: bash turns set -e off for everything f runs."""
        saved, self.errexit = self.errexit, False
        try:
            yield
        finally:
            self.errexit = saved

    def need_host(self):
        if not self.cfg.host:
            raise BenchError("set BENCH_HOST (e.g. in scripts/bench.env)")

    # setup
    def cmd_setup(self, args):
        self.need_host()
        c, r = self.cfg, self.r
        self.step("setup: distrobox %s (%s)" % (c["BENCH_BOX"], c["BENCH_IMAGE"]))
        self.ok(r.remote(r.prologue() + fill("setup_box.sh.in", BENCH_IMAGE=c["BENCH_IMAGE"]))[0])
        self.step("setup: packages")
        self.ok(r.in_box(r.ship("setup_pkgs.sh"))[0])
        # Proton runs from the host, not the box: the host ships umu-run.
        self.ok(r.remote(r.ship("check_umu.sh", prologue=False))[0])
        self.step("setup: llvm-mingw %s -> %s" % (c["LLVM_MINGW_TAG"], c["LLVM_MINGW_ROOT"]))
        tag = "TAG=%s\n" % shell_quote(c["LLVM_MINGW_TAG"])
        return self.ok(r.in_box(r.ship("setup_mingw.sh", tag))[0])

    # build
    def cmd_build(self, args):
        self.need_host()
        self.step("build: %s/build-win" % self.cfg.remote_game)
        # No arguments: an empty array (printf '%q ' with no arguments still
        # prints '', which made every build re-run cmake with an empty one).
        extra = "EXTRA_ARGS=(%s)\n" % quote_words(args)
        rc = self.r.in_box_locked(self.r.ship("build.sh", extra))[0]
        if rc == 75:
            raise BenchError("build: the run lock was held for an hour; nothing built")
        if rc:
            raise BenchError("build failed (exit %d)" % rc)
        return 0

    # run
    def run_game(self, game_args, bench_env=None, timeout=None, frames=None, lock_held=None):
        """Launch the game on the host and pull its logs; returns the stamp."""
        self.need_host()
        c = self.cfg
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.step("run: under %s -> bench-logs/%s" % (c["PROTONPATH"], stamp))
        assigns = (
            "STAMP=%s\nPROTONPATH=%s\nGAME_ARGS=(%s)\nGAME_ENV=(%s)\nTIMEOUT=%s\nFRAMES=%s\n"
            "KILL_GAME=%s\nLOCK_HELD=%s\nPROTON_LOG_MODE=%s\nPROTON_LOG_CAP=%d\n"
            % (
                shell_quote(stamp),
                shell_quote(c["PROTONPATH"]),
                quote_words(game_args),
                # Raw, as bench.sh sent it: the host word-splits BENCH_ENV.
                c["BENCH_ENV"] if bench_env is None else bench_env,
                shell_quote(c.get("BENCH_TIMEOUT") if timeout is None else timeout),
                shell_quote((c.get("BENCH_FRAMES") or "0") if frames is None else frames),
                shell_quote(c.get("BENCH_KILL_GAME") or "0"),
                shell_quote((c.get("BENCH_LOCK_HELD") or "0") if lock_held is None else lock_held),
                proton_log_mode(c),
                PROTON_LOG_CAP,
            )
        )
        self.r.remote(self.r.ship("run_game.sh", assigns))
        self.cmd_logs([stamp])
        self.say("local: %s" % os.path.join(c.game_dir, "bench-logs", stamp))
        return stamp

    def cmd_run(self, args):
        stamp = self.run_game(args)
        log = os.path.join(self.cfg.game_dir, "bench-logs", stamp)
        rc = 0
        with self.no_errexit():
            tag = self.cfg.game.m["bench"]["crash_tag"]
            if checks.check_run_end(log, out=self.say, crash_tag=tag) != 0:
                rc = 1
            if (
                checks.check_present_mismatch(os.path.join(log, "game-stdio.log"), out=self.say)
                != 0
            ):
                rc = 1
        return rc

    # tests
    def cmd_tests(self, args):
        self.need_host()
        if not self.cfg.game.m["bench"]["toolkit_tests"]:
            self.say("tests: skipped (game.toml: bench.toolkit_tests = false)")
            return 0
        self.step(
            "tests: d3d8_hlsl_split, d3d11_backend_smoke, input_map, input_keyboard, nv2a_zbuf, apu_irq, "
            "kernel_irql_abi, fp_precision, vblank_ack, vblank_schedule, spin_wait, rt_alias, irq_safe_points, "
            "kernel_missing_report, kernel_file_status under Proton"
        )
        rc = self.r.in_box_locked(self.r.ship("tests.sh"), exclusive=True, what="tests")[0]
        if rc == 0:
            self.say("tests: pass")
        elif rc == 75:
            self.say("tests: FAIL the run lock was held for an hour; nothing run")
        else:
            self.say("tests: FAIL (exit %d)" % rc)
        return rc

    # logs and symbols
    def cmd_symbolize(self, args):
        self.need_host()
        stamp = args[0] if args else ""
        return self.r.remote(self.r.ship("symbolize.sh", "STAMP=%s\n" % shell_quote(stamp)))[0]

    def cmd_logs(self, args):
        self.need_host()
        with self.no_errexit():
            self.cmd_symbolize(args[:1])
        dst = os.path.join(self.cfg.game_dir, "bench-logs")
        os.makedirs(dst, exist_ok=True)
        src = "%s:%s/bench-logs/" % (self.cfg.host, self.cfg.remote_game)
        # Frame dumps stay on the host (golden pulls the ones it needs). Pulls
        # keep -a's times: logs and frames are not build input.
        if proton_log_mode(self.cfg) == "full":
            return self.ok(rsync("-az", "--exclude", "/*/frames/", src, dst + "/"))
        # Proton's logs apart, and none larger than a capped one: a run from
        # before the cap (or with --proton-log) left gigabytes on the host,
        # which a pull of the whole tree would copy again.
        self.ok(
            rsync("-az", "--exclude", "/*/frames/", "--exclude", "/*/steam-*.log", src, dst + "/")
        )
        return self.ok(
            rsync(
                "-az",
                "--max-size=%d" % (2 * PROTON_LOG_CAP + (1 << 20)),
                "--include",
                "/*/",
                "--include",
                "/*/steam-*.log",
                "--exclude",
                "*",
                src,
                dst + "/",
            )
        )

    def cmd_shell(self, args):
        self.need_host()
        c = self.cfg
        return self.r.command(
            "cd %s 2>/dev/null || cd %s; exec $SHELL -l" % (c.remote_game, c["BENCH_DIR"]),
            batch=False,
            tty=True,
        )[0]

    # integrate
    def cmd_integrate(self, args):
        self.need_host()
        golden = dirty = stale_ok = False
        for a in args:
            if a == "--golden":
                golden = True
            elif a == "--dirty":
                dirty = True
            elif a == "--stale-gen-ok":
                stale_ok = True
            else:
                raise BenchError("integrate: unknown option %s" % a)
        c = self.cfg

        def git(d, *a):
            return subprocess.run(["git", "-C", d] + list(a), stdout=subprocess.PIPE).stdout.decode(
                errors="replace"
            )

        policy = c.game.m["bench"]
        b = git(c.game_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if b != policy["main_branch"]:
            raise BenchError(
                "integrate: %s is on %s, not %s (run from the integration checkout)"
                % (c.game_dir, b, policy["main_branch"])
            )
        b = git(c.toolkit, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if policy["toolkit_branch"] and b != policy["toolkit_branch"]:
            print(
                "integrate: WARNING %s is on %s, not %s (the branch this "
                "project is tested with)" % (c.toolkit, b, policy["toolkit_branch"]),
                file=sys.stderr,
            )
        if not dirty:
            if git(c.game_dir, "status", "--porcelain"):
                raise BenchError("integrate: %s is dirty (--dirty to sync anyway)" % c.game_dir)
            if git(c.toolkit, "status", "--porcelain"):
                raise BenchError("integrate: %s is dirty (--dirty to sync anyway)" % c.toolkit)
        gen = c.game.gen
        try:
            names = [n for n in os.listdir(gen) if not n.startswith(".")]
        except OSError:
            names = []
        if not names:
            raise BenchError(
                "integrate: no %s here (run %s recomp first)"
                % (c.game.m["pipeline"]["gen"], c.game.slug)
            )
        # Before a byte moves: a gen/ from before a toolkit or recomp_manual.c
        # change syncs, builds for minutes and fails at link (a wrapped
        # function's old gen/ defines it twice).
        if not stale_ok:
            why = gen_stale_reasons(c)
            if why:
                raise BenchError(
                    "integrate: %s/ is stale: %s; run '%s analyze && %s recomp' "
                    "(--stale-gen-ok to integrate anyway)"
                    % (c.game.m["pipeline"]["gen"], ", ".join(why), c.game.slug, c.game.slug)
                )
        self.ok(cmd_sync(self, []))
        self.step("integrate: gen/ check")
        lg = checks.gen_digest_local(gen)
        rc, rg = self.r.remote(self.r.ship("gen_digest.sh"), capture=True)
        self.ok(rc)
        rg = rg.rstrip("\n")
        if lg != rg:
            raise BenchError("integrate: host gen/ differs from local (%s vs %s)" % (rg, lg))
        self.say("gen: %d files, sha256 %s (host matches)" % (len(names), lg))
        self.ok(self.cmd_build([]))
        self.step(
            "integrate: built %s/%s/%s.exe"
            % (c.remote_game, c.game.m["build"]["windows_dir"], c.game.m["build"]["exe"])
        )
        self.ok(self.r.remote(self.r.ship("integrate_show.sh"))[0])
        if not golden:
            return 0
        # Integration runs see no host pad or keyboard and fail on a bad
        # script, as golden.json's env pins for golden.
        env = c["BENCH_ENV"]
        env = (
            env + " " if env else ""
        ) + "RECOMP_HOST_PAD=0 RECOMP_KEYBOARD=0 RECOMP_INPUT_STRICT=1"
        with self.no_errexit():
            return cmd_golden(self, [], bench_env=env)

    # doctor
    def cmd_doctor(self, args):
        """What the bench needs, checked without changing anything."""
        c, ok = self.cfg, True
        if not c.host:
            self.say("host:     BENCH_HOST is not set (scripts/bench.env)")
            return 1
        self.say("host:     %s (BENCH_DIR %s, box %s)" % (c.host, c["BENCH_DIR"], c["BENCH_BOX"]))
        self.say("rsync:    %s" % (shutil.which("rsync") or "MISSING on this machine"))
        ok &= bool(shutil.which("rsync"))
        if shutil.which("rsync") and " " in c.game.m["data"]["game_files"]:
            # A game files folder with a space reaches the host unquoted:
            # rsync 3.2.4+ escapes remote paths itself, openrsync does not.
            out = subprocess.run(["rsync", "--version"], capture_output=True, text=True).stdout
            if not rsync_escapes_args(out):
                self.say(
                    "rsync:    %s cannot sync %r (a space): install rsync 3.2.4 or newer"
                    % ((out.splitlines() or ["?"])[0].strip(), c.game.m["data"]["game_files"])
                )
                ok = False
        rc, _ = self.r.command("true", capture=True)
        self.say(
            "ssh:      %s"
            % ("ok" if rc == 0 else "FAIL (ssh -o BatchMode=yes %s true: exit %d)" % (c.host, rc))
        )
        if rc != 0:
            return 1
        rc, out = self.r.remote(self.r.prologue() + host_text("doctor.sh"), capture=True)
        self.say(out, end="")
        return 0 if ok and rc == 0 else 1


def gen_stale_reasons(cfg):
    """pipeline.gen_stale_reasons() for the tree and toolkit the bench
    drives (BENCH_GAME_DIR, XBOXRECOMP_DIR), which need not be the CLI's
    current ones."""
    from .. import manifest, pipeline

    saved, saved_tk = manifest.current(), os.environ.get("XBOXRECOMP_DIR")
    manifest.use(cfg.game)
    os.environ["XBOXRECOMP_DIR"] = cfg.toolkit
    try:
        return pipeline.gen_stale_reasons()
    finally:
        manifest.use(saved)
        if saved_tk is None:
            os.environ.pop("XBOXRECOMP_DIR", None)
        else:
            os.environ["XBOXRECOMP_DIR"] = saved_tk


def rsync_escapes_args(version_text):
    """Whether `rsync --version` names rsync 3.2.4 or newer, which escapes
    its remote arguments (openrsync and older rsync do not)."""
    m = re.match(r"rsync\s+version\s+v?(\d+)\.(\d+)\.(\d+)", version_text.strip())
    return bool(m) and tuple(int(x) for x in m.groups()) >= (3, 2, 4)


COMMANDS = {
    "setup": Bench.cmd_setup,
    "build": Bench.cmd_build,
    "run": Bench.cmd_run,
    "logs": Bench.cmd_logs,
    "symbolize": Bench.cmd_symbolize,
    "shell": Bench.cmd_shell,
    "integrate": Bench.cmd_integrate,
    "tests": Bench.cmd_tests,
    "doctor": Bench.cmd_doctor,
}


def dispatch(b, cmd, args):
    if cmd == "sync":
        return cmd_sync(b, args)
    if cmd == "golden":
        return cmd_golden(b, args)
    if cmd == "gc":
        return cmd_gc(b, args)
    if cmd == "pacing":
        return cmd_pacing(b, args)
    if cmd == "all":
        b.ok(cmd_sync(b, []))
        b.ok(b.cmd_build([]))
        return b.cmd_run([])
    return COMMANDS[cmd](b, args)


def main(argv, game):
    """game: the manifest.Game the CLI was started for."""
    argv = list(argv)
    cmd = argv.pop(0) if argv else ""
    if cmd in ("-h", "--help", "help"):
        sys.stdout.write(render_help(game))
        return 0
    if cmd not in COMMANDS and cmd not in ("sync", "golden", "pacing", "all", "gc"):
        sys.stdout.write(render_help(game))
        return 1
    try:
        cfg = Config(game)
    except ConfigError as e:
        print("bench: %s" % e, file=sys.stderr)
        return 1
    for w in cfg.warnings():
        print("bench: warning: %s" % w, file=sys.stderr)
    # --kill-game, for any command that runs the game: as BENCH_KILL_GAME=1.
    if "--kill-game" in argv:
        cfg.env["BENCH_KILL_GAME"] = "1"
        argv = [a for a in argv if a != "--kill-game"]
    # --keep-frames: as BENCH_KEEP_FRAMES=1.
    if "--keep-frames" in argv:
        cfg.env["BENCH_KEEP_FRAMES"] = "1"
        argv = [x for x in argv if x != "--keep-frames"]
    # --proton-log: as BENCH_PROTON_LOG=full.
    if "--proton-log" in argv:
        cfg.env["BENCH_PROTON_LOG"] = "full"
        argv = [a for a in argv if a != "--proton-log"]
    b = Bench(cfg)
    try:
        return dispatch(b, cmd, argv) or 0
    except Exit as e:
        return e.rc
    except BenchError as e:
        sys.stdout.flush()
        print("bench: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
