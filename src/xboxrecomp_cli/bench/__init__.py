"""bench: build and run the game's Windows x86-64 build under Proton on a
remote Linux host, driven from this machine over ssh. The controller is
here, standard-library Python; what runs on the host is shell, in host/,
sent on ssh's standard input exactly as the game's old scripts/bench.sh
sent it.

Loaded by main.py only for `<game> bench`.
"""

import contextlib
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import checks, tests_skip
from . import ship as ship_mod
from .config import Config, ConfigError
from .gc import cmd_gc
from .gc import parse_args as gc_parse_args
from .golden import cmd_golden, golden_py, select_rows
from .golden import parse_args as golden_parse_args
from .hosts import WindowsHost, refusal
from .pacing import cmd_pacing
from .remote import BenchError, Remote, fill, host_text, quote_words, shell_quote
from .sync import cmd_sync, rsync

HELP = """\
usage: @SLUG@ bench <command> [args]

Build and run the Windows x86-64 build of this game under Proton on a remote
x86-64 Linux host (the bench host), driven from this machine over ssh.

By default (BENCH_BUILD=local) this machine builds the exe, as `@SLUG@ build
windows` does, and the host only runs it: each exe ships, with its PDB,
config.default.toml and DLLs, to a run directory of its own,
$BENCH_DIR/<game>/runs/<id>/ (id: BENCH_RUN, else the exe's sha256, 12 hex),
seeded on the host from the newest one so only changed blocks move; the
ship then deletes what this build lacks, so the exe's directory holds exactly
the files shipped. run, golden and pacing never build: they ship the exe
built here, and the host checks its sha256 again under the run lock (exit 76
when another ship replaced it). A host with ~/.recomp-no-build refuses build,
tests and the build box whatever the controller asks.

Commands:
  setup     check umu-launcher, download llvm-mingw (symbolize); host mode
            also creates the build distrobox with cmake/ninja  (once per host)
  sync      local: ship the exe already built here. host: rsync the toolkit
            (see XBOXRECOMP_DIR) and this project to the host
            --game-files  also set up @GAME_FILES@/ (your own host only): the
                      host keeps ONE copy, BENCH_GAME_FILES. A sync from the
                      tree that owns it copies this machine's @GAME_FILES@/ into
                      it; any other tree gets a symlink to it (nothing copied)
            --game-files=reflink  a btrfs reflink copy instead of the symlink
  build     local: build here, then ship               [extra cmake args]
            host: configure + compile build-win/ on the host (holds the
            host's run lock, shared, so no game runs during a build)
  run       (local: ship first, when the host lacks this exe)
            launch @WINDOWS_EXE@ under Proton, log to
            bench-logs/<timestamp>/ on the host, then pull logs   [game args];
            fails if any D3D11 flip presented a surface other than the one the
            walker drew, or on @CRASH_TAG@, or (with a limit) an early exit.
            Prints the run's flips/s: exact with RECOMP_TRACE=flip, else a
            floor from the D3D11 backend's present #N line (at the first
            present and every 300th).
            The game starts from the Windows package's launch.env.default
            (as the installed launchers do; BENCH_ENV wins over it), so it
            gets the packaged backend; pacing and all do the same, golden
            does not (its scenarios pin their own env)
  golden    run the golden-frame scenarios (@GOLDEN_JSON@) and
            compare their frames; first runs `tests`. Exit 1 on a regression,
            3 when a run was slow on a busy host (INCONCLUSIVE)
            --record  take this run's frames as the new references (refused
                      after a crash, a present mismatch or a test failure
                      unless --force is also given); to record one frame, run
                      @SLUG@ golden record --only NAME SCEN=DIR
            --only SCEN[,SCEN]  run only these scenarios (a rerun of the
                      one that failed)
            --tests   run the tests even when nothing they test changed
            Each scenario's checks and verdict go to bench-logs/<stamp>/
            golden.txt, one line per pass to bench-logs/golden-sessions.tsv.
            The tests are skipped when the host's toolkit tree, the CLI, the
            toolchain and the Proton and prefix versions are those of their
            last pass on this host tree (bench-logs/tests-pass/), and when
            the tree has no configured build-win/ (synced for goldens only,
            its exe copied in), and always with BENCH_BUILD=local (tests=
            nobuild; --tests then fails)
  tests     (host only; local refuses) build and ctest the toolkit's
            Proton tests in build-win/,
            holding the run lock exclusively, under PROTONPATH; a pass is
            recorded for golden's skip. A box whose /etc/resolv.conf went
            stale (the host replaced it) is restarted first
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
  gc        replaced by `benchdb plan` and `benchdb clean`: prints the
            benchdb plan call and exits 2   [--host] [--days D]
  db        the bench results database: `benchdb VERB` for this game
            (import, query, releases, show, regress, plan, clean, ...)
  shell     open an ssh shell in the host's project directory
  all       sync, build, run (local: build here, ship, run)
  integrate build the integration heads (local: here, then ship and check
            the host's exe sha256; host: sync + build there); run from the
            integration checkout (@GAME_NAME@ @MAIN_BRANCH@@TOOLKIT_BRANCH@), clean trees only
            --golden  then run golden once (--only SCEN[,SCEN] and --tests
                      as for golden)
            --dirty   allow uncommitted changes in either tree
            --stale-gen-ok  sync even when gen/ is stale against its key
                      (run analyze and recomp instead)
  doctor    check the bench without changing anything: ssh, rsync, the
            host's umu-run and distrobox, a running box's /etc/resolv.conf
            (resolv: STALE, which tests restarts), the run lock's holder,
            the host's bench-provenance.txt

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
  BENCH_BUILD      local (build here, ship the exe) or host (the host
                   compiles a synced tree)            (local)
  BENCH_RUN        the run directory's name, instead of the exe's sha256
  BENCH_HOST_OS    posix (ssh bash, rsync, flock, Proton) or windows (native
                   Windows 11 over OpenSSH: PowerShell, scp/sftp, a lock file;
                   BENCH_BUILD=local, BENCH_DIR and BENCH_GAME_FILES drive
                   paths such as D:/bench; setup, tests, pacing, gc,
                   doctor and symbolize refused)               (posix)
  BENCH_LOCK       windows: the lock file every PC run creates and deletes
                   (BENCH_LOCK_HELD=1: the caller holds it)
                   (.bench-lock beside BENCH_DIR)
  BENCH_JOBS       local builds: compile jobs        (0: as memory allows)
  BENCH_BOX        distrobox name                    (xbr-build)
  BENCH_IMAGE      distrobox image                   (fedora:42)
  LLVM_MINGW_ROOT  llvm-mingw install on the host    ($BENCH_DIR/llvm-mingw)
  LLVM_MINGW_TAG   llvm-mingw release to install     (game.toml)
  PROTONPATH       Proton for umu-run                (GE-Proton = latest GE)
  BENCH_PREFIX     Wine prefix on the host           ($BENCH_DIR/prefix)
  BENCH_GAME_FILES the host's single @GAME_FILES@ copy; every run directory
                   (local) or other tree (host) links to it. local:
                   $BENCH_DIR/<game>/@GAME_FILES@, which sync --game-files
                   leaves read-only and a ship refuses with UDATA/TDATA in
                   it (the runtime would keep saves there); host:
                   ~/xbox-recomp/<game>/@GAME_FILES@, may be read-only
  BENCH_ENV        space-separated VAR=value pairs for the game, e.g.
                   "RECOMP_AC97_READY=0 WINEDEBUG=+seh"; RECOMP_SAVE_DIR=@run
                   gives the run an empty save dir, bench-logs/<stamp>/save/.
                   RECOMP_TRACE=/RECOMP_DEBUG= entries add up (comma-joined)
                   with the ones a scenario pins. run, pacing and all put
                   launch.env.default under it; a key here replaces the file's
  BENCH_TIMEOUT    stop the game with SIGINT after this many seconds
  BENCH_FRAMES     1: RECOMP_DEBUG=d3d11_dump into bench-logs/<stamp>/frames/
                   on the host (every 60th present; not pulled by logs).
                   A golden run's bounded dump list goes to /dev/shm while
                   the game runs on Proton hosts (when it has room) and
                   moves there after
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

The host's layout. local: one base per host, shared by every tree:

  $BENCH_DIR/prefix, llvm-mingw (symbolize), xboxrecomp-cli (running_game.py)
  $BENCH_DIR/<game>/runs/<id>  build-win/, game_files -> BENCH_GAME_FILES,
                               bench-logs/
  (<game>: [bench] remote_name, else the game's slug)

host: the toolkit next to the project, so CMakeLists.txt finds it at
../xboxrecomp there (external/ is not synced):

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


# Keys run_game.sh sets for every run itself, after the env it is sent, as
# the installed launchers set theirs after reading the defaults; PROTONPATH
# as the SteamOS launch.sh skips it: the bench runs the Proton it pins.
BENCH_OWN_KEYS = frozenset(
    ("PROTONPATH", "RECOMP_STDIO_LOG", "WINEPREFIX", "GAMEID", "PROTON_LOG", "PROTON_LOG_DIR")
)
# Keys only the launchers read (log trimming, finding umu-run, the start
# notice): the game never sees them, so they stay out of GAME_ENV and
# run-info.txt.
LAUNCHER_ONLY_KEYS = frozenset(("LOG_KEEP", "LOG_MAX_MB", "UMU_RUN", "LAUNCH_NOTICE", "NOTICE_MAX"))
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def read_launch_env(path):
    """[(key, value)] of a launch.env file, read as the SteamOS launch.sh
    reads it: KEY=value lines, the value raw to the end of the line, '#'
    comments and empty lines skipped. None when the file does not exist.
    Any other line (an indented one, one of blanks) is an error naming it,
    as launch.sh fails on one. Split on \n alone: splitlines() would also
    break a value at \x0b, \x0c, \x85 or U+2028, which bash does not."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        return None
    if lines and lines[-1] == "":
        lines.pop()
    out = []
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\r")
        if not line or line.startswith("#"):
            continue
        k, eq, v = line.partition("=")
        if not eq or not ENV_KEY.match(k):
            raise BenchError("%s:%d: not a KEY=value line: %s" % (path, n, line))
        out.append((k, v))
    return out


def env_pairs(text):
    """[(key, value)] of a raw GAME_ENV text (BENCH_ENV and what callers
    add), split as the host's bash splits it."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    return [tuple(w.split("=", 1)) for w in words if "=" in w]


def env_keys(text):
    return {k for k, _ in env_pairs(text)}


def launch_defaults(game):
    """(path, [(key, value)] or None): the game's launch.env.default for the
    Windows target, the file `package windows` and `package steamos` stage
    (package.launch_env_path), less the keys the bench owns and those only
    the launchers read."""
    from ..package import launch_env_path

    path = launch_env_path("windows", game)
    kvs = read_launch_env(path)
    if kvs is not None:
        kvs = [(k, v) for k, v in kvs if k not in BENCH_OWN_KEYS | LAUNCHER_ONLY_KEYS]
    return path, kvs


def with_defaults(defaults, text):
    """GAME_ENV's raw text with the defaults first and quoted, less any key
    the text sets itself: the explicit value wins outright, even for the
    list keys (RECOMP_TRACE, RECOMP_DEBUG) that run_game.sh would join."""
    own = env_keys(text)
    words = [shell_quote("%s=%s" % kv) for kv in defaults or () if kv[0] not in own]
    return " ".join(words + ([text] if text else []))


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
        self.host = WindowsHost(self) if cfg.windows else None
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
        tag = "TAG=%s\n" % shell_quote(c["LLVM_MINGW_TAG"])
        if c.local_build:
            # Nothing compiles there: no build box, no cmake or ninja. The
            # host keeps umu-run and llvm-mingw (symbolize's llvm-symbolizer
            # and llvm-readobj), fetched with the host's own curl and tar.
            self.step("setup: no build box (BENCH_BUILD=local)")
            self.ok(r.remote(r.ship("check_umu.sh", prologue=False))[0])
            self.step("setup: llvm-mingw %s -> %s" % (c["LLVM_MINGW_TAG"], c["LLVM_MINGW_ROOT"]))
            return self.ok(r.remote(r.ship("setup_mingw.sh", tag))[0])
        self.step("setup: distrobox %s (%s)" % (c["BENCH_BOX"], c["BENCH_IMAGE"]))
        rc = r.remote(r.prologue() + fill("setup_box.sh.in", BENCH_IMAGE=c["BENCH_IMAGE"]))[0]
        if rc == ship_mod.NO_BUILD_RC:
            raise ship_mod.no_build_error(c, "setup")
        self.ok(rc)
        self.step("setup: packages")
        # Shared run lock, as builds take it: a tests run (exclusive) may
        # restart the box (box_check.sh.in), which would kill this.
        self.ok(setup_locked(r.in_box_locked(r.ship("setup_pkgs.sh"), what="setup")[0]))
        # Proton runs from the host, not the box: the host ships umu-run.
        self.ok(r.remote(r.ship("check_umu.sh", prologue=False))[0])
        self.step("setup: llvm-mingw %s -> %s" % (c["LLVM_MINGW_TAG"], c["LLVM_MINGW_ROOT"]))
        return self.ok(
            setup_locked(r.in_box_locked(r.ship("setup_mingw.sh", tag), what="setup")[0])
        )

    # build
    def cmd_build(self, args):
        self.need_host()
        if self.cfg.local_build:
            exe, sha, _ = ship_mod.build_local(self, args)
            ship_mod.ship(self, exe, sha)
            return 0
        self.step("build: %s/build-win" % self.cfg.remote_game)
        # No arguments: an empty array (printf '%q ' with no arguments still
        # prints '', which made every build re-run cmake with an empty one).
        extra = "EXTRA_ARGS=(%s)\n" % quote_words(args)
        rc = self.r.in_box_locked(self.r.ship("build.sh", extra))[0]
        if rc == 75:
            raise BenchError("build: the run lock was held for an hour; nothing built")
        if rc == ship_mod.NO_BUILD_RC:
            raise ship_mod.no_build_error(self.cfg, "build")
        if rc:
            raise BenchError("build failed (exit %d)" % rc)
        return 0

    # run
    def run_game(
        self, game_args, bench_env=None, timeout=None, frames=None, lock_held=None, defaults=False
    ):
        """Launch the game on the host and pull its logs; returns the stamp.
        defaults: start from the game's launch.env.default (run, pacing,
        all); golden never does, so its verdicts stay comparable."""
        self.need_host()
        c = self.cfg
        expect = ""
        if c.local_build:
            # The exe this tree built, in its own run directory (sent once
            # per command; nothing moves when the host has it already).
            # run_game.sh checks it again under the run lock.
            expect = "EXPECT_SHA=%s\n" % ship_mod.ship(self)
        if bench_env is None:
            bench_env = c["BENCH_ENV"]
        if defaults:
            bench_env = self.apply_defaults(bench_env)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if c.windows:
            self.step("run: natively on %s -> bench-logs/%s" % (c.host, stamp))
            self.host.run_game(
                stamp,
                game_args,
                bench_env,
                c.get("BENCH_TIMEOUT") if timeout is None else timeout,
                (c.get("BENCH_FRAMES") or "0") if frames is None else frames,
                (c.get("BENCH_LOCK_HELD") or "0") if lock_held is None else lock_held,
                c.get("BENCH_KILL_GAME") or "0",
                expect[len("EXPECT_SHA=") :].strip(),
            )
            self.cmd_logs([stamp])
            self.record_run(stamp)
            self.say("local: %s" % os.path.join(c.game_dir, "bench-logs", stamp))
            return stamp
        self.step("run: under %s -> bench-logs/%s" % (c["PROTONPATH"], stamp))
        assigns = (
            "STAMP=%s\nPROTONPATH=%s\nGAME_ARGS=(%s)\nGAME_ENV=(%s)\nTIMEOUT=%s\nFRAMES=%s\n"
            "KILL_GAME=%s\nLOCK_HELD=%s\nPROTON_LOG_MODE=%s\nPROTON_LOG_CAP=%d\n"
            % (
                shell_quote(stamp),
                shell_quote(c["PROTONPATH"]),
                quote_words(game_args),
                # Raw, as bench.sh sent it: the host word-splits BENCH_ENV.
                bench_env,
                shell_quote(c.get("BENCH_TIMEOUT") if timeout is None else timeout),
                shell_quote((c.get("BENCH_FRAMES") or "0") if frames is None else frames),
                shell_quote(c.get("BENCH_KILL_GAME") or "0"),
                shell_quote((c.get("BENCH_LOCK_HELD") or "0") if lock_held is None else lock_held),
                proton_log_mode(c),
                PROTON_LOG_CAP,
            )
            + expect
        )
        rc = self.r.remote(self.r.ship("run_game.sh", assigns))[0]
        if rc == ship_mod.EXE_CHANGED_RC:
            raise BenchError(
                "run: the exe in %s is no longer the one this command shipped "
                "(another ship to the same run directory?); nothing ran" % c.remote_game
            )
        self.cmd_logs([stamp])
        self.record_run(stamp)
        self.say("local: %s" % os.path.join(c.game_dir, "bench-logs", stamp))
        return stamp

    def pull_inbox(self):
        """The host's inbox records into the db first (benchdb pull, D7.6):
        nothing waits when the host or the db is not there."""
        from ..benchdb import pull

        pull.pull_quietly(self.cfg.game_dir, self.cfg.host, say=lambda m: self.say("benchdb " + m))

    def record_run(self, stamp, **fields):
        """The pulled run in the db (benchdb, D7): host from BENCH_HOST,
        never the uname. Warn-only; returns the run id or None."""
        from ..benchdb import live

        d = os.path.join(self.cfg.game_dir, "bench-logs", stamp)
        if not os.path.isdir(d):
            return None
        return live.run(d, self.cfg.game.slug, host_alias=self.cfg.host, **fields)

    def apply_defaults(self, text):
        """text with the game's launch.env.default under it, and a warning
        (once per command, on stderr) when neither sets RECOMP_PB_BACKEND:
        the exe then falls back to the CPU rasteriser, which is slow enough
        to look like a hang."""
        path, kvs = launch_defaults(self.cfg.game)
        out = with_defaults(kvs, text)
        if not getattr(self, "_defaults_said", False):
            self._defaults_said = True
            rel = os.path.relpath(path, self.cfg.game.root)
            if kvs is None:
                print("bench: warning: no %s; the game gets BENCH_ENV only" % rel, file=sys.stderr)
            if not any(k == "RECOMP_PB_BACKEND" and v for k, v in env_pairs(out)):
                print(
                    "bench: warning: RECOMP_PB_BACKEND is not set (%s, BENCH_ENV); the game "
                    "falls back to the CPU rasteriser" % rel,
                    file=sys.stderr,
                )
        return out

    def cmd_run(self, args):
        stamp = self.run_game(args, defaults=True)
        log = os.path.join(self.cfg.game_dir, "bench-logs", stamp)
        rc = 0
        with self.no_errexit():
            tag = self.cfg.game.m["bench"]["crash_tag"]
            if checks.check_run_end(log, out=self.say, crash_tag=tag) != 0:
                rc = 1
            else:
                checks.report_flips(log, out=self.say)
            if (
                checks.check_present_mismatch(os.path.join(log, "game-stdio.log"), out=self.say)
                != 0
            ):
                rc = 1
        return rc

    # tests
    def tests_key(self):
        """(key or None, parts): what the toolkit's Proton tests depend on,
        for skipping them when it has not changed (tests_skip.py)."""
        from ..cli_dir import cli_dir

        c = self.cfg
        env_header = c.game.m["game"]["env_header"]
        assigns = "PROTONPATH=%s\nENV_DIR=%s\n" % (
            shell_quote(c["PROTONPATH"]),
            shell_quote(posixpath.dirname(env_header) or env_header),
        )
        rc, out = self.r.remote(self.r.ship("tests_key.sh", assigns), capture=True)
        parts = tests_skip.parse_host_parts(out) if rc == 0 else {}
        parts["cli"] = tests_skip.cli_part(cli_dir())
        parts["script"] = hashlib.sha256(self.tests_script().encode()).hexdigest()
        parts["llvm_mingw"] = c["LLVM_MINGW_TAG"]
        return tests_skip.key_of(parts), parts

    def tests_script(self):
        """tests.sh as cmd_tests ships it, with the configured PROTONPATH (as
        run and golden send it): without it proton_run.sh takes the newest
        GE-Proton, not the Proton tests_key.sh hashed."""
        return self.r.ship("tests.sh", "PROTONPATH=%s\n" % shell_quote(self.cfg["PROTONPATH"]))

    def tests_record(self):
        return tests_skip.record_path(self.cfg.game_dir, self.cfg.host, self.cfg.remote_game)

    def tests_or_skip(self, force=False):
        """golden's tests: skipped when the key matches the last pass on
        this host tree (and not force), and when the tree has no configured
        build-win/. Returns (rc, 'pass'|'skip'|'nobuild'|'FAIL'|'off')."""
        if not self.cfg.game.m["bench"]["toolkit_tests"]:
            self.say("tests: skipped (game.toml: bench.toolkit_tests = false)")
            return 0, "off"
        if self.cfg.local_build:
            # The ctests compile on the host, and a BENCH_BUILD=local host
            # only runs what this machine built.
            if force:
                self.say("tests: FAIL --tests: %s" % LOCAL_NO_TESTS)
                return 1, "FAIL"
            self.say("tests: skipped: %s" % LOCAL_NO_TESTS)
            return 0, "nobuild"
        key, parts = self.tests_key()
        if parts.get("build_win") == "none" and not force:
            # The ctests compile in the host's build-win/, and only bench
            # build (or integrate) makes one: sync never ships build trees,
            # and a Mac's could not be reused (its cache names Mac paths). A
            # tree synced for goldens alone, its exe copied in, has nothing
            # to test, which is no failure of the scenarios.
            self.say(
                "tests: skipped: %s has no configured build-win/ (%s bench build "
                "makes one; --tests fails instead)" % (self.cfg.remote_game, self.cfg.game.slug)
            )
            return 0, "nobuild"
        if not force:
            why = tests_skip.skip_reason(key, tests_skip.read_record(self.tests_record()))
            if why:
                self.say("tests: skipped: %s (--tests runs them)" % why)
                return 0, "skip"
        rc = self.cmd_tests([], key=(key, parts))
        return rc, "pass" if rc == 0 else "FAIL"

    def cmd_tests(self, args, key=None):
        self.need_host()
        if not self.cfg.game.m["bench"]["toolkit_tests"]:
            self.say("tests: skipped (game.toml: bench.toolkit_tests = false)")
            return 0
        if self.cfg.local_build:
            raise BenchError("tests: %s" % LOCAL_NO_TESTS)
        key, parts = key or self.tests_key()
        self.step(
            "tests: d3d8_hlsl_split, d3d11_backend_smoke, input_map, input_keyboard, nv2a_zbuf, apu_irq, "
            "kernel_irql_abi, fp_precision, x87_trig, vblank_ack, vblank_schedule, spin_wait, rt_alias, irq_safe_points, "
            "kernel_missing_report, kernel_file_status, kernel_guest_cpu, dpc_order, kernel_events, "
            "kernel_regressions, memory_regressions, guest_cpu, flip_hold, apu_voice_mask, apu_voice_lock, enhance_cfg, template_main (enhance off and on), fb_window_wanted (when the toolkit has them) "
            "under Proton"
        )
        rc = self.r.in_box_locked(self.tests_script(), exclusive=True, what="tests")[0]
        if rc == 0:
            self.say("tests: pass")
            if key:
                tests_skip.write_record(
                    self.tests_record(), key, parts, self.cfg.host, self.cfg.remote_game
                )
                from ..benchdb import live

                live.tests_pass(self.tests_record(), cwd=self.cfg.game_dir)
        elif rc == 75:
            self.say("tests: FAIL the run lock was held for an hour; nothing run")
        elif rc == ship_mod.NO_BUILD_RC:
            self.say("tests: FAIL %s" % ship_mod.no_build_error(self.cfg, "tests"))
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
        if self.cfg.windows:
            # sftp pulls named files: one run's, not the whole tree.
            if not args:
                raise BenchError("logs: name the run's stamp on a Windows host (logs STAMP)")
            return self.ok(self.host.logs(args[:1]))
        with self.no_errexit():
            self.cmd_symbolize(args[:1])
        dst = os.path.join(self.cfg.game_dir, "bench-logs")
        os.makedirs(dst, exist_ok=True)
        src = "%s:%s/bench-logs/" % (self.cfg.host, self.cfg.remote_game)
        # Frame dumps stay on the host (golden pulls the ones it needs). Pulls
        # keep -a's times: logs and frames are not build input.
        if proton_log_mode(self.cfg) == "full":
            return self.ok(
                rsync("-az", "--exclude", "/*/frames/", "--exclude", "/*/hdd/", src, dst + "/")
            )
        # Proton's logs apart, and none larger than a capped one: a run from
        # before the cap (or with --proton-log) left gigabytes on the host,
        # which a pull of the whole tree would copy again.
        self.ok(
            rsync(
                "-az",
                "--exclude",
                "/*/frames/",
                "--exclude",
                "/*/hdd/",
                "--exclude",
                "/*/steam-*.log",
                src,
                dst + "/",
            )
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
        if c.windows:
            return self.host.shell()
        return self.r.command(
            "cd %s 2>/dev/null || cd %s; exec $SHELL -l" % (c.remote_game, c["BENCH_DIR"]),
            batch=False,
            tty=True,
        )[0]

    # integrate
    def cmd_integrate(self, args):
        self.need_host()
        golden = dirty = stale_ok = False
        # Options golden takes, passed on with --golden.
        golden_args = []
        it = iter(args)
        for a in it:
            if a == "--golden":
                golden = True
            elif a == "--dirty":
                dirty = True
            elif a == "--stale-gen-ok":
                stale_ok = True
            elif a == "--tests" or a.startswith("--only="):
                golden_args.append(a)
            elif a == "--only":
                golden_args += [a, next(it, "")]
            else:
                raise BenchError("integrate: unknown option %s" % a)
        if golden_args and not golden:
            raise BenchError("integrate: %s needs --golden" % golden_args[0])
        only = golden_parse_args(golden_args)[3]
        if only is not None:
            # A misspelt scenario stops here, not after the sync and build.
            rc, plan = golden_py(self, "plan", capture=True)
            if rc != 0:
                raise BenchError("integrate: no golden plan")
            select_rows(plan, only)
        c = self.cfg
        self.pull_inbox()

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
        if c.local_build:
            return self.integrate_local(golden, golden_args)
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
        return self.integrate_golden(golden, golden_args)

    def integrate_local(self, golden, golden_args):
        """integrate with BENCH_BUILD=local: build here (gen/'s digest the
        same before and after), ship the exe, and check the host has the
        very bytes built."""
        c = self.cfg
        # The stale check above already ran (or --stale-gen-ok skipped it).
        exe, sha, gen = ship_mod.build_local(
            self, stale_ok=True, command="bench integrate", lineage="integration"
        )
        self.say("gen: %d files, sha256 %s (unchanged during the build)" % (gen[1], gen[0]))
        ship_mod.ship(self, exe, sha)
        self.step(
            "integrate: shipped %s/%s/%s.exe"
            % (c.remote_game, ship_mod.exe_rel(c), c.game.m["build"]["exe"])
        )
        if c.windows:
            rc, out = self.host.exe_sha()
        else:
            rc, out = self.r.remote(self.r.ship("integrate_show.sh"), capture=True)
        self.say(out, end="")
        self.ok(rc)
        host_sha = (out.split() or [""])[0]
        if host_sha != sha:
            raise BenchError(
                "integrate: the host's exe is %s, not the one built here (%s)" % (host_sha, sha)
            )
        rc = self.integrate_golden(golden, golden_args)
        if golden:
            # D13: the shipped build against the previous release, after the
            # golden's own verdicts; a flag is printed, golden's rc stands.
            from ..benchdb import live

            rec = live.record_for_exe(exe, sha)
            live.regress_lines(c.game.root, c.game.slug, rec and rec["uuid"], say=self.say)
        return rc

    def integrate_golden(self, golden, golden_args):
        if not golden:
            return 0
        # Integration runs see no host pad, keyboard or mouse and fail on a
        # bad script, as golden.json's env pins for golden.
        env = self.cfg["BENCH_ENV"]
        env = (
            env + " " if env else ""
        ) + "RECOMP_HOST_PAD=0 RECOMP_KEYBOARD=0 RECOMP_MOUSE=0 RECOMP_INPUT_STRICT=1"
        with self.no_errexit():
            return cmd_golden(self, golden_args, bench_env=env, kind="integrate")

    # doctor
    def cmd_doctor(self, args):
        """What the bench needs, checked without changing anything."""
        c, ok = self.cfg, True
        if not c.host:
            self.say("host:     BENCH_HOST is not set (scripts/bench.env)")
            return 1
        self.say("host:     %s (BENCH_DIR %s, box %s)" % (c.host, c["BENCH_DIR"], c["BENCH_BOX"]))
        if c.local_build:
            self.say(
                "build:    local (built here, shipped to %s/runs/<id>; run dir %s)"
                % (c.base, c.remote_game if c.remote_game != c.base else "none yet")
            )
        else:
            self.say("build:    host (BENCH_BUILD=host: the host compiles a synced tree)")
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


LOCAL_NO_TESTS = (
    "BENCH_BUILD=local: the toolkit's Proton ctests compile on the host; "
    "run them on a host that builds (BENCH_BUILD=host)"
)


def setup_locked(rc):
    if rc == 75:
        raise BenchError("setup: the run lock was held for an hour; the box is not set up")
    return rc


def gen_stale_reasons(cfg):
    """pipeline.gen_stale_reasons() for the tree and toolkit the bench
    drives (BENCH_GAME_DIR, XBOXRECOMP_DIR), which need not be the CLI's
    current ones."""
    from .. import pipeline

    with ship_mod.driving(cfg):
        return pipeline.gen_stale_reasons(extra=pipeline._load_json(cfg.game.stage_extras, {}))


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


# Commands that ship the local exe themselves (or never touch a run dir).
SHIPPING = ("setup", "sync", "build", "all", "integrate")


def dispatch(b, cmd, args):
    if b.cfg.windows:
        why = refusal(cmd)
        if why:
            raise BenchError(why)
        if cmd == "sync" and args:
            raise BenchError(
                "sync: %s not on a Windows host (BENCH_HOST_OS=windows): copy the game "
                "files to BENCH_GAME_FILES (%s) by hand" % (args[0], b.cfg["BENCH_GAME_FILES"])
            )
    if cmd not in SHIPPING:
        # logs, symbolize, shell, doctor, gc: this tree's run directory.
        ship_mod.resolve(b.cfg)
    if cmd == "sync":
        return cmd_sync(b, args)
    if cmd == "golden":
        return cmd_golden(b, args)
    if cmd == "gc":
        return cmd_gc(b, args)
    if cmd == "pacing":
        return cmd_pacing(b, args)
    if cmd == "all":
        if not b.cfg.local_build:
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
    if cmd == "db":
        # `<game> bench db VERB`: benchdb with --game set; no bench config
        # or host is needed to read or write the db.
        from ..benchdb import cli as benchdb_cli

        return benchdb_cli.main(["--game", game.root] + argv, prog="%s bench db" % game.slug)
    if cmd not in COMMANDS and cmd not in ("sync", "golden", "pacing", "all", "gc"):
        sys.stdout.write(render_help(game))
        return 1
    # A verb's -h/--help is the bench's usage, before any config or host is
    # touched: run once passed --help on as a game argument, and the host ran
    # the game with no BENCH_TIMEOUT until it was killed. gc's argparse has
    # its own usage.
    if "-h" in argv or "--help" in argv:
        if cmd == "gc":
            try:
                gc_parse_args(["--help"], game)
            except SystemExit as e:
                return e.code or 0
        sys.stdout.write(render_help(game))
        return 0
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
