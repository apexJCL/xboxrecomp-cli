"""The report's charts, written as SVG text (bench-report D3, D7).

Every chart takes a palette: the private page passes CSS variables, so
the charts follow the page's light and dark tokens; the public export
passes PUBLIC_LIGHT or PUBLIC_DARK with the colours baked in, since a
README image cannot read the page's CSS. Colours follow the dataviz
reference palette: the categorical slots in fixed order, status colours
only for the verdict, and text in the ink tokens, never a series colour.
"""

import math

from .bench_report import RIM, esc, fmt

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'

# The page's tokens (bench_report_html defines them for both themes).
PRIVATE = {
    "mean": "var(--mean)",
    "low1": "var(--low1)",
    "muted": "var(--muted)",
    "ink": "var(--ink)",
    "ink2": "var(--ink-2)",
    "grid": "var(--grid)",
    "axis": "var(--axis)",
    "good": "var(--good)",
    "bad": "var(--bad)",
    "hosts": ["var(--h1)", "var(--h2)", "var(--h3)"],
    "surface": "var(--surface)",
}
# GitHub shows a README image on #ffffff or #0d1117; tests check each
# palette against its background (3:1 for marks, 4.5:1 for text).
PUBLIC_LIGHT = {
    "mean": "#2a78d6",
    "low1": "#c4501f",
    "muted": "#7a7873",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "good": "#0a850a",
    "bad": "#d03b3b",
    "hosts": ["#2a78d6", "#c4501f", "#138a60"],
}
PUBLIC_DARK = {
    "mean": "#3987e5",
    "low1": "#d95926",
    "muted": "#898781",
    "ink": "#f0efec",
    "ink2": "#c3c2b7",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "good": "#2fb82f",
    "bad": "#e66767",
    "hosts": ["#3987e5", "#d95926", "#199e70"],
}
# Which palette keys are marks and which are text, for the contrast test.
MARK_KEYS = ("mean", "low1", "muted", "ink2")
TEXT_KEYS = ("ink", "ink2")

CAPTION = "Means at the target sit on the ring; the gaps show in the 1% low line and in the table."


def num(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def svg_open(w, h, title, desc, ident, extra=""):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
        'role="img" aria-labelledby="%s-t %s-d" font-family=\'%s\'%s>'
        '<title id="%s-t">%s</title><desc id="%s-d">%s</desc>'
        % (w, h, w, h, ident, ident, FONT, extra, ident, esc(title), ident, esc(desc))
    )


def runs_of(present):
    """The contiguous runs of present spokes, cyclic: [[i, ...], ...]; one
    run of all spokes when none is missing."""
    n = len(present)
    if all(present):
        return [list(range(n))]
    start = next(i for i in range(n) if not present[i])
    out, cur = [], []
    for k in range(1, n + 1):
        i = (start + k) % n
        if present[i]:
            cur.append(i)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def radar_groups(model):
    """[(caption suffix, [axis index ...])]: one radar of every arm, or one
    per value of the first split axis (else the first axis) past 24."""
    from .bench_report import RADAR_MAX_SPOKES

    arms = next(iter(model["hosts"].values()))["arms"] if model["hosts"] else []
    if len(arms) <= RADAR_MAX_SPOKES:
        return [("", list(range(len(arms))))]
    split = model.get("split") or []
    axis = next((a for a in model["axes"] if a["name"] in split), model["axes"][0])
    out = []
    for v, d in zip(axis["values"], axis["display"]):
        out.append((d, [i for i, e in enumerate(arms) if e["values"][axis["name"]] == v]))
    return out


def radar(model, host, pal, ident, only=None, baseline=None, tips=False, suffix="", caption=False):
    """One host's radar (D3) over the arms at indexes `only`."""
    hm = model["hosts"][host]
    target = model["target_fps"]
    arms = [hm["arms"][i] for i in (only if only is not None else range(len(hm["arms"])))]
    n = len(arms)
    W, H = 780, 600 if caption else 580
    cx, cy, R = 390, 282, 196

    def at(i, frac, extra=0.0):
        a = 2 * math.pi * i / n - math.pi / 2
        r = frac * R + extra
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def frac(v):
        return min(v / target, RIM) / RIM

    ok = [e["status"] == "ok" and (e.get("mean") or {}).get("median") is not None for e in arms]
    means = [(e["mean"]["median"] if k else None) for e, k in zip(arms, ok)]
    lows = [((e.get("low1") or {}).get("median") if k else None) for e, k in zip(arms, ok)]
    thr = hm["best"]["thresholds"]["mean_share_min"] * target
    run_arms = [(e, m) for e, m in zip(arms, means) if m is not None]
    within = sum(1 for _, m in run_arms if m >= thr)
    if run_arms:
        # The D4 pick, not the highest mean: the table's best row and the
        # screen reader's must agree.
        disp = {e["id"]: e["display"] for e in arms}
        picks = [
            "%s%s"
            % (disp[p["arm"]], (" at " + p["split_display"]) if p.get("split_display") else "")
            for p in hm["best"]["picks"]
            if p.get("arm") in disp
        ]
        pick_txt = (
            ("Best: %s." % "; ".join(picks)) if picks else "No tested setting holds the target."
        )
        top = max(run_arms, key=lambda x: (x[1], (x[0].get("low1") or {}).get("median") or 0))
        worst = min(run_arms, key=lambda x: (x[1], (x[0].get("low1") or {}).get("median") or 0))
        desc = (
            "Target %s fps. %s Highest mean: %s at %s fps; lowest: %s at %s fps. "
            "Of the %d settings run, %d hold a mean within %d%% of the target."
            % (
                num(target),
                pick_txt,
                top[0]["display"],
                fmt(top[1]),
                worst[0]["display"],
                fmt(worst[1]),
                len(run_arms),
                within,
                round(100 * hm["best"]["thresholds"]["mean_share_min"]),
            )
        )
    else:
        desc = "Target %s fps. No arm has clean runs." % num(target)
    title = "%s: mean fps and 1%% low per setting, as a share of %s fps%s" % (
        hm["label"],
        num(target),
        (" (" + suffix + ")") if suffix else "",
    )
    p = [svg_open(W, H, title, desc, ident)]
    # Rings: 25/50/75% thin, the target heavier and labelled.
    for f in (0.25, 0.5, 0.75):
        p.append(
            '<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="%s" stroke-width="1"/>'
            % (cx, cy, f / RIM * R, pal["grid"])
        )
        p.append(
            '<text x="%d" y="%.1f" font-size="10" fill="%s">%d%%</text>'
            % (cx + 4, cy - f / RIM * R - 3, pal["ink2"], round(f * 100))
        )
    p.append(
        '<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="%s" stroke-width="1"/>'
        % (cx, cy, R, pal["grid"])
    )
    p.append(
        '<circle cx="%d" cy="%d" r="%.1f" fill="none" stroke="%s" stroke-width="2"/>'
        % (cx, cy, 1 / RIM * R, pal["ink2"])
    )
    p.append(
        '<text x="%d" y="%.1f" font-size="11" font-weight="600" fill="%s">target %s fps</text>'
        % (cx + 4, cy - R / RIM - 4, pal["ink"], num(target))
    )
    # Spokes and labels.
    for i, e in enumerate(arms):
        x, y = at(i, 1.0)
        dash = "" if ok[i] else ' stroke-dasharray="2 4"'
        p.append(
            '<line x1="%d" y1="%d" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"%s/>'
            % (cx, cy, x, y, pal["axis"], dash)
        )
        lx, ly = at(i, 1.0, 14)
        c = math.cos(2 * math.pi * i / n - math.pi / 2)
        s = math.sin(2 * math.pi * i / n - math.pi / 2)
        anchor = "start" if c > 0.2 else ("end" if c < -0.2 else "middle")
        ly += 4 + (6 if s > 0.5 else (-4 if s < -0.5 else 0))
        if ok[i]:
            p.append(
                '<text x="%.1f" y="%.1f" font-size="12" text-anchor="%s" fill="%s">%s</text>'
                % (lx, ly, anchor, pal["ink"], esc(e["display"]))
            )
        else:
            p.append(
                '<text x="%.1f" y="%.1f" font-size="12" text-anchor="%s" fill="%s" font-style="italic">'
                '%s<tspan x="%.1f" dy="14">%s</tspan></text>'
                % (lx, ly, anchor, pal["ink2"], esc(e["display"]), lx, esc(e["status"]))
            )

    def series(vals, color, width, dash, fill, cls):
        present = [v is not None for v in vals]
        if not any(present):
            return
        whole = all(present)
        for run in runs_of(present):
            pts = [at(i, frac(vals[i])) for i in run]
            path = " ".join("%.1f,%.1f" % q for q in pts)
            if fill:
                if whole:
                    poly = path
                else:
                    poly = "%d,%d %s %d,%d" % (cx, cy, path, cx, cy)
                p.append(
                    '<polygon points="%s" fill="%s" fill-opacity="0.10" stroke="none"/>'
                    % (poly, color)
                )
            if len(pts) > 1:
                tag = "polygon" if whole else "polyline"
                p.append(
                    '<%s class="%s" points="%s" fill="none" stroke="%s" stroke-width="%s"%s '
                    'stroke-linejoin="round" stroke-linecap="round"/>'
                    % (tag, cls, path, color, width, dash)
                )

    if baseline:
        bm = [baseline.get(e["id"], {}).get("mean") for e in arms]
        bl = [baseline.get(e["id"], {}).get("low1") for e in arms]
        series(bm, pal["muted"], 1.5, ' stroke-dasharray="1 3"', False, "base-mean")
        series(bl, pal["muted"], 1.5, ' stroke-dasharray="1 3"', False, "base-low1")
    series(means, pal["mean"], 2, "", True, "mean")
    series(lows, pal["low1"], 2, ' stroke-dasharray="6 4"', False, "low1")
    # Spread ticks (n > 1), rim ticks for values past 110%, and markers.
    for i, e in enumerate(arms):
        if not ok[i]:
            continue
        m = e["mean"]
        if e.get("n", 0) > 1 and m["max"] > m["min"]:
            x1, y1 = at(i, frac(m["min"]))
            x2, y2 = at(i, frac(m["max"]))
            p.append(
                '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="4" '
                'stroke-opacity="0.45" stroke-linecap="round"/>' % (x1, y1, x2, y2, pal["mean"])
            )
        for v, col in ((means[i], pal["mean"]), (lows[i], pal["low1"])):
            if v is not None and v / target > RIM:
                x1, y1 = at(i, 1.0)
                x2, y2 = at(i, 1.0, 9)
                p.append(
                    '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>'
                    % (x1, y1, x2, y2, col)
                )
        if lows[i] is not None:
            x, y = at(i, frac(lows[i]))
            p.append(
                '<rect x="%.1f" y="%.1f" width="6" height="6" fill="%s" transform="rotate(45 %.1f %.1f)"/>'
                % (x - 3, y - 3, pal["low1"], x, y)
            )
        tip = "%s: mean %s fps, 1%% low %s, p99 %s ms, n %d" % (
            e["display"],
            fmt(means[i]),
            fmt(lows[i]),
            fmt((e.get("p99") or {}).get("median")),
            e.get("n", 0),
        )
        x, y = at(i, frac(means[i]))
        attr = ' data-tip="%s" tabindex="0"' % esc(tip) if tips else ""
        p.append(
            '<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="1.5"%s/>'
            % (x, y, pal["mean"], pal.get("surface", "none") if tips else "none", attr)
        )
        if tips:
            # A hit target bigger than the mark.
            p.append(
                '<circle cx="%.1f" cy="%.1f" r="11" fill="transparent" data-tip="%s"/>'
                % (x, y, esc(tip))
            )
    # Legend.
    ly = H - (42 if caption else 22)
    items = [
        ("line", pal["mean"], "", "mean fps"),
        ("line", pal["low1"], "6 4", "1% low fps"),
        ("ring", pal["ink2"], "", "target %s fps" % num(target)),
    ]
    if baseline:
        items.append(("line", pal["muted"], "1 3", baseline.get("_label") or "baseline"))
    x = 40
    for kind, col, dash, text in items:
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        if kind == "ring":
            p.append(
                '<circle cx="%d" cy="%d" r="7" fill="none" stroke="%s" stroke-width="2"/>'
                % (x + 11, ly, col)
            )
        else:
            p.append(
                '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2"%s/>'
                % (x, ly, x + 24, ly, col, d)
            )
        p.append(
            '<text x="%d" y="%d" font-size="12" fill="%s">%s</text>'
            % (x + 32, ly + 4, pal["ink"], esc(text))
        )
        x += 40 + int(len(text) * 6.8)
    if caption:
        # A README image carries its own caption; the page has a figcaption.
        p.append(
            '<text x="40" y="%d" font-size="11" fill="%s">%s</text>'
            % (H - 16, pal["ink2"], esc(caption))
        )
    p.append("</svg>")
    return "".join(p)


# --- the page's other charts ----------------------------------------------------


def nice_ticks(lo, hi, k=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / k
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        out.append(round(t, 6))
        t += step
    return out


def dot_plot(rows, hosts, pal, ident, title, unit, lower_better=False):
    """Steady fps (or p99) per arm across hosts: one row per arm, one dot
    per host, the host's slot colour, a legend above."""
    W = 1040
    left, right, top = 190, 30, 44
    rh = 20
    H = top + rh * len(rows) + 36
    vals = [v for _, per in rows for v in per.values() if v is not None]
    lo, hi = (min(vals), max(vals)) if vals else (0, 1)
    pad = (hi - lo) * 0.08 or 1
    ticks = nice_ticks(lo - pad, hi + pad)
    lo, hi = ticks[0], ticks[-1]

    def X(v):
        return left + (v - lo) / (hi - lo) * (W - left - right)

    desc = "%s per setting; %s" % (title, "lower is better" if lower_better else "higher is better")
    p = [svg_open(W, H, title, desc, ident)]
    for t in ticks:
        p.append(
            '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s"/>'
            % (X(t), top - 6, X(t), H - 30, pal["grid"])
        )
        p.append(
            '<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
            % (X(t), H - 14, pal["ink2"], num(t))
        )
    p.append(
        '<text x="%d" y="%d" font-size="11" text-anchor="end" fill="%s">%s</text>'
        % (W - right, H - 2, pal["ink2"], esc(unit))
    )
    x = left
    for k, (_h, lab) in enumerate(hosts):
        col = pal["hosts"][k % len(pal["hosts"])]
        p.append('<circle cx="%d" cy="16" r="5" fill="%s"/>' % (x + 5, col))
        p.append(
            '<text x="%d" y="20" font-size="12" fill="%s">%s</text>'
            % (x + 15, pal["ink"], esc(lab))
        )
        x += 30 + int(len(lab) * 7)
    for r, (label_, per) in enumerate(rows):
        y = top + r * rh + rh / 2
        p.append(
            '<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
            % (left - 10, y + 4, pal["ink"], esc(label_))
        )
        p.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-dasharray="1 3"/>'
            % (left, y, W - right, y, pal["grid"])
        )
        for k, (h, lab) in enumerate(hosts):
            v = per.get(h)
            if v is None:
                continue
            col = pal["hosts"][k % len(pal["hosts"])]
            tip = "%s, %s: %s %s" % (lab, label_, fmt(v), unit)
            p.append(
                '<circle cx="%.1f" cy="%.1f" r="5" fill="%s" stroke="%s" stroke-width="1.5" data-tip="%s"/>'
                % (X(v), y, col, "var(--surface)", esc(tip))
            )
    p.append("</svg>")
    return "".join(p)


def strip(groups, pal, ident, title, unit, guides=()):
    """A row of dots per group (one per run), coloured by verdict."""
    W = 1040
    left, right, top = 190, 30, 30
    rh = 34
    H = top + rh * len(groups) + 36
    vals = [v for _, pts in groups for v, _, _ in pts]
    lo, hi = (min(vals), max(vals)) if vals else (0, 1)
    ticks = nice_ticks(lo - 1, hi + 1)
    lo, hi = ticks[0], ticks[-1]

    def X(v):
        return left + (v - lo) / (hi - lo) * (W - left - right)

    p = [svg_open(W, H, title, title + ", one dot per run", ident)]
    for t in ticks:
        p.append(
            '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s"/>'
            % (X(t), top - 6, X(t), H - 30, pal["grid"])
        )
        p.append(
            '<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
            % (X(t), H - 14, pal["ink2"], num(t))
        )
    for g, lab in guides:
        if lo <= g <= hi:
            p.append(
                '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1.5" stroke-dasharray="4 3"/>'
                % (X(g), top - 10, X(g), H - 30, pal["ink2"])
            )
            p.append(
                '<text x="%.1f" y="%d" font-size="11" fill="%s">%s</text>'
                % (X(g) + 4, top - 12, pal["ink2"], esc(lab))
            )
    for r, (label_, pts) in enumerate(groups):
        y = top + r * rh + rh / 2
        p.append(
            '<text x="%d" y="%.1f" font-size="12" text-anchor="end" fill="%s">%s</text>'
            % (left - 10, y + 4, pal["ink"], esc(label_))
        )
        for k, (v, verdict, tip) in enumerate(pts):
            col = {"good": pal["good"], "bad": pal["bad"]}.get(verdict, pal["mean"])
            jitter = ((k * 7) % 11 - 5) * 1.6
            p.append(
                '<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s" fill-opacity="0.8" data-tip="%s"/>'
                % (X(v), y + jitter, col, esc(tip))
            )
    p.append("</svg>")
    return "".join(p)


def line_series(series, pal, ident, title, x_max, y_max, vline=None, x_unit="s", y_unit="fps"):
    """fps per second for many runs: thin lines coloured by verdict."""
    W, H = 1040, 300
    left, right, top, bottom = 48, 20, 16, 34

    def X(v):
        return left + v / x_max * (W - left - right)

    def Y(v):
        return top + (1 - min(v, y_max) / y_max) * (H - top - bottom)

    p = [svg_open(W, H, title, title, ident)]
    for t in nice_ticks(0, y_max):
        if t > y_max:
            continue
        p.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
            % (left, Y(t), W - right, Y(t), pal["grid"])
        )
        p.append(
            '<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="%s">%s</text>'
            % (left - 6, Y(t) + 4, pal["ink2"], num(t))
        )
    for t in nice_ticks(0, x_max, 10):
        if t > x_max:
            continue
        p.append(
            '<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
            % (X(t), H - 14, pal["ink2"], num(t))
        )
    p.append(
        '<text x="%d" y="%d" font-size="11" text-anchor="end" fill="%s">%s</text>'
        % (W - right, H - 2, pal["ink2"], x_unit)
    )
    if vline is not None:
        p.append(
            '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-dasharray="4 3"/>'
            % (X(vline), top, X(vline), H - bottom, pal["ink2"])
        )
        p.append(
            '<text x="%.1f" y="%d" font-size="11" fill="%s">steady from here</text>'
            % (X(vline) + 4, top + 10, pal["ink2"])
        )
    for ys, verdict, tip in series:
        col = {"good": pal["good"], "bad": pal["bad"]}.get(verdict, pal["mean"])
        pts = " ".join("%.1f,%.1f" % (X(i + 0.5), Y(v)) for i, v in enumerate(ys))
        p.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.2" stroke-opacity="0.55" data-tip="%s"/>'
            % (pts, col, esc(tip))
        )
    p.append("</svg>")
    return "".join(p)


def histogram(groups, pal, ident, title, bins, unit="ms"):
    """Steady frame-time shares per bin, a step line per mode."""
    W, H = 1040, 260
    left, right, top, bottom = 48, 20, 16, 34
    ymax = max([v for _, _, h in groups for v in h] or [1]) * 1.1 or 1

    def X(v):
        return left + v / bins[-1] * (W - left - right)

    def Y(v):
        return top + (1 - v / ymax) * (H - top - bottom)

    p = [svg_open(W, H, title, title, ident)]
    for t in nice_ticks(0, ymax * 100, 4):
        if t / 100 > ymax:
            continue
        p.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
            % (left, Y(t / 100), W - right, Y(t / 100), pal["grid"])
        )
        p.append(
            '<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="%s">%s%%</text>'
            % (left - 6, Y(t / 100) + 4, pal["ink2"], num(t))
        )
    for t in bins[:: max(1, len(bins) // 10)]:
        p.append(
            '<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
            % (X(t), H - 14, pal["ink2"], num(t))
        )
    p.append(
        '<text x="%d" y="%d" font-size="11" text-anchor="end" fill="%s">frame time, %s (last bin: and over)</text>'
        % (W - right, H - 2, pal["ink2"], unit)
    )
    for verdict, lab, h in groups:
        col = {"good": pal["good"], "bad": pal["bad"]}.get(verdict, pal["mean"])
        pts = []
        for i, v in enumerate(h):
            pts += ["%.1f,%.1f" % (X(bins[i]), Y(v)), "%.1f,%.1f" % (X(bins[i + 1]), Y(v))]
        p.append(
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="2" data-tip="%s"/>'
            % (" ".join(pts), col, esc(lab))
        )
    p.append("</svg>")
    return "".join(p)


def scatter(points, pal, ident, title, x_label, y_label, vline=None, hline=None):
    W, H = 1040, 300
    left, right, top, bottom = 56, 20, 16, 34
    xs = [x for x, _, _, _ in points] or [0, 1]
    ys = [y for _, y, _, _ in points] or [0, 1]
    xt = nice_ticks(min(xs + [vline or min(xs)]) - 1, max(xs) + 1)
    yt = nice_ticks(0, max(ys + [hline or 0]) * 1.05 + 1)

    def X(v):
        return left + (v - xt[0]) / (xt[-1] - xt[0]) * (W - left - right)

    def Y(v):
        return top + (1 - v / yt[-1]) * (H - top - bottom)

    p = [svg_open(W, H, title, title, ident)]
    for t in yt:
        p.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
            % (left, Y(t), W - right, Y(t), pal["grid"])
        )
        p.append(
            '<text x="%d" y="%.1f" font-size="11" text-anchor="end" fill="%s">%s</text>'
            % (left - 6, Y(t) + 4, pal["ink2"], num(t))
        )
    for t in xt:
        p.append(
            '<text x="%.1f" y="%d" font-size="11" text-anchor="middle" fill="%s">%s</text>'
            % (X(t), H - 14, pal["ink2"], num(t))
        )
    p.append(
        '<text x="%d" y="%d" font-size="11" text-anchor="end" fill="%s">%s</text>'
        % (W - right, H - 2, pal["ink2"], esc(x_label))
    )
    p.append(
        '<text x="%d" y="%d" font-size="11" fill="%s">%s</text>'
        % (left + 4, top + 10, pal["ink2"], esc(y_label))
    )
    if vline is not None:
        p.append(
            '<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-dasharray="4 3"/>'
            % (X(vline), top, X(vline), H - bottom, pal["ink2"])
        )
    if hline is not None:
        p.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-dasharray="4 3"/>'
            % (left, Y(hline), W - right, Y(hline), pal["ink2"])
        )
    for x, y, verdict, tip in points:
        col = {"good": pal["good"], "bad": pal["bad"]}.get(verdict, pal["mean"])
        p.append(
            '<circle cx="%.1f" cy="%.1f" r="5" fill="%s" fill-opacity="0.8" data-tip="%s"/>'
            % (X(x), Y(y), col, esc(tip))
        )
    p.append("</svg>")
    return "".join(p)


# --- contrast (the test's yardstick) --------------------------------------------


def luminance(hexc):
    h = hexc.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
