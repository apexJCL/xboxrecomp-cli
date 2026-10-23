"""The hardware, run and build blocks of a bench-matrix run-info.json
(bench-report D5).

Each capture fills a fixed schema from named fields of named tools. Raw
tool output is parsed here and dropped, never stored, so a host name, a
serial or a path a tool happens to print cannot reach a report. Free text
a machine's owner can set (a GPU name, a power scheme, DMI product_name)
is capped and limited to a small character set.

A capture takes run(cmd) -> stdout (a local subprocess, or ssh to the
Deck), so tests feed it recorded tool output.
"""

import json
import os
import re
import subprocess

from . import __version__

SCHEMA = 1
TEXT_MAX = 64
UNSAFE_RE = re.compile(r"[^A-Za-z0-9 ._()+,/:-]")


def clean(s):
    """Free text for the blocks: at most 64 characters of
    [A-Za-z0-9 ._()+,/:-], anything else as "?"; None stays None."""
    if s is None:
        return None
    s = " ".join(str(s).split())
    return UNSAFE_RE.sub("?", s)[:TEXT_MAX] or None


def num(s, kind=int):
    try:
        return kind(str(s).strip())
    except (TypeError, ValueError):
        return None


def empty_hw():
    return {
        "schema": SCHEMA,
        "os": {"name": None, "version": None, "build": None, "kernel": None},
        "device": {"model": None},
        "cpu": {
            "model": None,
            "cores": None,
            "threads": None,
            "p_cores": None,
            "e_cores": None,
            "max_mhz": None,
            "governor": None,
        },
        "gpu": [],
        "ram_gb": None,
        "display": {"refresh_hz": None, "width": None, "height": None},
        "power": {
            "source": None,
            "profile": None,
            "tdp_w": None,
            "gpu_mhz_cap": None,
            "cpu_mhz_cap": None,
            "perf_level": None,
        },
        "backend": {"name": None, "translation": None, "translation_version": None},
    }


GPU_KEYS = ("model", "vendor", "driver", "vram_mb", "max_mhz")
NUM_KEYS = {
    "cores",
    "threads",
    "p_cores",
    "e_cores",
    "max_mhz",
    "vram_mb",
    "ram_gb",
    "refresh_hz",
    "width",
    "height",
    "tdp_w",
    "gpu_mhz_cap",
    "cpu_mhz_cap",
}


def _value(k, v):
    if v is None or v == "":
        return None
    if k in NUM_KEYS:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return round(v, 2) if isinstance(v, float) else v
        x = num(v, float)
        if x is None:
            return None
        return int(x) if x == int(x) else round(x, 2)
    return clean(v)


def sanitize_hw(d):
    """The schema, field by field, from whatever a capture (or the PC's
    script) wrote: unknown keys are dropped and free text cleaned again, so
    run-info holds nothing the schema does not name."""
    out = empty_hw()
    if not isinstance(d, dict):
        return out
    for sect, keys in out.items():
        if sect in ("schema", "gpu", "ram_gb"):
            continue
        src = d.get(sect) if isinstance(d.get(sect), dict) else {}
        for k in keys:
            keys[k] = _value(k, src.get(k))
    gpus = d.get("gpu")
    if isinstance(gpus, dict):
        gpus = [gpus]
    for g in gpus if isinstance(gpus, list) else []:
        if isinstance(g, dict):
            row = {k: _value(k, g.get(k)) for k in GPU_KEYS}
            if any(v is not None for v in row.values()):
                out["gpu"].append(row)
    out["ram_gb"] = _value("ram_gb", d.get("ram_gb"))
    return out


def local_run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


# --- macOS --------------------------------------------------------------------

MAC_RES_RE = re.compile(r"(\d+)\s*x\s*(\d+)(?:\s*@\s*([\d.]+)\s*Hz)?")


def capture_macos(run=local_run, backend="metal"):
    """sysctl, sw_vers, uname, system_profiler SPDisplaysDataType and pmset;
    never SPHardwareDataType (it carries the serial and the UUID)."""
    hw = empty_hw()
    sysctl = lambda k: run(["sysctl", "-n", k]).strip()  # noqa: E731
    hw["cpu"].update(
        model=clean(sysctl("machdep.cpu.brand_string")),
        cores=num(sysctl("hw.physicalcpu")),
        threads=num(sysctl("hw.logicalcpu")),
        p_cores=num(sysctl("hw.perflevel0.physicalcpu")),
        e_cores=num(sysctl("hw.perflevel1.physicalcpu")),
    )
    hz = num(sysctl("hw.cpufrequency_max"))
    hw["cpu"]["max_mhz"] = hz // 1000000 if hz else None
    hw["device"]["model"] = clean(sysctl("hw.model"))
    mem = num(sysctl("hw.memsize"))
    hw["ram_gb"] = round(mem / 2**30) if mem else None
    hw["os"].update(
        name=clean(run(["sw_vers", "-productName"]).strip()),
        version=clean(run(["sw_vers", "-productVersion"]).strip()),
        build=clean(run(["sw_vers", "-buildVersion"]).strip()),
        kernel=clean(run(["uname", "-r"]).strip()),
    )
    try:
        disp = json.loads(run(["system_profiler", "SPDisplaysDataType", "-json"]) or "{}")
    except ValueError:
        disp = {}
    family = None
    for g in disp.get("SPDisplaysDataType") or []:
        vendor = str(g.get("spdisplays_vendor") or "").replace("sppci_vendor_", "")
        family = family or str(g.get("spdisplays_mtlgpufamilysupport") or "")
        hw["gpu"].append(
            {
                "model": clean(g.get("sppci_model")),
                "vendor": clean(vendor),
                # Apple's GPUs have no driver version of their own; the
                # Metal family is what the OS reports for them.
                "driver": clean(family.replace("spdisplays_", "").replace("metal", "Metal ")),
                "vram_mb": None,
                "max_mhz": None,
                "cores": num(g.get("sppci_cores")),
            }
        )
        for d in g.get("spdisplays_ndrvs") or []:
            if d.get("spdisplays_main") == "spdisplays_yes" or hw["display"]["refresh_hz"] is None:
                m = MAC_RES_RE.search(str(d.get("_spdisplays_resolution") or ""))
                if m:
                    hw["display"].update(
                        width=int(m.group(1)),
                        height=int(m.group(2)),
                        refresh_hz=num(m.group(3), float) if m.group(3) else None,
                    )
    for g in hw["gpu"]:
        g.pop("cores", None)
    pm = run(["pmset", "-g"])
    m = re.search(r"^\s*lowpowermode\s+(\d)", pm, re.M)
    if m:
        hw["power"]["profile"] = "low power" if m.group(1) == "1" else "normal"
    batt = run(["pmset", "-g", "batt"])
    if "AC Power" in batt:
        hw["power"]["source"] = "AC"
    elif "Battery Power" in batt:
        hw["power"]["source"] = "battery"
    hw["backend"]["name"] = backend
    return sanitize_hw(hw)


# --- Linux and the Deck -------------------------------------------------------

# One shell round trip prints each named source in its own section; the
# parser below keeps only the fields it names. No hostname, no
# product_serial/product_uuid/board_serial, no network.
LINUX_HW_SCRIPT = r"""sec() { echo "==xbr== $1"; }
sec os-release; cat /etc/os-release 2>/dev/null
sec kernel; uname -r
sec product; cat /sys/class/dmi/id/product_name 2>/dev/null
sec cpuinfo; grep -m1 '^model name' /proc/cpuinfo 2>/dev/null
sec lscpu; lscpu 2>/dev/null | grep -E '^(Core\(s\) per socket|Socket\(s\)|Thread\(s\) per core|CPU\(s\)|CPU max MHz):'
sec cpu-max; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null
sec cpu-hw-max; cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null
sec governor; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null
sec meminfo; grep -m1 '^MemTotal' /proc/meminfo
sec vulkan; timeout 20 vulkaninfo --summary 2>/dev/null | grep -E '^\s*(deviceName|driverName|driverInfo|vendorID)\s*='
sec lspci; lspci -nn 2>/dev/null | grep -E 'VGA|Display|3D' | sed 's/^[^ ]* //'
for d in /sys/class/drm/card[0-9]/device; do
  [ -r "$d/pp_dpm_sclk" ] || continue
  sec vram; cat "$d/mem_info_vram_total" 2>/dev/null
  sec sclk; cat "$d/pp_dpm_sclk" 2>/dev/null
  sec od; cat "$d/pp_od_clk_voltage" 2>/dev/null
  sec perf; cat "$d/power_dpm_force_performance_level" 2>/dev/null
  sec tdp; cat "$d"/hwmon/hwmon*/power1_cap 2>/dev/null
  break
done
sec xrandr; DISPLAY="${DISPLAY:-:0}" timeout 10 xrandr --current 2>/dev/null | grep -E '\*'
sec power; for p in /sys/class/power_supply/*; do echo "$(cat "$p/type" 2>/dev/null) $(cat "$p/online" 2>/dev/null) $(cat "$p/status" 2>/dev/null)"; done
"""


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("==xbr== "):
            cur = line[8:].strip()
            out.setdefault(cur, [])
        elif cur:
            out[cur].append(line)
    return out


def kv_lines(lines, sep="="):
    out = {}
    for line in lines:
        k, s, v = line.partition(sep)
        if s:
            out[k.strip()] = v.strip().strip('"')
    return out


MHZ_RE = re.compile(r"(\d+)\s*Mhz", re.I)


def parse_linux(text, backend=None, proton=None):
    """The schema from LINUX_HW_SCRIPT's output (the Deck over ssh, or any
    Linux host)."""
    s = sections(text)
    hw = empty_hw()
    osr = kv_lines(s.get("os-release", []))
    hw["os"].update(
        name=clean(osr.get("NAME")),
        version=clean(osr.get("VERSION_ID")),
        build=clean(osr.get("BUILD_ID")),
        kernel=clean(" ".join(s.get("kernel", [])).strip()),
    )
    hw["device"]["model"] = clean(" ".join(s.get("product", [])).strip())
    ci = kv_lines(s.get("cpuinfo", []), ":")
    hw["cpu"]["model"] = clean(ci.get("model name"))
    ls = kv_lines(s.get("lscpu", []), ":")
    cps, sockets, tpc = (
        num(ls.get(k)) for k in ("Core(s) per socket", "Socket(s)", "Thread(s) per core")
    )
    if cps:
        hw["cpu"]["cores"] = cps * (sockets or 1)
        if tpc:
            hw["cpu"]["threads"] = hw["cpu"]["cores"] * tpc
    hw["cpu"]["threads"] = hw["cpu"]["threads"] or num(ls.get("CPU(s)"))
    khz = num(" ".join(s.get("cpu-hw-max", [])).strip())
    cap = num(" ".join(s.get("cpu-max", [])).strip())
    hw["cpu"]["max_mhz"] = khz // 1000 if khz else (num(ls.get("CPU max MHz"), float) or None)
    if cap and khz and cap < khz:
        hw["power"]["cpu_mhz_cap"] = cap // 1000
    hw["cpu"]["governor"] = clean(" ".join(s.get("governor", [])).strip())
    mi = kv_lines(s.get("meminfo", []), ":")
    kb = num((mi.get("MemTotal") or "").split()[0] if mi.get("MemTotal") else None)
    hw["ram_gb"] = round(kb / 2**20, 1) if kb else None
    vk = kv_lines(s.get("vulkan", []))
    gpu = {k: None for k in GPU_KEYS}
    if vk.get("deviceName"):
        gpu["model"] = clean(vk["deviceName"])
        drv = " ".join(x for x in (vk.get("driverName"), vk.get("driverInfo")) if x)
        gpu["driver"] = clean(drv)
    elif s.get("lspci"):
        line = s["lspci"][0]
        # "VGA compatible controller [0300]: AMD [1002:163f] VanGogh [AMD Custom GPU 0405] (rev ae)"
        name = line.split(": ", 1)[-1]
        gpu["model"] = clean(re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", name))
    vendor = vk.get("vendorID", "")
    if "0x1002" in vendor or "AMD" in (gpu["model"] or ""):
        gpu["vendor"] = "AMD"
    elif "0x10de" in vendor:
        gpu["vendor"] = "NVIDIA"
    elif "0x8086" in vendor:
        gpu["vendor"] = "Intel"
    vram = num(" ".join(s.get("vram", [])).strip())
    gpu["vram_mb"] = vram // 2**20 if vram else None
    # pp_od_clk_voltage: OD_SCLK holds the clock range set now (a cap such
    # as 1040 MHz sits there), OD_RANGE's SCLK line the hardware's.
    od_cap = od_range = None
    part = None
    for line in s.get("od", []):
        if line.startswith("OD_"):
            part = line.split(":")[0]
            continue
        mhz = [int(x) for x in MHZ_RE.findall(line)]
        if not mhz:
            continue
        if part == "OD_SCLK":
            od_cap = max(mhz + [od_cap or 0])
        elif part == "OD_RANGE" and line.strip().startswith("SCLK"):
            od_range = max(mhz)
    sclk = [int(x) for line in s.get("sclk", []) for x in MHZ_RE.findall(line)]
    gpu["max_mhz"] = od_range or od_cap or (max(sclk) if sclk else None)
    hw["power"]["gpu_mhz_cap"] = od_cap or (max(sclk) if sclk else None)
    if any(v is not None for v in gpu.values()):
        hw["gpu"].append(gpu)
    hw["power"]["perf_level"] = clean(" ".join(s.get("perf", [])).strip())
    tdp = num(" ".join(s.get("tdp", [])).split()[0] if s.get("tdp") else None)
    hw["power"]["tdp_w"] = round(tdp / 1e6, 1) if tdp else None
    for line in s.get("xrandr", []):
        m = re.search(r"^\s*(\d+)x(\d+)\S*\s+.*?([\d.]+)\*", line)
        if m:
            hw["display"].update(
                width=int(m.group(1)), height=int(m.group(2)), refresh_hz=num(m.group(3), float)
            )
            break
    for line in s.get("power", []):
        parts = line.split()
        if parts[:1] == ["Mains"]:
            hw["power"]["source"] = "AC" if parts[1:2] == ["1"] else "battery"
            break
        if parts[:1] == ["Battery"] and hw["power"]["source"] is None:
            st = parts[-1] if len(parts) > 1 else ""
            hw["power"]["source"] = "battery" if st == "Discharging" else "AC"
    hw["backend"]["name"] = backend
    if proton:
        hw["backend"]["translation"] = clean(os.path.basename(proton.rstrip("/")))
    return sanitize_hw(hw)


def capture_linux(run, backend=None, proton=None):
    """run(script) -> its stdout; for the Deck, ssh with the script."""
    return parse_linux(run(LINUX_HW_SCRIPT) or "", backend, proton)


DXVK_RE = re.compile(r"\binfo:\s+DXVK:\s+(v[\w.\-+]+)")


def dxvk_version(log):
    """DXVK's version from Proton's log (umu.log), None without one."""
    m = DXVK_RE.search(log or "")
    return clean("DXVK " + m.group(1)) if m else None


# --- the run block ------------------------------------------------------------

ENHANCE_RE = re.compile(r"^\[ENHANCE\] render\.scale=.*$", re.M)
VSYNC_RE = re.compile(r"^\[PRESENT\] .*\bvsync (on|off)\b", re.M)


def window_mode(info):
    """fullscreen or windowed from run-info's present_fullscreen; "unknown"
    when the run predates the field, rather than guessing windowed."""
    if "present_fullscreen" not in info:
        return "unknown"
    fs = str(info.get("present_fullscreen") or "")
    return "fullscreen" if fs and fs != "0" else "windowed"


def run_block(info, log, sampler_hz, pacing_mode=None):
    """How the run was taken: the sampler's rate (0: none), the pacing and
    the present surface the runtime logged, vsync when logged, the window
    mode from run-info's present_fullscreen ("unknown" for a run from
    before that field) and the run's place in its series."""
    m = ENHANCE_RE.search(log or "")
    kv = dict(re.findall(r"(\S+?)=(\S+)", m.group(0))) if m else {}
    v = VSYNC_RE.search(log or "")
    return {
        "sampler_hz": sampler_hz or 0,
        "pacing": clean(kv.get("present.pacing") or pacing_mode),
        "present_mode": clean(kv.get("present.surface")),
        "vsync": v.group(1) if v else None,
        "window": window_mode(info),
        "order": info.get("run_order"),
    }


# --- the build block ----------------------------------------------------------


def find_manifest(exe):
    """The payload manifest beside an exe: the version folder's
    manifest.json, a macOS app's Contents/Resources/manifest.json, or the
    dist folder above it."""
    if not exe:
        return None
    d = os.path.dirname(os.path.abspath(exe))
    for p in (
        os.path.join(d, "manifest.json"),
        os.path.join(os.path.dirname(d), "Resources", "manifest.json"),
        os.path.join(os.path.dirname(d), "manifest.json"),
    ):
        if os.path.isfile(p):
            return p
    return None


VERSION_PUBLIC_RE = re.compile(r"^\d{8}\.\d+$")


def version_public(v):
    """The date and sequence part of a version (20261023.0049 of
    20261023.0049-c5d8e7b0-t4ec1a73): the rest names private shas. None
    unless that part has the date.seq form: a bare sha or "dev" would
    pass the leak check yet name a private commit."""
    head = str(v).split("-", 1)[0] if v else ""
    return head if VERSION_PUBLIC_RE.match(head) else None


def build_block(manifest=None, version=None, game_sha=None, toolkit_sha=None, exe_sha256=None):
    """version, version_public, the shas (the manifest's sources, or the
    flags), the CLI's version and the exe's sha256. None without a
    version: run refuses then."""
    m = {}
    if manifest:
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
    src = m.get("sources") or {}
    game = next((v for k, v in src.items() if k not in ("toolkit", "cli")), {})
    version = version or m.get("version")
    if not version:
        return None
    return {
        "version": clean(version),
        "version_public": version_public(version),
        "game_sha": clean(game_sha or game.get("commit")),
        "toolkit_sha": clean(toolkit_sha or (src.get("toolkit") or {}).get("commit")),
        "cli_version": __version__,
        "exe_sha256": exe_sha256,
    }
