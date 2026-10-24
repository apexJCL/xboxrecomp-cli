"""golden run: the golden scenarios on this machine's own build (macOS:
Metal, or the CPU path), headless and silent, one at a time under the Mac
run lock, each stopped once the last frame its check reads is dumped.

  <game> golden run [SCEN...] [--backend metal|cpu] [--secs S] [--exe PATH]
                    [--out DIR] [--keep-frames] [--lock-wait S] [VAR=val...]

SCEN: golden.json's scenarios (default: all, in its order). Per scenario:

  env    the process environment without inherited RECOMP_* variables,
         then the scenario's (golden plan), RECOMP_PB_BACKEND=BACKEND,
         RECOMP_HEADLESS=1, SDL_AUDIO_DRIVER=dummy (and SDL2's
         SDL_AUDIODRIVER=dummy), a save dir of its own
         (the run's save/; with RECOMP_HDD_DIR=@seed:PATH, a copy of the
         hdd tree PATH, relative to the game root, in the run's hdd/
         instead, its UDATA the save), frame dumps by flip (fb_dump into the run's
         frames/, fb_dump_at = golden dumpat), then the VAR=val arguments
         (RECOMP_TRACE and RECOMP_DEBUG add up, comma-joined)
  lock   ~/.recomp-mac-run.lock, as the workspace's mac-run-lock.sh takes
         it (a directory with pid and cmd; a dead holder's lock is taken
         over; waits --lock-wait seconds, default 3600, then exit 75). A
         lock held by one of this process's ancestors (the script that
         wraps this command) counts as held
  load   a 1-minute load average above 0.75 x the CPUs, or a game process
         already running, prints a WARNING and goes into run-info.txt: the
         pace check turns a slowed run into INCOMPLETE
  stop   each frame needs flip 60*dump+1, and an anchored frame, once its
         anchor event is in the log, A + 60*dump+1 - R for each reference
         (R its anchor flip), never past the end of its dumped range; until
         the anchor shows, that end. +-2 flips around each (check's
         window). The game gets SIGINT 3 flips after the last need, at
         --secs (default 2x the scenario's seconds), or ends by itself
  check  golden check of the run, as bench golden: the output and a last
         "verdict: SCEN WORD" line in the run's golden.txt; a passing run's
         unread flip dumps pruned unless --keep-frames

Run dirs: DIR/<stamp>-<backend>-<scen>/ (DIR: --out, default the game's
bench-logs/) with run-info.txt, game-stdio.log, frames/, save/ (or hdd/,
removed after the run unless --keep-frames), exit-code and golden.txt.
The game runs from the game's root (its game files there).
Exit 1 on a FAIL, a crash or an early end, else 2 on an INCOMPLETE, else 0;
75 when the lock was not free in time.

Stdlib only."""

import contextlib
import hashlib
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LOCK = os.path.join(os.path.expanduser("~"), ".recomp-mac-run.lock")
WINDOW = 2  # check's default --window
STOP_AFTER = 3  # flips past the last need before the stop
POLL = 0.5
BACKENDS = ("metal", "cpu")


class RunError(Exception):
    def __init__(self, msg, rc=1):
        Exception.__init__(self, msg)
        self.rc = rc


# ---- what a scenario needs ---------------------------------------------------


def frame_needs(sc, batches, window=WINDOW, slack=None, backend=None):
    """[(frame name, last flip it needs, settled)] for scenario sc given the
    run's flip log so far ({flip: batches}), for the frames a run on backend
    checks. settled: the need cannot move any more (an unanchored frame, or
    an anchored one whose anchor is in the log). Before its anchor shows, an
    anchored frame needs the end of its dumped range: past that nothing more
    of it is dumped."""
    from . import golden as G

    _, after = G.dump_slack(sc, slack)
    out = []
    for fr in G.backend_frames(sc, backend):
        f = 60 * fr["dump"] + 1
        w = max(window, G.verdict_window(fr))
        if "anchor" not in fr:
            out.append((fr["name"], f + w, True))
            continue
        end = f + w + after
        ev = sc.get("anchors", {}).get(fr["anchor"]["event"])
        A = G.find_anchor(batches, ev, sc.get("anchors")) if ev and batches else None
        if A is None:
            out.append((fr["name"], end, False))
            continue
        # Each reference's own anchor flip, else the frame's (ref_anchor_flip).
        refs = [r.get("anchor_flip", fr["anchor"]["ref_flip"]) for r in fr.get("references", [])]
        need = max(A + f - R for R in refs or [fr["anchor"]["ref_flip"]])
        out.append((fr["name"], min(need + w, end), True))
    return out


def stop_flip(sc, batches, window=WINDOW, slack=None, backend=None):
    """The flip after which the run can stop: STOP_AFTER past the last need
    of the frames backend checks (STOP_AFTER when it skips them all)."""
    needs = frame_needs(sc, batches, window, slack, backend)
    return max((n for _, n, _ in needs), default=0) + STOP_AFTER


class FlipLog:
    """The run's log, read as it grows: {flip: batches} as golden's
    flip_batches has it (the backend's line wins over the walker's) and the
    highest flip any flip line names."""

    LINE = re.compile(r"^\[(D3D11|METAL|GPU)\] flip (\d+)\b(?:.*?\bbatches (\d+))?")

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.rest = b""
        self.batches = {}
        self.last = 0

    def feed(self, data):
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        for raw in lines:
            m = self.LINE.match(raw.decode("utf-8", "replace"))
            if not m:
                continue
            flip = int(m.group(2))
            self.last = max(self.last, flip)
            if m.group(3) is None:
                continue
            b = int(m.group(3))
            if m.group(1) == "GPU":
                self.batches.setdefault(flip, b)
            else:
                self.batches[flip] = b

    def poll(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return
        self.pos += len(data)
        self.feed(data)


# ---- the Mac run lock ----------------------------------------------------------


def ancestors(pid=None):
    """This process's ancestors' pids (ps; empty when ps fails)."""
    out, pid = [], os.getpid() if pid is None else pid
    for _ in range(64):
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        try:
            pid = int(r.stdout.strip())
        except ValueError:
            break
        if pid <= 1:
            break
        out.append(pid)
    return out


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_holder(lock):
    try:
        with open(os.path.join(lock, "pid")) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


@contextlib.contextmanager
def mac_lock(cmd, wait=3600, lock=None, say=print, poll=5, parents=None):
    """mac-run-lock.sh's lock for the block. Yields the seconds waited."""
    lock = lock or LOCK
    waited, ours = 0, False
    parents = ancestors() if parents is None else parents
    while True:
        try:
            os.mkdir(lock)
            ours = True
            break
        except FileExistsError:
            pass
        holder = read_holder(lock)
        if holder and holder in parents:
            say("golden run: the Mac run lock is held by PID %d, which runs this command" % holder)
            break
        if holder and not alive(holder):
            say("golden run: removing the stale Mac run lock of PID %d" % holder)
            shutil.rmtree(lock, ignore_errors=True)
            continue
        try:
            with open(os.path.join(lock, "cmd")) as f:
                what = f.read().strip()
        except OSError:
            what = ""
        if waited >= wait:
            raise RunError(
                "the Mac run lock was held for %d s, by PID %s: %s" % (waited, holder or "?", what),
                75,
            )
        if waited == 0:
            say(
                "golden run: waiting for the Mac run lock, held by PID %s: %s"
                % (holder or "?", what)
            )
        time.sleep(poll)
        waited += poll
    if ours:
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write("%d\n" % os.getpid())
        with open(os.path.join(lock, "cmd"), "w") as f:
            f.write(cmd[:200] + "\n")
    try:
        yield waited
    finally:
        if ours and read_holder(lock) == os.getpid():
            shutil.rmtree(lock, ignore_errors=True)


# ---- one run -----------------------------------------------------------------------


def add_list(env, k, v):
    env[k] = env[k] + "," + v if env.get(k) else v


def run_env(base, scen_env, backend, run_dir, dumpat, extra):
    """The game's environment for one run (see the module doc)."""
    env = {k: v for k, v in base.items() if not k.startswith("RECOMP_")}
    env.update(scen_env)
    env.update(
        RECOMP_PB_BACKEND=backend,
        RECOMP_HEADLESS="1",
        # SDL3 reads SDL_AUDIO_DRIVER, SDL2 SDL_AUDIODRIVER. Both, until no
        # game pins an SDL2 toolkit: a golden run on one setting only the new
        # name would play sound on the Mac.
        SDL_AUDIO_DRIVER="dummy",
        SDL_AUDIODRIVER="dummy",
        RECOMP_SAVE_DIR=os.path.join(run_dir, "save"),
    )
    add_list(
        env, "RECOMP_DEBUG", "fb_dump=%s/,fb_dump_at=%s" % (os.path.join(run_dir, "frames"), dumpat)
    )
    for k, v in extra:
        if k in ("RECOMP_TRACE", "RECOMP_DEBUG"):
            add_list(env, k, v)
        else:
            env[k] = v
    return env


def seed_hdd(env, root, run_dir, game_files=None):
    """RECOMP_HDD_DIR=@seed:PATH: the hdd tree at PATH (under root, checked
    by golden's seed_dir) copied to the run's hdd/ (APFS clones on macOS),
    so every run boots on the same disk and save and none writes to the
    seed. RECOMP_SAVE_DIR points at the copy too: any save dir set turns off
    the runtime's fallback to the game dir's UDATA, and a save dir of the
    run's own would boot on an empty save. With no seed and no
    RECOMP_HDD_DIR, the run gets an empty hdd/ of its own: the runtime's
    default hdd root is the player's own disk, partition images and all.
    Returns the run's hdd dir (removed after the run), or None when the env
    names a plain RECOMP_HDD_DIR of its own."""
    from .golden import SeedError, hdd_seed, seed_dir

    seed = hdd_seed(env)
    dst = os.path.join(run_dir, "hdd")
    if seed is None:
        if env.get("RECOMP_HDD_DIR"):
            return None
        os.makedirs(dst)
        env["RECOMP_HDD_DIR"] = dst
        env["RECOMP_SAVE_DIR"] = dst
        return dst
    try:
        src = seed_dir(seed, root, game_files)
    except SeedError as e:
        raise RunError("golden run: %s" % e) from None
    if sys.platform == "darwin":
        r = subprocess.run(["cp", "-c", "-R", src + "/", dst])
        if r.returncode:
            raise RunError("golden run: copying the hdd seed %s failed" % src)
    else:
        shutil.copytree(src, dst, symlinks=True)
    # A fixture is kept read-only (chmod -R a-w); the game writes its save
    # and partition images in the copy, and the copy is removed after.
    for d, _, files in os.walk(dst):
        for n in [d] + [os.path.join(d, f) for f in files]:
            os.chmod(n, os.stat(n).st_mode | stat.S_IWUSR)
    env["RECOMP_HDD_DIR"] = dst
    env["RECOMP_SAVE_DIR"] = dst
    return dst


def host_load():
    """(1-minute load, CPUs), or (None, CPUs) where there is no load average."""
    try:
        return os.getloadavg()[0], os.cpu_count() or 1
    except (OSError, AttributeError):
        return None, os.cpu_count() or 1


def loaded(load, cpus):
    return load is not None and load > 0.75 * cpus


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(d):
    r = subprocess.run(
        ["git", "-C", d, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True
    )
    if r.returncode:
        return "not a git checkout"
    s = subprocess.run(["git", "-C", d, "status", "--porcelain"], capture_output=True, text=True)
    n = s.stdout.count("\n")
    return r.stdout.strip() + (" (%d paths dirty)" % n if n else " clean")


def run_one(o, scen, sc, scen_env, secs, say):
    """One scenario's run; returns (run dir, exit code, why it stopped,
    wall seconds to the stop, last flip)."""
    from . import golden as G

    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(o["out"], "%s-%s-%s" % (stamp, o["backend"], scen))
    os.makedirs(os.path.join(d, "frames"))
    dumpat = G.dumpat_ranges(sc, backend=o["backend"])
    env = run_env(os.environ, scen_env, o["backend"], d, dumpat, o["extra"])
    hdd = seed_hdd(env, o["root"], d, o.get("game_files"))
    if hdd is None:
        os.makedirs(os.path.join(d, "save"))
    try:
        return (d,) + run_game(o, sc, d, env, secs, say)
    finally:
        # The copy holds the save and the runtime's partition images: gone
        # after the run, a failed or interrupted one too, unless kept.
        if hdd and not o["keep"]:
            shutil.rmtree(hdd, ignore_errors=True)


def run_game(o, sc, d, env, secs, say):
    """The game's run in d; returns (exit code, why it stopped, wall
    seconds, last flip)."""
    load, cpus = host_load()
    warn = []
    if loaded(load, cpus):
        warn.append("host load %.1f on %d CPUs" % (load, cpus))
    games = foreign_games(o["exe"])
    if games:
        warn.append("a game process is running (pid %d)" % games[0][0])
    for w in warn:
        say("golden run: WARNING %s: this run's pace may be off (INCOMPLETE pace mismatch)" % w)
    logp = os.path.join(d, "game-stdio.log")
    info = [
        "host:   %s  %s" % (platform.node(), time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        "backend: %s" % o["backend"],
        "exe:    %s  %s" % (sha256_file(o["exe"]), o["exe"]),
        "tree:   %s %s" % (o["root"], git_state(o["root"])),
        "env:    %s"
        % " ".join("%s=%s" % (k, v) for k, v in sorted(env.items()) if k.startswith("RECOMP_")),
        "limit:  %d s (SIGINT); stop at the last frame's flip" % secs,
        "lock:   %s, waited %ds" % (LOCK, o["waited"]),
        "load:   %s on %d CPUs at start%s"
        % (
            "%.1f" % load if load is not None else "?",
            cpus,
            "; WARNING " + "; ".join(warn) if warn else "",
        ),
    ]
    flog = FlipLog(logp)
    t0 = time.time()
    why, max_load = "limit", load
    with open(logp, "wb") as log:
        p = subprocess.Popen(
            [o["exe"]],
            cwd=o["root"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        try:
            while time.time() - t0 < secs:
                if p.poll() is not None:
                    why = "exited"
                    break
                time.sleep(POLL)
                flog.poll()
                if flog.last and flog.last >= stop_flip(sc, flog.batches, backend=o["backend"]):
                    why = "done"
                    break
                ld = host_load()[0]
                if ld is not None and (max_load is None or ld > max_load):
                    max_load = ld
        finally:
            wall = time.time() - t0
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    flog.poll()
    info.append(
        "stop:   %s at flip %d after %.1f s (max load %s)"
        % (why, flog.last, wall, "%.1f" % max_load if max_load is not None else "?")
    )
    if loaded(max_load, cpus) and not loaded(load, cpus):
        say(
            "golden run: WARNING the host load rose to %.1f on %d CPUs during the run: its "
            "pace may be off" % (max_load, cpus)
        )
    with open(os.path.join(d, "run-info.txt"), "w") as f:
        f.write("\n".join(info) + "\n")
    with open(os.path.join(d, "exit-code"), "w") as f:
        f.write("%s\n" % p.returncode)
    return p.returncode, why, wall, flog.last


def foreign_games(exe):
    try:
        from . import running_game as rg

        name = os.path.basename(exe)
        return rg.find_games(rg.list_processes(name), os.getpid(), name)
    except Exception:  # a listing failure never stops a run
        return []


def end_check(d, why, rc, crash_tag):
    """(rc, lines): 1 on a crash or an end before the last needed flip."""
    try:
        with open(os.path.join(d, "game-stdio.log"), errors="replace") as f:
            crash = next((x.rstrip("\n") for x in f if x.startswith(crash_tag)), "")
    except OSError:
        crash = ""
    if crash:
        return 1, ["end: FAIL crashed: %s" % crash]
    if why == "exited":
        return 1, ["end: FAIL the game exited (%s) before the last frame was dumped" % rc]
    if why == "limit":
        return 0, ["end: stopped at the --secs limit before the last frame's flip"]
    return 0, ["end: stopped after the last frame's flip"]


# ---- the command ---------------------------------------------------------------------


def parse(args):
    o = {
        "scens": [],
        "backend": "metal",
        "secs": None,
        "exe": None,
        "out": None,
        "keep": False,
        "lock_wait": 3600,
        "extra": [],
    }
    it = iter(args)
    for a in it:
        if a in ("--backend", "--secs", "--exe", "--out", "--lock-wait"):
            v = next(it, None)
            if v is None:
                raise RunError("golden run: %s takes a value" % a)
            if a == "--backend":
                if v not in BACKENDS:
                    raise RunError("golden run: --backend is one of %s" % ", ".join(BACKENDS))
                o["backend"] = v
            elif a in ("--secs", "--lock-wait"):
                try:
                    o["secs" if a == "--secs" else "lock_wait"] = int(v)
                except ValueError:
                    raise RunError("golden run: %s takes seconds, not %r" % (a, v)) from None
            else:
                o[a[2:]] = os.path.abspath(v)
        elif a == "--keep-frames":
            o["keep"] = True
        elif a.startswith("-"):
            raise RunError("golden run: unknown option %s" % a)
        elif "=" in a:
            k, v = a.split("=", 1)
            o["extra"].append((k, v))
        else:
            o["scens"].append(a)
    return o


def cmd_run(args):
    from . import golden as G
    from . import manifest

    try:
        o = parse(args)
    except RunError as e:
        print(e, file=sys.stderr)
        return e.rc
    if sys.platform != "darwin":
        print("golden run: macOS only (the Proton bench is `bench golden`)", file=sys.stderr)
        return 1
    game = manifest.load(G.REPO)
    o["root"] = game.root
    o["game_files"] = game.game_files
    o["exe"] = o["exe"] or game.exe(game.build_dir("macos"), "macos")
    o["out"] = o["out"] or os.path.join(game.root, "bench-logs")
    if not os.path.isfile(o["exe"]):
        print(
            "golden run: no %s (build it: %s build macos, or --exe PATH)" % (o["exe"], game.slug),
            file=sys.stderr,
        )
        return 1
    if not os.path.isdir(game.game_files):
        print(
            "golden run: no %s: the game runs from %s and reads its game files there"
            % (game.rel(game.game_files) + "/", game.root),
            file=sys.stderr,
        )
        return 1
    g = G.load_golden()
    names = list(g["scenarios"])
    bad = [s for s in o["scens"] if s not in names]
    if bad:
        print(
            "golden run: no scenario %s (golden.json has %s)" % (", ".join(bad), ", ".join(names)),
            file=sys.stderr,
        )
        return 1
    scens = [s for s in names if s in o["scens"]] if o["scens"] else names
    if G.refs_missing():
        print("golden run: reference PNGs missing (see above)", file=sys.stderr)
        return 1
    if G.seeds_missing(g, game.game_files):
        return 1
    os.makedirs(o["out"], exist_ok=True)

    def say(msg):
        print(msg)
        sys.stdout.flush()

    words, rcs, total = [], [], 0.0
    try:
        with mac_lock("golden run " + " ".join(scens), o["lock_wait"], say=say) as waited:
            o["waited"] = waited
            for scen in scens:
                sc = g["scenarios"][scen]
                secs = o["secs"] or 2 * sc["seconds"]
                say("\n== golden run: %s (%s, stop by %d s) ==" % (scen, o["backend"], secs))
                d, rc, why, wall, last = run_one(o, scen, sc, G.scenario_env(g, scen), secs, say)
                total += wall
                say(
                    "run: %s at flip %d after %.1f s (%.1f flips/s), exit %s -> %s"
                    % (why, last, wall, last / wall if wall else 0, rc, d)
                )
                erc, lines = end_check(d, why, rc, game.m["bench"]["crash_tag"])
                for x in lines:
                    say(x)
                word, crc = check_and_prune(d, scen, erc, lines, o["keep"])
                words.append("%s %s" % (scen, word))
                rcs.append(1 if erc else crc)
    except RunError as e:
        print(e, file=sys.stderr)
        return e.rc
    say("\ngolden run: verdicts: %s (%.0f s of runs)" % (", ".join(words), total))
    return 1 if 1 in rcs else 2 if any(rcs) else 0


def check_and_prune(d, scen, erc, lines, keep):
    """golden check of one run (a subprocess, as bench golden runs it):
    printed, and written with the run's end lines and a verdict line to
    golden.txt. A clean pass has its unread flip dumps pruned. Returns
    (verdict word, check's exit code)."""
    from . import golden as G
    from .bench.golden import verdict_word

    frames = os.path.join(d, "frames")
    fd, used = tempfile.mkstemp(prefix="golden-used-")
    os.close(fd)
    try:
        argv = [sys.executable, "-m", "xboxrecomp_cli.golden"] + G.config_argv()
        argv += [
            "check",
            "--used",
            used,
            "--log",
            "%s=%s" % (scen, os.path.join(d, "game-stdio.log")),
        ]
        r = subprocess.run(argv + ["%s=%s" % (scen, frames)], stdout=subprocess.PIPE)
        text = r.stdout.decode(errors="replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        res = G.read_used(used) if os.path.getsize(used) else {}
    finally:
        os.remove(used)
    word = verdict_word(erc, 0, r.returncode, text)
    with open(os.path.join(d, "golden.txt"), "w") as f:
        f.write("".join(x + "\n" for x in lines) + text + "verdict: %s %s\n" % (scen, word))
    if word == "pass" and not keep:
        rc, paths = res.get(scen, (None, []))
        if rc == 0:
            n, size, kept = G.prune_frames(frames, paths)
            print(
                "golden run: %s: pruned %d flip dumps (%.1f MB), kept %d"
                % (scen, n, size / 1e6, kept)
            )
    return word, r.returncode


# ---- golden play: a person plays a scenario's boot, the pads recorded ----------------

PLAY_SECS = 1800
# The toolkit's pin a golden env sets that would keep a person from playing;
# a game's own input pins come from game.toml's [input] play_unset.
PLAY_UNPIN = ("RECOMP_HEADLESS",)


def play_env(
    base, scen_env, backend, run_dir, record, script=True, extra=(), unset=(), script_env=None
):
    """The game's environment for golden play: the scenario's, in a window
    with the host's sound (a person plays it), the keyboard on as pad 1
    beside any host pad, no frame dumps, the flip trace on, and
    pad_record=RECORD. unset: the game's input pins to drop as well
    (game.toml [input] play_unset). script False drops every script the
    scenario names, the pad_script in RECOMP_DEBUG, RECOMP_PAD_SCRIPT and
    the game's own input script (script_env): the person starts at the
    title."""
    from .pad_script import script_of

    env = {k: v for k, v in base.items() if not k.startswith("RECOMP_")}
    # A golden run's dummy audio, if the caller's shell carries it, is
    # theirs to keep; play adds none.
    env.update(scen_env)
    # A golden env pins the person's input off (no host pad, no keyboard)
    # so a stray key cannot change a run: play is the run where it counts.
    for k in PLAY_UNPIN + tuple(unset):
        env.pop(k, None)
    env.update(RECOMP_PB_BACKEND=backend, RECOMP_KEYBOARD="1")
    env["RECOMP_SAVE_DIR"] = os.path.join(run_dir, "save")
    debug = env.get("RECOMP_DEBUG", "")
    if not script:
        old = script_of(debug)
        if old is not None:
            debug = debug.replace("pad_script=" + old, "").replace(",,", ",").strip(",")
        env.pop("RECOMP_PAD_SCRIPT", None)
        if script_env:
            env.pop(script_env, None)
    env["RECOMP_DEBUG"] = debug
    add_list(env, "RECOMP_DEBUG", "pad_record=%s" % record)
    # The flips the run-info's stop line and the recording are matched on.
    if "flip" not in env.get("RECOMP_TRACE", "").split(","):
        add_list(env, "RECOMP_TRACE", "flip")
    for k, v in extra:
        if k in ("RECOMP_TRACE", "RECOMP_DEBUG"):
            add_list(env, k, v)
        else:
            env[k] = v
    if not env["RECOMP_DEBUG"]:
        env.pop("RECOMP_DEBUG")
    return env


def parse_play(args):
    o = {
        "scen": None,
        "record": None,
        "backend": "metal",
        "secs": PLAY_SECS,
        "exe": None,
        "out": None,
        "script": True,
        "lock_wait": 60,
        "extra": [],
    }
    it = iter(args)
    for a in it:
        if a in ("--record", "--secs", "--exe", "--out", "--lock-wait", "--backend"):
            v = next(it, None)
            if v is None:
                raise RunError("golden play: %s takes a value" % a)
            if a in ("--secs", "--lock-wait"):
                try:
                    o["secs" if a == "--secs" else "lock_wait"] = int(v)
                except ValueError:
                    raise RunError("golden play: %s takes seconds, not %r" % (a, v)) from None
            elif a == "--backend":
                if v not in BACKENDS:
                    raise RunError("golden play: --backend is one of %s" % ", ".join(BACKENDS))
                o["backend"] = v
            else:
                o[a[2:]] = os.path.abspath(v)
                # RECOMP_DEBUG is a comma list: pad_record= would stop at
                # the comma and write some other file than the one checked.
                if a == "--record" and "," in o["record"]:
                    raise RunError("golden play: the --record path cannot hold a ','")
        elif a == "--no-script":
            o["script"] = False
        elif a.startswith("-"):
            raise RunError("golden play: unknown option %s" % a)
        elif "=" in a:
            k, v = a.split("=", 1)
            o["extra"].append((k, v))
        elif o["scen"] is None:
            o["scen"] = a
        else:
            raise RunError("golden play: one scenario, not %s too" % a)
    if not o["scen"] or not o["record"]:
        raise RunError("golden play: usage: golden play SCEN --record FILE [options]", 2)
    return o


def cmd_play(args):
    """golden play SCEN --record FILE: SCEN's boot in a window, with sound,
    its hdd seed copied (never anyone's own saves), for a person to play
    while the pads are recorded to FILE as a flip-timed pad_script (golden
    pad turns it into a scenario's). Starts where the scenario's own
    script leaves the game, or at the title with --no-script. Ends when the
    game is closed, on Ctrl-C, SIGTERM or SIGHUP, or at --secs (default
    1800); the game is stopped with it, never left running. Plays the
    Mac's sound (it is a person's run): an agent runs it headless with
    SDL_AUDIODRIVER=dummy and SDL_AUDIO_DRIVER=dummy in its env. Takes the Mac
    run lock (waits --lock-wait, default 60 s), so no golden run overlaps
    it. The run dir (--out, default bench-logs/) keeps run-info.txt and
    game-stdio.log, the flip log the recording is matched against."""
    from . import golden as G
    from . import manifest

    try:
        o = parse_play(args)
    except RunError as e:
        print(e, file=sys.stderr)
        return e.rc
    if sys.platform != "darwin":
        print("golden play: macOS only", file=sys.stderr)
        return 1
    game = manifest.load(G.REPO)
    exe = o["exe"] or game.exe(game.build_dir("macos"), "macos")
    out = o["out"] or os.path.join(game.root, "bench-logs")
    if not os.path.isfile(exe):
        print("golden play: no %s (build it, or --exe PATH)" % exe, file=sys.stderr)
        return 1
    g = G.load_golden()
    if o["scen"] not in g["scenarios"]:
        print(
            "golden play: no scenario %s (golden.json has %s)"
            % (o["scen"], ", ".join(g["scenarios"])),
            file=sys.stderr,
        )
        return 1
    if G.seeds_missing(dict(g, scenarios={o["scen"]: g["scenarios"][o["scen"]]}), game.game_files):
        return 1
    if os.path.exists(o["record"]):
        print("golden play: %s exists; record to a new file" % o["record"], file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(o["record"]), exist_ok=True)
    d = os.path.join(out, "%s-play-%s" % (time.strftime("%Y%m%d-%H%M%S"), o["scen"]))
    os.makedirs(d)
    env = play_env(
        os.environ,
        G.scenario_env(g, o["scen"]),
        o["backend"],
        d,
        o["record"],
        o["script"],
        o["extra"],
        game.m["input"]["play_unset"],
        game.m["input"]["script_env"],
    )
    try:
        hdd = seed_hdd(env, game.root, d, game.game_files)
    except RunError as e:
        print(e, file=sys.stderr)
        return e.rc
    if hdd is None:
        os.makedirs(os.path.join(d, "save"))
    logp = os.path.join(d, "game-stdio.log")
    seed = G.hdd_seed(G.scenario_env(g, o["scen"]))
    info = [
        "host:   %s  %s" % (platform.node(), time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        "play:   %s (%s), %s"
        % (o["scen"], o["backend"], "from its pad_script" if o["script"] else "from the title"),
        "exe:    %s  %s" % (sha256_file(exe), exe),
        "tree:   %s %s" % (game.root, git_state(game.root)),
        "seed:   %s (copied to %s)" % (seed, hdd)
        if seed
        else "seed:   none (an empty %s)" % hdd
        if hdd
        else "seed:   none (RECOMP_HDD_DIR=%s)" % env.get("RECOMP_HDD_DIR"),
        "record: %s" % o["record"],
        "env:    %s"
        % " ".join("%s=%s" % (k, v) for k, v in sorted(env.items()) if k.startswith("RECOMP_")),
    ]
    rc, why = None, "closed"

    def ended(signum, frame):
        raise KeyboardInterrupt

    # SIGTERM or SIGHUP (a closed terminal) would skip the cleanup below and
    # leave the game running on a deleted hdd copy: end as Ctrl-C does.
    old = {n: signal.signal(n, ended) for n in (signal.SIGTERM, signal.SIGHUP)}
    try:
        with mac_lock("golden play " + o["scen"], o["lock_wait"]):
            print("golden play: %s -> %s (the run: %s)" % (o["scen"], o["record"], d))
            sys.stdout.flush()
            t0 = time.time()
            with open(logp, "wb") as log:
                p = subprocess.Popen(
                    [exe],
                    cwd=game.root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                try:
                    while p.poll() is None:
                        if time.time() - t0 > o["secs"]:
                            why = "limit"
                            break
                        time.sleep(POLL)
                except KeyboardInterrupt:
                    why = "ctrl-c"
                finally:
                    # A second Ctrl-C (or a signal) while the game winds
                    # down must not skip the kill: ignore them meanwhile.
                    held = {
                        n: signal.signal(n, signal.SIG_IGN)
                        for n in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
                    }
                    try:
                        if p.poll() is None:
                            p.send_signal(signal.SIGINT)
                            try:
                                p.wait(15)
                            except subprocess.TimeoutExpired:
                                p.kill()
                                p.wait()
                    finally:
                        for n, h in held.items():
                            signal.signal(n, h)
                    rc = p.returncode
            wall = time.time() - t0
    except RunError as e:
        print(e, file=sys.stderr)
        return e.rc
    finally:
        for n, h in old.items():
            signal.signal(n, h)
        # The copy holds the save the person made and the partition images:
        # never kept, however the play ends.
        if hdd:
            shutil.rmtree(hdd, ignore_errors=True)
    flog = FlipLog(logp)
    flog.poll()
    info.append("stop:   %s at flip %d after %.1f s, exit %s" % (why, flog.last, wall, rc))
    with open(os.path.join(d, "run-info.txt"), "w") as f:
        f.write("\n".join(info) + "\n")
    try:
        with open(o["record"]) as f:
            n = sum(1 for x in f if x.strip() and not x.startswith("#"))
    except OSError:
        n = -1
    print(
        "golden play: %s at flip %d after %.0f s; %s"
        % (
            why,
            flog.last,
            wall,
            "%d steps in %s" % (n, o["record"])
            if n >= 0
            else "no recording written (%s)" % o["record"],
        )
    )
    return 0 if n >= 0 else 1
