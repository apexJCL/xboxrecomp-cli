"""hud-map capture and check: the runs (design A15, steps 1 and 5).

Each run is the game's own build, headless and silent, under the Mac run
lock (golden_run's), from the game's root, with a config file of its own
(RECOMP_CONFIG): the player's config never reaches a mapping run, and the
run's [display] aspect and [mods] switch are set there (the mods switch
has no environment variable).

  capture  aspect 4:3; RECOMP_DEBUG gains hud_capture=<dir>,
           hud_capture_at=<from>:<count> (guest frames) and
           hud_capture_table=<the game's [hud] table>. Level 1 runs the
           game's HUD mod ([hud] mod) so its hook records at hud_apply;
           level 0 switches it off, so the capture loads the table file
           and records at its [capture] draws. Stops once capture.jsonl
           holds the last frame, at --secs, or when the game ends.
  check    per ratio, the mod on with RECOMP_TRACE=hud,aspect, then off,
           each with frame dumps (fb_dump_at, host flips); then the sheet.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from ..host import CliError
from . import table as T

POLL = 0.5
DEFAULT_FRAMES = "600:120"
DEFAULT_SECS = 120


def parse_frames(s):
    m = re.match(r"^(\d+):(\d+)$", s or "")
    # The toolkit's hud_capture_at: frames counted from 1, at most 10000.
    if not m or int(m.group(1)) < 1 or not 1 <= int(m.group(2)) <= 10000:
        raise CliError("--frames %r is not FROM:COUNT (FROM >= 1, COUNT 1..10000)" % s)
    return int(m.group(1)), int(m.group(2))


def scenario(G, name):
    """(env, seconds) for a scenario: a golden.json scenario by name, or an
    input preset (@name) through [input] script_env."""
    if name.startswith("@"):
        var = G.m.get("input", {}).get("script_env") or ""
        if not var:
            raise CliError("%s: game.toml's [input] names no script_env for presets" % name)
        return {var: name}, DEFAULT_SECS
    gj = G.m.get("golden", {}).get("json")
    g = None
    if gj:
        try:
            with open(G.path(gj), encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, ValueError):
            g = None
    if g and name in g.get("scenarios", {}):
        from ..golden import scenario_env

        return scenario_env(g, name), int(g["scenarios"][name].get("seconds") or DEFAULT_SECS)
    names = sorted((g or {}).get("scenarios", {}))
    presets = G.m.get("input", {}).get("presets") or []
    raise CliError(
        "no scenario %s: golden.json has %s; presets: %s"
        % (name, ", ".join(names) or "none", ", ".join(presets) or "none")
    )


def run_config(path, aspect, mod, on):
    """The run's config file: its aspect and its mod switch."""
    lines = ["[display]", 'aspect = "%s"' % aspect]
    if mod:
        lines += ["", "[mods]", "%s = %s" % (T._key(mod), "true" if on else "false")]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def base_env(environ, scen_env, run_dir, config):
    """The process environment without inherited RECOMP_ variables, then
    the scenario's, headless and silent, a config and a save of its own."""
    env = {k: v for k, v in environ.items() if not k.startswith("RECOMP_")}
    env.update(scen_env)
    env.update(
        RECOMP_HEADLESS="1",
        SDL_AUDIO_DRIVER="dummy",
        SDL_AUDIODRIVER="dummy",
        RECOMP_CONFIG=config,
    )
    return env


def _no_comma(what, path):
    if "," in path:
        raise CliError("%s %s holds a comma, which RECOMP_DEBUG splits on" % (what, path))
    return path


def capture_env(env, out, frames, table):
    from ..golden_run import add_list

    keys = "hud_capture=%s,hud_capture_at=%d:%d" % (_no_comma("--out", out), frames[0], frames[1])
    if table:
        keys += ",hud_capture_table=%s" % _no_comma("the table", table)
    add_list(env, "RECOMP_DEBUG", keys)
    return env


def check_env(env, frames_dir, dumpat):
    from ..golden_run import add_list

    add_list(env, "RECOMP_TRACE", "hud,aspect")
    add_list(
        env, "RECOMP_DEBUG", "fb_dump=%s/,fb_dump_at=%s" % (_no_comma("--out", frames_dir), dumpat)
    )
    return env


_FRAME_TYPE_RE = re.compile(r'"type"\s*:\s*"frame"')
_FRAME_NO_RE = re.compile(r'"frame"\s*:\s*(\d+)')
DONE_RE = re.compile(r"^\[hud\.capture\] done:", re.M)


def captured_up_to(path):
    """The highest frame whose frame line capture.jsonl holds (0 when
    none): the capture writes a frame's sprites first and its frame line
    last, so that frame is complete."""
    last = 0
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    for line in tail.splitlines():
        m = _FRAME_TYPE_RE.search(line) and _FRAME_NO_RE.search(line)
        if m and line.endswith("}"):
            last = max(last, int(m.group(1)))
    return last


def capture_done(logp):
    """Whether the run's log has the capture's done line (it ends the
    file after the last frame)."""
    try:
        with open(logp, "rb") as f:
            f.seek(max(0, os.path.getsize(logp) - 65536))
            return bool(DONE_RE.search(f.read().decode("utf-8", "replace")))
    except OSError:
        return False


def play(
    exe, env, cwd, logp, secs, done, popen=subprocess.Popen, clock=time.time, sleep=time.sleep
):
    """The game until done() says so, it ends, or secs pass. Returns (exit
    code, why: done, exited or limit)."""
    t0 = clock()
    why = "limit"
    with open(logp, "wb") as log:
        p = popen(
            [exe], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
        try:
            while clock() - t0 < secs:
                if p.poll() is not None:
                    why = "exited"
                    break
                sleep(POLL)
                if done():
                    why = "done"
                    break
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    return p.returncode, why


@contextlib.contextmanager
def locked(cmd, wait, say):
    from ..golden_run import RunError, mac_lock

    try:
        with mac_lock(cmd, wait=wait, say=say) as waited:
            yield waited
    except RunError as e:
        raise CliError(str(e)) from None


@contextlib.contextmanager
def own_hdd(G, env, run_dir):
    """golden run's hdd for the run (a seed's copy, or an empty one), gone
    after it: never the player's own disk."""
    from ..golden_run import RunError, seed_hdd

    hdd = os.path.join(run_dir, "hdd")
    shutil.rmtree(hdd, ignore_errors=True)
    try:
        made = seed_hdd(env, G.root, run_dir, G.game_files)
    except RunError as e:
        raise CliError(str(e)) from None
    try:
        yield made
    finally:
        if made:
            shutil.rmtree(made, ignore_errors=True)


def game_exe(G, exe):
    if exe:
        path = os.path.abspath(exe)
    elif sys.platform == "darwin":
        path = G.exe(G.build_dir("macos"), "macos")
    else:
        raise CliError("hud-map runs the macOS build here; on another host pass --exe PATH")
    if not os.path.isfile(path):
        raise CliError("no %s (build it: %s build macos, or --exe PATH)" % (path, G.slug))
    return path


def table_path(G):
    return G.path(G.m.get("hud", {}).get("table") or "config/hud.toml")


CAPTURE_TABLE = "capture-table.toml"
_FRAME_BMP = re.compile(r"frame_[0-9]{5}\.bmp")


def _sig_of(d):
    """The sig: name a draw entry asks for, else None."""
    if isinstance(d, str) and d.startswith("sig:"):
        return d
    if isinstance(d, dict):
        v = d.get("fn", d.get("va"))
        if isinstance(v, str) and v.startswith("sig:"):
            return v
        if v is None and isinstance(d.get("sig"), str):
            return "sig:" + d["sig"]
    return None


def capture_table(G, table, out):
    """The table the run loads: the game's own, or when a [capture] draws
    entry is a sig: name, a copy in the capture dir with its VA from
    analyze's hook_functions.json (the toolkit skips a sig: draw: hook
    slots carry no names and guest code is not mapped at its init)."""
    from .. import hooks
    from . import emit as E

    doc = E.parse(E.read_text(table), G.rel(table))
    cap = doc.get("capture")
    raw = cap.get("draws") if isinstance(cap, dict) else None
    raw = raw if isinstance(raw, list) else [raw] if raw is not None else []
    if not any(_sig_of(d) for d in raw):
        return table
    vas = {}
    for s in hooks.load_json(G):
        try:
            vas[s["entry"]] = int(s["va"], 16)
        except (KeyError, TypeError, ValueError):
            continue
    draws = []
    for d in raw:
        sig = _sig_of(d)
        if sig is None:
            draws.append(d)
            continue
        if sig not in vas:
            raise CliError(
                "[capture] draws %s: no VA in %s (list it in [mods] hook_functions, run analyze)"
                % (sig, G.rel(G.hook_functions))
            )
        if isinstance(d, dict):
            d = {k: v for k, v in d.items() if k not in ("fn", "va", "sig")}
            draws.append({"fn": vas[sig], **d})
        else:
            draws.append(vas[sig])
    doc["capture"]["draws"] = draws
    path = os.path.join(out, CAPTURE_TABLE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            T.dumps(doc, head=["hud-map capture's copy of %s: sig: draws resolved" % G.rel(table)])
        )
    return path


def do_capture(G, a, say=print, popen=subprocess.Popen):
    frames = parse_frames(a.frames)
    scen_env, secs = scenario(G, a.scenario)
    secs = a.secs or 2 * secs
    out = os.path.abspath(a.out or os.path.join(G.dist, "hud-map", a.scenario.lstrip("@")))
    mod = G.m.get("hud", {}).get("mod") or ""
    if a.level == 1 and not mod:
        raise CliError("--level 1 records through the game's HUD mod: name it in [hud] mod")
    table = table_path(G)
    if not os.path.isfile(table):
        if a.level == 0:
            raise CliError("--level 0 reads [capture] draws from %s: there is none" % G.rel(table))
        table = None
    exe = game_exe(G, a.exe)
    os.makedirs(out, exist_ok=True)
    jsonl = os.path.join(out, "capture.jsonl")
    # An earlier run's frames in the same dir would pass for this run's.
    for name in os.listdir(out):
        # Only what a capture writes: a --frames dump or other BMPs kept
        # in the same directory are not ours to delete.
        if name in ("capture.jsonl", CAPTURE_TABLE) or _FRAME_BMP.fullmatch(name):
            os.remove(os.path.join(out, name))
    if table:
        table = capture_table(G, table, out)
    cfg = run_config(os.path.join(out, "run-config.toml"), "4:3", mod, a.level == 1)
    env = capture_env(base_env(os.environ, scen_env, out, cfg), out, frames, table)
    logp = os.path.join(out, "game-stdio.log")
    last = frames[0] + frames[1] - 1
    with locked("%s hud-map capture %s" % (G.slug, a.scenario), a.lock_wait, say):
        with own_hdd(G, env, out):
            rc, why = play(
                exe,
                env,
                G.root,
                logp,
                secs,
                lambda: capture_done(logp) or captured_up_to(jsonl) >= last,
                popen=popen,
            )
    got = captured_up_to(jsonl)
    say("hud-map capture: %s, %s (exit %s); frames up to %d of %d" % (out, why, rc, got, last))
    if not os.path.exists(jsonl):
        raise CliError("no capture.jsonl in %s: see game-stdio.log" % out)
    return 0 if why == "done" else 1


# ── check ────────────────────────────────────────────────────────────────


def dumpat_for(secs, every=120, first=300, most=12):
    """Host flips to dump: every `every` from `first`, at most `most`,
    within the run."""
    flips = list(range(first, max(first + 1, secs * 60 - 60), every))[:most]
    return ",".join(str(f) for f in flips) or str(first)


def last_flip(dumpat):
    nums = [int(x) for x in re.findall(r"\d+", dumpat)]
    return max(nums) if nums else 0


def do_check(G, a, say=print, popen=subprocess.Popen):
    from ..golden_run import FlipLog
    from . import sheet
    from .review import ratio

    hud = G.m.get("hud", {})
    mod = hud.get("mod") or ""
    if not mod:
        raise CliError("check switches the game's HUD mod on and off: name it in [hud] mod")
    scens = a.scenario or hud.get("scenarios") or []
    if not scens:
        raise CliError("no scenario: pass one, or list them in [hud] scenarios")
    ratios = [r.strip() for r in a.ratios.split(",") if r.strip()]
    for r in ratios:
        ratio(r)
    exe = game_exe(G, a.exe)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.abspath(a.out or os.path.join(G.dist, "hud-map", "check", stamp))
    known = {}
    tp = table_path(G)
    if os.path.isfile(tp):
        import tomllib

        with open(tp, "rb") as f:
            known = tomllib.load(f)
    bad = 0
    sheets = []
    with locked("%s hud-map check" % G.slug, a.lock_wait, say):
        for scen in scens:
            scen_env, secs = scenario(G, scen)
            dumpat = a.at or dumpat_for(secs)
            stop = last_flip(dumpat) + 3
            runs = {}
            for r in ratios:
                for on in (True, False):
                    d = os.path.join(
                        out, scen.lstrip("@"), r.replace(":", "x"), "on" if on else "off"
                    )
                    os.makedirs(os.path.join(d, "frames"), exist_ok=True)
                    cfg = run_config(os.path.join(d, "run-config.toml"), r, mod, on)
                    env = check_env(
                        base_env(os.environ, scen_env, d, cfg), os.path.join(d, "frames"), dumpat
                    )
                    logp = os.path.join(d, "game-stdio.log")
                    flog = FlipLog(logp)

                    def done(flog=flog, stop=stop):
                        flog.poll()
                        return flog.last >= stop

                    with own_hdd(G, env, d):
                        rc, why = play(
                            exe, env, G.root, logp, a.secs or 2 * secs, done, popen=popen
                        )
                    say(
                        "hud-map check: %s %s mod %s: %s (exit %s)"
                        % (scen, r, "on" if on else "off", why, rc)
                    )
                    runs[(r, on)] = (d, rc, why)
            for r in ratios:
                path, failed = sheet.write(
                    os.path.join(out, scen.lstrip("@")),
                    scen,
                    r,
                    runs[(r, True)],
                    runs[(r, False)],
                    known,
                )
                sheets.append(path)
                bad += failed
    for p in sheets:
        say("hud-map check: sheet %s" % p)
    return 1 if bad else 0
