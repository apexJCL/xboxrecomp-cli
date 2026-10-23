"""A benchmark matrix: one scene, every combination of a few settings, on
several hosts, measured the same way everywhere.

  <game> bench-matrix plan SCENE [--host H] [--json]
  <game> bench-matrix run SCENE --host H --arm ID --out DIR --local LOCAL
                          [--no-prof] [--rep N] [--benches FILE]
  <game> bench-matrix collect RUN_DIR [--scene SCENE] [--exe EXE]
  <game> bench-matrix analyse ROOT [--out DIR] [--include-disturbed]
  <game> bench-matrix windows SCENE --local LOCAL --out DIR

SCENE is the game's TOML (its config, never this CLI's): how long to run,
how the scene's start is found from the flip trace, the axes and how each
maps onto config.toml or the environment, and which hosts run which arms.
LOCAL is the machine's own TOML (paths, ssh names, locks); it is not
tracked. An arm is one value per axis, named by the scene's arm_id.

run takes the host's lock, waits (30 min at most) for the host to be quiet,
starts the game from a fresh copy of the hdd seed with the arm's config,
and collects the run into DIR/run.json:
  - host "local": this machine (the Mac); lockf on the lock file.
  - host "proton": a SteamOS box over ssh, the Windows exe under umu-run
    and Proton, flock on the lock file, the GPU clock and busy sampled
    every 250 ms from sysfs, the CPU cores' clocks and the game's threads
    (load, last core) every second from /proc.
  - host "windows" is not run from here: windows writes run-arm.ps1 and the
    arms, for whoever drives that PC; collect reads what it brings back.

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
import glob
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
import tomllib

PACING_FLIP_RE = re.compile(r"^\[PACING\] flip (\d+) t_us (\d+) batches (\d+)")

# A process above this share of one core makes the host busy for a
# measured run; the game itself and the tools of the run are not counted.
BUSY_PCT = 50.0
QUIET_WAIT_S = 30 * 60
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


# The Deck's CPU sampler (1 Hz): each core's clock, and where each of the
# game's threads last ran with its CPU time so far. On 4 cores / 8 threads
# two spinning guest threads plus Wine's own can land on one core, which
# would show as a slow run that no setting explains.
CPU_SAMPLER = r"""
import glob, os, re, sys, time
d, name, period = sys.argv[1], sys.argv[2].lower(), float(sys.argv[3]) / 1000.0
num = lambda p: int(re.search(r"cpu(\d+)/", p).group(1))
cores = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"), key=num)
cpu = open(d + "/cpu.tsv", "w", buffering=1)
thr = open(d + "/threads.tsv", "w", buffering=1)
pid = None
me = "/proc/%d" % os.getpid()
# The game is the process with the most threads whose command line names
# the exe. This sampler's own command line names it too, and so do
# timeout and umu-run, so look again every few seconds until Wine's
# process, with its many threads, is up.
def find():
    best = None
    for p in glob.glob("/proc/[0-9]*"):
        if p == me:
            continue
        try:
            if name in open(p + "/cmdline", "rb").read().decode(errors="replace").lower():
                n = len(os.listdir(p + "/task"))
                if not best or n > best[0]:
                    best = (n, p)
        except OSError:
            pass
    return best[1] if best else None
tick = -1
while True:
    m = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    r = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    f = []
    for c in cores:
        try:
            f.append(open(c).read().strip())
        except OSError:
            f.append("-1")
    cpu.write("%d %d %s\n" % (m, r, " ".join(f)))
    tick += 1
    if pid is None or not os.path.isdir(pid) or tick % 5 == 0:
        pid = find()
    for t in glob.glob(pid + "/task/*") if pid else []:
        try:
            s = open(t + "/stat").read()
        except OSError:
            continue
        comm, rest = s[s.index("(") + 1 : s.rindex(")")], s[s.rindex(")") + 2 :].split()
        thr.write("%d %d %s %s %d %s\n" % (m, r, t.rsplit("/", 1)[1], rest[36],
                  int(rest[11]) + int(rest[12]), comm.replace(" ", "_") or "-"))
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
    """ "mono_us raw_us tid core ticks comm" -> [(mono, raw, tid, core,
    ticks, comm)]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) == 6 and p[0].isdigit():
            out.append((int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]), p[5]))
    return out


def cpu_stats(samples, threads, col, t0, duration_s, warmup_s, hot_pct=50.0):
    """The cores' clocks by part of the window, and the game's hot threads
    (over hot_pct of a core in the steady part): their load, the cores
    they ran on, and how often another hot thread was on the same core at
    the same sample."""
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
    hot = {}
    for tid, ts in by_tid.items():
        if len(ts) < 2:
            continue
        secs = (ts[-1][col] - ts[0][col]) / 1e6
        load = 100.0 * (ts[-1][4] - ts[0][4]) / CLK_TCK / secs if secs > 0 else 0.0
        if load >= hot_pct:
            hot[tid] = (load, ts)
    at = {}
    for tid, (_, ts) in hot.items():
        for t in ts:
            at.setdefault(t[col], {})[tid] = t[3]
    rows = []
    for tid, (load, ts) in sorted(hot.items(), key=lambda kv: -kv[1][0]):
        cores = {}
        shared = 0
        for t in ts:
            cores[t[3]] = cores.get(t[3], 0) + 1
            if any(o != tid and c == t[3] for o, c in at[t[col]].items()):
                shared += 1
        rows.append(
            {
                "tid": tid,
                "name": ts[-1][5],
                "load_pct": round(load, 1),
                "cores": dict(sorted(cores.items())),
                "shared_share": round(shared / len(ts), 3),
            }
        )
    out["hot_threads"] = rows
    return out


def parse_nvsmi(text):
    """nvidia-smi csv (timestamp, clocks.gr, utilization.gpu, no header or
    units) -> [(unix_us, mhz, busy)]."""
    out = []
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3:
            continue
        try:
            dt = datetime.datetime.strptime(p[0], "%Y/%m/%d %H:%M:%S.%f")
            out.append((int(dt.timestamp() * 1e6), int(float(p[1])), int(float(p[2]))))
        except ValueError:
            continue
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


# Where a thread blocked in a wait sits under Wine. A profile taken in
# mode "all" (Wine's thread cycle counter does not move, so every thread is
# sampled, waiting or not) is mostly these; the no-wait view leaves them out.
WAIT_MODULES = ("ntdll.dll", "win32u.dll")


def hot_paths(prof, exe=None, nm=None, top=25, run_exe=None):
    """The profile by function (the exe's) and by module (everything else),
    overall and for each named thread. exe is the symbol source; run_exe is
    the name the game ran under, when the symbol copy was renamed."""
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
    return {
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
        d = info["qpc_us"] - info["unix_us"]
        s = [(u + d, u + d, mhz, busy) for u, mhz, busy in parse_nvsmi(gpu_c)]
        rows, summ = gpu_stats(s, 0, t0, sc["duration_s"], warm)
        out["gpu"] = dict(summ, source="nvidia-smi", samples=rows)
    cpu_t = read(os.path.join(run_dir, "cpu.tsv"))
    if cpu_t:
        s = parse_cpu_tsv(cpu_t)
        col = gpu_clock_for([(x[0], x[1]) for x in s], [f[1] for f in flips])
        if col is not None:
            th = parse_threads_tsv(read(os.path.join(run_dir, "threads.tsv")) or "")
            out["cpu"] = cpu_stats(s, th, col, t0, sc["duration_s"], warm)
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
    "cpu_hot_threads",
    "cpu_shared_share",
    "top_function",
    "top_share",
    "busy_during",
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
    # Mode "all" samples waiting threads too, and a thread blocked outside
    # ntdll (audio in mmdevapi) still tops the no-wait view; the exe's own
    # hottest function is the useful column there.
    if (p.get("header") or {}).get("mode") == "all":
        fns = p.get("functions_no_wait") or []
        top = next((f for f in fns if not f["name"].startswith("[")), fns[0] if fns else {})
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
        "cpu_hot_threads": len(hot) if "hot_threads" in c else None,
        "cpu_shared_share": max((h["shared_share"] for h in hot), default=None),
        "top_function": top.get("name"),
        "top_share": top.get("share"),
        # Busy samples of other processes during the run (the Mac only).
        "busy_during": len(r.get("busy_during") or []),
    }
    # Another process busy mid-run may have cost frames: such a run stays
    # in the rows but out of the per-arm figures unless asked for.
    row["disturbed"] = row["busy_during"] > 0
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
    "cpu_shared_share",
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


def analyse(root, out_dir=None, include_disturbed=False):
    """summary.json (rows, arms, prof_overhead, runs), summary.csv (one row
    per run) and summary-arms.csv (one per arm) from
    ROOT/<host>/<run>/run.json."""
    out_dir = out_dir or root
    runs, rows = [], []
    for path in sorted(glob.glob(os.path.join(root, "*", "*", "run.json"))):
        with open(path) as f:
            r = json.load(f)
        runs.append(r)
        rows.append(summary_row(r, os.path.relpath(os.path.dirname(path), root)))
    if not runs:
        raise MatrixError("no run.json under %s/<host>/<arm>/" % root)
    axes = sorted({k for r in rows for k in r if k.startswith("axis_")})
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
                "runs": [summary_detail(r) for r in runs],
            },
            f,
            indent=1,
        )
        f.write("\n")
    cols = COLUMNS[:4] + axes + COLUMNS[4:]
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


# --- hosts -------------------------------------------------------------------


def busy_processes(ps_text, ignore=()):
    """[(pct, command)] above BUSY_PCT from "ps -Ao pcpu,comm" output."""
    out = []
    for line in ps_text.splitlines()[1:]:
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

    def __init__(self, ps_cmd, ignore=(), step_s=10):
        super().__init__(daemon=True)
        self.ps_cmd, self.ignore, self.step_s = ps_cmd, ignore, step_s
        self.hits, self.stop = [], threading.Event()
        self.t0 = time.monotonic()

    def run(self):
        while not self.stop.wait(self.step_s):
            r = subprocess.run(self.ps_cmd, capture_output=True, text=True)
            t = round(time.monotonic() - self.t0)
            self.hits += [[t, v, c] for v, c in busy_processes(r.stdout, self.ignore)]


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


def run_local(scene, host, local, arm, out, prof, benches, rep_n=None):
    """This machine (the Mac): fresh hdd copy, the arm's config, lockf."""
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
    cap = scene["scene"]["run_s"] + 120
    os.makedirs(os.path.join(out, "save"))
    ignore = quiet_ignore(scene, host, os.path.basename(exe))
    waited, busy = wait_quiet(["ps", "-Ao", "pcpu,comm"], ignore=ignore)
    info = base_info(scene, host, arm, prof, exe, env, waited, busy, rep_n)
    benches_line(
        benches,
        "%s: bench-matrix %s %s run start (measured; host quiet)"
        % (host["_name"], scene["scene"]["name"], arm["id"]),
    )
    info["started"] = datetime.datetime.now().isoformat(timespec="seconds")
    watch = BusyWatch(["ps", "-Ao", "pcpu,comm"], ignore=ignore)
    watch.start()
    with open(os.path.join(out, "game.log"), "w") as log:
        try:
            r = subprocess.run(
                ["lockf", "-t", "3600", lock, exe]
                if shutil.which("lockf")
                else ["flock", "-w", "3600", lock, exe],
                cwd=out,
                env=full,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=cap,
            )
            info["rc"] = r.returncode
        except subprocess.TimeoutExpired:
            info["rc"] = "killed"
    watch.stop.set()
    watch.join()
    # [s after start, %cpu, command] for each busy sample; the lock wait
    # counts too, so read it against "started".
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


def base_info(scene, host, arm, prof, exe, env, waited, busy, rep_n=None):
    return {
        # Which repeat of the arm this is (run --rep): analyse groups them.
        "rep": rep_n,
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
python3 "$B/gpu_sampler.py" "$R/gpu.tsv" %(period)d & GS=$!
CS=
if [ %(cpu_period)d -gt 0 ]; then python3 "$B/cpu_sampler.py" "$R" %(exe_name)s %(cpu_period)d & CS=$!; fi
date -Is > "$R/started.txt"
timeout -k 10 -s INT "$CAP" umu-run "$EXE" </dev/null >"$R/umu.log" 2>&1
echo "rc=$?" > "$R/rc.txt"
date -Is >> "$R/rc.txt"
kill $GS $CS 2>/dev/null
sleep 3
"$PROTONPATH/files/bin/wineserver" -k 2>/dev/null || true
"""


def quiet_ignore(scene, host, exe_name):
    """The processes the quiet check does not count: the scene's and the
    host's quiet_ignore (the game's own exe when the scene names none),
    plus what runs the game under Proton."""
    own = scene["scene"].get("quiet_ignore", [os.path.splitext(exe_name)[0]] if exe_name else [])
    return tuple(own) + tuple(host.get("quiet_ignore", [])) + ("wineserver", "umu-run")


def run_proton(scene, host, local, arm, out, prof, benches, exe_name=None, rep_n=None):
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
        scene, host, arm, prof, expand(local.get("symbols", "")), env, round(waited), busy, rep_n
    )
    info["remote_exe"] = exe
    info["remote_dir"] = rr
    script = "".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in env.items())
    script += DECK_RUN % {
        "period": GPU_PERIOD_MS,
        "cpu_period": int(host.get("cpu_period_ms", CPU_PERIOD_MS)),
        "exe_name": shlex.quote(os.path.basename(exe)),
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
# `bench-matrix windows`; scene %(scene)s). Usage, in the logged-on desktop:
#   powershell -ExecutionPolicy Bypass -File run-arm.ps1 -Arm <id> [-NoProf] [-Rep N]
# Results in %(dir)s\runs\<id>[-noprof][-rN]\ (N above 1): game-stdio.log, prof.txt,
# gpu.csv (nvidia-smi, when present), cpu.csv, run-info.json. Copy that
# folder back as runs/<scene>/windows/<id>/ and run `bench-matrix collect`.
param([Parameter(Mandatory)][string]$Arm, [switch]$NoProf, [int]$Rep = 1)
$S = '%(dir)s'
# PowerShell names are case-blind: $ArmDir must not share a name with any
# later variable (a quiet-loop "a" once overwrote the arm dir "A", and every
# run lost its arm).
$ArmDir = "$S\arms\$Arm"
if (-not (Test-Path "$ArmDir\env.txt") -or -not (Test-Path "$ArmDir\config.toml")) {
    Write-Host "no arm $Arm in $S\arms (env.txt and config.toml)"; exit 2
}
# The present rate follows the display's refresh; refuse below 60 Hz.
$hz = (Get-CimInstance Win32_VideoController | Measure-Object CurrentRefreshRate -Minimum).Minimum
if ($hz -lt 60) { Write-Host "display at $hz Hz: set 60 Hz or more first"; exit 1 }
# A measured run needs the PC quiet: wait (30 min at most) while another
# process uses more than half a core. Get-Process .CPU is CPU seconds, so
# its rise over the 5 s sleep, divided by 5, is cores in use.
$waited = 0
while ($true) {
    $before = Get-Process | Select-Object Id, ProcessName, CPU
    Start-Sleep -Seconds 5
    $after = Get-Process | Select-Object Id, ProcessName, CPU
    $busy = @(foreach ($p in $after) {
        $q = $before | Where-Object Id -eq $p.Id
        if ($q -and $p.CPU -and $q.CPU -and (($p.CPU - $q.CPU) / 5) -gt %(busy_cores)g -and $p.ProcessName -notin 'Idle', 'System', 'powershell', 'pwsh') { "$($p.ProcessName)" }
    })
    if ($busy.Count -eq 0 -or $waited -ge 1800) { break }
    Write-Host "busy: $($busy -join ', '); waiting"; Start-Sleep -Seconds 25; $waited += 30
}
$I = "$env:LOCALAPPDATA\Programs\%(app)s"; $v = (Get-Content "$I\current.txt").Trim()
$exe = "$I\versions\$v\%(exe)s"
$name = if ($NoProf) { "$Arm-noprof" } else { $Arm }
$R = if ($Rep -gt 1) { "$S\runs\$name-r$Rep" } else { "$S\runs\$name" }
Remove-Item -Recurse -Force $R -ErrorAction SilentlyContinue
New-Item -ItemType Directory "$R\save" | Out-Null
# The hdd seed holds sparse partition images (about 100 MB used of 5.3 GB)
# that NTFS copies stored in full. One work copy serves every arm: robocopy
# /MIR copies only what a run changed or added since, so the first arm pays
# the full copy and the next ones a few files, all before the game starts.
$H = "$S\hdd-work"
& robocopy "$S\hdd-seed" $H /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "hdd copy failed: robocopy $LASTEXITCODE"; exit 1 }
Copy-Item "$ArmDir\config.toml" "$R\config.toml" -ErrorAction Stop
Get-ChildItem env: | Where-Object { $_.Name -like 'RECOMP_*' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
$want = @{}
foreach ($kv in Get-Content "$ArmDir\env.txt" -ErrorAction Stop) {
    if (-not $kv.Trim()) { continue }
    $k, $val = $kv -split '=', 2; $want[$k] = $val -replace '@R', $R; Set-Item "env:$k" $want[$k]
}
if ($NoProf) { $env:RECOMP_TRACE = ($env:RECOMP_TRACE -split ',' | Where-Object { $_ -notlike 'prof*' }) -join ',' }
# Refuse to measure a run that did not get its arm: the config copied
# byte for byte, every env.txt key set (the trace aside under -NoProf), and
# the quit timer there, or the run is killed at the cap with stock settings.
$bad = @()
if ((Get-FileHash "$ArmDir\config.toml").Hash -ne (Get-FileHash "$R\config.toml").Hash) { $bad += 'config.toml' }
foreach ($k in $want.Keys) {
    if ($NoProf -and $k -eq 'RECOMP_TRACE') { continue }
    if ((Get-Item "env:$k" -ErrorAction SilentlyContinue).Value -ne $want[$k]) { $bad += $k }
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
$killed = $false
while (-not $proc.HasExited) {
    try { $proc.Refresh(); $cpu.Add("$([math]::Round(((Get-Date) - $t0).TotalSeconds, 1)),$([int]$proc.TotalProcessorTime.TotalMilliseconds)") } catch {}
    if (((Get-Date) - $t0).TotalSeconds -gt %(cap)d) { $killed = $true; & taskkill /F /T /PID $proc.Id | Out-Null; Start-Sleep 3; break }
    Start-Sleep -Milliseconds 1000
}
if ($gpu) { Stop-Process -Id $gpu.Id -ErrorAction SilentlyContinue }
$cpu | Set-Content "$R\cpu.csv"
$rc = if ($killed) { 'killed' } else { "$($proc.ExitCode)" }
$envs = @{}; Get-ChildItem env: | Where-Object { $_.Name -like 'RECOMP_*' } | ForEach-Object { $envs[$_.Name] = $_.Value }
@{ host = 'windows'; backend = '%(backend)s'; arm = $name; rep = $Rep; prof = (-not $NoProf); version = $v; rc = $rc;
   refresh_hz = $hz; quiet_wait_s = $waited; busy_at_start = $busy; qpc_us = $qpc_us; unix_us = $wall * 1000;
   started = $t0.ToString('s'); ended = (Get-Date).ToString('s'); env = $envs } | ConvertTo-Json -Depth 4 | Set-Content "$R\run-info.json"
Write-Host "$name (rep $Rep) rc=$rc -> $R"
"""


def write_windows(scene, local, out, exe_name=None):
    """run-arm.ps1 and arms/<id>/{config.toml,env.txt} for the PC."""
    host = dict(scene["hosts"]["windows"], _name="windows")
    os.makedirs(os.path.join(out, "arms"), exist_ok=True)
    for arm in arms(scene, "windows"):
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
    ps1 = RUN_ARM_PS1 % {
        "scene": scene["scene"]["name"],
        "dir": local["dir"],
        "app": local["app"],
        "exe": exe,
        "backend": host["backend"],
        # BUSY_PCT is a share of one core; the script measures cores.
        "busy_cores": BUSY_PCT / 100,
        "period": GPU_PERIOD_MS,
        "cap": scene["scene"]["run_s"] + 90,
    }
    with open(os.path.join(out, "run-arm.ps1"), "w", newline="\r\n") as f:
        f.write(ps1)
    return [a["id"] for a in arms(scene, "windows")]


def windows_info(run_dir, scene, symbols_exe):
    """The PC's run-info.json lacks the scene and the axes: add them."""
    path = os.path.join(run_dir, "run-info.json")
    raw = read(path)
    if raw is None:
        raise MatrixError("%s: no run-info.json" % run_dir)
    info = json.loads(raw.lstrip("﻿"))
    arm_id = info.get("arm", os.path.basename(run_dir)).removesuffix("-noprof")
    a = find_arm(scene, "windows", arm_id)
    info.update(
        scene=scene, scene_name=scene["scene"]["name"], values=a["values"], config=a["config"]
    )
    if symbols_exe:
        info["symbols"] = symbols_exe
    write_info(run_dir, info)


# --- the command line --------------------------------------------------------


def host_of(scene, name):
    if name not in scene["hosts"]:
        raise MatrixError("no host %s in the scene" % name)
    return dict(scene["hosts"][name], _name=name)


def main(argv=None, prog="bench-matrix", exe_name=None):
    """exe_name: the game's executable without extension (game.toml's
    build.exe), which the CLI passes; the scene and the local TOML can
    name others."""
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="list the arms, in leg order")
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
    p = sub.add_parser("windows", help="run-arm.ps1 and the arms for a Windows PC")
    p.add_argument("scene")
    p.add_argument("--local", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    a.exe_name = exe_name
    try:
        return dispatch(a)
    except MatrixError as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 2


def dispatch(a):
    if a.cmd == "plan":
        scene = load_scene(a.scene)
        hosts = [a.host] if a.host else list(scene["hosts"])
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
        local = load_toml(a.local).get(a.host)
        if not local:
            raise MatrixError("%s: no [%s] table" % (a.local, a.host))
        arm = find_arm(scene, a.host, a.arm)
        if os.path.exists(a.out):
            raise MatrixError("%s exists" % a.out)
        kind = host.get("kind")
        if kind == "local":
            run_local(scene, host, local, arm, a.out, not a.no_prof, a.benches, a.rep)
        elif kind == "proton":
            run_proton(scene, host, local, arm, a.out, not a.no_prof, a.benches, a.exe_name, a.rep)
        else:
            raise MatrixError(
                "host %s (kind %s) is not run from here; see `windows`" % (a.host, kind)
            )
        r = collect(a.out)
        print(table([summary_row(r, a.out)]))
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
        rows, overhead, arms_ = analyse(a.root, a.out, a.include_disturbed)
        print(table(rows))
        print()
        print(arms_table(arms_))
        for o in overhead:
            line = "sampler cost on %(host)s %(arm)s at %(prof)d Hz (steady fps, medians): " % o
            line += "%s -> %s (%s%%)" % (o.get("fps_off"), o.get("fps_on"), o.get("cost_pct"))
            if "cost_pct_good" in o:
                line += "; good runs only %s%%" % o["cost_pct_good"]
            print(line)
        return 0
    if a.cmd == "windows":
        scene = load_scene(a.scene)
        local = load_toml(a.local).get("windows")
        if not local:
            raise MatrixError("%s: no [windows] table" % a.local)
        ids = write_windows(scene, local, a.out, a.exe_name)
        print("%s: run-arm.ps1 and %d arms" % (a.out, len(ids)))
        return 0
    raise AssertionError(a.cmd)


if __name__ == "__main__":
    sys.exit(main())
