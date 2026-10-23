"""The private report (bench-report D7): one self-contained HTML page with
the title-scene results page's layout, generated from report.json and summary.json.
It stays on the owner's machine; the public export (bench_report_public)
never reads it."""

import json
import os
import re
import statistics

from . import bench_report as R
from . import bench_report_svg as S
from .bench_report import esc, fmt

CSS = """
:root {
  --bg: #f4f5f3; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #7a7873;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.11); --accent: #1c5cab;
  --mean: #2a78d6; --low1: #c4501f; --h1: #2a78d6; --h2: #c4501f; --h3: #138a60;
  --good: #0a850a; --bad: #d03b3b;
  --pill-ok-bg: #e3f1e6; --pill-ok-fg: #0d5e1c; --pill-warn-bg: #fbecd6; --pill-warn-fg: #7a4500;
  --pill-bad-bg: #fbe1df; --pill-bad-fg: #8c1d1b;
  --f-body: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --f-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e0f10; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --accent: #86b6ef;
    --mean: #3987e5; --low1: #d95926; --h1: #3987e5; --h2: #d95926; --h3: #199e70;
    --good: #2fb82f; --bad: #e66767;
    --pill-ok-bg: #16301c; --pill-ok-fg: #8fdc9c; --pill-warn-bg: #3a2a10; --pill-warn-fg: #f2c46d;
    --pill-bad-bg: #3d1716; --pill-bad-fg: #f3a09e;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e0f10; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --accent: #86b6ef;
  --mean: #3987e5; --low1: #d95926; --h1: #3987e5; --h2: #d95926; --h3: #199e70;
  --good: #2fb82f; --bad: #e66767;
  --pill-ok-bg: #16301c; --pill-ok-fg: #8fdc9c; --pill-warn-bg: #3a2a10; --pill-warn-fg: #f2c46d;
  --pill-bad-bg: #3d1716; --pill-bad-fg: #f3a09e;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--f-body);
  font-size: 15.5px; line-height: 1.58; padding: 28px 16px 64px; }
.wrap { max-width: 1040px; margin: 0 auto; display: flex; flex-direction: column; gap: 48px; }
/* Flex items default to their content's width: a long build id or a wide
   table would push the page past a phone's width. */
.wrap > *, header > * { min-width: 0; }
h1, .meta { overflow-wrap: anywhere; }
.prose { max-width: 70ch; }
h1, h2, h3 { margin: 0; line-height: 1.15; text-wrap: balance; }
h1 { font-size: clamp(28px, 5vw, 40px); font-weight: 650; }
h2 { font-size: 24px; font-weight: 650; }
h3 { font-size: 18px; font-weight: 600; margin-top: 8px; }
p { margin: 0; }
section { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.sec-head { border-top: 1px solid var(--axis); padding-top: 16px; display: flex; flex-direction: column; gap: 6px; }
.eyebrow { font-family: var(--f-mono); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-2); }
.meta { display: flex; flex-wrap: wrap; gap: 4px 18px; font-family: var(--f-mono); font-size: 12.5px; color: var(--ink-2); }
code, .mono { font-family: var(--f-mono); font-size: .88em; }
.hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.card .name { font-weight: 600; font-size: 17px; }
.figs { display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: 8px; }
.fig .v { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
.fig .l { font-size: 12px; color: var(--ink-2); }
.pill { font-family: var(--f-mono); font-size: 11.5px; padding: 2px 8px; border-radius: 999px; white-space: nowrap; align-self: flex-start; }
.pill.ok { background: var(--pill-ok-bg); color: var(--pill-ok-fg); }
.pill.warn { background: var(--pill-warn-bg); color: var(--pill-warn-fg); }
.pill.bad { background: var(--pill-bad-bg); color: var(--pill-bad-fg); }
figure { margin: 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
figure svg { width: 100%; height: auto; max-width: 1040px; display: block; }
figure.radar svg { max-width: 780px; }
figcaption { font-size: 13px; color: var(--ink-2); max-width: 70ch; }
.scroll { overflow-x: auto; max-width: 100%; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }
table { border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; width: 100%; }
th, td { padding: 5px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--grid); }
th { font-weight: 600; color: var(--ink-2); background: var(--surface); position: sticky; top: 0; }
td:first-child, th:first-child { text-align: left; }
tr.pick td { font-weight: 600; }
tr.gap td { color: var(--ink-2); font-style: italic; }
.box { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; max-width: 70ch; }
ul { margin: 0; padding-left: 1.2em; display: flex; flex-direction: column; gap: 4px; }
details { max-width: 70ch; }
details summary { cursor: pointer; font-weight: 600; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 8px 0 0; font-size: 13.5px; }
dt { color: var(--ink-2); }
dd { margin: 0; }
.notes pre { white-space: pre-wrap; }
.note { font-size: 13px; color: var(--ink-2); max-width: 70ch; }
#tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 12px; padding: 4px 8px; border-radius: 4px; display: none; max-width: 320px; z-index: 10; }
.theme { align-self: flex-end; font: inherit; font-size: 12px; background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; }
"""

# The one script: hover and focus tooltips for marks with data-tip, and the
# light/dark switch.
SCRIPT = """
(function () {
  var tip = document.getElementById('tip');
  function show(e) {
    var t = e.target.closest('[data-tip]');
    if (!t) { tip.style.display = 'none'; return; }
    tip.textContent = t.getAttribute('data-tip');
    tip.style.display = 'block';
    var r = t.getBoundingClientRect();
    var x = (e.clientX || r.left + r.width / 2) + 12, y = (e.clientY || r.top) + 12;
    if (x + tip.offsetWidth > innerWidth - 8) x = innerWidth - tip.offsetWidth - 8;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  document.addEventListener('mousemove', show);
  document.addEventListener('focusin', show);
  document.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
  var b = document.getElementById('theme');
  b.addEventListener('click', function () {
    var d = document.documentElement, dark = d.getAttribute('data-theme') === 'dark' ||
      (!d.getAttribute('data-theme') && matchMedia('(prefers-color-scheme: dark)').matches);
    d.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
"""


def sec(ident, eyebrow, title, body, lede=""):
    return (
        '<section id="%s"><div class="sec-head"><div class="eyebrow">%s</div><h2>%s</h2>%s</div>%s</section>'
        % (
            ident,
            esc(eyebrow),
            esc(title),
            ('<p class="prose">%s</p>' % lede) if lede else "",
            body,
        )
    )


def table(head, rows, classes=None):
    out = ['<div class="scroll"><table><thead><tr>']
    out += ["<th>%s</th>" % esc(h) for h in head]
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(rows):
        c = (classes or {}).get(i)
        out.append("<tr%s>" % (' class="%s"' % c if c else ""))
        out += ["<td>%s</td>" % (x if isinstance(x, Raw) else esc(x)) for x in r]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


class Raw(str):
    pass


def rng(f, nd=1):
    if not f:
        return "-"
    if f["min"] == f["max"]:
        return fmt(f["median"], nd)
    return "%s (%s-%s)" % (fmt(f["median"], nd), fmt(f["min"], nd), fmt(f["max"], nd))


def med_of(f):
    return (f or {}).get("median")


def delta(d, k):
    if not d or d.get(k) is None:
        return "-"
    return "%+.1f%s" % (d[k], R.delta_marks(d))


# --- sections -------------------------------------------------------------------


def header(model, game):
    builds = []
    for h, hm in model["hosts"].items():
        for v in hm["disclosure"]["build"]:
            b = v["value"] or {}
            builds.append(
                "%s: %s%s%s (%d runs)"
                % (
                    h,
                    b.get("version") or "build not recorded",
                    (" · game " + b["game_sha"][:12]) if b.get("game_sha") else "",
                    (" · toolkit " + b["toolkit_sha"][:12]) if b.get("toolkit_sha") else "",
                    v["runs"],
                )
            )
    dates = [d for hm in model["hosts"].values() for d in (hm["disclosure"]["dates"] or [])]
    runs = sum(hm["runs"] for hm in model["hosts"].values())
    base = model.get("baseline")
    meta = [
        "scene %s" % model["scene"],
        "target %s fps" % S.num(model["target_fps"]),
        "runs %d" % runs,
        "dates %s to %s" % (min(dates), max(dates)) if dates else "dates not recorded",
    ]
    if base:
        meta.append("baseline %s" % base["dir"])
    if model.get("hosts_not_in_scene"):
        meta.append("left out, not in the scene: %s" % ", ".join(model["hosts_not_in_scene"]))
    return (
        '<header style="display:flex;flex-direction:column;gap:10px">'
        '<button id="theme" class="theme" type="button">light / dark</button>'
        '<div class="eyebrow">%s bench-matrix report</div><h1>%s</h1>'
        '<div class="meta">%s</div><ul class="meta" style="list-style:none;padding:0">%s</ul></header>'
        % (
            esc(game),
            esc(model["label"]),
            "".join("<span>%s</span>" % esc(m) for m in meta),
            "".join("<li>%s</li>" % esc(b) for b in builds),
        )
    )


def span(vals, nd=1):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    lo, hi = min(vals), max(vals)
    return fmt(lo, nd) if lo == hi else "%s-%s" % (fmt(lo, nd), fmt(hi, nd))


def pick_line(p):
    if p.get("arm"):
        return "best%s: %s%s" % (
            (" at " + p["split_display"]) if p["split_display"] else "",
            p["display"],
            " (single run)" if p.get("single_run") else "",
        )
    return p.get("text") or "no pick"


def verdict_cards(model):
    out = ['<div class="hosts">']
    for hm in model["hosts"].values():
        ok = [e for e in hm["arms"] if e["status"] == "ok"]
        picks = hm["best"]["picks"]
        found = [p for p in picks if p.get("arm")]
        cls = "ok" if len(found) == len(picks) else ("warn" if found else "bad")
        pill = (
            "holds the target"
            if cls == "ok"
            else ("holds it in part" if cls == "warn" else "no setting holds it")
        )
        if hm["best"].get("unstable"):
            cls, pill = "warn", "unstable start"
        out.append(
            '<div class="card"><div class="name">%s</div><span class="pill %s">%s</span>'
            '<div class="figs"><div class="fig"><div class="v">%s</div><div class="l">steady fps</div></div>'
            '<div class="fig"><div class="v">%s</div><div class="l">1%% low</div></div>'
            '<div class="fig"><div class="v">%s</div><div class="l">p99 ms</div></div></div>'
            '<ul style="font-size:13px">%s</ul></div>'
            % (
                esc(hm["label"]),
                cls,
                esc(pill),
                span([med_of(e["mean"]) for e in ok]),
                span([med_of(e["low1"]) for e in ok]),
                span([med_of(e["p99"]) for e in ok]),
                "".join("<li>%s</li>" % esc(pick_line(p)) for p in picks),
            )
        )
    out.append("</div>")
    return "".join(out)


def arm_table(model, hm):
    picks = {p["arm"] for p in hm["best"]["picks"] if p.get("arm")}
    head = [
        "setting",
        "target",
        "mean fps",
        "1% low",
        "worst second",
        "p99 ms",
        "n",
        "good/bad",
        "judged",
    ]
    if model.get("baseline"):
        head += ["Δ mean", "Δ 1% low"]
    rows, cls = [], {}
    for i, e in enumerate(hm["arms"]):
        if e["status"] != "ok":
            r = [e["display"], S.num(model["target_fps"]), e["status"], "", "", "", "", "", ""]
            if model.get("baseline"):
                r += ["", ""]
            rows.append(r)
            cls[i] = "gap"
            continue
        j = e.get("judged") or {}
        if j.get("status") == "judged":
            jt = "%d of %d pass%s" % (
                j["passes"],
                j["n"],
                "" if j["basis"] == "all" else " (good runs)",
            )
        else:
            jt = j.get("status") or "-"
        r = [
            e["display"],
            S.num(model["target_fps"]),
            rng(e["mean"]),
            rng(e["low1"]),
            rng(e.get("worst_s"), 0),
            rng(e["p99"]),
            str(e["n"]),
            "%d/%d" % (e["good"], e["bad"]) if hm["has_verdict"] else "-",
            jt,
        ]
        if model.get("baseline"):
            r += [delta(e.get("delta"), "fps"), delta(e.get("delta"), "low1")]
        rows.append(r)
        if e["id"] in picks:
            cls[i] = "pick"
    marks = "".join(R.delta_marks(e.get("delta")) for e in hm["arms"])
    notes = "".join(
        '<p class="note">%s %s.</p>' % (m, esc(t)) for m, t in R.DELTA_NOTES.items() if m in marks
    )
    return table(head, rows, cls) + notes


def thresholds_line(t, target):
    return (
        "mean at least %s fps (%d%% of %s), at most %s hitches a minute, 1%% low at least %s fps (%d%%); %s of a setting's runs must pass, at least %d judged run%s"
        % (
            R.fmt_g(t["mean_share_min"] * target),
            round(100 * t["mean_share_min"]),
            S.num(target),
            R.fmt_g(t["hitches_per_min_max"]),
            R.fmt_g(t["low1_share_min"] * target),
            round(100 * t["low1_share_min"]),
            t["min_pass"],
            t["min_runs"],
            "" if t["min_runs"] == 1 else "s",
        )
    )


def best_box(model, scene, hm):
    b = hm["best"]
    items = ["<li>%s</li>" % esc(pick_line(p)) for p in b["picks"]]
    for p in b["picks"]:
        if p.get("not_judged_above"):
            items.append(
                "<li>not judged, would rank above the pick: %s</li>"
                % esc(", ".join(arm_name(hm, a) for a in p["not_judged_above"]))
            )
    out = ['<div class="box"><h3>Best settings</h3><ul>%s</ul>' % "".join(items)]
    out.append(
        '<p style="font-size:13.5px">Judged on %s. Thresholds: %s.</p>'
        % (esc(b["basis"]), esc(thresholds_line(b["thresholds"], model["target_fps"])))
    )
    if b.get("mode"):
        rows = []
        for x in b["mode"]["axes"]:
            counts = ", ".join(
                "%s %d/%d" % (val_display(model, x["axis"], v), k, n)
                for v, (k, n) in x["counts"].items()
            )
            rows.append(
                "<li><code>%s</code>: slow %s; p %.3f%s</li>"
                % (
                    esc(x["axis"]),
                    esc(counts),
                    x["p"],
                    " (follows this axis)" if x["significant"] else "",
                )
            )
        out.append(
            '<p style="font-size:13.5px">Does the slow mode follow a setting? A permutation test per axis '
            '(%d shuffles, level %.4f):</p><ul style="font-size:13.5px">%s</ul>'
            % (b["mode"]["shuffles"], b["mode"]["alpha"], "".join(rows))
        )
    if b.get("tied"):
        out.append("<p>%s</p>" % esc(b["tied"]["text"]))
    if b.get("unstable"):
        out.append('<p><span class="pill warn">unstable</span> %s</p>' % esc(b["unstable"]["text"]))
    if b.get("not_judged"):
        out.append(
            '<p style="font-size:13.5px">Not judged (no good run): %s</p>'
            % esc(", ".join(arm_name(hm, a) for a in b["not_judged"]))
        )
    out.append("</div>")
    return "".join(out)


def arm_name(hm, aid):
    return next((e["display"] for e in hm["arms"] if e["id"] == aid), aid)


def val_display(model, axis, v):
    a = next(x for x in model["axes"] if x["name"] == axis)
    for val, d in zip(a["values"], a["display"]):
        if str(val) == str(v):
            return d
    return str(v)


HW_ROWS = (
    ("device", lambda h: (h.get("device") or {}).get("model")),
    (
        "OS",
        lambda h: " ".join(
            str(x) for x in ((h.get("os") or {}).get(k) for k in ("name", "version", "build")) if x
        ),
    ),
    ("kernel", lambda h: (h.get("os") or {}).get("kernel")),
    (
        "CPU",
        lambda h: (
            "%s, %s cores / %s threads%s"
            % (
                h["cpu"].get("model") or "?",
                h["cpu"].get("cores") or "?",
                h["cpu"].get("threads") or "?",
                (", up to %s MHz" % h["cpu"]["max_mhz"]) if h["cpu"].get("max_mhz") else "",
            )
            if h.get("cpu") and h["cpu"].get("model")
            else None
        ),
    ),
    (
        "GPU",
        lambda h: (
            "; ".join(
                "%s%s%s"
                % (
                    g.get("model") or "?",
                    (", driver " + str(g["driver"])) if g.get("driver") else "",
                    (", up to %s MHz" % g["max_mhz"]) if g.get("max_mhz") else "",
                )
                for g in h.get("gpu") or []
            )
            or None
        ),
    ),
    ("RAM", lambda h: ("%s GB" % h["ram_gb"]) if h.get("ram_gb") else None),
    (
        "display",
        lambda h: (
            "%sx%s at %s Hz"
            % (
                h["display"].get("width"),
                h["display"].get("height"),
                h["display"].get("refresh_hz"),
            )
            if (h.get("display") or {}).get("refresh_hz")
            else None
        ),
    ),
    (
        "power",
        lambda h: (
            ", ".join(
                "%s %s" % (k.replace("_", " "), v)
                for k, v in (h.get("power") or {}).items()
                if v is not None
            )
            or None
        ),
    ),
    (
        "backend",
        lambda h: (
            " ".join(
                str(x)
                for x in (
                    (h.get("backend") or {}).get(k)
                    for k in ("name", "translation", "translation_version")
                )
                if x
            )
            or None
        ),
    ),
)


def hw_rows(hw):
    if not hw:
        return [("hardware", "not recorded")]
    return [(k, f(hw)) for k, f in HW_ROWS if f(hw)]


def run_rows(rb):
    out = []
    for k in ("sampler_hz", "pacing", "present_mode", "vsync", "window"):
        if rb.get(k) is not None:
            v = rb[k]
            if k == "sampler_hz":
                v = "%s Hz" % v if v else "off"
            out.append((k.replace("_hz", "").replace("_", " "), v))
    return out


def dl(rows):
    return "<dl>%s</dl>" % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in rows)


def disclosure_block(model, hm, private=True):
    d = hm["disclosure"]
    parts = ["<details><summary>Hardware, build and run (%s)</summary>" % esc(hm["label"])]
    if d["differs"]:
        parts.append(
            '<p><span class="pill warn">runs differ</span> the runs used more than one hardware or build variant:</p>'
        )
    for v in d["hw"]:
        parts.append(dl(hw_rows(v["value"]) + [("runs", v["runs"])]))
    for v in d["build"]:
        b = v["value"] or {}
        rows = [
            ("build", (b.get("version") if private else b.get("version_public")) or "not recorded")
        ]
        if private:
            rows += [
                (k.replace("_", " "), b[k])
                for k in ("game_sha", "toolkit_sha", "cli_version", "exe_sha256")
                if b.get(k)
            ]
        parts.append(dl(rows + [("runs", v["runs"])]))
    for v in d["run"]:
        parts.append(dl(run_rows(v["value"]) + [("runs", v["runs"])]))
    if d["windowed_runs"]:
        parts.append(
            "<p>%d run%s windowed, not borderless fullscreen: a deviation from the bench rule; the OS may throttle a covered window.</p>"
            % (d["windowed_runs"], " was" if d["windowed_runs"] == 1 else "s were")
        )
    if d.get("window_unknown_runs"):
        parts.append(
            "<p>%d run%s no window mode (run-info from before present_fullscreen).</p>"
            % (
                d["window_unknown_runs"],
                " records" if d["window_unknown_runs"] == 1 else "s record",
            )
        )
    if d["refresh_below_target"]:
        parts.append(
            "<p>The display refreshes at %s Hz, below the %s fps target; the target is kept.</p>"
            % (", ".join(S.num(r) for r in d["refresh_below_target"]), S.num(model["target_fps"]))
        )
    parts.append("</details>")
    return "".join(parts)


def sampler_line(hm):
    sg = hm["sampler"]
    if sg["mixed"]:
        t = (
            "mixed sampler rates: no one rate covers every arm, so each arm uses its lowest (%s)"
            % ", ".join("%s %s Hz" % (a, hz) for a, hz in sorted(sg["per_arm"].items()))
        )
    elif sg["hz"] is None:
        t = "no clean runs"
    else:
        t = "figures from the runs %s (%d runs)" % (
            "without the sampler" if sg["hz"] == 0 else "with the sampler at %d Hz" % sg["hz"],
            hm["group_runs"],
        )
    if sg.get("others"):
        t += "; the runs %s are left out of the figures" % " and ".join(
            "without the sampler" if x == 0 else "at %d Hz" % x for x in sg["others"]
        )
    return t


def host_sections(model, scene, s):
    out = []
    groups = S.radar_groups(model)
    for h, hm in model["hosts"].items():
        base = None
        if model.get("baseline") and h in model["baseline"]["hosts"]:
            bh = model["baseline"]["hosts"][h]
            base = dict(
                bh["arms"],
                _label="baseline %s (%s)"
                % (bh.get("version_public") or "build not recorded", bh.get("date") or "?"),
            )
        figs = []
        for gi, (suffix, idx) in enumerate(groups):
            svg = S.radar(
                model, h, S.PRIVATE, "radar-%s-%d" % (h, gi), idx, base, tips=True, suffix=suffix
            )
            cap = S.CAPTION + (
                " One radar per %s value past %d settings." % (suffix, R.RADAR_MAX_SPOKES)
                if suffix
                else ""
            )
            figs.append(
                '<figure class="radar">%s<figcaption>%s</figcaption></figure>' % (svg, esc(cap))
            )
        body = "".join(figs)
        body += '<p class="prose" style="font-size:13.5px">%s</p>' % esc(sampler_line(hm))
        body += arm_table(model, hm)
        body += best_box(model, scene, hm)
        body += disclosure_block(model, hm)
        out.append(
            '<div style="display:flex;flex-direction:column;gap:16px"><h3>%s</h3>%s</div>'
            % (esc(hm["label"]), body)
        )
    return "".join(out)


def matrix_section(model, s):
    hosts = [(h, hm["label"]) for h, hm in model["hosts"].items()]
    first = next(iter(model["hosts"].values()))
    out = []
    for key, title, unit, low in (
        ("mean", "Steady fps", "fps", False),
        ("low1", "1% low", "fps", False),
        ("p99", "p99 frame time", "ms", True),
    ):
        rows = []
        for i, e in enumerate(first["arms"]):
            per = {}
            for h, hm in model["hosts"].items():
                a = hm["arms"][i]
                per[h] = med_of(a.get(key)) if a["status"] == "ok" else None
            rows.append((e["display"], per))
        out.append(
            "<figure>%s<figcaption>%s per setting, each host's sampler group; the median of its clean runs.</figcaption></figure>"
            % (S.dot_plot(rows, hosts, S.PRIVATE, "dots-" + key, title, unit, low), esc(title))
        )
    head = [
        "host",
        "setting",
        "prof",
        "rep",
        "verdict",
        "steady fps",
        "1% low",
        "worst s",
        "p99 ms",
        "hitches",
        "warm-up fps",
        "disturbed",
    ]
    rows = []
    for r in s["rows"]:
        rows.append(
            [
                r["host"],
                r["arm"],
                str(r["prof"]),
                str(r.get("rep", "")),
                r.get("verdict") or "-",
                fmt(r.get("steady_fps"), 2),
                fmt(r.get("steady_low1_fps"), 1),
                fmt(r.get("steady_worst_s_fps"), 0),
                fmt(r.get("steady_p99_ms"), 1),
                str(r.get("steady_hitches", "-")),
                fmt(r.get("warmup_fps"), 1),
                "yes" if r.get("disturbed") else "",
            ]
        )
    out.append("<h3>Every run</h3>" + table(head, rows))
    return "".join(out)


def range_section(model, s):
    groups, lines = [], []
    for h, hm in model["hosts"].items():
        rs = [
            r
            for r in s["rows"]
            if r["host"] == h and not r["disturbed"] and r.get("steady_fps") is not None
        ]
        if not rs:
            continue
        pts = [
            (
                r["steady_fps"],
                r.get("verdict"),
                "%s %s prof %s: %s fps" % (h, r["arm"], r["prof"], fmt(r["steady_fps"], 2)),
            )
            for r in rs
        ]
        groups.append((hm["label"], pts))
        b = max(rs, key=lambda r: r["steady_fps"])
        w = min(rs, key=lambda r: r["steady_fps"])
        lines.append(
            "<li>%s: best run %s (%s fps, prof %s), worst %s (%s fps, prof %s), over %d clean runs</li>"
            % (
                esc(hm["label"]),
                esc(b["arm"]),
                fmt(b["steady_fps"], 2),
                b["prof"],
                esc(w["arm"]),
                fmt(w["steady_fps"], 2),
                w["prof"],
                len(rs),
            )
        )
    svg = S.strip(
        groups,
        S.PRIVATE,
        "range",
        "Steady fps of every clean run",
        "fps",
        [(model["target_fps"], "target")],
    )
    return (
        '<ul class="prose">%s</ul><figure>%s<figcaption>One dot per clean run, every sampler rate; green good, red slow where the host has a verdict.</figcaption></figure>'
        % ("".join(lines), svg)
    )


def run_json(result, run_dir):
    try:
        with open(os.path.join(result, run_dir, "run.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def detail_section(model, s, result):
    out = []
    warm = model["warmup_s"]
    for h, hm in model["hosts"].items():
        rs = [(i, r) for i, r in enumerate(s["rows"]) if r["host"] == h and not r["disturbed"]]
        per_arm = {}
        for _, r in rs:
            per_arm[r["arm"]] = per_arm.get(r["arm"], 0) + 1
        if not hm["has_verdict"] and max(per_arm.values() or [0]) <= 1:
            continue
        runs = s.get("runs") or []
        series = []
        for i, r in rs:
            d = runs[i] if i < len(runs) else {}
            if d.get("fps_per_s"):
                series.append(
                    (
                        d["fps_per_s"],
                        r.get("verdict"),
                        "%s prof %s: %s" % (r["arm"], r["prof"], r.get("verdict") or "run"),
                    )
                )
        ymax = max([max(x[0]) for x in series] + [model["target_fps"]]) * 1.05
        block = ["<h3>%s</h3>" % esc(hm["label"])]
        block.append(
            "<figure>%s<figcaption>fps per second of every clean run (%d), by verdict.</figcaption></figure>"
            % (
                S.line_series(
                    series,
                    S.PRIVATE,
                    "fps-" + h,
                    "fps per second, %s" % hm["label"],
                    model["duration_s"],
                    ymax,
                    warm,
                ),
                len(series),
            )
        )
        bins = list(range(0, 51, 2))
        hist = {}
        for _, r in rs:
            rj = run_json(result, r["run_dir"])
            if not rj:
                continue
            ft = [x for t, x in (rj.get("frames") or {}).get("frames") or [] if t >= warm * 1000]
            hb = hist.setdefault(r.get("verdict") or "run", [0] * (len(bins) - 1))
            for x in ft:
                hb[min(int(x // 2), len(bins) - 2)] += 1
        groups = []
        for v, hb in hist.items():
            n = sum(hb) or 1
            groups.append((v, "%s runs" % v, [c / n for c in hb]))
        if groups:
            block.append(
                "<figure>%s<figcaption>Steady frame times, share of frames per 2 ms bin, by verdict.</figcaption></figure>"
                % S.histogram(
                    groups, S.PRIVATE, "hist-" + h, "Steady frame times, %s" % hm["label"], bins
                )
            )
        rule = next((x for x in model["verdict_rules"] if h in x.get("hosts", [])), {})
        pts = [
            (
                r["warmup_fps"],
                r["steady_hitches"],
                r.get("verdict"),
                "%s prof %s: warm-up %s fps, %s hitches"
                % (r["arm"], r["prof"], fmt(r["warmup_fps"]), r["steady_hitches"]),
            )
            for _, r in rs
            if r.get("warmup_fps") is not None and r.get("steady_hitches") is not None
        ]
        block.append(
            "<figure>%s<figcaption>Warm-up fps against steady hitches; the dashed lines are the verdict's thresholds.</figcaption></figure>"
            % S.scatter(
                pts,
                S.PRIVATE,
                "scatter-" + h,
                "Warm-up fps against steady hitches, %s" % hm["label"],
                "warm-up fps",
                "steady hitches",
                rule.get("warmup_fps_min"),
                rule.get("steady_hitches_max"),
            )
        )
        out.append("".join(block))
    return "".join(out) or "<p>No host has a verdict or more than one run per setting.</p>"


def hot_section(model, s):
    out = []
    runs = s.get("runs") or []
    for h, hm in model["hosts"].items():
        shares = {}
        nrun = 0
        for i, r in enumerate(s["rows"]):
            if r["host"] != h or r["disturbed"] or not r["prof"] or i >= len(runs):
                continue
            fns = runs[i].get("functions") or []
            if not fns:
                continue
            nrun += 1
            for f in fns:
                shares.setdefault(f["name"], []).append(f["share"])
        if not shares:
            continue
        spin_names = set()
        for i, r in enumerate(s["rows"]):
            if r["host"] == h and i < len(runs):
                for x in runs[i].get("spin") or []:
                    if isinstance(x, dict) and x.get("name"):
                        spin_names.add(x["name"])
        top = sorted(
            shares.items(), key=lambda kv: -statistics.median(kv[1] + [0] * (nrun - len(kv[1])))
        )[:10]
        rows = [
            [
                name + (" (spin)" if name in spin_names else ""),
                "%.1f%%" % statistics.median(v + [0] * (nrun - len(v))),
                "%d of %d" % (len(v), nrun),
            ]
            for name, v in top
        ]
        out.append(
            "<h3>%s</h3>%s"
            % (esc(hm["label"]), table(["function", "median share", "in top 10 of"], rows))
        )
    return "".join(out) or "<p>No profiled runs.</p>"


HEAD_COLS = (
    ("gpu_busy_steady", "GPU busy %"),
    ("gpu_mhz_steady", "GPU MHz"),
    ("cpu_process_cores", "process cores"),
    ("cpu_core_max_pct", "busiest core %"),
    ("spin_share", "spin share"),
)


def headroom_section(model, s):
    out = []
    first = model["axes"][0]
    cols = [(k, t) for k, t in HEAD_COLS if any(r.get(k) is not None for r in s["rows"])]
    if cols:
        rows = []
        for h, hm in model["hosts"].items():
            rs = [
                r
                for r in s["rows"]
                if r["host"] == h and not r["disturbed"] and r["prof"] == hm["sampler"]["hz"]
            ]
            for v, d in zip(first["values"], first["display"]):
                sub = [r for r in rs if r.get("axis_" + first["name"]) == v]
                if not sub:
                    continue
                rows.append(
                    [hm["label"], d]
                    + [fmt(R.med([r.get(k) for r in sub])) for k, _ in cols]
                    + [str(len(sub))]
                )
        out.append(
            "<h3>By %s</h3>" % esc(first["name"])
            + table(["host", first["name"]] + [t for _, t in cols] + ["runs"], rows)
        )
    for a in model["axes"]:
        if len(a["values"]) != 2:
            continue
        v0, v1 = a["values"]
        rows = []
        for hm in model["hosts"].values():
            by = {e["id"]: e for e in hm["arms"] if e["status"] == "ok"}
            ds, dl_ = [], []
            for e in hm["arms"]:
                if e["values"][a["name"]] != v0 or e["id"] not in by:
                    continue
                other = next(
                    (
                        x
                        for x in hm["arms"]
                        if x["values"][a["name"]] == v1
                        and all(
                            x["values"][k] == e["values"][k] for k in e["values"] if k != a["name"]
                        )
                    ),
                    None,
                )
                if other and other["id"] in by:
                    ds.append(med_of(other["mean"]) - med_of(e["mean"]))
                    if med_of(other["low1"]) is not None and med_of(e["low1"]) is not None:
                        dl_.append(med_of(other["low1"]) - med_of(e["low1"]))
            if ds:
                rows.append(
                    [
                        hm["label"],
                        "%+.2f" % statistics.median(ds),
                        "%+.2f to %+.2f" % (min(ds), max(ds)),
                        "%+.2f" % statistics.median(dl_) if dl_ else "-",
                        str(len(ds)),
                    ]
                )
        if rows:
            out.append(
                "<h3>%s: %s against %s</h3>"
                % (esc(a["name"]), esc(a["display"][1]), esc(a["display"][0]))
                + table(["host", "Δ mean fps (median)", "range", "Δ 1% low", "pairs"], rows)
            )
    return "".join(out) or "<p>No GPU or CPU series in these runs.</p>"


def method_section(model, scene):
    sc = scene["scene"]
    st = sc.get("start") or {}
    items = [
        "Each run measures %s s from the scene's start, the first %s s as warm-up; the four figures come from the steady %s s after it."
        % (sc["duration_s"], model["warmup_s"], sc["duration_s"] - model["warmup_s"]),
        "The start is the first flip with at least %s batches held for %s flips (else %s s after the first flip)."
        % (st.get("min_batches", "?"), st.get("hold_flips", "?"), st.get("fallback_s", "?"))
        if st
        else "The start is the first flip.",
        "Runs are taken in a borderless fullscreen-desktop window, remote hosts first and the Mac last and quiet; runs disturbed by other processes are left out.",
        "A setting's figures are the median of its clean runs in the host's sampler group; n and the range are shown beside them.",
    ]
    for hm in model["hosts"].values():
        items.append(
            "%s: %d runs, %d clean; %s."
            % (hm["label"], hm["runs"], hm["clean_runs"], sampler_line(hm))
        )
    costs = {}
    for o in model.get("prof_overhead") or []:
        costs.setdefault((o["host"], o["prof"]), []).append(
            o.get("cost_pct_good", o.get("cost_pct"))
        )
    for (h, p), v in sorted(costs.items()):
        v = [x for x in v if x is not None]
        if v:
            items.append(
                "Sampler cost on %s at %s Hz: median %+.1f%% of steady fps over %d settings."
                % (h, p, statistics.median(v), len(v))
            )
    t = next(iter(model["hosts"].values()))["best"]["thresholds"] if model["hosts"] else R.DEFAULTS
    items.append(
        "Best settings: "
        + thresholds_line(t, model["target_fps"])
        + ". Host overrides are named in each host's box."
    )
    return '<ul class="prose">%s</ul>' % "".join("<li>%s</li>" % esc(x) for x in items)


def md(text):
    """report-notes.md as simple HTML: headings, lists, paragraphs, code."""
    out, para, lst = [], [], []

    def inline(t):
        t = esc(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)

    def flush():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para)))
            para.clear()
        if lst:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(x) for x in lst))
            lst.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            out.append("<h3>%s</h3>" % inline(m.group(2)))
        elif re.match(r"^\s*[-*]\s+", line):
            if para:
                flush()
            lst.append(re.sub(r"^\s*[-*]\s+", "", line))
        elif not line.strip():
            flush()
        elif lst and line.startswith("  "):
            lst[-1] += " " + line.strip()
        else:
            para.append(line.strip())
    flush()
    return "".join(out)


def notes_section(result):
    p = os.path.join(result, "report-notes.md")
    try:
        with open(p) as f:
            return '<div class="prose notes">%s</div>' % md(f.read())
    except OSError:
        return '<p class="prose">No <code>report-notes.md</code> in the result: key findings, hypotheses and follow-ups go there.</p>'


def render(model, scene, s, result, game="game"):
    parts = [
        header(model, game),
        sec("verdict", "1 · verdict", "Per host", verdict_cards(model)),
        sec(
            "hosts",
            "2 · hosts",
            "Per host: chart, figures, best settings",
            host_sections(model, scene, s),
        ),
        sec("matrix", "3 · matrix", "Every setting on every host", matrix_section(model, s)),
        sec("range", "4 · range", "Best and worst runs", range_section(model, s)),
        sec("detail", "5 · detail", "Runs in detail", detail_section(model, s, result)),
        sec("hot", "6 · hot paths", "Where the time goes", hot_section(model, s)),
        sec(
            "headroom",
            "7 · headroom",
            "CPU and GPU headroom, and each two-value setting's cost",
            headroom_section(model, s),
        ),
        sec("method", "8 · method", "Methodology and caveats", method_section(model, scene)),
        sec("notes", "9 · notes", "Notes", notes_section(result)),
    ]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>%s bench report</title><style>%s</style></head><body><div class="wrap">%s</div>'
        '<div id="tip" role="tooltip"></div><script>%s</script></body></html>\n'
        % (esc(model["scene"]), CSS, "".join(parts), SCRIPT)
    )
