"""What a preset costs before anything is built (D6): pack bytes per encode and
scale, the VRAM each dumped scene holds, the upscale's minutes.

Time comes from seconds per source megatexel: a row in
build-logs/timings.json when one was measured here (a flat label, so package's
plan reads the file as it always did), else SEEDS, measured on an M4 Max,
and covering the upscale only. `lanczos`, `epx` and `nearest` have no
seed: the spike's were numpy stand-ins, not this CLI's pure-Python kernels, so
they read "not measured yet" until a build times them.
"""

import json
import os

from ..package.progress import load_timings
from . import classes, traced

RATE_PREFIX = "upscale s/Mtexel: "
SEEDS = {
    "xbr": 8.2,
    "realesr-animevideov3": 1.07,
    "realesrgan-x4plus": 11.7,
    "realesrgan-x4plus-anime": 4.1,
    "waifu2x-cunet": 2.6,
    "realcugan-se": 9.6,
    "upscayl-2xhdcube": 1.0,
    "realesr-general-x4v3": 0.57,
    "4xhdcube4compact": 0.34,
    "pbrify-spanv4": 0.51,
    "rplksrd": 3.6,
}
SCALES = (2, 3, 4)
MB = 1000000


def rate(upscaler, timings):
    """Seconds per source megatexel, or None when not measured yet."""
    # With no named font the atlas is upscaled by xbr, and a font fit costs less.
    name = "xbr" if upscaler == "atlas" else upscaler
    v = timings.get(RATE_PREFIX + name)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return SEEDS.get(name)


def save_rate(logs, upscaler, seconds_per_mtexel):
    """One measured rate, beside the steps' durations; never fails a build."""
    d = load_timings(logs)
    d[RATE_PREFIX + upscaler] = round(seconds_per_mtexel, 3)
    try:
        os.makedirs(logs, exist_ok=True)
        p = os.path.join(logs, "timings.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def treated(entries, preset, config, cfg):
    """[(entry, treatment)] of what the preset would build, the trace's proposals
    under the owner's rules."""
    use_trace = preset == "traced" or config.get("coverage", {}).get("mode") == "traced"
    r = config.get("render_scale", 1)
    untraced = config.get("coverage", {}).get("untraced", "class")
    out = []
    for e in entries:
        if not classes.in_preset(e, preset, cfg):
            continue
        proposal = None
        if use_trace:
            mag = e.get("mag_scale1")
            if mag is not None:
                proposal = traced.propose(mag, r)
            elif untraced == "skip":
                proposal = traced.SKIP
        t = classes.treatment(e, config, preset, cfg, proposal)
        if not t["skip"]:
            out.append((e, t))
    return out


def _new_row():
    return {
        "entries": 0,
        "texels": 0,
        "pack_bytes": 0,
        "raw_bytes": 0,
        "block_bytes": 0,
        "by_scale": {s: {"raw": 0, "block": 0} for s in SCALES},
        "build_seconds": 0.0,
        "unmeasured": [],
    }


def estimate(entries, preset, config, cfg, timings, jobs=1):
    """The preset's figures: per class and in total, per dumped scene, and
    against the config's budget. MB are decimal, as the inventory doc's."""
    rows = {}
    scenes = {}
    picked = treated(entries, preset, config, cfg)
    pack_of = {}
    for e, t in picked:
        r = rows.setdefault(e["class"], _new_row())
        src = e["width"] * e["height"]
        r["entries"] += 1
        r["texels"] += src
        n = classes.pack_bytes(e, t["scale"], t["encode"], t["mips"])
        pack_of[e["hash"]] = n
        r["pack_bytes"] += n
        r["raw_bytes"] += classes.pack_bytes(e, t["scale"], "raw", t["mips"])
        r["block_bytes"] += classes.pack_bytes(e, t["scale"], "auto", t["mips"])
        for s in SCALES:
            r["by_scale"][s]["raw"] += classes.pack_bytes(e, s, "raw", t["mips"])
            r["by_scale"][s]["block"] += classes.pack_bytes(e, s, "auto", t["mips"])
        per = rate(t["upscaler"], timings)
        if per is None:
            if t["upscaler"] not in r["unmeasured"]:
                r["unmeasured"].append(t["upscaler"])
        else:
            r["build_seconds"] += per * src / MB
    # The cache holds what a scene uploaded: a pack entry in place of its
    # stock texture, everything else as the guest had it.
    for e in entries:
        for scene in e.get("scenes", {}):
            s = scenes.setdefault(scene, {"stock_bytes": 0, "pack_bytes": 0, "entries": 0})
            stock = classes.stock_bytes(e)
            s["stock_bytes"] += stock
            s["pack_bytes"] += pack_of.get(e["hash"], stock)
            s["entries"] += 1
    jobs = max(1, jobs)
    for r in rows.values():
        r["build_seconds"] = r["build_seconds"] / jobs
    total = sum(r["pack_bytes"] for r in rows.values())
    budget = config.get("budget", {})
    over = []
    if budget.get("pack_mb") and total > budget["pack_mb"] * MB:
        over.append("pack %d MB over the budget's %d" % (total // MB, budget["pack_mb"]))
    if budget.get("vram_mb"):
        for name, s in sorted(scenes.items()):
            if s["pack_bytes"] > budget["vram_mb"] * MB:
                over.append(
                    "%s: VRAM %d MB over the budget's %d"
                    % (name, s["pack_bytes"] // MB, budget["vram_mb"])
                )
    return {
        "preset": preset,
        "render_scale": config.get("render_scale", 1),
        "jobs": jobs,
        "classes": rows,
        "scenes": scenes,
        "total_bytes": total,
        "over_budget": over,
    }


def _mb(n):
    return "%.1f" % (n / MB)


def minutes(row):
    """The class's upscale minutes, or why there is no figure."""
    if row["unmeasured"]:
        return "not measured yet (%s)" % ", ".join(row["unmeasured"])
    return "%.1f" % (row["build_seconds"] / 60)


def report(doc):
    """The estimate's lines."""
    out = [
        "preset %s at render scale %d, %d jobs" % (doc["preset"], doc["render_scale"], doc["jobs"])
    ]
    head = "  %-6s %8s %9s %10s %10s %10s  %s"
    out.append(
        head % ("class", "entries", "Mtexels", "pack MB", "raw MB", "bc MB", "upscale minutes")
    )
    rows = doc["classes"]
    for c in classes.CLASSES:
        r = rows.get(c)
        if not r:
            continue
        out.append(
            head
            % (
                c,
                r["entries"],
                "%.2f" % (r["texels"] / MB),
                _mb(r["pack_bytes"]),
                _mb(r["raw_bytes"]),
                _mb(r["block_bytes"]),
                minutes(r),
            )
        )
    out.append("  total pack: %s MB" % _mb(doc["total_bytes"]))
    if rows:
        out.append("  every entry at one scale (raw / bc1+bc3 MB, each class's mips):")
        for s in SCALES:
            raw = sum(r["by_scale"][s]["raw"] for r in rows.values())
            block = sum(r["by_scale"][s]["block"] for r in rows.values())
            out.append("    %dx  %10s / %s" % (s, _mb(raw), _mb(block)))
    if doc["scenes"]:
        out.append("  resident VRAM per dumped scene (MB, stock -> with this pack):")
        for name, s in sorted(doc["scenes"].items()):
            out.append(
                "    %-24s %8s -> %s  (%d textures)"
                % (name, _mb(s["stock_bytes"]), _mb(s["pack_bytes"]), s["entries"])
            )
    else:
        out.append("  resident VRAM: no dump in this inventory")
    for line in doc["over_budget"]:
        out.append("  over budget: %s" % line)
    return out
