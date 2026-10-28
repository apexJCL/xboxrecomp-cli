"""Parsers for what the bench and build commands leave on disk (design D1,
D12.2): run-info.txt in its three formats (Proton, native Windows, Mac),
golden.txt, both session TSVs, tests-pass files, patch.json, the bundle
index, the CLI's own record.json files, and the flip trace's statistics.

Every parser is pure (text in, dict out) and never raises on a malformed
line: what it could not read goes in the result's "problems", so a run
is kept as partial with its reason (D17 Q7) rather than dropped."""

import datetime
import hashlib
import json
import os
import re

SHA_RE = r"[0-9a-f]{64}"
EXE_LINE_RE = re.compile(r"^(%s)  (\S.*?)\s*$" % SHA_RE)
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?$")
TREE_RE = re.compile(r"^([\w.-]+): ([0-9a-f]{7,40}) (\S+) (clean|dirty)(?: \((\d+) paths?\))?\s*$")
SHIPPED_RE = re.compile(
    r"^(built|shipped \(not built by bench\)): (\S+) on (\S+) from (.+?)(?: \(|$)"
)
# A host build of a synced tree (BENCH_BUILD=host): the run's host compiled it.
SYNCED_RE = re.compile(r"^synced: (\S+) from ([^:\s]+):(.+)$")
# The hand-written form of mid-October runs: "<exe> built on <name> <iso> (...)".
HAND_BUILT_RE = re.compile(r"^\S+ built on (the Mac|\S+) (\d{4}-\d\d-\d\dT\S+)")
LOAD_POSIX_RE = re.compile(
    r"^([\d.]+) [\d.]+ [\d.]+ at start, ([\d.]+) [\d.]+ [\d.]+ at end \((\d+) cpus\)"
)
LOAD_WIN_RE = re.compile(r"^([\d.]+)% at start, ([\d.]+)% at end \((\d+) cpus\)")
LOAD_MAC_RE = re.compile(r"^([\d.]+) on (\d+) CPUs at start")
STOP_RE = re.compile(r"^(.*?) at flip (\d+) after ([\d.]+) s(?: \(max load ([\d.]+)\))?")
LIMIT_RE = re.compile(r"^([\d.]+) s\b")
WAIT_RE = re.compile(r"waited (\d+)s")
STALL_RE = re.compile(r"^([\d.]+)%")
ELAPSED_RE = re.compile(r"^(\d+)s, game exit (-?\d+)")
FLIPS_RE = re.compile(r"^(\d+)(.*)$")
MAC_TREE_RE = re.compile(r"^(\S.*?) ([0-9a-f]{7,40})(?: \((\d+) paths? dirty\)| clean)?\s*$")


def iso_utc(text, local_offset=None):
    """An ISO 8601 time as UTC ('...Z'). A time without an offset (the
    Windows host's local time) is converted with local_offset seconds, or
    None when that is unknown (the caller keeps the controller's stamp)."""
    t = text.strip()
    if not t:
        return None
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    m = re.search(r"([+-]\d\d)(\d\d)$", t)
    if m and ":" not in t[-6:]:
        t = t[: m.start()] + m.group(1) + ":" + m.group(2)
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        if local_offset is None:
            return None
        try:
            dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(seconds=local_offset)))
        except (ValueError, OverflowError):
            return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_iso(stamp):
    """The controller's YYYYMMDD-HHMMSS (local time) as UTC."""
    try:
        dt = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(s, kind=float):
    try:
        return kind(s)
    except (TypeError, ValueError):
        return None


def tree_line(text):
    """'cat: <sha> main clean' / '... dirty (4 paths)' -> dict, or None."""
    m = TREE_RE.match(text.strip())
    if not m:
        return None
    return {
        "name": m.group(1),
        "sha": m.group(2),
        "branch": m.group(3),
        "dirty": int(m.group(4) == "dirty"),
        "paths": int(m.group(5) or 0),
    }


def run_info(text):
    """A run-info.txt as a dict. format is 'windows' (os: windows), 'proton'
    (a proton: line) or 'mac' (a backend: line); 'unknown' otherwise."""
    out = {
        "format": "unknown",
        "uname": None,
        "time": None,
        "time_raw": None,
        "trees": {},
        "problems": [],
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "host" and out["time_raw"] is None:
            parts = val.split()
            if parts and ISO_RE.match(parts[-1]):
                out["time_raw"] = parts[-1]
                parts = parts[:-1]
            out["uname"] = " ".join(parts)
        elif key == "os":
            out["os_line"] = val
            if val.startswith("windows"):
                out["format"] = "windows"
        elif key == "proton":
            out["proton"] = val.split("  prefix:")[0].strip()
            if "prefix:" in val:
                out["prefix"] = val.split("prefix:", 1)[1].strip()
            if out["format"] == "unknown":
                out["format"] = "proton"
        elif key == "backend":
            out["backend"] = val
            if out["format"] == "unknown":
                out["format"] = "mac"
        elif key == "play":
            # golden play: 'menu (metal), from the title'.
            m = re.match(r"(\S+) \((\w+)\)", val)
            if m:
                out["play"], out["backend"] = m.group(1), m.group(2)
            if out["format"] == "unknown":
                out["format"] = "mac"
        elif key == "env":
            out["env"] = val
        elif key == "args":
            out["args"] = val
        elif key == "limit":
            m = LIMIT_RE.match(val)
            out["limit_secs"] = float(m.group(1)) if m else None
        elif key == "lock":
            m = WAIT_RE.search(val)
            out["lock_wait_secs"] = float(m.group(1)) if m else None
        elif key == "built":
            built_line(out, val)
        elif key == "exe":
            m = EXE_LINE_RE.match(val)
            if m:
                out["exe_sha256"], out["exe"] = m.group(1), m.group(2)
        elif key == "tree":
            m = MAC_TREE_RE.match(val)
            if m:
                out["trees"].setdefault(
                    "game",
                    {
                        "name": "game",
                        "path": m.group(1),
                        "sha": m.group(2),
                        "branch": None,
                        "dirty": int(bool(m.group(3)) and int(m.group(3)) > 0),
                        "paths": int(m.group(3) or 0),
                    },
                )
        elif key in ("toolkit", "cli") and out["format"] == "mac":
            # golden run's own lines (task 3.2): '<path> <sha> clean|(n paths dirty)'.
            m = MAC_TREE_RE.match(val)
            if m:
                out["trees"][key] = {
                    "name": key,
                    "path": m.group(1),
                    "sha": m.group(2),
                    "branch": None,
                    "dirty": int(bool(m.group(3)) and int(m.group(3)) > 0),
                    "paths": int(m.group(3) or 0),
                }
        elif key == "build":
            out["build_uuid"] = val.split()[0] if val else None
        elif key == "flips":
            m = FLIPS_RE.match(val)
            if m:
                out["flips"] = int(m.group(1))
                out["flips_exact"] = int("at least" not in m.group(2))
        elif key == "load":
            m = LOAD_POSIX_RE.match(val) or LOAD_WIN_RE.match(val)
            if m:
                out["load_start"], out["load_end"] = float(m.group(1)), float(m.group(2))
                out["ncpu"] = int(m.group(3))
                if "%" in val:
                    out["load_unit"] = "%"
            else:
                m = LOAD_MAC_RE.match(val)
                if m:
                    out["load_start"], out["ncpu"] = float(m.group(1)), int(m.group(2))
        elif key == "cpu-stall":
            m = STALL_RE.match(val)
            out["cpu_stall_pct"] = float(m.group(1)) if m else None
        elif key == "busy":
            out["busy"] = int(not val.startswith("no"))
            if out["busy"]:
                out["busy_why"] = val
        elif key == "elapsed":
            m = ELAPSED_RE.match(val)
            if m:
                out["secs"], out["exit_code"] = float(m.group(1)), int(m.group(2))
        elif key == "stop":
            m = STOP_RE.match(val)
            if m:
                out["stop"] = m.group(1)
                out["flips"] = int(m.group(2))
                out["flips_exact"] = 1
                out["secs"] = float(m.group(3))
            else:
                out["stop"] = val
        elif EXE_LINE_RE.match(line.strip()) and "exe_sha256" not in out:
            m = EXE_LINE_RE.match(line.strip())
            out["exe_sha256"], out["exe"] = m.group(1), m.group(2)
    if "exe_sha256" not in out:
        for line in text.splitlines():
            m = EXE_LINE_RE.match(line.strip())
            if m:
                out["exe_sha256"], out["exe"] = m.group(1), m.group(2)
                break
    if out["format"] == "unknown":
        out["problems"].append("run-info in no known format")
    if "exe_sha256" not in out:
        out["problems"].append("no exe line")
    if "flips" not in out:
        out["problems"].append("no flips line")
    if out["format"] in ("proton", "windows") and not out.get("shipped") and not out["trees"]:
        out["problems"].append("no built: lines")
    return out


def built_line(out, val):
    """One `built:` line (a provenance.txt line quoted by run_game)."""
    t = tree_line(val)
    if t:
        name = t["name"]
        if name not in ("toolkit", "cli"):
            out["trees"]["game"] = dict(t, name=name)
        else:
            out["trees"][name] = t
        return
    if val.startswith("build: "):
        out["build_uuid"] = val.split()[1]
        return
    if val.startswith("exe: "):
        p = val.split()
        if len(p) >= 2:
            out["built_exe_sha256"] = p[1]
        return
    if val.startswith("gen: "):
        out["gen"] = val[5:]
        return
    m = SHIPPED_RE.match(val)
    if m:
        out["shipped"] = {
            "how": "built" if m.group(1) == "built" else "shipped",
            "at": iso_utc(m.group(2)),
            "on": m.group(3),
            "from": m.group(4),
        }
        return
    m = HAND_BUILT_RE.match(val)
    if m:
        out["shipped"] = {"how": "built", "at": iso_utc(m.group(2)), "on": m.group(1), "from": ""}
        return
    m = SYNCED_RE.match(val)
    if m:
        out["synced"] = {"at": iso_utc(m.group(1)), "from": m.group(2)}
        return
    if val.startswith("unknown sources"):
        out["problems"].append("build unknown (%s)" % val)


def host_key(info, text, cfg):
    """(key, why) of a run's host: the [hosts] markers over the run-info's
    text first, then the uname, exactly (case-sensitive: 'guindos' and
    'GUINDOS' are the same PC under two OSes), else (None, 'host unknown')
    (D12.2)."""
    for h in cfg.hosts.values():
        if any(m.search(text) for m in h.markers):
            return h.key, "marker"
    uname = info.get("uname") or ""
    if uname:
        for h in cfg.hosts.values():
            if uname in h.names:
                return h.key, "name"
    return None, "host unknown"


# ---- golden.txt ----------------------------------------------------------------

FRAME_RE = re.compile(r"^([A-Z][A-Z-]+)\s+(\S+) \(dump (\d+), present (\d+)\)(.*)$")
REF_EQ_RE = re.compile(r"^ = (\S+)")
REF_VS_RE = re.compile(r"^ vs (\S+?):")
MAE_RE = re.compile(r"\bmae ([\d.]+)/([\d.]+)/([\d.]+)")
BAD_RE = re.compile(r"\bbad ([\d.]+)%")
TILE_RE = re.compile(r"\bworst tile ([\d.]+)%")
PRESENT_RE = re.compile(r"^present: (\d+)/(\d+) flips mismatched")
VERDICT_RE = re.compile(r"^verdict: (\S+) (\S+)\s*$")
WORDS = (
    "pass",
    "pass-with-known",
    "REGRESSION",
    "FAIL-RUN",
    "FAIL-PRESENT",
    "INCOMPLETE",
    "MISSING",
    "NEWVIEW",
    "INCONCLUSIVE",
)


def golden_txt(text):
    """golden.txt: end, present, one frame check per frame line, the
    golden: line and the verdict (scenario, word)."""
    out = {"frames": [], "problems": []}
    for line in text.splitlines():
        if line.startswith("end: "):
            out["end"] = line[5:].strip()
            continue
        m = PRESENT_RE.match(line)
        if m:
            out["present"] = (int(m.group(1)), int(m.group(2)))
            continue
        if line.startswith("golden: "):
            out["golden"] = line[8:].strip()
            continue
        m = VERDICT_RE.match(line)
        if m:
            out["verdict"] = (m.group(1), m.group(2))
            if m.group(2) not in WORDS:
                out["problems"].append("unknown verdict word %s" % m.group(2))
            continue
        m = FRAME_RE.match(line)
        if m:
            rest = m.group(5)
            ref = REF_EQ_RE.match(rest) or REF_VS_RE.match(rest)
            mae = MAE_RE.search(rest)
            bad = BAD_RE.search(rest)
            tile = TILE_RE.search(rest)
            out["frames"].append(
                {
                    "result": m.group(1),
                    "frame": m.group(2),
                    "dump": int(m.group(3)),
                    "present": int(m.group(4)),
                    "ref": ref.group(1) if ref else None,
                    "mae_r": float(mae.group(1)) if mae else None,
                    "mae_g": float(mae.group(2)) if mae else None,
                    "mae_b": float(mae.group(3)) if mae else None,
                    "bad_pct": float(bad.group(1)) if bad else None,
                    "tile_pct": float(tile.group(1)) if tile else None,
                }
            )
    if "verdict" not in out:
        out["problems"].append("golden.txt has no verdict line")
    return out


# ---- session TSVs --------------------------------------------------------------

SCEN_RE = re.compile(r"^([\w.-]+)=(\d{8}-\d{6}(?:-[A-Za-z0-9_.-]+)?):(\S+)$")


def sessions_tsv(text):
    """Each line of golden-sessions.tsv or its .reconstructed twin: time,
    kind, tests, only, {scenario: (stamp, word)}, rc (None: unknown), host
    (None when the line has no host=), src, and the line itself (its
    sha256 is the session's key, D12.3). Comment lines are skipped."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        s = {
            "line": line,
            "time": f[0],
            "kind": None,
            "tests": None,
            "only": None,
            "scen": {},
            "rc": None,
            "host": None,
            "src": None,
        }
        for i, x in enumerate(f[1:]):
            if i == 0 and "=" not in x:
                s["kind"] = x
                continue
            m = SCEN_RE.match(x)
            k, _, v = x.partition("=")
            if k == "kind":
                s["kind"] = v
            elif k in ("tests", "only", "host", "src"):
                s[k] = v
            elif k == "rc":
                s["rc"] = _num(v, int)
            elif m:
                s["scen"][m.group(1)] = (m.group(2), m.group(3))
        out.append(s)
    return out


def line_uuid(game, line):
    """A TSV line's session key: the game plus the line's sha256 (D5)."""
    return "tsv:%s:%s" % (game, hashlib.sha256(line.encode()).hexdigest()[:32])


def tsv_time(t):
    """'2026-10-24 13:43:58' (the controller's local time) as UTC."""
    try:
        dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- small records --------------------------------------------------------------


def key_value(text):
    """'key: value' lines (tests-pass/<key16>.txt)."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def tests_pass(text):
    kv = key_value(text)
    return {
        "key": kv.get("key"),
        "host": kv.get("host"),
        "passed": kv.get("passed"),
        "cli": kv.get("cli"),
        "cmake": kv.get("cmake"),
        "game_src": kv.get("game_src"),
        "llvm_mingw": kv.get("llvm_mingw"),
        "proton": kv.get("proton"),
        "prefix": kv.get("prefix"),
    }


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def patch_json(doc):
    """patch.json (package/patch.py): versions and sizes."""
    if not isinstance(doc, dict):
        return None
    shipped = (doc.get("changed") or []) + (doc.get("new") or [])
    every = shipped + (doc.get("kept") or [])
    return {
        "version": doc.get("version"),
        "base_version": doc.get("base_version"),
        "base_manifest_sha256": doc.get("base_manifest_sha256"),
        "payload_bytes": sum(int(e.get("size", 0)) for e in shipped),
        "full_bytes": sum(int(e.get("size", 0)) for e in every),
        "files": {k: len(doc.get(k) or []) for k in ("changed", "new", "deleted", "kept")},
    }


def bundle_index(doc):
    """bundles/<target>/index.json: {manifest sha: {version, made, bundle, size}}."""
    if not isinstance(doc, dict):
        return []
    out = []
    for sha, e in sorted(doc.items()):
        if isinstance(e, dict):
            out.append(dict(e, manifest_sha256=sha))
    return out


# ---- the flip trace --------------------------------------------------------------

HITCH_MS = 50.0


def flip_stats(log):
    """Run-wide frame statistics from a game log's flip trace, through
    pacing_stats' parser (one reading of the trace): flips, the interval
    percentiles, flips/s, hitches (intervals over HITCH_MS) and the 1% low
    fps. None when the log has no flip lines."""
    from .. import pacing_stats

    try:
        run = pacing_stats.parse(log)
    except OSError:
        return None
    t = run["flip_us"]
    if len(t) < 3:
        return None
    flips = sorted(t)
    iv = [(t[f] - t[f - 1]) / 1000.0 for f in flips if f - 1 in t]
    if len(iv) < 2:
        return None
    span = (t[flips[-1]] - t[flips[0]]) / 1e6
    worst = sorted(iv, reverse=True)[: max(1, len(iv) // 100)]
    low1 = 1000.0 / (sum(worst) / len(worst)) if worst and sum(worst) > 0 else None
    return {
        "flips": flips[-1],
        "flips_exact": 1,
        "secs": span,
        "flips_per_s": (len(flips) - 1) / span if span > 0 else None,
        "ms_p50": pacing_stats.pct(iv, 0.50),
        "ms_p95": pacing_stats.pct(iv, 0.95),
        "ms_p99": pacing_stats.pct(iv, 0.99),
        "ms_max": max(iv),
        "hitches": sum(1 for x in iv if x > HITCH_MS),
        "low1_fps": low1,
        "coarse": run.get("coarse", False),
        # Time to the first traced flip (the boot time charts drew), when
        # the trace's clock starts before it (the [GPU] fallback starts at 0).
        "first_s": t[flips[0]] / 1e6 if t[flips[0]] > 0 else None,
    }


def size_of(path):
    """(bytes, files) under path, links not followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size, 1
    total = files = 0
    for dp, _dn, fn in os.walk(path):
        for n in fn:
            try:
                total += os.lstat(os.path.join(dp, n)).st_size
                files += 1
            except OSError:
                pass
    return total, files
