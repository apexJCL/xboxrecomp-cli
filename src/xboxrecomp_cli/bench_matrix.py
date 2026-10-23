"""A benchmark matrix: one scene, every combination of a few settings, on
several hosts, measured the same way everywhere.

  <game> bench-matrix plan SCENE [--host H] [--json]
  <game> bench-matrix run SCENE --host H --arm ID --out DIR --local LOCAL
                          [--no-prof] [--rep N] [--benches FILE]
  <game> bench-matrix series SCENE --host H[,H] --root ROOT --local LOCAL
                          [--arms A,B] [--reps N] [--first-rep N]
                          [--prof on|off|both] [--order interleave|blocks]
                          [--benches FILE] [--dry-run]
  <game> bench-matrix collect RUN_DIR [--scene SCENE] [--exe EXE]
  <game> bench-matrix analyse ROOT [--out DIR] [--include-disturbed]
                          [--recollect [--scene SCENE] [--exe EXE]]
  <game> bench-matrix compare BEFORE AFTER [--out DIR] [--include-disturbed]
  <game> bench-matrix windows SCENE --local LOCAL --out DIR [--host H]
                          [--series [series options]]

SCENE is the game's TOML (its config, never this CLI's): how long to run,
how the scene's start is found from the flip trace, the axes and how each
maps onto config.toml or the environment, and which hosts run which arms.
LOCAL is the machine's own TOML (paths, ssh names, locks); it is not
tracked. An arm is one value per axis, named by the scene's arm_id.

run takes the host's lock, then waits (30 min at most) for the host to be
quiet, starts the game from a fresh copy of the hdd seed with the arm's config,
and collects the run into DIR/run.json:
  - host "local": this machine (the Mac); flock (as lockf takes it) on the
    lock file and quiet_locks, then run_wrapper, a command prefix holding
    a lock of its own (a run queue), all before the quiet checks; the game
    runs in its own session, killed whole at the cap.
  - host "proton": a SteamOS box over ssh, the Windows exe under umu-run
    and Proton, flock on the lock file, the GPU clock and busy sampled
    every 250 ms from sysfs, the CPU cores' clocks and the game's threads
    (load, last core) every second from /proc.
  - host "windows" is not run from here: windows writes run-arm.ps1 and the
    arms (and run-series.ps1 with --series), for whoever drives that PC;
    collect reads what it brings back.
Every run opens borderless desktop fullscreen (present.fullscreen): an OS
throttles the presents of a covered or unfocused window. Each runner also
watches the run: another process busy (the game is skipped by id, never by
name, so a second copy of it counts) and, on the PC, another window taking the foreground mark it
disturbed.

The local host (the Mac) pulls and analyses every host's data, so it runs
last: plan and series put the remote hosts first (the Deck and the PC are
separate machines and may overlap). A local run holds its locks, then waits
out builds and other benches inside them; run-info.json lists the locks
it held. A quiet wait that gave up marks the run disturbed.

series runs many arms in order into ROOT/<host>/<run>/, each named
<arm>[-noprof][-rN], skipping those collected (run.json), and collects
each. A run folder without run.json (a run that failed) is moved aside to
<run>.failed-<time> and run again; a failed run does not stop the series,
which then exits 1.
--order interleave (the default) runs each arm's sampled and unsampled runs
back to back, alternating which goes first; blocks runs every sampled run,
then every unsampled one.

The frame series is the "[PACING] flip N t_us T batches B" trace
(RECOMP_TRACE=pacing=all), every backend. The hot paths come from the
runtime's own sampler (RECOMP_TRACE=prof), since it is the one profiler
that works alike on macOS, Wine and native Windows; collect symbolises its
module offsets with the exe's symbol table (nm / llvm-nm).

analyse reads ROOT/<host>/<run>/run.json and writes summary.json,
summary.csv (one row per run) and summary-arms.csv (one row per arm: the
median, min, max and MAD over its repeats, and how many runs the scene's
[verdict] calls good or bad). A run's repeat is run --rep, else a -rN
suffix on its directory. Runs another process disturbed (busy_during) stay
in the rows but out of the per-arm figures unless --include-disturbed.
--recollect runs collect again on each run first (newer columns for old
runs). A run whose directory is not its arm plus -noprof and -rN is
listed as misnamed.

compare puts two results (a baseline and its rerun) side by side per
(host, arm, sampler rate): the run counts, good and bad, and the medians of
steady fps, 1% low, p99, the process's cores, the spin share, the busiest
core and the load gap, before, after and the change. The cores' change
is blank when the two sides measured them differently, and an arm
windowed on one side and fullscreen on the other is flagged.

Frame statistics: frame times run between the window's own flips (the
load before the scene is load_gap_ms, not a frame); percentiles are
nearest rank; a hitch is a frame over 1.5x the window's median, a stutter
one over 2.5x; slow_share is the share of frames slower than the scene's
slow_fps (50). Each is given for the whole window, the warm-up and the
steady part, and the steady part leads the CSV.
"""

import argparse
import bisect
import csv
import datetime
import fcntl
import glob
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import threading
import time
import tomllib

from . import bench_hw
from .host import CliError

PACING_FLIP_RE = re.compile(r"^\[PACING\] flip (\d+) t_us (\d+) batches (\d+)")

# A process above this share of one core makes the host busy for a
# measured run; the game itself and the tools of the run are not counted.
BUSY_PCT = 50.0
QUIET_WAIT_S = 30 * 60
# A local run's cap past the scene's run_s: the game's own quit comes first;
# the cap ends a hung one. The lock and quiet waits come before it.
LOCAL_CAP_EXTRA_S = 120
GPU_PERIOD_MS = 250


class MatrixError(Exception):
    pass


# --- the scene ---------------------------------------------------------------


def load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MatrixError("%s: %s" % (path, e)) from e


def load_scene(path):
    s = load_toml(path)
    for k in ("scene", "axis", "arm_id", "hosts"):
        if k not in s:
            raise MatrixError("%s: no %s" % (path, k))
    sc = s["scene"]
    for k in ("name", "run_s", "duration_s"):
        if k not in sc:
            raise MatrixError("%s: [scene] has no %s" % (path, k))
    for a in s["axis"]:
        if "name" not in a or not a.get("values"):
            raise MatrixError("%s: an [[axis]] needs name and values" % path)
        if ("config" in a) == ("env" in a):
            raise MatrixError("%s: axis %s needs one of config or env" % (path, a["name"]))
        if "labels" in a and len(a["labels"]) != len(a["values"]):
            raise MatrixError("%s: axis %s: labels and values differ in length" % (path, a["name"]))
        if "display" in a and len(a["display"]) != len(a["values"]):
            raise MatrixError(
                "%s: axis %s: display and values differ in length" % (path, a["name"])
            )
    if "report" in s:
        # bench_report imports this module: imported here, not at the top.
        from .bench_report import check_report

        check_report(s, path)
    return s


def label(axis, value):
    if "labels" in axis:
        return str(axis["labels"][axis["values"].index(value)])
    return str(value)


def excluded(host, values):
    """A host's exclude entries are "axis=value" strings."""
    for e in host.get("exclude", []):
        k, _, v = e.partition("=")
        if k in values and str(values[k]) == v:
            return True
    return False


def arms(scene, host_name=None):
    """The arms in leg order (the first axis outermost), each
    {"id", "values", "config", "env"}; a host's excluded arms are left out."""
    axes = scene["axis"]
    host = scene["hosts"].get(host_name, {}) if host_name else {}
    if host_name and host_name not in scene["hosts"]:
        raise MatrixError("no host %s in the scene (%s)" % (host_name, ", ".join(scene["hosts"])))
    out = []
    for combo in itertools.product(*(a["values"] for a in axes)):
        values = {a["name"]: v for a, v in zip(axes, combo)}
        if excluded(host, values):
            continue
        cfg, env = {}, {}
        for a, v in zip(axes, combo):
            if "env" in a:
                env[a["env"]] = str(v)
            elif v != a.get("stock"):
                sect, _, key = a["config"].rpartition(".")
                cfg.setdefault(sect, {})[key] = v
        aid = scene["arm_id"].format(**{a["name"]: label(a, v) for a, v in zip(axes, combo)})
        out.append({"id": aid, "values": values, "config": cfg, "env": env})
    return out


def find_arm(scene, host_name, arm_id):
    for a in arms(scene, host_name):
        if a["id"] == arm_id:
            return a
    raise MatrixError("no arm %s for host %s" % (arm_id, host_name))


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v))


def config_toml(cfg):
    """config.toml text for an arm: the sections in order, stock keys
    absent."""
    lines = ["# bench-matrix arm config"]
    for sect in sorted(cfg):
        lines.append("[%s]" % sect)
        for k in sorted(cfg[sect]):
            lines.append("%s = %s" % (k, toml_value(cfg[sect][k])))
    return "\n".join(lines) + "\n"


def run_env(scene, arm, prof=True, prof_out=None, host=None):
    """The environment every host gets for an arm: the scene's, the trace
    keys, the arm's, the quit timer. A host's own [profile] keys win over
    the scene's (under Wine the sampler stops every thread each round, so
    a Proton host samples at a lower rate)."""
    env = {str(k): str(v) for k, v in scene.get("env", {}).items()}
    trace = ["pacing=all"]
    p = dict(scene.get("profile", {}), **((host or {}).get("profile") or {}))
    if prof and p.get("hz"):
        trace.append("prof=%d" % p["hz"])
        trace.append("prof_delay=%g" % p.get("delay_s", 0))
        if prof_out:
            trace.append("prof_out=" + prof_out)
    extra = env.pop("RECOMP_TRACE", "")
    env["RECOMP_TRACE"] = ",".join([t for t in extra.split(",") if t] + trace)
    env.update(arm["env"])
    # Borderless fullscreen over the desktop (present.fullscreen; no mode
    # change): a window another one covers, or one not in front, gets its
    # presents throttled by the OS (Windows to about one a second). An arm
    # or the scene's env may still set it; scene.fullscreen = false keeps
    # the window.
    if scene["scene"].get("fullscreen", True):
        env.setdefault("RECOMP_PRESENT_FULLSCREEN", "1")
    env["RECOMP_WINDOW_QUIT_AFTER"] = str(scene["scene"]["run_s"])
    return env


# --- the frame series --------------------------------------------------------


def parse_flips(text):
    """[(flip, t_us, batches)] from a log."""
    out = []
    for line in text.splitlines():
        m = PACING_FLIP_RE.match(line)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def find_start(flips, start):
    """(index of the scene's first flip, how it was found). The first flip
    with min_batches or more whose next hold_flips flips have a median of
    min_batches or more (a lone busy flip in a movie is not the scene);
    without one, the first flip at fallback_s after the first flip."""
    mb = start.get("min_batches")
    hold = max(1, int(start.get("hold_flips", 1)))
    if mb is not None:
        for i, f in enumerate(flips):
            if f[2] >= mb and statistics.median(x[2] for x in flips[i : i + hold]) >= mb:
                return i, "batches"
    if "fallback_s" in start and flips:
        t = flips[0][1] + start["fallback_s"] * 1e6
        for i, f in enumerate(flips):
            if f[1] >= t:
                return i, "fallback"
    raise MatrixError("the scene's start is not in the log")


def pct(sorted_vals, p):
    """The p-th percentile (nearest rank: the ceil(p/100 * n)-th smallest),
    from sorted values."""
    if not sorted_vals:
        return None
    k = max(1, min(len(sorted_vals), math.ceil(p / 100.0 * len(sorted_vals))))
    return sorted_vals[k - 1]


# A hitch is a frame over 1.5x the window's median frame time (at a 60 Hz
# vsync a missed vblank, 33.3 ms, is one for sure); a stutter one over 2.5x.
# 2x sat exactly on a missed vblank, so noise decided whether it counted.
HITCH_X = 1.5
STUTTER_X = 2.5


def part_stats(ft, med, span_s, nflips, slow_ms):
    """Frame-time statistics of one part of the window (all, warm-up or
    steady): ft its frame times, med the whole window's median (so every
    part counts hitches against one line)."""
    if not ft:
        return None
    srt = sorted(ft)
    n1 = max(1, len(srt) // 100)
    return {
        "fps": round(nflips / span_s, 3) if span_s > 0 else None,
        "frames": len(ft),
        "mean_ms": round(statistics.fmean(ft), 3),
        "p50_ms": round(pct(srt, 50), 3),
        "p99_ms": round(pct(srt, 99), 3),
        "max_ms": round(srt[-1], 3),
        "low1_fps": round(1000.0 / statistics.fmean(srt[-n1:]), 3),
        "hitches": sum(1 for x in ft if x > HITCH_X * med),
        "stutters": sum(1 for x in ft if x > STUTTER_X * med),
        "slow_share": round(sum(1 for x in ft if x > slow_ms) / len(ft), 4),
    }


def worst_second(fps_per_s, warmup_s):
    """The lowest whole 1-second bucket of the steady part (bench-report
    D2): bucket k covers [k, k+1) s, so the first steady one starts at or
    after the warm-up."""
    steady = (fps_per_s or [])[math.ceil(warmup_s or 0) :]
    return min(steady) if steady else None


def frame_stats(times_us, t0, duration_s, warmup_s, slow_fps=50):
    """Statistics of the flips in [t0, t0 + duration). Frame times run
    between the window's own flips: the interval into its first flip is
    the load before the scene (1-2 s into a title screen), reported apart
    as load_gap_ms, since as a frame it would be the window's max and most
    of its 1% low. slow_share is the share of frames slower than
    slow_fps."""
    end = t0 + duration_s * 1e6
    win = [t for t in times_us if t0 <= t < end]
    if len(win) < 2:
        raise MatrixError("fewer than 2 flips in the window")
    i0 = times_us.index(win[0])
    gap = (win[0] - times_us[i0 - 1]) / 1000.0 if i0 > 0 else None
    ft = [(b - a) / 1000.0 for a, b in zip(win, win[1:])]
    covered = (win[-1] - t0) / 1e6
    complete = covered >= duration_s - 1.0
    span = duration_s if complete else covered
    med = statistics.median(ft)
    slow_ms = 1000.0 / slow_fps
    # Whole seconds only: a last bucket the flips do not reach to its end
    # (a run that stopped early) would read as a slow second.
    nb = min(int(span), int(covered + 1.5 * med / 1000.0))
    per_s = [0] * nb
    for t in win:
        k = int((t - t0) // 1e6)
        if k < nb:
            per_s[k] += 1
    tw = t0 + warmup_s * 1e6
    # A frame belongs to the part its ending flip falls in.
    ends = win[1:]
    warm_ft = [x for b, x in zip(ends, ft) if b < tw]
    steady_ft = [x for b, x in zip(ends, ft) if b >= tw]
    nwarm = sum(1 for t in win if t < tw)
    steady_span = span - warmup_s
    whole = part_stats(ft, med, span, len(win), slow_ms)
    warm = part_stats(warm_ft, med, warmup_s, nwarm, slow_ms) if warmup_s else None
    steady = part_stats(steady_ft, med, steady_span, len(win) - nwarm, slow_ms)
    return {
        "flips": len(win),
        "window_s": round(span, 3),
        "complete": complete,
        "load_gap_ms": round(gap, 3) if gap is not None else None,
        "avg_fps": whole["fps"],
        "max_fps": max(per_s) if per_s else None,
        "min_fps": min(per_s) if per_s else None,
        "frame_ms": {
            "mean": whole["mean_ms"],
            "p1": round(pct(sorted(ft), 1), 3),
            "p50": whole["p50_ms"],
            "p99": whole["p99_ms"],
            "max": whole["max_ms"],
        },
        "low1_fps": whole["low1_fps"],
        "hitches": whole["hitches"],
        "stutters": whole["stutters"],
        "slow_fps": slow_fps,
        "slow_share": whole["slow_share"],
        "warmup_s": warmup_s,
        "warmup_fps": warm["fps"] if warm else None,
        "steady_fps": steady["fps"] if steady and steady_span > 0 else None,
        "warmup": warm,
        "steady": steady if steady_span > 0 else None,
        "fps_per_s": per_s,
        "frames": [[round((b - t0) / 1000.0, 3), round(x, 3)] for b, x in zip(ends, ft)],
    }


PACING_BLOCK_RE = re.compile(r"^\[PACING\] flips \d+: .*cpu process (\d+(?:\.\d+)?)%; mode (\w+)")
PACING_SITE_RE = re.compile(r"^\[PACING\]\s+site 0x([0-9A-Fa-f]+): waits (\d+)")


def parse_pacing(text):
    """The runtime's [PACING] block every 600 flips: [(t_us of its flip or
    None, process CPU %, pacing mode)], and {site va: waits} over the run
    for each spin-wait site that waited. A run with no site is one whose
    generated code has no lowered spin wait, so present.pacing=sleep had
    nothing to act on."""
    blocks, sites, last = [], {}, None
    for line in text.splitlines():
        m = PACING_FLIP_RE.match(line)
        if m:
            last = int(m.group(2))
            continue
        m = PACING_BLOCK_RE.match(line)
        if m:
            blocks.append((last, float(m.group(1)), m.group(2)))
            continue
        m = PACING_SITE_RE.match(line)
        if m:
            va = "0x%08X" % int(m.group(1), 16)
            sites[va] = sites.get(va, 0) + int(m.group(2))
    return blocks, sites


def pacing_stats(blocks, sites, t0, duration_s, warmup_s):
    """The process's cores (the [PACING] block's CPU over its 600 flips) in
    the steady part, the mode, and the spin-wait sites. A block counts when
    it starts and ends inside the steady part (it starts where the one
    before it ended)."""
    out = {"spin_sites": dict(sorted(sites.items())), "mode": blocks[-1][2] if blocks else None}
    lo, hi = t0 + warmup_s * 1e6, t0 + duration_s * 1e6
    steady, start = [], None
    for t, pct, _ in blocks:
        if t is not None and start is not None and lo <= start and t <= hi:
            steady.append(pct)
        start = t
    if steady:
        out["cores_steady"] = round(statistics.median(steady) / 100.0, 2)
        out["blocks_steady"] = len(steady)
    return out


# --- GPU samples -------------------------------------------------------------


def parse_gpu_tsv(text):
    """The Deck sampler's lines "mono_us raw_us mhz busy" -> [(mono, raw,
    mhz, busy)]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) == 4 and p[0].isdigit():
            out.append((int(p[0]), int(p[1]), int(p[2]), int(p[3])))
    return out


def gpu_clock_for(samples, flips_us):
    """Which of the sampler's clocks the flips use: Wine's QPC is one of
    CLOCK_MONOTONIC or _RAW, and the two drift apart over a long uptime.
    The one whose range holds the first and last flip, the closer one when
    both do."""
    if not samples or not flips_us:
        return None
    best = None
    for col in (0, 1):
        lo, hi = samples[0][col], samples[-1][col]
        miss = max(0, lo - flips_us[0]) + max(0, flips_us[-1] - hi)
        if best is None or miss < best[0]:
            best = (miss, col)
    return best[1] if best[0] < 5e6 else None


def gpu_stats(samples, col, t0, duration_s, warmup_s):
    """(per-sample list relative to the scene's start, summary)."""
    end = t0 + duration_s * 1e6
    rows = [
        [round((s[col] - t0) / 1e6, 3), s[2], s[3]] for s in samples if t0 - 5e6 <= s[col] < end
    ]
    out = {}
    for name, lo, hi in (("warmup", 0, warmup_s), ("steady", warmup_s, duration_s)):
        part = [r for r in rows if lo <= r[0] < hi]
        if part:
            out[name] = {
                "mhz_mean": round(statistics.fmean(r[1] for r in part), 1),
                "busy_mean": round(statistics.fmean(r[2] for r in part), 1),
                # The averages hide short bursts; the highest sample bounds them.
                "mhz_max": max(r[1] for r in part),
                "busy_max": max(r[2] for r in part),
                "samples": len(part),
            }
    lowclk = [r[0] for r in rows if r[0] >= 0 and r[1] > min(x[1] for x in rows)]
    out["first_above_idle_s"] = lowclk[0] if lowclk else None
    return rows, out


# The Deck's CPU sampler (1 Hz): each core's clock and /proc/stat times, and
# where each of the game's threads (and wineserver's) last ran, its CPU time
# so far and the cores it may run on. On 4 cores / 8 threads two spinning
# guest threads plus Wine's own can land on one core, which would show as a
# slow run that no setting explains. Every few seconds it also looks for
# another process busy mid-run (busy.tsv), which the quiet check before the
# start cannot see.
CPU_SAMPLER = r"""
import glob, os, re, sys, time
d, name, period = sys.argv[1], sys.argv[2].lower(), float(sys.argv[3]) / 1000.0
busy_pct = float(sys.argv[4]) if len(sys.argv) > 4 else 50.0
ignore = [x.lower() for x in (sys.argv[5] if len(sys.argv) > 5 else "").split(",") if x not in ("", "-")]
tck = os.sysconf("SC_CLK_TCK")
num = lambda p: int(re.search(r"cpu(\d+)/", p).group(1))
cores = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"), key=num)
cpu = open(d + "/cpu.tsv", "w", buffering=1)
thr = open(d + "/threads.tsv", "w", buffering=1)
stat = open(d + "/cores.tsv", "w", buffering=1)
bsy = open(d + "/busy.tsv", "w", buffering=1)
pid = ws = None
me = "/proc/%d" % os.getpid()
# run.sh starts this; should it die without stopping it (its trap is the
# first line of defence), the sampler is reparented and stops by itself.
parent = os.getppid()
def text(p, f, mode="r"):
    try:
        with open(p + "/" + f, mode) as h:
            return h.read()
    except OSError:
        return None
# The game is the process with the most threads whose command line names
# the exe. This sampler's own command line names it too, and so do
# timeout and umu-run, so look again every few seconds until Wine's
# process, with its many threads, is up.
def find():
    best, server = None, None
    for p in glob.glob("/proc/[0-9]*"):
        if p == me:
            continue
        if (text(p, "comm") or "").strip() == "wineserver":
            server = p
        c = text(p, "cmdline", "rb")
        if c and name in c.decode(errors="replace").lower():
            try:
                n = len(os.listdir(p + "/task"))
            except OSError:
                continue
            if not best or n > best[0]:
                best = (n, p)
    return (best[1] if best else None), server
def ticks(s):
    rest = s[s.rindex(")") + 2 :].split()
    return s[s.index("(") + 1 : s.rindex(")")], rest
prev, prev_t = {}, None
def scan(m, r):
    # Every process's CPU time against the last scan: one over busy_pct of
    # a core that is not the game, Wine's own or an ignored name is logged.
    global prev, prev_t
    now, cur = time.monotonic(), {}
    for p in glob.glob("/proc/[0-9]*"):
        s = text(p, "stat")
        if not s:
            continue
        comm, rest = ticks(s)
        cur[p] = int(rest[11]) + int(rest[12])
        if prev_t is None or p in (me, pid, ws) or p not in prev:
            continue
        pct = 100.0 * (cur[p] - prev[p]) / tck / (now - prev_t)
        if pct < busy_pct:
            continue
        cmd = (text(p, "cmdline", "rb") or b"").replace(b"\0", b" ").decode(errors="replace")
        if any(i in comm.lower() or i in cmd.lower() for i in ignore):
            continue
        bsy.write("%d %d %.1f %s\n" % (m, r, pct, comm.replace(" ", "_") or "-"))
    prev, prev_t = cur, now
tick = -1
while os.getppid() == parent:
    m = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    r = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    f = []
    for c in cores:
        try:
            f.append(open(c).read().strip())
        except OSError:
            f.append("-1")
    cpu.write("%d %d %s\n" % (m, r, " ".join(f)))
    # Per core: busy and total jiffies so far (idle and iowait are not busy).
    st = []
    for line in (text("/proc", "stat") or "").splitlines():
        p = line.split()
        if p and p[0].startswith("cpu") and p[0] != "cpu":
            v = [int(x) for x in p[1:9]]
            st.append("%s:%d:%d" % (p[0][3:], sum(v) - v[3] - v[4], sum(v)))
    stat.write("%d %d %s\n" % (m, r, " ".join(st)))
    tick += 1
    if pid is None or not os.path.isdir(pid) or tick % 5 == 0:
        pid, ws = find()
    for t in [x for q in (pid, ws) if q for x in glob.glob(q + "/task/*")]:
        s = text(t, "stat")
        if not s:
            continue
        comm, rest = ticks(s)
        allowed = "-"
        for line in (text(t, "status") or "").splitlines():
            if line.startswith("Cpus_allowed_list:"):
                allowed = line.split(":", 1)[1].strip() or "-"
        thr.write("%d %d %s %s %d %s %s\n" % (m, r, t.rsplit("/", 1)[1], rest[36],
                  int(rest[11]) + int(rest[12]), comm.replace(" ", "_") or "-", allowed))
    if tick % 5 == 0:
        scan(m, r)
    time.sleep(period)
"""
CPU_PERIOD_MS = 1000
CLK_TCK = 100


def parse_cpu_tsv(text):
    """The CPU sampler's lines "mono_us raw_us khz0 khz1 ..." -> [(mono,
    raw, [mhz...])]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) > 2 and p[0].isdigit():
            out.append((int(p[0]), int(p[1]), [int(x) // 1000 for x in p[2:]]))
    return out


def parse_threads_tsv(text):
    """ "mono_us raw_us tid core ticks comm [allowed]" -> [(mono, raw, tid,
    core, ticks, comm, allowed)]; allowed (the Cpus_allowed_list) is "-"
    in logs from before it was logged."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) in (6, 7) and p[0].isdigit():
            allowed = p[6] if len(p) == 7 else "-"
            out.append((int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), p[5], allowed))
    return out


def parse_cores_tsv(text):
    """ "mono_us raw_us N:busy:total ..." (jiffies so far, from /proc/stat)
    -> [(mono, raw, {core: (busy, total)})]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) < 3 or not p[0].isdigit():
            continue
        try:
            cores = {int(c): (int(b), int(t)) for c, b, t in (x.split(":") for x in p[2:])}
        except ValueError:
            continue
        out.append((int(p[0]), int(p[1]), cores))
    return out


def parse_busy_tsv(text):
    """The CPU sampler's busy scan, "mono_us raw_us pct comm" -> [(mono,
    raw, pct, comm)]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) == 4 and p[0].isdigit():
            try:
                out.append((int(p[0]), int(p[1]), float(p[2]), p[3]))
            except ValueError:
                continue
    return out


# A thread over LISTED_PCT of a core in the steady part is listed (and can
# share a core with a hot one); one over HOT_PCT is hot. Sharing counted
# among hot threads alone hid the slow mode: a 10% helper on the main
# thread's core in every sample.
LISTED_PCT = 5.0
HOT_PCT = 50.0


def cpu_stats(samples, threads, col, t0, duration_s, warmup_s, cores=None):
    """The cores' clocks by part of the window; the game's threads over
    LISTED_PCT in the steady part (load, the cores they ran on, their
    affinity, how often another listed thread was on the same core at the
    same sample), the hot ones apart; each core's load from those threads
    (a thread's CPU time between two samples goes to the core it was last
    seen on) and, from /proc/stat, each core's busy share whoever ran."""
    out = {}
    for name, lo, hi in (("warmup", 0, warmup_s), ("steady", warmup_s, duration_s)):
        part = [s[2] for s in samples if lo <= (s[col] - t0) / 1e6 < hi]
        if part:
            per_core = [statistics.fmean(c[i] for c in part) for i in range(len(part[0]))]
            out[name] = {
                "mhz_mean": round(statistics.fmean(per_core), 1),
                "mhz_min_core": round(min(per_core), 1),
                "samples": len(part),
            }
    lo, hi = t0 + warmup_s * 1e6, t0 + duration_s * 1e6
    by_tid = {}
    for t in threads:
        if lo <= t[col] < hi:
            by_tid.setdefault(t[2], []).append(t)
    listed = {}
    per_core = {}
    span = 0.0
    for tid, ts in by_tid.items():
        if len(ts) < 2:
            continue
        secs = (ts[-1][col] - ts[0][col]) / 1e6
        span = max(span, secs)
        for a, b in zip(ts, ts[1:]):
            per_core[b[3]] = per_core.get(b[3], 0) + b[4] - a[4]
        load = 100.0 * (ts[-1][4] - ts[0][4]) / CLK_TCK / secs if secs > 0 else 0.0
        if load >= LISTED_PCT:
            listed[tid] = (load, ts)
    at = {}
    for tid, (_, ts) in listed.items():
        for t in ts:
            at.setdefault(t[col], {})[tid] = t[3]
    rows = []
    for tid, (load, ts) in sorted(listed.items(), key=lambda kv: -kv[1][0]):
        cores_ = {}
        shared = 0
        mates = set()
        for t in ts:
            cores_[t[3]] = cores_.get(t[3], 0) + 1
            others = [o for o, c in at[t[col]].items() if o != tid and c == t[3]]
            if others:
                shared += 1
                mates.update(others)
        allowed = {}
        for t in ts:
            allowed[t[6]] = allowed.get(t[6], 0) + 1
        rows.append(
            {
                "tid": tid,
                "name": ts[-1][5],
                "load_pct": round(load, 1),
                "cores": dict(sorted(cores_.items())),
                # The most common Cpus_allowed_list (one core: pinned).
                "allowed": max(allowed, key=allowed.get),
                "shared_share": round(shared / len(ts), 3),
                "shared_with": sorted(listed[o][1][-1][5] for o in mates),
            }
        )
    out["threads"] = rows
    out["hot_threads"] = [r for r in rows if r["load_pct"] >= HOT_PCT]
    if span > 0:
        load = {c: round(100.0 * n / CLK_TCK / span, 1) for c, n in sorted(per_core.items())}
        out["cores_load_pct"] = load
        if load:
            busiest = max(load, key=load.get)
            out["core_max"] = busiest
            out["core_max_pct"] = load[busiest]
    part = [c for c in cores or [] if lo <= c[col] < hi]
    if len(part) >= 2:
        first, last = part[0][2], part[-1][2]
        busy = {}
        for c in sorted(set(first) & set(last)):
            dt = last[c][1] - first[c][1]
            if dt > 0:
                busy[c] = round(100.0 * (last[c][0] - first[c][0]) / dt, 1)
        out["sys_cores_busy_pct"] = busy
        if busy:
            out["sys_core_max_pct"] = max(busy.values())
    return out


def parse_nvsmi(text, utc_offset_s=0):
    """nvidia-smi csv (timestamp, clocks.gr, utilization.gpu, no header or
    units) -> [(unix_us, mhz, busy)]. The stamps are the PC's local time
    with no zone, so the PC's offset from UTC turns them into unix time; the
    analysing machine's own zone must play no part."""
    out = []
    utc = datetime.UTC
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3:
            continue
        try:
            dt = datetime.datetime.strptime(p[0], "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=utc)
            us = int(dt.timestamp() * 1e6) - int(utc_offset_s * 1e6)
            out.append((us, int(float(p[1])), int(float(p[2]))))
        except ValueError:
            continue
    return out


def pc_utc_offset(info):
    """The PC's offset from UTC in seconds: run-info's `started` is local
    time with no zone, taken next to `unix_us`. Zones are whole quarter
    hours, so rounding absorbs the second between the two stamps. None
    when either stamp is missing or unreadable: guessing a zone would empty
    the GPU columns without a word."""
    try:
        dt = datetime.datetime.fromisoformat(info["started"]).replace(tzinfo=datetime.UTC)
        return round((dt.timestamp() - info["unix_us"] / 1e6) / 900) * 900
    except (KeyError, TypeError, ValueError):
        return None


def parse_pc_cpu(text):
    """The PC runner's cpu.csv (t,total_ms: seconds since the launch, the
    game process's CPU time so far) -> [(t_s, total_ms)]."""
    out = []
    for line in text.splitlines()[1:]:
        p = line.split(",")
        try:
            out.append((float(p[0]), int(p[1])))
        except (IndexError, ValueError):
            continue
    return out


def pc_cpu_stats(samples, qpc0, t0, duration_s, warmup_s):
    """Cores' worth of CPU the game used per window, from its CPU time.
    qpc0 is the launch in QPC microseconds, t0 the scene's start. A run
    that waits (throttled presents, a stalled thread) shows as few cores
    where a spinning one shows many."""
    rel = [((qpc0 + t * 1e6 - t0) / 1e6, ms) for t, ms in samples]
    out = {}
    for name, lo, hi in (("warmup", 0, warmup_s), ("steady", warmup_s, duration_s)):
        part = [r for r in rel if lo <= r[0] <= hi]
        if len(part) >= 2 and part[-1][0] > part[0][0]:
            span = part[-1][0] - part[0][0]
            out[name] = {
                "cores_mean": round((part[-1][1] - part[0][1]) / 1000 / span, 2),
                "samples": len(part),
            }
    steps = [
        (b[1] - a[1]) / 1000 / (b[0] - a[0])
        for a, b in zip(rel, rel[1:])
        if warmup_s <= a[0] and b[0] <= duration_s and b[0] > a[0]
    ]
    if steps:
        out["cores_max"] = round(max(steps), 2)
    if rel:
        out["total_s"] = round(rel[-1][1] / 1000, 1)
    return out


# --- the profile -------------------------------------------------------------


def parse_prof(text):
    """The sampler's report -> {"header", "threads", "modules", "rows"}."""
    head, threads, modules, rows = {}, {}, {}, []
    for line in text.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "#" and len(p) > 2 and p[1] == "recomp-prof":
            it = iter(p[3:])
            head = {k: v for k, v in zip(it, it)}
        elif p[0] == "thread" and len(p) >= 5:
            threads[int(p[1])] = {"tid": p[2], "samples": int(p[3]), "name": " ".join(p[4:])}
        elif p[0] == "module" and len(p) >= 4:
            modules[int(p[1])] = {"base": int(p[2], 16), "path": " ".join(p[3:])}
        elif p[0] == "s" and len(p) == 5:
            rows.append((int(p[1]), int(p[2]), int(p[3], 16), int(p[4])))
    return {"header": head, "threads": threads, "modules": modules, "rows": rows}


def pe_image_base(path):
    """The preferred ImageBase of a PE file (llvm-nm prints addresses with
    it added), or None."""
    try:
        with open(path, "rb") as f:
            d = f.read(4096)
    except OSError:
        return None
    if d[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", d, 0x3C)[0]
    if d[off : off + 4] != b"PE\0\0":
        return None
    opt = off + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    if magic == 0x20B:
        return struct.unpack_from("<Q", d, opt + 24)[0]
    return struct.unpack_from("<I", d, opt + 28)[0]


def find_nm(exe):
    """llvm-nm reads PE and Mach-O alike; plain nm for a Mach-O."""
    return shutil.which("llvm-nm") or shutil.which("nm")


def symbols(exe, nm=None):
    """(sorted start offsets, names) of the exe's text symbols, as offsets
    from the image's load address (what the sampler reports)."""
    nm = nm or find_nm(exe)
    if not nm:
        raise MatrixError("no nm or llvm-nm to symbolise %s" % exe)
    r = subprocess.run([nm, "-n", exe], capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise MatrixError("%s %s: %s" % (nm, exe, r.stderr.strip()))
    base = pe_image_base(exe)
    pe = base is not None
    syms = []
    for line in r.stdout.splitlines():
        p = line.split(None, 2)
        if len(p) != 3:
            continue
        addr, kind, name = p
        if base is None and name == "__mh_execute_header":
            base = int(addr, 16)
        if kind in "Tt":
            syms.append((int(addr, 16), name))
    base = base or 0
    syms.sort()
    # Mach-O prefixes C names with "_"; COFF on x86-64 does not.
    return [a - base for a, _ in syms], [n if pe else n.removeprefix("_") for _, n in syms]


def module_name(path):
    return re.split(r"[\\/]", path)[-1]


# Where a thread blocked in a wait sits. Under Wine a profile taken in
# mode "all" (Wine's thread cycle counter does not move, so every thread is
# sampled, waiting or not) is mostly these; native Windows' "running" mode
# still counts threads parked in a wait here too. The no-wait view leaves
# them out.
WAIT_MODULES = ("ntdll.dll", "win32u.dll")


def hot_paths(prof, exe=None, nm=None, top=25, run_exe=None, spin_functions=()):
    """The profile by function (the exe's) and by module (everything else),
    overall and for each named thread. exe is the symbol source; run_exe is
    the name the game ran under, when the symbol copy was renamed.
    spin_functions are the scene's busy-wait loops: their share of the
    samples says how much of the profile is waiting dressed as work."""
    mods = prof["modules"]
    bases = {module_name(x).lower() for x in (exe, run_exe) if x}
    main = [i for i, m in mods.items() if exe and module_name(m["path"]).lower() in bases]
    starts, names = symbols(exe, nm) if exe and main else ([], [])

    def where(mod, off):
        if mod in main and starts:
            k = bisect.bisect_right(starts, off) - 1
            if k >= 0:
                return names[k]
            return "%s+0x%x" % (module_name(mods[mod]["path"]), off)
        if mod in mods:
            return "[%s]" % module_name(mods[mod]["path"])
        return "[?]"

    total = sum(r[3] for r in prof["rows"]) or 1
    by_fn, by_mod, by_thread, no_wait = {}, {}, {}, {}
    for slot, mod, off, n in prof["rows"]:
        fn = where(mod, off)
        by_fn[fn] = by_fn.get(fn, 0) + n
        if mod not in mods or module_name(mods[mod]["path"]).lower() not in WAIT_MODULES:
            no_wait[fn] = no_wait.get(fn, 0) + n
        mname = module_name(mods[mod]["path"]) if mod in mods else "?"
        by_mod[mname] = by_mod.get(mname, 0) + n
        tn = prof["threads"].get(slot, {}).get("name", "?")
        d = by_thread.setdefault(tn, {})
        d[fn] = d.get(fn, 0) + n

    def ranked(d, k, of=total):
        return [
            {"name": name, "samples": n, "share": round(100.0 * n / of, 2)}
            for name, n in sorted(d.items(), key=lambda kv: -kv[1])[:k]
        ]

    threads = {}
    for tn, d in sorted(by_thread.items(), key=lambda kv: -sum(kv[1].values())):
        threads[tn] = {"samples": sum(d.values()), "top": ranked(d, 8)}
    spin = None
    if spin_functions:
        hits = {f: by_fn.get(f, 0) for f in spin_functions}
        n = sum(hits.values())
        nw = sum(no_wait.values())
        spin = {
            "functions": hits,
            # Of the samples outside the wait modules (what ran, in every
            # mode), and of all of them.
            "share": round(100.0 * n / nw, 2) if nw else None,
            "share_all": round(100.0 * n / total, 2),
        }
    return {
        "spin": spin,
        "header": prof["header"],
        "samples": total,
        "functions": ranked(by_fn, top),
        # Shares of the samples outside the wait modules.
        "functions_no_wait": ranked(no_wait, top, sum(no_wait.values()) or 1),
        "modules": ranked(by_mod, 15),
        "threads": dict(itertools.islice(threads.items(), 12)),
    }


# --- collect -----------------------------------------------------------------


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def collect(run_dir, scene=None, exe=None, nm=None):
    """run.json for one run directory: the frame statistics over the scene's
    window, the GPU samples when there are any, the hot paths when there is
    a profile."""
    info = json.loads(read(os.path.join(run_dir, "run-info.json")) or "{}")
    if scene is None:
        scene = info.get("scene")
        if scene is None:
            raise MatrixError("%s: no run-info.json with the scene; pass --scene" % run_dir)
    sc = scene["scene"]
    log = None
    for name in ("game.log", "game-stdio.log"):
        log = read(os.path.join(run_dir, name))
        if log:
            break
    if not log:
        raise MatrixError("%s: no game.log or game-stdio.log" % run_dir)
    flips = parse_flips(log)
    if not flips:
        raise MatrixError("%s: no [PACING] flip lines (RECOMP_TRACE=pacing=all)" % run_dir)
    i0, how = find_start(flips, sc.get("start", {}))
    t0 = flips[i0][1]
    warm = sc.get("warmup_s", 20)
    out = dict(info)
    out["prof_hz"] = prof_hz(dict(info, scene=scene))
    out.pop("scene", None)
    out["start"] = {
        "flip": flips[i0][0],
        "found_by": how,
        "s_after_first_flip": round((t0 - flips[0][1]) / 1e6, 3),
    }
    out["frames"] = frame_stats(
        [f[1] for f in flips], t0, sc["duration_s"], warm, sc.get("slow_fps", 50)
    )
    # What analyse judges a run by (the scene's [verdict]), kept with the
    # run so analyse needs no scene.
    if scene.get("verdict"):
        out["verdict_rule"] = scene["verdict"]
    blocks, sites = parse_pacing(log)
    out["pacing"] = pacing_stats(blocks, sites, t0, sc["duration_s"], warm)
    # How the run was taken, and what on (bench-report D5): the hardware
    # block is cleaned again field by field, whoever wrote it (the PC's
    # script among them), and Proton's log names the DXVK it ran.
    out["run"] = bench_hw.run_block(info, log, out["prof_hz"], out["pacing"].get("mode"))
    if info.get("hw"):
        hw = bench_hw.sanitize_hw(info["hw"])
        dx = bench_hw.dxvk_version(read(os.path.join(run_dir, "umu.log")))
        if dx and not hw["backend"]["translation_version"]:
            hw["backend"]["translation"] = hw["backend"]["translation"] or "DXVK"
            hw["backend"]["translation_version"] = dx
        out["hw"] = hw
    b = info.get("build")
    if isinstance(b, dict) and b.get("version") and not b.get("version_public"):
        out["build"] = bench_hw.build_block(
            version=b["version"],
            game_sha=b.get("game_sha"),
            toolkit_sha=b.get("toolkit_sha"),
            exe_sha256=b.get("exe_sha256"),
        )
    gpu_t = read(os.path.join(run_dir, "gpu.tsv"))
    gpu_c = read(os.path.join(run_dir, "gpu.csv"))
    if gpu_t:
        s = parse_gpu_tsv(gpu_t)
        col = gpu_clock_for(s, [f[1] for f in flips])
        if col is not None:
            rows, summ = gpu_stats(s, col, t0, sc["duration_s"], warm)
            out["gpu"] = dict(summ, source="amdgpu sysfs", samples=rows)
        else:
            out["gpu"] = {"source": "amdgpu sysfs", "error": "clock does not match the flips"}
    elif gpu_c and "qpc_us" in info and "unix_us" in info:
        # The PC runner wrote (QPC, wall) at one instant; nvidia-smi stamps
        # wall time. col 0 then holds QPC microseconds like the flips.
        # The offset is taken once, at `started`: a DST change inside a run
        # (or the repeated fall-back hour) is not handled, which is fine for
        # a bench.
        off = pc_utc_offset(info)
        if off is None:
            out["gpu"] = {"source": "nvidia-smi", "error": "no started stamp for the PC's zone"}
        else:
            d = info["qpc_us"] - info["unix_us"]
            nv = parse_nvsmi(gpu_c, off)
            s = [(u + d, u + d, mhz, busy) for u, mhz, busy in nv]
            rows, summ = gpu_stats(s, 0, t0, sc["duration_s"], warm)
            out["gpu"] = dict(summ, source="nvidia-smi", utc_offset_s=off, samples=rows)
    cpu_t = read(os.path.join(run_dir, "cpu.tsv"))
    cpu_c = read(os.path.join(run_dir, "cpu.csv"))
    if not cpu_t and cpu_c and "qpc_us" in info:
        stats = pc_cpu_stats(parse_pc_cpu(cpu_c), info["qpc_us"], t0, sc["duration_s"], warm)
        out["cpu"] = dict(stats, source="process cpu time")
    if cpu_t:
        s = parse_cpu_tsv(cpu_t)
        col = gpu_clock_for([(x[0], x[1]) for x in s], [f[1] for f in flips])
        if col is not None:
            th = parse_threads_tsv(read(os.path.join(run_dir, "threads.tsv")) or "")
            cores = parse_cores_tsv(read(os.path.join(run_dir, "cores.tsv")) or "")
            out["cpu"] = cpu_stats(s, th, col, t0, sc["duration_s"], warm, cores)
            busy = parse_busy_tsv(read(os.path.join(run_dir, "busy.tsv")) or "")
            if busy:
                # [s after the scene's start, %cpu, command], as the Mac's
                # watch gives them; only those inside the window disturb it.
                end = t0 + sc["duration_s"] * 1e6
                out["busy_during"] = (out.get("busy_during") or []) + [
                    [round((b[col] - t0) / 1e6, 1), b[2], b[3]] for b in busy if t0 <= b[col] < end
                ]
        else:
            out["cpu"] = {"error": "clock does not match the flips"}
    prof_t = read(os.path.join(run_dir, "prof.txt"))
    if prof_t:
        exe = exe or info.get("symbols")
        try:
            run_exe = info.get("remote_exe") or info.get("exe")
            out["profile"] = hot_paths(
                parse_prof(prof_t),
                exe if exe and os.path.isfile(exe) else None,
                nm,
                run_exe=run_exe,
                spin_functions=tuple((scene.get("profile") or {}).get("spin_functions", ())),
            )
        except MatrixError as e:
            out["profile"] = {"error": str(e)}
    with open(os.path.join(run_dir, "run.json"), "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    return out


# --- analyse -----------------------------------------------------------------

# The steady part leads: the warm-up is where a GPU clock ramps (and a
# title screen's first seconds), so whole-window lows mostly measure that.
COLUMNS = [
    "host",
    "backend",
    "arm",
    "prof",
    "rep",
    "run_dir",
    "verdict",
    "disturbed",
    "complete",
    "start_s",
    "load_gap_ms",
    "flips",
    "steady_fps",
    "steady_low1_fps",
    "steady_worst_s_fps",
    "steady_p99_ms",
    "steady_hitches",
    "steady_stutters",
    "steady_slow_share",
    "warmup_fps",
    "warmup_low1_fps",
    "warmup_hitches",
    "avg_fps",
    "max_fps",
    "min_fps",
    "low1_fps",
    "p1_ms",
    "p50_ms",
    "p99_ms",
    "max_ms",
    "hitches",
    "stutters",
    "gpu_mhz_warmup",
    "gpu_mhz_steady",
    "gpu_busy_warmup",
    "gpu_busy_steady",
    "gpu_busy_max",
    "gpu_mhz_max",
    "gpu_first_above_idle_s",
    "cpu_mhz_steady",
    "cpu_cores_steady",
    "cpu_hot_threads",
    "cpu_shared_share",
    "cpu_process_cores",
    "cpu_core_max_pct",
    "cpu_sys_core_max_pct",
    "spin_share",
    "spin_sites",
    "pacing_mode",
    "top_function",
    "top_share",
    "busy_during",
    "focus_lost",
    "busy_at_start",
    "fullscreen",
]

REP_RE = re.compile(r"-r(\d+)$")
PROF_HZ_RE = re.compile(r"(?:^|,)prof=(\d+)")


def prof_hz(r):
    """The sampler's rate in a run, 0 without it: runs at two rates are two
    groups, since the sampler's cost grows with it (all-threads mode)."""
    if not r.get("prof"):
        return 0
    if r.get("prof_hz"):
        return r["prof_hz"]
    m = PROF_HZ_RE.search((r.get("env") or {}).get("RECOMP_TRACE", ""))
    if m:
        return int(m.group(1))
    sc = r.get("scene") or {}
    host = (sc.get("hosts") or {}).get(r.get("host")) or {}
    return int(dict(sc.get("profile") or {}, **(host.get("profile") or {})).get("hz", 0))


def verdict(row, rule):
    """ "good" or "bad" by the scene's [verdict]: hosts (all when absent)
    and <column>_min / <column>_max bounds a good run meets; "" when the
    rule does not cover the host or a bounded column is missing."""
    if not rule or (rule.get("hosts") and row["host"] not in rule["hosts"]):
        return ""
    ok = True
    for k, bound in rule.items():
        col, _, side = k.rpartition("_")
        if side not in ("min", "max"):
            continue
        v = row.get(col)
        if v is None:
            return ""
        ok = ok and (v >= bound if side == "min" else v <= bound)
    return "good" if ok else "bad"


def summary_row(r, run_dir=None):
    fr = r["frames"]
    st = fr.get("steady") or {}
    wu = fr.get("warmup") or {}
    g = r.get("gpu", {})
    c = r.get("cpu", {})
    p = r.get("profile", {})
    pc = r.get("pacing") or {}
    # Mode "all" samples waiting threads too, and a thread blocked outside
    # ntdll (audio in mmdevapi) still tops the no-wait view; the exe's own
    # hottest function is the useful column there. Windows' "running" mode
    # still counts threads parked in a wait (95% of samples in ntdll), so
    # a wait module on top takes the same view.
    first = ((p.get("functions") or [{}])[0].get("name") or "").strip("[]").lower()
    if (p.get("header") or {}).get("mode") == "all" or first in WAIT_MODULES:
        fns = p.get("functions_no_wait") or []
        # An all-wait profile has no no-wait rows; the wait module itself
        # says more than an empty column.
        top = next(
            (f for f in fns if not f["name"].startswith("[")),
            fns[0] if fns else (p.get("functions") or [{}])[0],
        )
    else:
        top = (p.get("functions") or [{}])[0]
    m = REP_RE.search(os.path.basename(os.path.normpath(run_dir))) if run_dir else None
    hot = c.get("hot_threads") or []
    gparts = [g.get(k, {}) for k in ("warmup", "steady")]
    gbusy = [x["busy_max"] for x in gparts if "busy_max" in x]
    gmhz = [x["mhz_max"] for x in gparts if "mhz_max" in x]
    row = {
        "host": r.get("host"),
        "backend": r.get("backend"),
        # The arm itself; prof is the sampler's rate (0: it did not run).
        "arm": (r.get("arm") or "").removesuffix("-noprof"),
        "prof": prof_hz(r),
        "rep": r.get("rep") or (int(m.group(1)) if m else 1),
        "run_dir": run_dir,
        "complete": fr["complete"],
        "start_s": r["start"]["s_after_first_flip"],
        "load_gap_ms": fr.get("load_gap_ms"),
        "flips": fr["flips"],
        "steady_fps": fr["steady_fps"],
        "steady_low1_fps": st.get("low1_fps"),
        "steady_worst_s_fps": worst_second(fr.get("fps_per_s"), fr.get("warmup_s")),
        "steady_p99_ms": st.get("p99_ms"),
        "steady_hitches": st.get("hitches"),
        "steady_stutters": st.get("stutters"),
        "steady_slow_share": st.get("slow_share"),
        "warmup_fps": fr["warmup_fps"],
        "warmup_low1_fps": wu.get("low1_fps"),
        "warmup_hitches": wu.get("hitches"),
        "avg_fps": fr["avg_fps"],
        "max_fps": fr["max_fps"],
        "min_fps": fr["min_fps"],
        "low1_fps": fr["low1_fps"],
        "p1_ms": fr["frame_ms"]["p1"],
        "p50_ms": fr["frame_ms"]["p50"],
        "p99_ms": fr["frame_ms"]["p99"],
        "max_ms": fr["frame_ms"]["max"],
        "hitches": fr.get("hitches"),
        "stutters": fr["stutters"],
        "gpu_mhz_warmup": g.get("warmup", {}).get("mhz_mean"),
        "gpu_mhz_steady": g.get("steady", {}).get("mhz_mean"),
        "gpu_busy_warmup": g.get("warmup", {}).get("busy_mean"),
        "gpu_busy_steady": g.get("steady", {}).get("busy_mean"),
        "gpu_busy_max": max(gbusy) if gbusy else None,
        "gpu_mhz_max": max(gmhz) if gmhz else None,
        "gpu_first_above_idle_s": g.get("first_above_idle_s"),
        "cpu_mhz_steady": c.get("steady", {}).get("mhz_mean"),
        "cpu_cores_steady": c.get("steady", {}).get("cores_mean"),
        "cpu_hot_threads": len(hot) if "hot_threads" in c else None,
        "cpu_shared_share": max((h["shared_share"] for h in hot), default=None),
        # The process's cores from the runtime's own [PACING] block: every
        # host has it, the Mac included.
        "cpu_process_cores": pc.get("cores_steady"),
        "cpu_core_max_pct": c.get("core_max_pct"),
        "cpu_sys_core_max_pct": c.get("sys_core_max_pct"),
        "spin_share": (p.get("spin") or {}).get("share"),
        # None for a run collected before the count was kept.
        "spin_sites": len(pc["spin_sites"]) if "spin_sites" in pc else None,
        "pacing_mode": pc.get("mode"),
        "top_function": top.get("name"),
        "top_share": top.get("share"),
        # Busy samples of other processes during the run (the Mac's watch,
        # the Deck's sampler, the PC's runner).
        "busy_during": len(r.get("busy_during") or []),
        # Times another window took the foreground mid-run (the PC's
        # runner: Windows throttles an unfocused game's presents).
        "focus_lost": len(r.get("focus_lost") or []),
        # A quiet wait that gave up: what was still busy, or still
        # building, when the run started anyway.
        "busy_at_start": len(r.get("busy_at_start") or []) + len(r.get("builds_at_start") or []),
        # "1" for a borderless fullscreen run, None for a windowed one
        # (every run before fullscreen runs were added).
        "fullscreen": r.get("present_fullscreen") or None,
    }
    for core, v in (c.get("cores_load_pct") or {}).items():
        row["cpu_core%s_pct" % core] = v
    # Another process busy or another window in front mid-run may have cost
    # frames: such a run stays in the rows but out of the per-arm figures
    # unless asked for.
    row["disturbed"] = row["busy_during"] > 0 or row["focus_lost"] > 0 or row["busy_at_start"] > 0
    row["verdict"] = verdict(row, r.get("verdict_rule"))
    for k, v in (r.get("values") or {}).items():
        row["axis_" + k] = v
    return row


# Per-arm figures over the repeats: median, min, max and MAD (the median
# absolute deviation from the median).
ARM_STATS = (
    "avg_fps",
    "steady_fps",
    "warmup_fps",
    "steady_low1_fps",
    "steady_worst_s_fps",
    "steady_p99_ms",
    "steady_hitches",
    "steady_stutters",
    "steady_slow_share",
    "low1_fps",
    "gpu_mhz_warmup",
    "gpu_mhz_steady",
    "gpu_busy_steady",
    "gpu_busy_max",
    "cpu_mhz_steady",
    "cpu_cores_steady",
    "cpu_shared_share",
    "cpu_process_cores",
    "cpu_core_max_pct",
    "spin_share",
    "load_gap_ms",
)
ARM_SUFFIXES = ("_median", "_min", "_max", "_mad")


def spread(vals):
    med = statistics.median(vals)
    return {
        "_median": round(med, 3),
        "_min": round(min(vals), 3),
        "_max": round(max(vals), 3),
        "_mad": round(statistics.median(abs(v - med) for v in vals), 3),
    }


def arm_rows(rows, include_disturbed=False):
    """One row per (host, arm, prof rate): n, the good and bad counts and the
    spread of ARM_STATS over its runs."""
    groups = {}
    for r in rows:
        if r["disturbed"] and not include_disturbed:
            continue
        groups.setdefault((r["host"], r["arm"], r["prof"]), []).append(r)
    out = []
    for (host, arm, prof), rs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        a = {"host": host, "arm": arm, "prof": prof, "n": len(rs)}
        a["good"] = sum(r["verdict"] == "good" for r in rs)
        a["bad"] = sum(r["verdict"] == "bad" for r in rs)
        for k in ARM_STATS:
            v = [r[k] for r in rs if r.get(k) is not None]
            if v:
                a.update({k + s: x for s, x in spread(v).items()})
        for k, v in rs[0].items():
            if k.startswith("axis_"):
                a[k] = v
        a["fullscreen"] = sorted({str(r.get("fullscreen")) for r in rs})
        out.append(a)
    return out


def prof_overhead(rows, include_disturbed=False):
    """The sampler's cost per (host, arm, rate): the median steady fps of
    the runs without it against those with it, and, when the runs carry a
    verdict, the same within the good runs only (a slow-mode run on one
    side alone would otherwise read as the sampler's cost)."""
    by = {}
    for r in rows:
        if r["disturbed"] and not include_disturbed or r.get("steady_fps") is None:
            continue
        by.setdefault((r["host"], r["arm"]), {}).setdefault(r["prof"], []).append(r)
    out = []
    for (h, a), sides in sorted(by.items()):
        if 0 not in sides:
            continue
        for hz in sorted(k for k in sides if k):
            o = {"host": h, "arm": a, "prof": hz, "n_off": len(sides[0]), "n_on": len(sides[hz])}
            for tag, keep in (("", lambda r: True), ("_good", lambda r: r["verdict"] == "good")):
                off = [r["steady_fps"] for r in sides[0] if keep(r)]
                on = [r["steady_fps"] for r in sides[hz] if keep(r)]
                if off and on and (tag == "" or any(r["verdict"] for r in sides[0])):
                    f_off, f_on = statistics.median(off), statistics.median(on)
                    o["fps_off" + tag] = round(f_off, 3)
                    o["fps_on" + tag] = round(f_on, 3)
                    o["cost_pct" + tag] = round(100.0 * (f_off - f_on) / f_off, 2)
            out.append(o)
    return out


def run_name(arm_id, prof=True, rep=1):
    """A run's directory name: the arm, -noprof without the sampler, -rN
    for a repeat past the first. Every runner names its runs so (the PC's
    script too), and a run copied back keeps the name."""
    return arm_id + ("" if prof else "-noprof") + ("-r%d" % rep if rep and rep > 1 else "")


def misnamed(rows):
    """Runs whose directory is not their arm plus -noprof and -rN (a PC run
    copied back as "noprof-3", or arm a1 in a10-noprof): the rows still group by run.json's arm, but a
    reader of the tree cannot tell the arm from the name."""
    out = []
    for r in rows:
        base = os.path.basename(os.path.normpath(r["run_dir"] or ""))
        rest = base[len(r["arm"]) :] if r["arm"] and base.startswith(r["arm"]) else None
        if r["arm"] and (rest is None or not re.fullmatch(r"(-noprof)?(-r\d+)?", rest)):
            out.append((r["run_dir"], run_name(r["arm"], r["prof"] != 0, r["rep"])))
    return out


def load_rows(root, recollect=False, scene=None, exe=None):
    """(run.json dicts, summary rows) for ROOT/<host>/<run>/run.json.
    recollect runs collect again on each run first (a run collected by an
    older CLI lacks the newer columns), with scene, when given, in place of
    the one in run-info.json (a key added since, such as spin_functions)."""
    runs, rows = [], []
    for path in sorted(glob.glob(os.path.join(root, "*", "*", "run.json"))):
        d = os.path.dirname(path)
        r = None
        if recollect:
            try:
                r = collect(d, scene, exe)
            except (MatrixError, OSError) as e:
                # A run kept as run.json alone (its raw logs elsewhere)
                # stays as collected.
                print("recollect %s: %s; kept its run.json" % (d, e), file=sys.stderr)
        if r is None:
            with open(path) as f:
                r = json.load(f)
        runs.append(r)
        rows.append(summary_row(r, os.path.relpath(d, root)))
    if not runs:
        raise MatrixError("no run.json under %s/<host>/<arm>/" % root)
    return runs, rows


def dynamic_columns(rows, prefix):
    """The axis_* or cpu_core*_pct columns the rows have, in order."""
    keys = {k for r in rows for k in r if k.startswith(prefix)}
    return sorted(keys, key=lambda k: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", k)])


def analyse(root, out_dir=None, include_disturbed=False, recollect=False, scene=None, exe=None):
    """summary.json (rows, arms, prof_overhead, runs), summary.csv (one row
    per run) and summary-arms.csv (one per arm) from
    ROOT/<host>/<run>/run.json."""
    out_dir = out_dir or root
    runs, rows = load_rows(root, recollect, scene, exe)
    axes = dynamic_columns(rows, "axis_")
    core_cols = [
        k for k in dynamic_columns(rows, "cpu_core") if re.fullmatch(r"cpu_core\d+_pct", k)
    ]
    arms_ = arm_rows(rows, include_disturbed)
    overhead = prof_overhead(rows, include_disturbed)
    rules = {json.dumps(r["verdict_rule"], sort_keys=True) for r in runs if r.get("verdict_rule")}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(
            {
                "rows": rows,
                "arms": arms_,
                "prof_overhead": overhead,
                "verdict_rules": [json.loads(x) for x in sorted(rules)],
                "include_disturbed": include_disturbed,
                # [run dir, the name it should have] (see misnamed).
                "misnamed": [list(x) for x in misnamed(rows)],
                "runs": [summary_detail(r) for r in runs],
            },
            f,
            indent=1,
        )
        f.write("\n")
    cols = COLUMNS[:4] + axes + COLUMNS[4:] + core_cols
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    acols = ["host", "arm", "prof"] + axes + ["n", "good", "bad"]
    acols += [k + s for k in ARM_STATS for s in ARM_SUFFIXES]
    with open(os.path.join(out_dir, "summary-arms.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=acols, extrasaction="ignore")
        w.writeheader()
        w.writerows(arms_)
    return rows, overhead, arms_


def summary_detail(r):
    """What a chart needs beyond the row: the per-second fps series, the
    GPU samples and the top functions."""
    return {
        "host": r.get("host"),
        "arm": r.get("arm"),
        "prof": prof_hz(r),
        "fps_per_s": r["frames"]["fps_per_s"],
        "gpu": [s[:3] for s in r.get("gpu", {}).get("samples", [])],
        "hot_threads": r.get("cpu", {}).get("hot_threads"),
        "threads": r.get("cpu", {}).get("threads"),
        "cores_load_pct": r.get("cpu", {}).get("cores_load_pct"),
        "sys_cores_busy_pct": r.get("cpu", {}).get("sys_cores_busy_pct"),
        "spin": r.get("profile", {}).get("spin"),
        "spin_sites": (r.get("pacing") or {}).get("spin_sites"),
        "functions": (r.get("profile", {}).get("functions") or [])[:10],
        "modules": (r.get("profile", {}).get("modules") or [])[:6],
    }


def table(rows):
    """A plain-text table for a terminal or a tracker comment."""
    cols = ["host", "arm", "prof", "rep", "steady_fps", "steady_low1_fps", "steady_p99_ms"]
    cols += ["steady_hitches", "warmup_fps", "avg_fps", "load_gap_ms", "gpu_mhz_warmup"]
    cols += ["cpu_shared_share", "top_function", "verdict", "busy_during"]
    lines = [" | ".join(cols)]
    for r in rows:
        lines.append(" | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    return "\n".join(lines)


def arms_table(arms_):
    cols = ["host", "arm", "prof", "n", "good", "bad", "steady_fps_median", "steady_fps_mad"]
    cols += ["steady_low1_fps_median", "warmup_fps_median", "avg_fps_median"]
    lines = [" | ".join(cols)]
    for a in arms_:
        lines.append(" | ".join("" if a.get(c) is None else str(a.get(c)) for c in cols))
    return "\n".join(lines)


# --- compare -----------------------------------------------------------------

# What compare sets side by side, per (host, arm, prof rate): the arm
# medians of these. "cores" is the runtime's own process CPU when both
# sides have it, else the runner's when both have that; with one measure
# on each side the values show but the change stays blank.
COMPARE_STATS = (
    ("fps", "steady_fps"),
    ("low1_fps", "steady_low1_fps"),
    ("p99_ms", "steady_p99_ms"),
    ("cores", "cpu_process_cores"),
    ("spin_pct", "spin_share"),
    ("core_max_pct", "cpu_core_max_pct"),
    ("load_gap_ms", "load_gap_ms"),
)


def compare(before, after, include_disturbed=False, recollect=False, scene=None, exe=None):
    """Two matrix results (ROOT dirs, e.g. a baseline and its rerun) side
    by side: one row per (host, arm, prof rate) in either, with n, the good
    and bad counts, and before, after and the change of each COMPARE_STATS
    median. An arm on one side only has the other side blank."""
    sides = []
    for root in (before, after):
        _, rows = load_rows(root, recollect, scene, exe)
        sides.append(
            {(a["host"], a["arm"], a["prof"]): a for a in arm_rows(rows, include_disturbed)}
        )
    out = []
    for key in sorted(set(sides[0]) | set(sides[1]), key=lambda k: tuple(map(str, k))):
        b, a = sides[0].get(key, {}), sides[1].get(key, {})
        row = {"host": key[0], "arm": key[1], "prof": key[2]}
        for tag, side in (("before", b), ("after", a)):
            row["n_" + tag] = side.get("n")
            row["good_" + tag] = side.get("good") if side else None
            row["bad_" + tag] = side.get("bad") if side else None
        for name, col in COMPARE_STATS:
            vb, va = (x.get(col + "_median") for x in (b, a))
            same = True
            if name == "cores" and (vb is None or va is None):
                sb, sa = (x.get("cpu_cores_steady_median") for x in (b, a))
                if sb is not None and sa is not None:
                    vb, va = sb, sa
                else:
                    vb, va = (vb if vb is not None else sb), (va if va is not None else sa)
                    same = False
            row[name + "_before"], row[name + "_after"] = vb, va
            both = same and vb is not None and va is not None
            row[name + "_delta"] = round(va - vb, 3) if both else None
        fb, fa = b.get("fullscreen"), a.get("fullscreen")
        row["fullscreen_before"] = ",".join(fb) if fb else None
        row["fullscreen_after"] = ",".join(fa) if fa else None
        # Windowed and fullscreen present differently (size, compositor),
        # so their numbers do not compare directly.
        row["present_differs"] = bool(fb and fa and fb != fa)
        out.append(row)
    return out


def compare_columns():
    cols = ["host", "arm", "prof"]
    for tag in ("before", "after"):
        cols += ["n_" + tag, "good_" + tag, "bad_" + tag]
    for name, _ in COMPARE_STATS:
        cols += [name + "_before", name + "_after", name + "_delta"]
    return cols + ["fullscreen_before", "fullscreen_after", "present_differs"]


def compare_table(rows, before="before", after="after"):
    """A Markdown table per host: each cell "before -> after (change)"."""

    def num(v):
        return "-" if v is None else ("%g" % v)

    def cell(r, name):
        b, a, d = (r[name + x] for x in ("_before", "_after", "_delta"))
        if b is None and a is None:
            return ""
        return "%s -> %s" % (num(b), num(a)) + (" (%+g)" % d if d is not None else "")

    def counts(r, tag):
        if r["n_" + tag] is None:
            return "-"
        return "%s (%s/%s)" % (r["n_" + tag], r["good_" + tag], r["bad_" + tag])

    heads = ["arm", "prof", "n (good/bad)"] + [n for n, _ in COMPARE_STATS]
    lines = ["before: %s" % before, "after: %s" % after]
    for host in sorted({str(r["host"]) for r in rows}):
        lines += ["", "### %s" % host, "", "| " + " | ".join(heads) + " |"]
        lines.append("|" + "---|" * len(heads))
        for r in (x for x in rows if str(x["host"]) == host):
            cells = [
                r["arm"],
                str(r["prof"]),
                "%s -> %s" % (counts(r, "before"), counts(r, "after")),
            ]
            cells += [cell(r, n) for n, _ in COMPARE_STATS]
            lines.append("| " + " | ".join(cells) + " |")
    differ = [r for r in rows if r.get("present_differs")]
    if differ:
        lines += [
            "",
            "Windowed against fullscreen (not directly comparable): %s"
            % ", ".join("%s/%s/%s" % (r["host"], r["arm"], r["prof"]) for r in differ),
        ]
    return "\n".join(lines) + "\n"


def write_compare(rows, out_dir, before, after):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "compare.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=compare_columns(), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out_dir, "compare.md"), "w") as f:
        f.write(compare_table(rows, before, after))


# --- hosts -------------------------------------------------------------------


def busy_processes(ps_text, ignore=(), skip_pids=None):
    """[(pct, command)] above BUSY_PCT from "ps -Ao pcpu,comm" output, or
    from "ps -Ao pid,pcpu,comm" when skip_pids is given: those (the game
    itself) are skipped by id, so a second copy of the game still counts."""
    out = []
    for line in ps_text.splitlines()[1:]:
        if skip_pids is not None:
            p = line.strip().split(None, 2)
            if len(p) != 3 or p[0] in skip_pids:
                continue
            p = p[1:]
        else:
            p = line.strip().split(None, 1)
        if len(p) != 2:
            continue
        try:
            v = float(p[0])
        except ValueError:
            continue
        if v >= BUSY_PCT and not any(i in p[1] for i in ignore):
            out.append((v, p[1]))
    return out


# Commands that mean a build or another bench is going on this machine,
# whatever their CPU use at the instant ps looks (a link between compiles,
# a game waiting on its lock).
BUILD_COMMANDS = (
    "ninja",
    "make",
    "gmake",
    "cmake",
    "clang",
    "clang++",
    "cc1",
    "ld",
    "ld64",
    "xcodebuild",
    "swift-frontend",
    "go",
    "cargo",
    "rustc",
)


def build_jobs(ps_text, own=()):
    """The BUILD_COMMANDS and own (the game's exe: another bench) running,
    from "ps -Ao comm" output."""
    names = set(BUILD_COMMANDS) | set(own)
    return sorted({os.path.basename(c.strip()) for c in ps_text.splitlines()[1:]} & names)


def wait_builds(own=(), limit_s=QUIET_WAIT_S, step_s=30, log=print):
    """Waits while a build or another bench runs; (waited s, jobs at the end)."""
    t0 = time.monotonic()
    while True:
        r = subprocess.run(["ps", "-Ao", "comm"], capture_output=True, text=True)
        jobs = build_jobs(r.stdout, own)
        waited = time.monotonic() - t0
        if not jobs or waited >= limit_s:
            return round(waited), jobs
        log("build or bench running: %s; waiting (%d s so far)" % (jobs[:3], waited))
        time.sleep(step_s)


class Locks:
    """Locks a measured run holds from before its quiet checks to the
    game's end, so nothing the checks waited out can start in between."""

    def __init__(self):
        self.fds, self.holder, self.held = [], None, []

    def take_file(self, path, wait_s=3600, step_s=1.0):
        """flock(2) on path: what BSD lockf(1) and util-linux flock(1) take,
        so shell users of the same file queue with this run. lockf(1)
        removes its file when done, so a lock on a file no longer at path
        is no lock: look again."""
        t0 = time.monotonic()
        while True:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    same = os.stat(path).st_ino == os.fstat(fd).st_ino
                except FileNotFoundError:
                    same = False
                if same:
                    self.fds.append(fd)
                    self.held.append(path)
                    return
            except BlockingIOError:
                pass
            os.close(fd)
            if time.monotonic() - t0 >= wait_s:
                raise MatrixError("lock %s not granted in %d s" % (path, wait_s))
            time.sleep(step_s)

    def take_wrapper(self, wrapper):
        """wrapper is a command prefix that runs its command under a lock
        of its own (a queue such as mac-run-lock.sh, not a lock file): it
        runs a child that says so and then waits on its stdin, held until
        release closes that."""
        cmd = list(wrapper) + ["sh", "-c", "echo held; exec cat >/dev/null"]
        h = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        if h.stdout.readline().strip() != "held":
            h.stdin.close()
            raise MatrixError("run wrapper %s: no lock (rc %s)" % (wrapper, h.wait()))
        self.holder = h
        self.held.append(" ".join(map(str, wrapper)))

    def release(self):
        if self.holder:
            self.holder.stdin.close()
            try:
                self.holder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                kill_group(self.holder)
            self.holder = None
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []


def kill_group(proc, grace_s=5):
    """Ends proc and everything it started (its own session): TERM, then
    KILL after grace_s, so no child is left running or holding a lock."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue


def wait_quiet(ps_cmd, ignore=(), limit_s=QUIET_WAIT_S, step_s=30, log=print):
    """Waits while another process is busy; (waited s, busy at the end)."""
    t0 = time.monotonic()
    while True:
        r = subprocess.run(ps_cmd, capture_output=True, text=True)
        busy = busy_processes(r.stdout, ignore)
        waited = time.monotonic() - t0
        if not busy or waited >= limit_s:
            return round(waited), busy
        log("busy host: %s; waiting (%d s so far)" % (busy[:3], waited))
        time.sleep(step_s)


class BusyWatch(threading.Thread):
    """Samples ps every step_s while a measured run goes: another process
    that turns busy mid-run (an IDE indexing, a build) shows in the run's
    numbers, and the quiet check before the start cannot see it."""

    def __init__(self, ps_cmd, ignore=(), step_s=10, skip_pids=None):
        super().__init__(daemon=True)
        self.ps_cmd, self.ignore, self.step_s = ps_cmd, ignore, step_s
        self.skip_pids = skip_pids
        self.hits, self.stop = [], threading.Event()
        self.t0 = time.monotonic()

    def run(self):
        while not self.stop.wait(self.step_s):
            r = subprocess.run(self.ps_cmd, capture_output=True, text=True)
            t = round(time.monotonic() - self.t0)
            self.hits += [
                [t, v, c] for v, c in busy_processes(r.stdout, self.ignore, self.skip_pids)
            ]


def benches_line(path, text):
    if not path:
        return
    with open(path, "a") as f:
        f.write("- %s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), text))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def run_local(scene, host, local, arm, out, prof, benches, rep_n=None, order=None, extra=None):
    """This machine (the Mac): fresh hdd copy, the arm's config, locks.
    The Mac also collects every host's data, so its runs come last and it
    must be quiet. The run takes its locks first: the bench lock, the
    local TOML's quiet_locks (lock files, none by default) and run_wrapper
    (a command prefix holding a lock of its own, such as a run queue).
    Inside them it waits out builds and busy processes, then runs the game
    in its own session, killed whole at the cap."""
    exe = expand(local["exe"])
    seed = expand(local["hdd_seed"])
    os.makedirs(out)
    hdd = os.path.join(out, "hdd")
    clone = ["cp", "-c", "-R"] if sys.platform == "darwin" else ["cp", "-a"]
    subprocess.run(clone + [seed, hdd], check=True)
    cfg = os.path.join(out, "config.toml")
    with open(cfg, "w") as f:
        f.write(config_toml(arm["config"]))
    env = run_env(scene, arm, prof, os.path.join(out, "prof.txt"), host)
    env.update({str(k): str(v) for k, v in host.get("env", {}).items()})
    env.update(
        {
            "RECOMP_PB_BACKEND": host["backend"],
            "RECOMP_GAME_FILES": expand(local["game_files"]),
            "RECOMP_HDD_DIR": hdd,
            # Every runner gives a run its own empty save folder, so no arm
            # loads a save another arm wrote (that would change the path
            # to the scene).
            "RECOMP_SAVE_DIR": os.path.join(out, "save"),
            "RECOMP_CONFIG": cfg,
        }
    )
    full = {k: v for k, v in os.environ.items() if not k.startswith("RECOMP_")}
    full.update(env)
    lock = expand(local.get("lock", "~/.recomp-mac.lock"))
    files = [lock] + [x for x in map(expand, local.get("quiet_locks", [])) if x != lock]
    wrapper = [expand(x) for x in local.get("run_wrapper", [])]
    cap = scene["scene"]["run_s"] + LOCAL_CAP_EXTRA_S
    os.makedirs(os.path.join(out, "save"))
    ignore = quiet_ignore(scene, host, os.path.basename(exe))
    # Before the locks and the quiet wait: none of it is measured.
    hw = capture_hw_local(host)
    locks = Locks()
    try:
        for x in files:
            locks.take_file(x)
        if wrapper:
            locks.take_wrapper(wrapper)
        waited_b, jobs = wait_builds(own=(os.path.basename(exe),))
        waited, busy = wait_quiet(["ps", "-Ao", "pcpu,comm"], ignore=ignore)
        info = base_info(scene, host, arm, prof, exe, env, waited_b + waited, busy, rep_n, order)
        info["builds_at_start"] = jobs
        info["hw"] = hw
        info.update(extra or {})
        # Only what this run holds, in the order taken.
        info["locks"] = list(locks.held)
        benches_line(
            benches,
            "%s: bench-matrix %s %s run start (measured; host quiet)"
            % (host["_name"], scene["scene"]["name"], arm["id"]),
        )
        info["started"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(out, "game.log"), "w") as log:
            proc = subprocess.Popen(
                [exe],
                cwd=out,
                env=full,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            watch = BusyWatch(
                ["ps", "-Ao", "pid,pcpu,comm"], ignore=ignore, skip_pids={str(proc.pid)}
            )
            watch.start()
            try:
                info["rc"] = proc.wait(timeout=cap)
            except subprocess.TimeoutExpired:
                kill_group(proc)
                info["rc"] = "killed"
            except BaseException:
                kill_group(proc)
                raise
            finally:
                watch.stop.set()
                watch.join()
    finally:
        locks.release()
    # [s after start, %cpu, command] for each busy sample.
    info["busy_during"] = watch.hits
    info["ended"] = datetime.datetime.now().isoformat(timespec="seconds")
    benches_line(
        benches,
        "%s: bench-matrix %s %s run end rc=%s%s"
        % (
            host["_name"],
            scene["scene"]["name"],
            arm["id"],
            info["rc"],
            " (busy mid-run: %s)" % sorted({c for _, _, c in watch.hits})[:3] if watch.hits else "",
        ),
    )
    info["symbols"] = expand(local.get("symbols", exe))
    write_info(out, info)
    shutil.rmtree(hdd, ignore_errors=True)


def capture_hw_local(host):
    """This machine's hw block; a failed capture leaves it out, never the
    run."""
    try:
        if sys.platform == "darwin":
            return bench_hw.capture_macos(backend=host.get("backend"))
        return bench_hw.capture_linux(
            lambda script: bench_hw.local_run(["sh", "-c", script]), host.get("backend")
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def capture_hw_ssh(ssh, host, proton=None):
    def run(script):
        try:
            r = subprocess.run(
                ["ssh", ssh, "sh -s"], input=script, capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout if r.returncode == 0 else ""

    try:
        return bench_hw.capture_linux(run, host.get("backend"), proton=proton)
    except (OSError, ValueError):
        return None


def run_build(a, host, local):
    """The build block of the exe a run uses: its payload manifest, else
    the flags. None refuses the run: a result must say what it measured."""
    if host.get("kind") == "local":
        exe = expand(local.get("exe", ""))
    else:
        # The remote exe is a copy of the local one the profile is
        # symbolised with.
        exe = expand(local.get("symbols", ""))
    b = bench_hw.build_block(
        bench_hw.find_manifest(exe) if exe else None,
        a.build_version,
        a.game_sha,
        a.toolkit_sha,
        sha256(exe) if exe and os.path.isfile(exe) else None,
    )
    if not b:
        raise MatrixError(
            "%s: no manifest.json beside the exe (%s): pass --build-version, and "
            "--game-sha and --toolkit-sha" % (host["_name"], exe or "none set")
        )
    if not b.get("game_sha") or not b.get("toolkit_sha"):
        raise MatrixError(
            "%s: the build has no game or toolkit sha: pass --game-sha and --toolkit-sha"
            % host["_name"]
        )
    return b


def base_info(scene, host, arm, prof, exe, env, waited, busy, rep_n=None, order=None):
    return {
        # Which repeat of the arm this is (run --rep): analyse groups them.
        "rep": rep_n,
        # Where the run sat in its series (series: index, of, order, the
        # host order, the run before it); None for a lone `run`.
        "run_order": order,
        "scene": scene,
        "scene_name": scene["scene"]["name"],
        "host": host["_name"],
        "backend": host["backend"],
        "arm": arm["id"] + ("" if prof else "-noprof"),
        "values": arm["values"],
        "prof": prof,
        "exe": exe,
        "exe_sha256": sha256(exe) if os.path.isfile(exe) else None,
        "env": env,
        "config": arm["config"],
        "quiet_wait_s": waited,
        "busy_at_start": busy,
        # Borderless desktop fullscreen (run_env), or what the arm set.
        "present_fullscreen": env.get("RECOMP_PRESENT_FULLSCREEN"),
    }


def write_info(out, info):
    with open(os.path.join(out, "run-info.json"), "w") as f:
        json.dump(info, f, indent=1)
        f.write("\n")


# The Deck's GPU sampler: CLOCK_MONOTONIC and _RAW both, since Wine's QPC
# follows one of them (collect picks the one the flips match).
GPU_SAMPLER = r"""
import glob, sys, time
dev = sorted(glob.glob("/sys/class/drm/card*/device/pp_dpm_sclk"))[0].rsplit("/", 1)[0]
out = open(sys.argv[1], "w", buffering=1)
period = float(sys.argv[2]) / 1000.0
while True:
    try:
        lv = [l for l in open(dev + "/pp_dpm_sclk") if "*" in l]
        mhz = int(lv[0].split(":")[1].strip().split("M")[0]) if lv else -1
        busy = int(open(dev + "/gpu_busy_percent").read().strip())
    except (OSError, ValueError, IndexError):
        mhz, busy = -1, -1
    m = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    r = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    out.write("%d %d %d %d\n" % (m, r, mhz, busy))
    time.sleep(period)
"""

DECK_RUN = r"""set -u
R=$1; CAP=$2
B=$(dirname "$(dirname "$R")")
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
eval "$(systemctl --user show-environment 2>/dev/null | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY)=' | sed 's/^/export /')" || true
export DISPLAY="${DISPLAY:-:0}"
cd "$R"
GS=; CS=
# Whatever ends this script (the run's end, a kill, a signal), the samplers
# stop with it and flush what they have. ssh runs it without a pty, so a
# dropped ssh sends no HUP, and bash runs a trap only once the foreground
# timeout returns: at the latest the cap ends the game and with it this.
trap 'kill $GS $CS 2>/dev/null' EXIT
trap 'exit 130' HUP INT TERM
python3 "$B/gpu_sampler.py" "$R/gpu.tsv" %(period)d & GS=$!
if [ %(cpu_period)d -gt 0 ]; then python3 "$B/cpu_sampler.py" "$R" %(exe_name)s %(cpu_period)d %(busy_pct)g %(ignore)s & CS=$!; fi
date -Is > "$R/started.txt"
timeout -k 10 -s INT "$CAP" umu-run "$EXE" </dev/null >"$R/umu.log" 2>&1
echo "rc=$?" > "$R/rc.txt"
date -Is >> "$R/rc.txt"
kill $GS $CS 2>/dev/null; GS=; CS=
sleep 3
"$PROTONPATH/files/bin/wineserver" -k 2>/dev/null || true
"""


def quiet_ignore(scene, host, exe_name):
    """The processes the quiet checks do not count: the scene's and the
    host's quiet_ignore, plus what runs the game under Proton. Never the
    game's own name: the mid-run checks skip the game by id, so a second
    copy of it (a run left over, another agent's) counts as busy, and
    one running before the start holds the start."""
    stem = os.path.splitext(exe_name or "")[0].lower()
    names = tuple(scene["scene"].get("quiet_ignore", ())) + tuple(host.get("quiet_ignore", ()))
    return tuple(x for x in names if x.lower() != stem) + ("wineserver", "umu-run")


def run_proton(
    scene, host, local, arm, out, prof, benches, exe_name=None, rep_n=None, order=None, extra=None
):
    """A SteamOS box over ssh: the Windows exe under umu-run, flock, GPU
    sampled. Only the logs come back; the hdd copy stays there."""
    ssh = local["ssh"]
    rdir = local["dir"]
    rrun = "%s/runs/%s" % (rdir, os.path.basename(os.path.normpath(out)))
    os.makedirs(out)
    cfg = config_toml(arm["config"])
    win = lambda p: "Z:" + p  # noqa: E731

    def sh(cmd, **kw):
        return subprocess.run(["ssh", ssh, cmd], capture_output=True, text=True, **kw)

    home = sh("echo $HOME").stdout.strip()
    rd = rdir.replace("~", home, 1)
    rr = rrun.replace("~", home, 1)
    if not local.get("exe") and not exe_name:
        raise MatrixError("no exe: set exe in the local TOML's host table")
    exe = "%s/%s" % (rd, local.get("exe") or "exe/%s.exe" % exe_name)
    env = run_env(scene, arm, prof, win(rr + "/prof.txt"), host)
    env.update({str(k): str(v) for k, v in host.get("env", {}).items()})
    env.update(
        {
            "RECOMP_PB_BACKEND": host["backend"],
            "RECOMP_GAME_FILES": win(expand_remote(local["game_files"], home)),
            "RECOMP_HDD_DIR": win(rr + "/hdd"),
            "RECOMP_SAVE_DIR": win(rr + "/save"),
            "RECOMP_CONFIG": win(rr + "/config.toml"),
            "RECOMP_STDIO_LOG": win(rr + "/game-stdio.log"),
            "WINEPREFIX": "%s/prefix" % rd,
            "GAMEID": "umu-default",
            "UMU_RUNTIME_UPDATE": "0",
            "PROTONPATH": expand_remote(local["proton"], home),
            "EXE": exe,
        }
    )
    # Upload the sampler, the arm's config and the hdd copy before the lock:
    # none of it is measured.
    r = subprocess.run(
        ["ssh", ssh, "mkdir -p %s && cat > %s/gpu_sampler.py" % (shlex.quote(rr), shlex.quote(rd))],
        input=GPU_SAMPLER,
        text=True,
    )
    if r.returncode:
        raise MatrixError("ssh %s: cannot write the sampler" % ssh)
    r = subprocess.run(
        ["ssh", ssh, "cat > %s/cpu_sampler.py" % shlex.quote(rd)], input=CPU_SAMPLER, text=True
    )
    if r.returncode:
        raise MatrixError("ssh %s: cannot write the CPU sampler" % ssh)
    r = subprocess.run(["ssh", ssh, "cat > %s/config.toml" % shlex.quote(rr)], input=cfg, text=True)
    r = sh(
        "rm -rf {r}/hdd {r}/save && mkdir {r}/save"
        " && cp -a --sparse=always {d}/hdd-seed {r}/hdd".format(
            r=shlex.quote(rr), d=shlex.quote(rd)
        )
    )
    if r.returncode:
        raise MatrixError("ssh %s: hdd copy failed: %s" % (ssh, r.stderr.strip()))
    # The Proton build's folder name (GE-Proton10-25) names the translation.
    hw = capture_hw_ssh(ssh, host, local.get("proton"))
    t0 = time.monotonic()
    while True:
        busy = busy_processes(
            sh("ps -Ao pcpu,comm").stdout,
            quiet_ignore(scene, host, os.path.basename(exe)),
        )
        waited = time.monotonic() - t0
        if not busy or waited >= QUIET_WAIT_S:
            break
        print("busy %s: %s; waiting (%d s)" % (ssh, busy[:3], waited))
        time.sleep(30)
    info = base_info(
        scene,
        host,
        arm,
        prof,
        expand(local.get("symbols", "")),
        env,
        round(waited),
        busy,
        rep_n,
        order,
    )
    info["remote_exe"] = exe
    info["remote_dir"] = rr
    info["hw"] = hw
    info.update(extra or {})
    script = "".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in env.items())
    script += DECK_RUN % {
        "period": GPU_PERIOD_MS,
        "cpu_period": int(host.get("cpu_period_ms", CPU_PERIOD_MS)),
        "exe_name": shlex.quote(os.path.basename(exe)),
        "busy_pct": BUSY_PCT,
        "ignore": shlex.quote(",".join(quiet_ignore(scene, host, os.path.basename(exe))) or "-"),
    }
    cap = scene["scene"]["run_s"] + 60
    lock = local.get("lock", "~/.recomp-run.lock")
    benches_line(
        benches,
        "%s: bench-matrix %s %s run start (measured; host quiet)"
        % (host["_name"], scene["scene"]["name"], arm["id"]),
    )
    info["started"] = datetime.datetime.now().isoformat(timespec="seconds")
    remote = "cat > {r}/run.sh && flock -w 3600 {lock} bash {r}/run.sh {r} {cap}".format(
        r=shlex.quote(rr), lock=lock, cap=cap
    )
    try:
        r = subprocess.run(
            ["ssh", ssh, remote],
            input=script,
            text=True,
            capture_output=True,
            timeout=3600 + cap + 120,
        )
        info["ssh_rc"] = r.returncode
    except subprocess.TimeoutExpired:
        info["ssh_rc"] = "timeout"
    info["ended"] = datetime.datetime.now().isoformat(timespec="seconds")
    rc = sh("cat %s/rc.txt" % shlex.quote(rr)).stdout.split()
    info["rc"] = rc[0].split("=", 1)[1] if rc else None
    benches_line(
        benches,
        "%s: bench-matrix %s %s run end rc=%s"
        % (host["_name"], scene["scene"]["name"], arm["id"], info["rc"]),
    )
    for name in (
        "game-stdio.log",
        "gpu.tsv",
        "cpu.tsv",
        "threads.tsv",
        "cores.tsv",
        "busy.tsv",
        "prof.txt",
        "umu.log",
        "started.txt",
        "rc.txt",
        "config.toml",
    ):
        subprocess.run(["scp", "-q", "%s:%s/%s" % (ssh, rr, name), os.path.join(out, name)])
    info["symbols"] = expand(local.get("symbols", ""))
    write_info(out, info)


def expand_remote(p, home):
    return p.replace("~", home, 1) if p.startswith("~") else p


# --- the Windows runner ------------------------------------------------------

RUN_ARM_PS1 = r"""# bench-matrix: one measured run of an arm on this PC (written by
# `bench-matrix windows`; scene %(scene)s, host %(host)s). Usage, in the
# logged-on desktop:
#   powershell -ExecutionPolicy Bypass -File run-arm.ps1 -Arm <id> [-NoProf] [-Rep N] [-SkipDone]
# -SkipDone (what run-series.ps1 passes) keeps a run that has its
# run-info.json, so a series restarted after a stop resumes; without it the
# run is redone.
# Results in %(dir)s\runs\<id>[-noprof][-rN]\ (N above 1): game-stdio.log, prof.txt,
# gpu.csv (nvidia-smi, when present), cpu.csv, run-info.json. Copy that
# folder back under runs/<scene>/%(host)s/ keeping its name (the arm leads
# it, so the tree reads without opening run.json) and run `bench-matrix collect`.
# A run another process or window disturbed prints DISTURBED: run it again.
param([Parameter(Mandatory)][string]$Arm, [switch]$NoProf, [int]$Rep = 1, [switch]$SkipDone)
$S = '%(dir)s'
# PowerShell names are case-blind: $ArmDir must not share a name with any
# later variable (a quiet-loop "a" once overwrote the arm dir "A", and every
# run lost its arm).
$ArmDir = "$S\arms\$Arm"
if (-not (Test-Path "$ArmDir\env.txt") -or -not (Test-Path "$ArmDir\config.toml")) {
    Write-Host "no arm $Arm in $S\arms (env.txt and config.toml)"; exit 2
}
# The present rate follows the display's refresh; refuse below 60 Hz. An
# adapter with no display (a virtual one) reports 0 Hz and does not count.
$hz = (Get-CimInstance Win32_VideoController | Where-Object CurrentRefreshRate -gt 0 | Measure-Object CurrentRefreshRate -Minimum).Minimum
if (-not $hz -or $hz -lt 60) { Write-Host "display at $hz Hz: set 60 Hz or more first"; exit 1 }
# CPU use per process from the raw performance counters: Get-Process gives
# no CPU time for SYSTEM's or elevated processes (Defender, the indexer)
# unless this shell is elevated, so those never counted. Process time over
# the counter's own clock, both in 100 ns ticks, is cores in use.
$Ign = @('Idle', 'System', 'powershell', 'pwsh', 'WmiPrvSE'%(ignore)s)
function Get-CpuTicks {
    $ticks = @{}
    foreach ($c in Get-CimInstance Win32_PerfRawData_PerfProc_Process -ErrorAction SilentlyContinue) {
        if ($c.Name -eq '_Total' -or $c.IDProcess -eq 0) { continue }
        $ticks[[int]$c.IDProcess] = @([double]$c.PercentProcessorTime, [double]$c.Timestamp_Sys100NS, ($c.Name -replace '#\d+$', ''))
    }
    $ticks
}
# (name, percent of one core) for each process using more than %(busy_cores)g cores
# between two snapshots, by process id: a second copy of the game counts.
function Get-Busy($From, $To, $SkipId) {
    foreach ($id in $To.Keys) {
        $x = $From[$id]; $y = $To[$id]
        if (-not $x -or $id -eq $SkipId -or $y[2] -in $Ign) { continue }
        $span = $y[1] - $x[1]
        if ($span -le 0) { continue }
        $use = ($y[0] - $x[0]) / $span
        if ($use -gt %(busy_cores)g) { ,@($y[2], [math]::Round($use * 100, 1)) }
    }
}
# A measured run needs the PC quiet: wait (30 min at most) while another
# process is busy.
$waited = 0
while ($true) {
    $snap0 = Get-CpuTicks
    Start-Sleep -Seconds 5
    $busy = @(Get-Busy $snap0 (Get-CpuTicks) 0)
    if ($busy.Count -eq 0 -or $waited -ge 1800) { break }
    Write-Host "busy: $(($busy | ForEach-Object { $_[0] }) -join ', '); waiting"; Start-Sleep -Seconds 25; $waited += 30
}
# Windows throttles the presents of a window not in front to about one a
# second, so another window taking the foreground mid-run spoils it.
Add-Type -Namespace BenchMatrix -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
'@
$I = Join-Path $env:LOCALAPPDATA 'Programs\%(app)s'; $v = (Get-Content -Raw "$I\current.txt").Trim()
$exe = Join-Path "$I\versions\$v" '%(exe)s'
$name = if ($NoProf) { "$Arm-noprof" } else { $Arm }
$R = if ($Rep -gt 1) { "$S\runs\$name-r$Rep" } else { "$S\runs\$name" }
if ($SkipDone -and (Test-Path "$R\run-info.json")) { Write-Host "$R done, skipped"; exit 0 }
if (Test-Path $R) { Remove-Item -Recurse -Force $R -ErrorAction Stop }
New-Item -ItemType Directory "$R\save" -ErrorAction Stop | Out-Null
# The hdd seed holds sparse partition images (about 100 MB used of 5.3 GB)
# that NTFS copies stored in full. One work copy serves every arm: robocopy
# /MIR copies only what a run changed or added since, so the first arm pays
# the full copy and the next ones a few files, all before the game starts.
$H = "$S\hdd-work"
& robocopy "$S\hdd-seed" $H /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "hdd copy failed: robocopy $LASTEXITCODE"; exit 1 }
Copy-Item "$ArmDir\config.toml" "$R\config.toml" -ErrorAction Stop
Get-ChildItem env: | Where-Object { $_.Name -like 'RECOMP_*' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
# An empty value cannot be set (Set-Item removes the variable), so it is
# neither set nor checked; @R is the run dir, replaced as text, not a regex.
$want = @{}
foreach ($kv in Get-Content "$ArmDir\env.txt" -ErrorAction Stop) {
    if (-not $kv.Trim()) { continue }
    $k, $val = $kv -split '=', 2
    if (-not $val) { continue }
    $want[$k] = $val.Replace('@R', $R); Set-Item "env:$k" $want[$k]
}
if ($NoProf) { $env:RECOMP_TRACE = ($env:RECOMP_TRACE -split ',' | Where-Object { $_ -notlike 'prof*' }) -join ',' }
# Refuse to measure a run that did not get its arm: the config copied
# byte for byte, every env.txt key set (the trace aside under -NoProf), and
# the quit timer there, or the run is killed at the cap with stock settings.
$bad = @()
if ((Get-FileHash "$ArmDir\config.toml").Hash -ne (Get-FileHash "$R\config.toml").Hash) { $bad += 'config.toml' }
foreach ($k in $want.Keys) {
    if ($NoProf -and $k -eq 'RECOMP_TRACE') { continue }
    if ((Get-Item "env:$k" -ErrorAction SilentlyContinue).Value -cne $want[$k]) { $bad += $k }
}
if (-not $want.ContainsKey('RECOMP_WINDOW_QUIT_AFTER') -or -not $env:RECOMP_WINDOW_QUIT_AFTER) { $bad += 'RECOMP_WINDOW_QUIT_AFTER' }
if ($bad.Count) { Write-Host "arm $Arm not applied ($($bad -join ', ')): not running"; exit 3 }
$env:RECOMP_PB_BACKEND = '%(backend)s'
$env:RECOMP_GAME_FILES = "$I\game_files"
$env:RECOMP_HDD_DIR = $H
$env:RECOMP_SAVE_DIR = "$R\save"
$env:RECOMP_CONFIG = "$R\config.toml"
$env:RECOMP_STDIO_LOG = "$R\game-stdio.log"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
$gpu = $null
if ($smi) {
    $gpu = Start-Process $smi.Source -ArgumentList '--query-gpu=timestamp,clocks.gr,utilization.gpu', '--format=csv,noheader,nounits', '-lms', '%(period)d' -RedirectStandardOutput "$R\gpu.csv" -WindowStyle Hidden -PassThru
}
# QPC and wall time at one instant: the flips are QPC microseconds,
# nvidia-smi stamps wall time.
$qpc = [System.Diagnostics.Stopwatch]::GetTimestamp(); $wall = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
$qpc_us = [long]([double]$qpc * 1000000 / [System.Diagnostics.Stopwatch]::Frequency)
$t0 = Get-Date
$proc = Start-Process $exe -WorkingDirectory $R -PassThru
$null = $proc.Handle
$cpu = [System.Collections.Generic.List[string]]::new()
$cpu.Add('t,total_ms')
# [s after start, percent of one core, name] per busy process every 10 s, the
# game skipped by its id; [s after start, name] each time another window
# took the front after the game first had it.
$busyRun = [System.Collections.Generic.List[object]]::new()
$focusLost = [System.Collections.Generic.List[object]]::new()
$hadFocus = $false; $away = $false; $tick = 0; $snapPrev = Get-CpuTicks
$killed = $false
while (-not $proc.HasExited) {
    $el = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    try { $proc.Refresh(); $cpu.Add("$el,$([int]$proc.TotalProcessorTime.TotalMilliseconds)") } catch {}
    $fg = [uint32]0
    $null = [BenchMatrix.Win]::GetWindowThreadProcessId([BenchMatrix.Win]::GetForegroundWindow(), [ref]$fg)
    if ($fg -eq $proc.Id) { $hadFocus = $true; $away = $false }
    elseif ($hadFocus -and -not $away) {
        $away = $true; $focusLost.Add(@($el, "$((Get-Process -Id $fg -ErrorAction SilentlyContinue).ProcessName)"))
    }
    $tick += 1
    if ($tick %% 10 -eq 0) {
        $snapNow = Get-CpuTicks
        foreach ($b in @(Get-Busy $snapPrev $snapNow $proc.Id)) { $busyRun.Add(@($el, $b[1], $b[0])) }
        $snapPrev = $snapNow
    }
    if ($el -gt %(cap)d) { $killed = $true; & taskkill /F /T /PID $proc.Id | Out-Null; Start-Sleep 3; break }
    Start-Sleep -Milliseconds 1000
}
if (-not $hadFocus) { $focusLost.Add(@(0, 'never in front')) }
if ($gpu) { Stop-Process -Id $gpu.Id -ErrorAction SilentlyContinue }
$cpu | Set-Content "$R\cpu.csv"
$rc = if ($killed) { 'killed' } else { "$($proc.ExitCode)" }
$envs = @{}; Get-ChildItem env: | Where-Object { $_.Name -like 'RECOMP_*' } | ForEach-Object { $envs[$_.Name] = $_.Value }
# The hardware and build blocks (bench-report D5): named fields only, never
# the computer's name, a serial or a UUID; collect cleans the text again.
$cs = Get-CimInstance Win32_ComputerSystem; $os = Get-CimInstance Win32_OperatingSystem
$cpuI = Get-CimInstance Win32_Processor | Select-Object -First 1
$vc = @(Get-CimInstance Win32_VideoController | Where-Object CurrentRefreshRate -gt 0)
$nv = @(); if ($smi) { $nv = @((& $smi.Source --query-gpu=driver_version,clocks.max.gr --format=csv,noheader,nounits 2>$null | Select-Object -First 1) -split ',\s*') }
$scheme = $null; if ((powercfg /getactivescheme | Out-String) -match '\(([^)]*)\)') { $scheme = $Matches[1] }
$hwInfo = @{ schema = 1
  os = @{ name = $os.Caption; version = $os.Version; build = $os.BuildNumber }
  device = @{ model = "$($cs.Manufacturer) $($cs.Model)" }
  cpu = @{ model = $cpuI.Name; cores = $cpuI.NumberOfCores; threads = $cpuI.NumberOfLogicalProcessors; max_mhz = $cpuI.MaxClockSpeed }
  gpu = @($vc | ForEach-Object {
    $isNv = $nv.Count -ge 2 -and $_.Name -like '*NVIDIA*'
    @{ model = $_.Name; vendor = $_.AdapterCompatibility; driver = $(if ($isNv) { $nv[0] } else { $_.DriverVersion }); max_mhz = $(if ($isNv) { $nv[1] } else { $null }) } })
  ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
  display = @{ refresh_hz = $hz; width = $vc[0].CurrentHorizontalResolution; height = $vc[0].CurrentVerticalResolution }
  power = @{ source = $(if (Get-CimInstance Win32_Battery) { $null } else { 'ac' }); profile = $scheme }
  backend = @{ name = '%(backend)s' } }
# The build: the version folder's payload manifest names the shas.
$build = @{ version = $v }
$man = "$I\versions\$v\manifest.json"
if (Test-Path $man) {
    $mj = Get-Content -Raw $man | ConvertFrom-Json
    foreach ($p in $mj.sources.PSObject.Properties) {
        if ($p.Name -eq 'toolkit') { $build.toolkit_sha = $p.Value.commit }
        elseif ($p.Name -ne 'cli' -and -not $build.game_sha) { $build.game_sha = $p.Value.commit }
    }
}
@{ host = '%(host)s'; backend = '%(backend)s'; arm = $name; rep = $Rep; prof = (-not $NoProf); version = $v; rc = $rc;
   refresh_hz = $hz; present_fullscreen = $env:RECOMP_PRESENT_FULLSCREEN; quiet_wait_s = $waited; busy_at_start = $busy; busy_during = $busyRun; focus_lost = $focusLost;
   qpc_us = $qpc_us; unix_us = $wall * 1000; exe = (Split-Path -Leaf $exe);
   hw = $hwInfo; build = $build;
   started = $t0.ToString('s'); ended = (Get-Date).ToString('s'); env = $envs } | ConvertTo-Json -Depth 6 | Set-Content "$R\run-info.json"
Write-Host "$name (rep $Rep) rc=$rc -> $R"
if ($busyRun.Count -or $focusLost.Count) {
    Write-Host "DISTURBED: $($busyRun.Count) busy samples, focus lost $($focusLost.Count) times: run it again"
}
exit 0
"""

RUN_SERIES_PS1 = r"""# bench-matrix: a series of runs on this PC in order (written by
# `bench-matrix windows --series`; scene %(scene)s, order %(order)s). Each
# line is one run-arm.ps1 run; a setup error stops the series, and a
# restart skips the runs already done.
%(lines)s
Write-Host "series done"
"""


def ps_quote(s):
    """s inside a single-quoted PowerShell string."""
    return str(s).replace("'", "''")


def windows_host(scene, name=None):
    """The scene's PC host: the one named, else its only kind "windows"
    host, else the host called "windows"."""
    if name:
        return host_of(scene, name)
    pcs = [n for n, h in scene["hosts"].items() if h.get("kind") == "windows"]
    if len(pcs) == 1:
        return host_of(scene, pcs[0])
    if "windows" in scene["hosts"]:
        return host_of(scene, "windows")
    raise MatrixError("pass --host: the scene has %s kind windows hosts" % (len(pcs) or "no"))


def write_windows(
    scene, local, out, exe_name=None, host_name=None, series=None, order="interleave"
):
    """run-arm.ps1 and arms/<id>/{config.toml,env.txt} for the PC; with
    series (series_plan's list), run-series.ps1 too."""
    host = windows_host(scene, host_name)
    os.makedirs(os.path.join(out, "arms"), exist_ok=True)
    for arm in arms(scene, host["_name"]):
        d = os.path.join(out, "arms", arm["id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.toml"), "w") as f:
            f.write(config_toml(arm["config"]))
        env = run_env(scene, arm, True, "@R\\prof.txt", host)
        env.update({str(k): str(v) for k, v in host.get("env", {}).items()})
        with open(os.path.join(d, "env.txt"), "w", newline="\r\n") as f:
            f.write("".join("%s=%s\n" % kv for kv in env.items()))
    exe = local.get("exe_name") or (exe_name + ".exe" if exe_name else None)
    if not exe:
        raise MatrixError("no exe: set exe_name in the local TOML's [windows] table")
    # The quiet check before the start ignores these; the game's own name is
    # not among them, so a copy of it still running holds the start.
    stem = os.path.splitext(exe)[0].lower()
    ign = [
        x
        for x in tuple(scene["scene"].get("quiet_ignore", ())) + tuple(host.get("quiet_ignore", ()))
        if x.lower() != stem
    ]
    ps1 = RUN_ARM_PS1 % {
        "scene": scene["scene"]["name"],
        "host": ps_quote(host["_name"]),
        "dir": ps_quote(local["dir"]),
        "app": ps_quote(local["app"]),
        "exe": ps_quote(exe),
        "backend": ps_quote(host["backend"]),
        # Perf counter instances are named without .exe.
        "ignore": "".join(
            ", '%s'" % ps_quote(x[:-4] if x.lower().endswith(".exe") else x) for x in ign
        ),
        # BUSY_PCT is a share of one core; the script measures cores.
        "busy_cores": BUSY_PCT / 100,
        "period": GPU_PERIOD_MS,
        "cap": scene["scene"]["run_s"] + 90,
    }
    with open(os.path.join(out, "run-arm.ps1"), "w", newline="\r\n") as f:
        f.write(ps1)
    ids = [a["id"] for a in arms(scene, host["_name"])]
    if series is not None:
        unknown = sorted({a for a, _, _ in series} - set(ids))
        if unknown:
            raise MatrixError("no arm %s on %s" % (", ".join(unknown), host["_name"]))
        lines = [
            "& \"$PSScriptRoot\\run-arm.ps1\" -Arm '%s'%s -Rep %d -SkipDone; "
            "if ($LASTEXITCODE -in 1, 2) { exit $LASTEXITCODE }"
            % (ps_quote(a), "" if p else " -NoProf", rep)
            for a, p, rep in series
        ]
        with open(os.path.join(out, "run-series.ps1"), "w", newline="\r\n") as f:
            f.write(
                RUN_SERIES_PS1
                % {
                    "scene": scene["scene"]["name"],
                    "order": order,
                    "lines": "\n".join(lines),
                }
            )
    return ids


def windows_info(run_dir, scene, symbols_exe):
    """The PC's run-info.json lacks the scene and the axes: add them."""
    path = os.path.join(run_dir, "run-info.json")
    raw = read(path)
    if raw is None:
        raise MatrixError("%s: no run-info.json" % run_dir)
    info = json.loads(raw.lstrip("﻿"))
    arm_id = info.get("arm", os.path.basename(run_dir)).removesuffix("-noprof")
    host = info.get("host") or windows_host(scene)["_name"]
    a = find_arm(scene, host, arm_id)
    info.update(
        scene=scene, scene_name=scene["scene"]["name"], values=a["values"], config=a["config"]
    )
    if symbols_exe:
        info["symbols"] = symbols_exe
    write_info(run_dir, info)


# --- series ------------------------------------------------------------------


def series_plan(arm_ids, reps, first_rep=1, prof="both", order="interleave"):
    """[(arm, prof, rep)] in run order. blocks: every sampled run, then
    every unsampled one (as the first matrix ran). interleave: each arm's
    sampled and unsampled runs back to back, the side that goes first
    alternating, so a drift over the series (heat, a background job) falls
    on both sides alike."""
    sides = {"on": [True], "off": [False], "both": [True, False]}[prof]
    rep_range = range(first_rep, first_rep + reps)
    if order == "blocks":
        return [(a, p, r) for p in sides for r in rep_range for a in arm_ids]
    out = []
    for k, (r, a) in enumerate((r, a) for r in rep_range for a in arm_ids):
        out += [(a, p, r) for p in (sides if k % 2 == 0 else sides[::-1])]
    return out


# --- the command line --------------------------------------------------------


def host_of(scene, name):
    if name not in scene["hosts"]:
        raise MatrixError("no host %s in the scene" % name)
    return dict(scene["hosts"][name], _name=name)


# The machine that runs this CLI (kind "local") also pulls and analyses
# every host's data, so its arms go last, once the remote hosts are done;
# the remote hosts (the Deck, the PC) are separate machines and may overlap.
REMOTE_KINDS = ("proton", "windows")


def host_order(scene, names=None):
    """The hosts in run order: the remote ones as the scene lists them,
    then the local ones."""
    names = list(names or scene["hosts"])
    for n in names:
        host_of(scene, n)
    return sorted(names, key=lambda n: scene["hosts"][n].get("kind") not in REMOTE_KINDS)


def run_one(a, scene, host, local, arm, out, prof, rep, order=None):
    kind = host.get("kind")
    extra = {"build": run_build(a, host, local)} if kind in ("local", "proton") else None
    if kind == "local":
        run_local(scene, host, local, arm, out, prof, a.benches, rep, order, extra)
    elif kind == "proton":
        run_proton(scene, host, local, arm, out, prof, a.benches, a.exe_name, rep, order, extra)
    else:
        raise MatrixError(
            "host %s (kind %s) is not run from here; see `windows`" % (host["_name"], kind)
        )


def arm_list(scene, host, spec):
    ids = [x["id"] for x in arms(scene, host)]
    if not spec or spec == "all":
        return ids
    want = [x for x in spec.split(",") if x]
    for x in want:
        find_arm(scene, host, x)
    return want


def build_args(p):
    # A run says which build it measured (bench-report D5): the payload's
    # manifest.json beside the exe names it; these stand in for a build
    # without one (a dev build).
    p.add_argument("--build-version", help="the build's version, without a manifest.json")
    p.add_argument("--game-sha", help="the game repo's commit, without a manifest.json")
    p.add_argument("--toolkit-sha", help="the toolkit's commit, without a manifest.json")


def series_args(p):
    p.add_argument("--arms", help="comma-separated arm ids (default: all of the host's)")
    p.add_argument("--reps", type=int, default=1, help="repeats per arm and side")
    p.add_argument("--first-rep", type=int, default=1, help="the first repeat's number")
    p.add_argument("--prof", choices=("on", "off", "both"), default="both")
    p.add_argument("--order", choices=("interleave", "blocks"), default="interleave")


def main(argv=None, prog="bench-matrix", exe_name=None):
    """exe_name: the game's executable without extension (game.toml's
    build.exe), which the CLI passes; the scene and the local TOML can
    name others."""
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="list the arms, hosts in run order (the local one last)")
    p.add_argument("scene")
    p.add_argument("--host")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run", help="one measured run of an arm on a host")
    p.add_argument("scene")
    p.add_argument("--host", required=True)
    p.add_argument("--arm", required=True)
    p.add_argument("--out", required=True, help="the run's directory (must not exist)")
    p.add_argument("--local", required=True, help="this machine's paths (TOML)")
    p.add_argument(
        "--no-prof", action="store_true", help="without the sampler (its overhead check)"
    )
    p.add_argument("--benches", help="append start and end lines to this file")
    p.add_argument(
        "--rep", type=int, help="which repeat of the arm this is (else a -rN dir suffix, else 1)"
    )
    build_args(p)
    p = sub.add_parser(
        "series", help="many runs in order into ROOT/<host>/<run>/, each collected; skips done ones"
    )
    p.add_argument("scene")
    p.add_argument("--host", required=True, help="comma-separated; run remote hosts first")
    p.add_argument("--root", required=True, help="the matrix's result dir")
    p.add_argument("--local", required=True)
    p.add_argument("--benches")
    p.add_argument("--dry-run", action="store_true", help="print the runs, run none")
    series_args(p)
    build_args(p)
    p = sub.add_parser("collect", help="run.json for a run directory")
    p.add_argument("run_dir")
    p.add_argument("--scene", help="for a run whose run-info.json has none (the PC's)")
    p.add_argument("--exe", help="the exe to symbolise the profile with")
    p.add_argument("--nm")
    p = sub.add_parser("analyse", help="summary.json, summary.csv and summary-arms.csv")
    p.add_argument("root")
    p.add_argument("--out")
    p.add_argument(
        "--include-disturbed",
        action="store_true",
        help="count runs another process disturbed in the per-arm figures",
    )
    p.add_argument("--recollect", action="store_true", help="run collect again on every run first")
    p.add_argument("--scene", help="with --recollect: this scene in place of each run's")
    p.add_argument("--exe", help="with --recollect: the exe to symbolise the profiles with")
    p = sub.add_parser("compare", help="two matrix results side by side, per host and arm")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--out", help="write compare.csv and compare.md here")
    p.add_argument("--include-disturbed", action="store_true")
    p.add_argument("--recollect", action="store_true")
    p.add_argument("--scene")
    p.add_argument("--exe")
    p = sub.add_parser("windows", help="run-arm.ps1 and the arms for a Windows PC")
    p.add_argument("scene")
    p.add_argument("--local", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--host", help="the PC's host in the scene (default: its kind windows one)")
    p.add_argument("--series", action="store_true", help="also run-series.ps1, in order")
    series_args(p)
    p = sub.add_parser("report", help="the result's HTML report; --public: its README part")
    p.add_argument("result", help="the matrix's result dir (analysed)")
    p.add_argument("--scene", help="the scene (default: the newest run's)")
    p.add_argument("--baseline", help="an earlier result to compare with")
    p.add_argument("--out", help="the report's dir (default: RESULT/report)")
    p.add_argument(
        "--public",
        metavar="REPO",
        help="write the SVGs and the README part into this game repo, after the leak check",
    )
    a = ap.parse_args(argv)
    a.exe_name = exe_name
    a.prog = prog
    try:
        return dispatch(a)
    except MatrixError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2
    except CliError as e:
        # public.Cannot: no audit-private patterns, so the leak check
        # cannot run.
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2


def report_cmd(a):
    from . import bench_report as R
    from . import bench_report_html as HTML

    scene = R.find_scene(a.result, a.scene)
    model, s = R.build_model(a.result, scene, a.baseline)
    out = a.out or os.path.join(a.result, "report")
    R.write_json(os.path.join(out, "report.json"), model)
    page = os.path.join(out, "index.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(HTML.render(model, scene, s, a.result, game=a.prog.split()[0]))
    print(page)
    if not a.public:
        return 0
    from . import bench_report_public as P

    try:
        code, written = P.export(model, scene, a.public)
    except P.Refused as e:
        # A leak or a host without hw: refused like missing README markers
        # (1); 2 stays for a check that cannot run.
        print("%s: %s" % (a.prog, e), file=sys.stderr)
        return 1
    for w in written:
        print(w)
    return code


def local_table(path, host):
    local = load_toml(path).get(host)
    if not local:
        raise MatrixError("%s: no [%s] table" % (path, host))
    return local


def dispatch(a):
    if a.cmd == "report":
        return report_cmd(a)
    if a.cmd == "plan":
        scene = load_scene(a.scene)
        hosts = host_order(scene, [a.host] if a.host else None)
        plan = {h: [x["id"] for x in arms(scene, h)] for h in hosts}
        if a.json:
            print(json.dumps(plan, indent=1))
        else:
            for h, ids in plan.items():
                print("%s (%d): %s" % (h, len(ids), " ".join(ids)))
        return 0
    if a.cmd == "run":
        scene = load_scene(a.scene)
        host = host_of(scene, a.host)
        local = local_table(a.local, a.host)
        arm = find_arm(scene, a.host, a.arm)
        if os.path.exists(a.out):
            raise MatrixError("%s exists" % a.out)
        run_one(a, scene, host, local, arm, a.out, not a.no_prof, a.rep)
        r = collect(a.out)
        print(table([summary_row(r, a.out)]))
        return 0
    if a.cmd == "series":
        scene = load_scene(a.scene)
        hosts = host_order(scene, [h for h in a.host.split(",") if h])
        for h in hosts:
            if scene["hosts"][h].get("kind") == "windows":
                raise MatrixError("host %s is not run from here; see `windows --series`" % h)
        todo = []
        for h in hosts:
            plan = series_plan(arm_list(scene, h, a.arms), a.reps, a.first_rep, a.prof, a.order)
            todo += [(h, arm, p, rep) for arm, p, rep in plan]
        prev, failed = None, []
        for i, (h, arm_id, p, rep) in enumerate(todo):
            out = os.path.join(a.root, h, run_name(arm_id, p, rep))
            # Done means collected: a run that died (ssh, a lock not
            # granted, Ctrl-C) leaves its folder without run.json, and is
            # moved aside and run again.
            if os.path.exists(os.path.join(out, "run.json")):
                print("%d/%d %s: done, skipped" % (i + 1, len(todo), out))
                continue
            if os.path.exists(out):
                aside = "%s.failed-%s" % (out, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
                print("%d/%d %s: not collected; %s" % (i + 1, len(todo), out, aside))
                if a.dry_run:
                    continue
                os.rename(out, aside)
            else:
                print("%d/%d %s" % (i + 1, len(todo), out))
            if a.dry_run:
                continue
            order = {
                "index": i + 1,
                "of": len(todo),
                "order": a.order,
                "hosts": hosts,
                "previous": prev,
            }
            try:
                run_one(
                    a,
                    scene,
                    host_of(scene, h),
                    local_table(a.local, h),
                    find_arm(scene, h, arm_id),
                    out,
                    p,
                    rep,
                    order,
                )
                prev = os.path.join(h, os.path.basename(out))
                print(table([summary_row(collect(out), out)]))
            except (MatrixError, OSError, subprocess.SubprocessError) as e:
                # One failed run does not end the series; the next series
                # runs it again.
                print("%s: failed: %s" % (out, e))
                failed.append(out)
        if failed:
            print("%d run(s) failed, run the series again: %s" % (len(failed), failed))
            return 1
        return 0
    if a.cmd == "collect":
        scene = load_scene(a.scene) if a.scene else None
        if scene and "scene" not in json.loads(
            (read(os.path.join(a.run_dir, "run-info.json")) or "{}").lstrip("﻿")
        ):
            windows_info(a.run_dir, scene, a.exe)
            scene = None
        r = collect(a.run_dir, scene, a.exe, a.nm)
        print(table([summary_row(r, a.run_dir)]))
        return 0
    if a.cmd == "analyse":
        if (a.scene or a.exe) and not a.recollect:
            print("--scene and --exe apply with --recollect only; ignored", file=sys.stderr)
        scene = load_scene(a.scene) if a.scene else None
        rows, overhead, arms_ = analyse(
            a.root, a.out, a.include_disturbed, a.recollect, scene, a.exe
        )
        print(table(rows))
        print()
        print(arms_table(arms_))
        for o in overhead:
            line = "sampler cost on %(host)s %(arm)s at %(prof)d Hz (steady fps, medians): " % o
            line += "%s -> %s (%s%%)" % (o.get("fps_off"), o.get("fps_on"), o.get("cost_pct"))
            if "cost_pct_good" in o:
                line += "; good runs only %s%%" % o["cost_pct_good"]
            print(line)
        for d, want in misnamed(rows):
            print("misnamed: %s holds %s (rename it so the tree reads)" % (d, want))
        return 0
    if a.cmd == "compare":
        if (a.scene or a.exe) and not a.recollect:
            print("--scene and --exe apply with --recollect only; ignored", file=sys.stderr)
        scene = load_scene(a.scene) if a.scene else None
        rows = compare(a.before, a.after, a.include_disturbed, a.recollect, scene, a.exe)
        if a.out:
            write_compare(rows, a.out, a.before, a.after)
        print(compare_table(rows, a.before, a.after), end="")
        return 0
    if a.cmd == "windows":
        scene = load_scene(a.scene)
        host = windows_host(scene, a.host)
        local = local_table(a.local, host["_name"])
        series = None
        if a.series:
            series = series_plan(
                arm_list(scene, host["_name"], a.arms), a.reps, a.first_rep, a.prof, a.order
            )
        ids = write_windows(scene, local, a.out, a.exe_name, host["_name"], series, a.order)
        print(
            "%s: run-arm.ps1 and %d arms%s"
            % (a.out, len(ids), ", run-series.ps1 (%d runs)" % len(series) if series else "")
        )
        return 0
    raise AssertionError(a.cmd)


if __name__ == "__main__":
    sys.exit(main())
