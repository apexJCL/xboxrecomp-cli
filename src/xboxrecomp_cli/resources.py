"""What this machine can give a build: its memory, cores and disk, the job
counts that fit them, the peaks the steps measured, and the disk a run
needs before it starts.

The generated code is the one heavy compile: a gen/ file peaks at up to
1.2 GiB in clang, while the toolkit's files, the link and the HUD workers
stay under 256 MiB. A build at -j<cores> on a machine with many threads
and modest RAM swaps or crashes, so the gen/ target gets a Ninja pool
sized from the available memory and everything else keeps the cores.

Sizing never caps memory and never stops a build: low memory warns and
compiles one file at a time. Low disk refuses before the first stage,
since a full disk leaves a half-written tree; --ignore-disk-space turns
that into a warning. Every query returns None when it cannot tell, and
every measurement that fails records nothing."""

import datetime
import os
import re
import shutil
import subprocess
import sys

from . import host

MiB = 1 << 20
GiB = 1 << 30
# Any compile job outside the gen/ pool (measured at most 133 MiB).
OTHER_JOB = 256 * MiB
# The size estimate when game.toml names no gen_job_mib: the worst measured
# ratio of peak to source size, with a floor for a game of small files.
GEN_MIB_PER_MB = 140
GEN_FLOOR = 512 * MiB
# Jobs when the available memory is unknown.
UNKNOWN_JOBS = 4
# enhance hud: the parent's measured 480 MiB, and the least a worker gets.
HUD_PARENT = 512 * MiB
HUD_WORKER_MIN = 128 * MiB
# Free space each volume keeps beyond the stages' estimates (the wizard's
# BundleNeed uses the same 1 GiB).
SLACK = GiB
STATE_NAME = "resources.json"
DISK_FLAG = "--ignore-disk-space"


def _run(argv):
    """A query command's stdout, or None."""
    try:
        r = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ── queries ─────────────────────────────────────────────────────────────


def cgroup_dirs(proc_cgroup="/proc/self/cgroup", root="/sys/fs/cgroup"):
    """The process's cgroup v2 folder and its ancestors up to the root,
    innermost first: [] without cgroup v2. A limit set on a parent (a
    container, a Flatpak sandbox, a systemd slice) binds the children."""
    text = _read(proc_cgroup)
    if not text:
        return []
    rel = None
    for line in text.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
    if rel is None:
        return []
    out = []
    d = os.path.normpath(os.path.join(root, rel.lstrip("/")))
    top = os.path.normpath(root)
    while True:
        out.append(d)
        if d == top or not d.startswith(top):
            break
        d = os.path.dirname(d)
    return out


def cgroup_mem_headroom(dirs):
    """The least of memory.max - memory.current over the folders that set a
    limit, or None when none does."""
    best = None
    for d in dirs:
        mx = (_read(os.path.join(d, "memory.max")) or "").strip()
        cur = (_read(os.path.join(d, "memory.current")) or "").strip()
        if not mx or mx == "max" or not mx.isdigit() or not cur.isdigit():
            continue
        room = max(0, int(mx) - int(cur))
        best = room if best is None else min(best, room)
    return best


def cgroup_cpus(dirs):
    """The least cpu.max quota over the folders, in whole CPUs rounded up,
    or None when none sets one."""
    best = None
    for d in dirs:
        parts = (_read(os.path.join(d, "cpu.max")) or "").split()
        if len(parts) != 2 or parts[0] == "max":
            continue
        try:
            quota, period = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if quota <= 0 or period <= 0:
            continue
        n = max(1, -(-quota // period))
        best = n if best is None else min(best, n)
    return best


def cores(os_name=None, dirs=None):
    """The CPUs this process may use, or None."""
    os_name = os_name or host.host_os()
    n = None
    f = getattr(os, "process_cpu_count", None)
    if f is not None:
        n = f()
    if n is None and hasattr(os, "sched_getaffinity"):
        try:
            n = len(os.sched_getaffinity(0))
        except OSError:
            n = None
    if n is None:
        n = os.cpu_count()
    if os_name == "linux":
        q = cgroup_cpus(cgroup_dirs() if dirs is None else dirs)
        if q is not None:
            n = min(n, q) if n else q
    return n


def meminfo(text):
    """{name: bytes} of /proc/meminfo's kB lines."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def vm_stat_available(text):
    """macOS: (free + inactive + speculative + purgeable) pages, in bytes.
    It leaves out what the compressor could free, so it reads lower than
    Activity Monitor: the safe side."""
    if not text:
        return None
    m = re.search(r"page size of (\d+) bytes", text)
    if not m:
        return None
    page = int(m.group(1))
    pages = {}
    for k, v in re.findall(r"^Pages (free|inactive|speculative|purgeable):\s+(\d+)\.", text, re.M):
        pages[k] = int(v)
    if "free" not in pages:
        return None
    return sum(pages.values()) * page


def _win_memstatus():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return st


def mem_total(os_name=None, meminfo_path="/proc/meminfo", run=_run):
    """The physical memory in bytes, or None."""
    os_name = os_name or host.host_os()
    try:
        if os_name == "linux":
            return meminfo(_read(meminfo_path)).get("MemTotal")
        if os_name == "macos":
            out = run(["sysctl", "-n", "hw.memsize"])
            return int(out.strip()) if out and out.strip().isdigit() else None
        if os_name == "windows":
            st = _win_memstatus()
            return int(st.ullTotalPhys) if st else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def mem_available(os_name=None, meminfo_path="/proc/meminfo", run=_run, dirs=None):
    """The memory a build can take now, in bytes, or None."""
    os_name = os_name or host.host_os()
    try:
        if os_name == "linux":
            avail = meminfo(_read(meminfo_path)).get("MemAvailable")
            room = cgroup_mem_headroom(cgroup_dirs() if dirs is None else dirs)
            if room is not None:
                avail = room if avail is None else min(avail, room)
            return avail
        if os_name == "macos":
            return vm_stat_available(run(["vm_stat"]))
        if os_name == "windows":
            st = _win_memstatus()
            # Windows does not overcommit: the commit headroom limits too.
            return int(min(st.ullAvailPhys, st.ullAvailPageFile)) if st else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def existing(path):
    """path, or its nearest folder that exists."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
    return p


def disk_free(path):
    try:
        return shutil.disk_usage(existing(path)).free
    except OSError:
        return None


def volume(path):
    """The device a path (or its nearest existing folder) is on."""
    try:
        return os.stat(existing(path)).st_dev
    except OSError:
        return None


# ── measured runs ───────────────────────────────────────────────────────


class _WinJob:
    """An accounting-only Job Object: no limit flags (no memory cap, ever),
    only PeakProcessMemoryUsed, the largest commit of any process in the
    tree. Descendants join it by themselves."""

    def __init__(self, p):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.k = k
        self.h = k.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError("CreateJobObjectW failed")
        if not k.AssignProcessToJobObject(self.h, int(p._handle)):
            k.CloseHandle(self.h)
            raise OSError("AssignProcessToJobObject failed")

    def peak(self):
        ctypes = self.ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "Other")
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("Basic", BASIC),
                ("Io", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = EXTENDED()
        # 9: JobObjectExtendedLimitInformation.
        ok = self.k.QueryInformationJobObject(
            self.h, 9, ctypes.byref(info), ctypes.sizeof(info), None
        )
        return int(info.PeakProcessMemoryUsed) if ok else None

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None


def watch(p, os_name=None):
    """Start measuring a Popen just started: the Windows job, else None
    (POSIX measures at the wait). A failure measures nothing."""
    if (os_name or host.host_os()) != "windows":
        return None
    try:
        return _WinJob(p)
    except Exception:
        return None


def wait(p, job=None):
    """Wait for a Popen started with watch(); (exit code, the peak of its
    largest process in bytes or None). The peak is wait4's ru_maxrss on
    POSIX (the child and the descendants it waited for: for cmake --build,
    the largest compile), the job's PeakProcessMemoryUsed on Windows."""
    if job is not None:
        try:
            rc = p.wait()
            try:
                peak = job.peak()
            except Exception:
                peak = None
        finally:
            job.close()
        return rc, peak
    if hasattr(os, "wait4") and p.returncode is None:
        try:
            _pid, status, ru = os.wait4(p.pid, 0)
        except ChildProcessError:
            return p.wait(), None
        # Popen must learn the status, or its own wait() finds no child
        # (ECHILD) and reports 0.
        p.returncode = os.waitstatus_to_exitcode(status)
        scale = 1 if sys.platform == "darwin" else 1024
        return p.returncode, (ru.ru_maxrss * scale) or None
    return p.wait(), None


def self_peak():
    """This process's own peak in bytes (a HUD worker reports it), or None."""
    try:
        if host.host_os() == "windows":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            psapi = ctypes.WinDLL("psapi")
            k = ctypes.WinDLL("kernel32")
            k.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
            if not psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return int(c.PeakWorkingSetSize)
        import resource

        n = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return n * (1 if sys.platform == "darwin" else 1024)
    except Exception:
        return None


# ── the measured peaks (.git/xbr/resources.json) ─────────────────────────


def state_path():
    from . import buildstate

    return os.path.join(buildstate.state_dir(host.g().root), STATE_NAME)


def load():
    from . import buildstate

    try:
        d = buildstate.load_json(state_path(), {})
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def peak_of(name, data=None):
    rec = (load() if data is None else data).get(name)
    p = rec.get("peak") if isinstance(rec, dict) else None
    return p if isinstance(p, int) and p > 0 else None


def record(name, peak, jobs=None):
    """Keep the largest peak a step has shown, with its job count and date:
    an incremental build that recompiles only small files measures little,
    and must not lower what the next full build is sized by. Never fails
    the step."""
    if not peak:
        return
    try:
        from . import __version__, buildstate

        data = load()
        old = peak_of(name, data)
        if old is not None and old >= peak:
            return
        data[name] = {
            "peak": int(peak),
            "jobs": jobs,
            "at": datetime.date.today().isoformat(),
            "cli": __version__,
        }
        buildstate.write_json(state_path(), data)
    except Exception:
        return


# ── sizing ──────────────────────────────────────────────────────────────


def gb(n):
    """Bytes as the CLI prints them elsewhere (clean.human: decimal GB)."""
    from .clean import human

    return human(n) if n is not None else "unknown"


def reserve_for(total):
    """10% of the RAM, at least 1 GiB and at most 2 GiB: room for the
    desktop and the CLI. The workstation gives the rest to the build."""
    if not total:
        return GiB
    return min(max(total // 10, GiB), 2 * GiB)


def gen_files_estimate(gen_dir):
    """140 MiB per MB of the largest generated file, at least 512 MiB, or
    None without gen/."""
    try:
        sizes = [e.stat().st_size for e in os.scandir(gen_dir) if e.name.endswith((".c", ".cpp"))]
    except OSError:
        return None
    if not sizes:
        return None
    return max(GEN_FLOOR, int(max(sizes) / 1e6 * GEN_MIB_PER_MB) * MiB)


def gen_job(target, data=None):
    """(G, source): the per-job peak of the gen/ target. The larger of
    game.toml's gen_job_mib (else the size estimate) and the largest
    compile:<target> measured here; source names which one won."""
    G = host.g()
    r = G.m["build"]["resources"]
    if r["gen_job_mib"]:
        base, src = r["gen_job_mib"] * MiB, "game.toml"
    else:
        base, src = gen_files_estimate(G.gen) or GEN_FLOOR, "estimated from gen/"
    data = load() if data is None else data
    meas = peak_of("compile:" + target, data)
    if meas is not None and meas > base:
        rec = data.get("compile:" + target) or {}
        return meas, "measured %s" % rec.get("at", "")
    return base, src


class Sizing:
    """The job counts for one build, and what they were made from."""

    def __init__(self, **kw):
        self.cores = self.total = self.available = None
        self.reserve = self.budget = None
        self.gen_job = self.gen_source = None
        self.jobs = self.gen_jobs = self.formula = None
        self.pool = True
        self.forced = False
        self.warnings = []
        self.__dict__.update(kw)

    def line(self):
        """build: 7 jobs, generated code 6 at a time (9.0 GB available, about
        1.2 GB per job)"""
        if self.forced:
            return "%d jobs (--jobs)" % self.jobs
        if self.available is None:
            return "%d jobs (available memory unknown)" % self.jobs
        if self.pool and self.gen_jobs != self.jobs:
            head = "%d jobs, generated code %d at a time" % (self.jobs, self.gen_jobs)
        else:
            head = "%d jobs" % self.jobs
        return "%s (%s available, about %s per job)" % (
            head,
            gb(self.available),
            gb(self.gen_job),
        )


def size(available, total, ncores, gen_job_bytes, pool=True, jobs=None):
    """D3: the reserve, the budget left for the build, then as many gen/
    jobs as fit (at least one, at most the cores) and, with the pool, more
    small jobs in what remains."""
    C = ncores or UNKNOWN_JOBS
    s = Sizing(cores=ncores, total=total, available=available, gen_job=gen_job_bytes, pool=pool)
    if available is None:
        s.gen_jobs = s.jobs = min(C, UNKNOWN_JOBS)
    else:
        s.reserve = reserve_for(total)
        s.budget = max(0, available - s.reserve)
        s.gen_jobs = max(1, min(C, s.budget // gen_job_bytes))
        if pool:
            more = max(0, s.budget - s.gen_jobs * gen_job_bytes) // OTHER_JOB
            s.jobs = max(s.gen_jobs, min(C, s.gen_jobs + more))
        else:
            s.jobs = s.gen_jobs
        if s.budget < gen_job_bytes:
            s.warnings.append(
                "memory is low (%s available): the build will compile one file at a time "
                "and may use swap; close other programs first" % gb(available)
            )
    s.formula = s.gen_jobs
    if jobs:
        if available is not None and jobs > s.gen_jobs:
            s.warnings.append(
                "--jobs %d: more than the %d generated-code jobs that fit in %s available; "
                "the build may use swap" % (jobs, s.gen_jobs, gb(available))
            )
        s.jobs = s.gen_jobs = jobs
        s.forced = True
    return s


def plan(target, jobs=None, pool=True, data=None):
    """The sizing of a build of target on this machine now."""
    g_bytes, src = gen_job(target, data)
    s = size(mem_available(), mem_total(), cores(), g_bytes, pool, jobs)
    s.gen_source = src
    return s


def hud_jobs(items, jobs=None):
    """enhance hud's workers: the budget less the parent, over the larger
    of 128 MiB and the measured worker peak; at most the cores and the
    items. jobs (--jobs N) wins."""
    if jobs:
        return jobs
    C = cores() or UNKNOWN_JOBS
    hi = max(1, min(C, items))
    avail = mem_available()
    if avail is None:
        return min(hi, UNKNOWN_JOBS)
    budget = max(0, avail - reserve_for(mem_total()))
    h = max(HUD_WORKER_MIN, peak_of("hud:worker") or 0)
    return max(1, min(hi, (budget - HUD_PARENT) // h if budget > HUD_PARENT else 1))


def tool_need(data=None):
    """(bytes, step name) of the largest single tool process: game.toml's
    tool_mib or a larger measured tool peak; (0, "") when neither."""
    r = host.g().m["build"]["resources"]
    data = load() if data is None else data
    need, name = 0, ""
    if r["tool_mib"]:
        need, name = r["tool_mib"] * MiB, r["tool_step"] or "the largest tool step"
    for k, rec in data.items():
        if k.startswith("tool:") and isinstance(rec, dict):
            p = rec.get("peak")
            if isinstance(p, int) and p > need:
                need, name = p, k[5:]
    return need, name


def tool_warning(available=None, total=None, data=None):
    """The warning when the largest tool process does not fit beside the
    reserve, or ""."""
    need, name = tool_need(data)
    if not need:
        return ""
    available = mem_available() if available is None else available
    if available is None:
        return ""
    total = mem_total() if total is None else total
    if need + reserve_for(total) <= available:
        return ""
    return "%s needs about %s in one process and %s is available; close other programs first" % (
        name,
        gb(need),
        gb(available),
    )


def warn_tools():
    """Before the generate steps: say the tool warning (never stops)."""
    w = tool_warning()
    if w:
        host.say("WARNING: " + w)


# ── disk ────────────────────────────────────────────────────────────────


class Need:
    """One stage's write: (stage, folder, bytes, what)."""

    def __init__(self, stage, path, n, what):
        self.stage, self.path, self.n, self.what = stage, path, int(n), what


def tree_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def setup_need(needs, root, tk):
    """The setup stage's share: what is missing."""
    out = []
    if any(n in needs for n in ("no cmake", "no ninja", "no tools Python", "no uv")):
        out.append(Need("setup", root, 256 * MiB, "the tools environment (.venv)"))
    if "no llvm-mingw" in needs:
        out.append(Need("setup", root, GiB, "llvm-mingw"))
    if "no toolkit" in needs:
        out.append(Need("setup", tk, 256 * MiB, "the toolkit"))
    if "no NSIS" in needs:
        out.append(Need("setup", root, 64 * MiB, "NSIS"))
    return out


def generate_need():
    G = host.g()
    return Need(
        "generate",
        G.out,
        G.m["build"]["resources"]["generate_mib"] * MiB,
        "analysis and generated code",
    )


def build_need(bdir):
    return Need(
        "build",
        bdir,
        host.g().m["build"]["resources"]["tree_mib"] * MiB,
        os.path.basename(os.path.normpath(bdir)),
    )


def hud_need(out_dir):
    """Twice the last pack (the pack and its store copy), else 512 MiB."""
    try:
        n = 2 * os.path.getsize(os.path.join(out_dir, "hud.pack"))
    except OSError:
        n = 512 * MiB
    return Need("enhance hud", out_dir, n, "the HUD pack")


def by_volume(needs, free=None, dev=None):
    """[{"path", "free", "need", "stages": {stage: bytes}, "items"}] per
    volume, in the order the needs first name them; need holds SLACK."""
    free, dev = free or disk_free, dev or volume
    vols = {}
    for n in needs:
        if n.n <= 0:
            continue
        d = dev(n.path)
        v = vols.get(d)
        if v is None:
            v = vols[d] = {"path": n.path, "free": free(n.path), "need": SLACK, "stages": {}}
            v["items"] = []
        v["need"] += n.n
        v["stages"][n.stage] = v["stages"].get(n.stage, 0) + n.n
        v["items"].append(n)
    return list(vols.values())


def short(vols):
    return [v for v in vols if v["free"] is not None and v["free"] < v["need"]]


def disk_json(vols):
    return [
        {"path": v["path"], "free": v["free"], "need": v["need"], "stages": dict(v["stages"])}
        for v in vols
    ]


def disk_text(vols):
    """The refusal: each short volume with its shares, the others in one
    line, then the fix."""
    lines = []
    bad = short(vols)
    for v in bad:
        lines.append(
            "disk: this build needs %s in %s and %s is free"
            % (gb(v["need"]), v["path"], gb(v["free"]))
        )
        for n in v["items"]:
            lines.append("  %-11s %8s  %s" % (n.stage, gb(n.n), n.what))
        lines.append("  %-11s %8s" % ("slack", gb(SLACK)))
    for v in vols:
        if v not in bad:
            lines.append(
                "  (and %s in %s for %s, which has %s free)"
                % (gb(v["need"]), v["path"], ", ".join(v["stages"]), gb(v["free"]))
            )
    for v in bad:
        lines.append(
            "free %s in %s: '%s clean' shows what it can remove (old bundles in dist/, "
            "the other targets' build trees), or move the build folder to a larger drive; "
            "%s builds anyway, and the build stops if the disk fills"
            % (gb(v["need"] - v["free"]), v["path"], host.cli_name(), DISK_FLAG)
        )
    return "\n".join(lines)


def check_disk(needs, ignore=False):
    """Refuse (code disk) when a volume has less free space than its
    stages need; with ignore (--ignore-disk-space), say it as a warning.
    Returns the volumes."""
    vols = by_volume(needs)
    if short(vols):
        text = disk_text(vols)
        if not ignore:
            raise host.Refusal(text, "disk")
        host.say("WARNING: " + text.replace("\n", "\n  "))
    return vols


def add_flag(p):
    p.add_argument(
        DISK_FLAG,
        dest="ignore_disk_space",
        action="store_true",
        help="build even when the free space looks too small (a warning instead of a "
        "refusal; for a filesystem that compresses or deduplicates)",
    )


def jobs_arg(v):
    """--jobs N: a whole number of 1 or more."""
    import argparse

    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("%s: want a whole number of 1 or more" % v)
    return n


def add_jobs(p, help=None):
    p.add_argument(
        "--jobs",
        type=jobs_arg,
        default=0,
        metavar="N",
        help=help
        or "compile jobs at once (default: as many as the free memory and the "
        "cores allow; doctor shows the figure)",
    )
