"""hud-map review: a contact sheet for a proposal (design A15, step 3).

One static HTML file beside the capture: per captured frame, the frame at
4:3 with each element's boxes coloured by kind and labelled with element,
group and site, and beside it a preview at the wide ratio (the frame in
grey pads, each element's crop moved or stretched by its kind's rule).
The frames are the capture's BMPs, linked by relative path, never copied
or embedded: the sheet is as private as the capture.
"""

import html
import os
import re
import tomllib

from ..host import CliError
from . import capture as C
from . import table as T

COLOURS = {
    "left": "#2f7de1",
    "right": "#e1532f",
    "centre": "#2fb36b",
    "proportional": "#b52fe1",
    "world": "#e1b12f",
    "stretch": "#2fd3e1",
    None: "#999999",
}


def ratio(s):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$", s or "")
    if not m or float(m.group(2)) <= 0:
        raise CliError("%r is not a ratio W:H" % s)
    return float(m.group(1)), float(m.group(2))


def pad_of(tw, th, r):
    """H1's pad a side, in guest pixels, at ratio r (0 at 4:3 and below)."""
    return max(0, round((th * r[0] / r[1] - tw) / 2))


def dx_of(kind, box, tw, pad, ref_x=None):
    """The preview's move for a box (A3): left -pad, right +pad,
    proportional by its centre (or the group's ref_x, px from the centre),
    the rest 0. Stretch is a map, not a move (see stretch_x)."""
    if kind == "left":
        return -pad
    if kind == "right":
        return pad
    if kind == "proportional":
        cx = ref_x if ref_x is not None else (box[0] + box[2]) / 2 - tw / 2
        return max(-pad, min(pad, round(cx / (tw / 2) * pad)))
    return 0


def stretch_x(x, tw, pad):
    """A13's map: the 4:3 columns onto the wide frame, x in [0, tw] to
    [-pad, tw + pad]."""
    return -pad + x * (tw + 2 * pad) / tw


def load_proposal(path):
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except OSError as e:
        raise CliError("no proposal at %s (%s)" % (path, e.strerror)) from e
    except tomllib.TOMLDecodeError as e:
        raise CliError("%s: %s" % (path, e)) from e
    return doc


def sources(path):
    """The captures a proposal names in its head (the lines under
    "Captures", one path each, indented), else its directory."""
    out, under = [], False
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            line = line.rstrip("\n")
            if re.match(r"^#\s*Captures\b.*:$", line):
                out, under = [], True
            elif under and line.startswith("#   "):
                out.append(line[4:])
            else:
                under = False
    base = os.path.dirname(os.path.abspath(path))
    out = [p if os.path.isabs(p) else os.path.join(base, p) for p in out]
    return [p for p in out if os.path.exists(p)] or [base]


def rows_by_site(doc):
    """{ret_va: (element, group name or None, kind, ref_x px or None)}."""
    groups = {g["name"]: g for g in T._rows(doc, "group") if isinstance(g.get("name"), str)}
    out = {}
    for s in T._rows(doc, "site"):
        if not isinstance(s, dict) or not isinstance(s.get("ret_va"), int):
            continue
        g = groups.get(s["group"]) if isinstance(s.get("group"), str) else None
        kind = T.kind_of(g.get("kind") if g else s.get("kind"))
        out[s["ret_va"]] = (s.get("element"), s.get("group"), kind, g.get("ref_x") if g else None)
    return out


def _svg_frame(fr, canvas, items, rel, tw, th, pad, uid):
    """Two SVGs for one frame: (4:3 with boxes, wide preview). rel None:
    the frame has no picture (bmp null), a grey canvas stands in."""
    if rel is None:
        pic = '<rect x="%g" y="0" width="%g" height="%g" fill="#222"%s/>'
    else:
        pic = '<image href="%s" x="%%g" y="0" width="%%g" height="%%g"%%s/>' % html.escape(
            rel, quote=True
        )
    a = [
        '<svg viewBox="0 0 %g %g" width="%g" height="%g">' % (tw, th, tw, th),
        pic % (0, tw, th, ""),
    ]
    W = tw + 2 * pad
    b = [
        '<svg viewBox="0 0 %g %g" width="%g" height="%g">' % (W, th, W, th),
        '<rect x="0" y="0" width="%g" height="%g" fill="#555"/>' % (W, th),
        pic % (pad, tw, th, ""),
    ]
    for i, (box, label, kind, ref_x) in enumerate(items):
        col = COLOURS.get(kind, COLOURS[None])
        x0, y0, x1, y1 = box
        a.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" stroke="%s"'
            ' stroke-width="1"><title>%s</title></rect>'
            % (x0, y0, max(x1 - x0, 0.5), max(y1 - y0, 0.5), col, html.escape(label))
        )
        a.append(
            '<text x="%.1f" y="%.1f" fill="%s" font-size="7">%s</text>'
            % (x0 + 1, max(y0 - 1, 7), col, html.escape(label.split(" ")[0]))
        )
        if kind is None or kind in ("centre", "world") or pad == 0:
            continue
        cid = "c%s_%d" % (uid, i)
        b.append(
            '<clipPath id="%s"><rect x="%.1f" y="%.1f" width="%.1f" height="%.1f"/></clipPath>'
            % (cid, x0, y0, x1 - x0, y1 - y0)
        )
        # The element leaves its stock place.
        b.append(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#000" opacity="0.6"/>'
            % (pad + x0, y0, x1 - x0, y1 - y0)
        )
        if kind == "stretch":
            s = (tw + 2 * pad) / tw
            tf = "matrix(%g 0 0 1 %g 0)" % (s, pad + stretch_x(0, tw, pad))
        else:
            tf = "translate(%g 0)" % (pad + dx_of(kind, box, tw, pad, ref_x))
        b.append(
            '<g transform="%s">%s<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f"'
            ' fill="none" stroke="%s" stroke-width="1"/></g>'
            % (
                tf,
                pic % (0, tw, th, ' clip-path="url(#%s)"' % cid),
                x0,
                y0,
                x1 - x0,
                y1 - y0,
                col,
            )
        )
    a.append("</svg>")
    b.append("</svg>")
    return "".join(a), "".join(b)


def sheet(doc, captures, out_dir, r, title):
    """The sheet's HTML text: per capture frame, 4:3 and the wide preview."""
    sites = rows_by_site(doc)
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>%s</title>" % html.escape(title),
        "<style>body{font:13px sans-serif;background:#222;color:#ddd}"
        ".f{margin:12px 0}.f svg{margin-right:8px;background:#000}"
        "td,th{padding:2px 8px;text-align:left}</style></head><body>",
        "<h1>%s</h1>" % html.escape(title),
        "<p>Ratio %g:%g. Kinds: %s. Unlisted sprites are grey.</p>"
        % (
            r[0],
            r[1],
            " ".join("<span style='color:%s'>%s</span>" % (COLOURS[k], k) for k in T.KINDS),
        ),
    ]
    legend = ["<table><tr><th>site</th><th>element</th><th>group</th><th>kind</th></tr>"]
    for ret, (el, gr, kind, _) in sorted(sites.items()):
        legend.append(
            "<tr><td>0x%08X</td><td>%s</td><td>%s</td><td style='color:%s'>%s</td></tr>"
            % (ret, html.escape(el or ""), html.escape(gr or ""), COLOURS.get(kind), kind)
        )
    parts.append("".join(legend) + "</table>")
    n = 0
    for ci, cap in enumerate(captures):
        by_frame = {}
        for s in cap.sprites:
            if s.bbox is not None:
                by_frame.setdefault(s.frame, []).append(s)
        for f in sorted(set(cap.frames) | set(by_frame)):
            canvas = cap.canvas(f)
            tw, th = canvas.tw, canvas.th
            pad = pad_of(tw, th, r)
            fr = cap.frames.get(f)
            rel = os.path.relpath(fr.bmp, out_dir) if fr and fr.bmp else None
            items = []
            for s in by_frame.get(f, []):
                box = canvas.to_px(s.bbox)
                ret = s.name_ret()
                el, gr, kind, ref = sites.get(ret, (None, None, None, None))
                ref_px = None
                if ref is not None:
                    ref_px = (ref - canvas.to_units(tw / 2)) * canvas.view[2] / canvas.w
                label = "%s %s 0x%08X" % (el or "unlisted", gr or "-", ret or 0)
                items.append((box, label, kind, ref_px))
            a, b = _svg_frame(fr, canvas, items, rel, tw, th, pad, "%d_%d" % (ci, f))
            parts.append(
                "<div class='f'><div>%s frame %d</div>%s%s</div>"
                % (html.escape(os.path.basename(cap.dir)), f, a, b)
            )
            n += 1
    parts.append("</body></html>\n")
    return "\n".join(parts), n


def review(path, r, capture_paths=None, out=None):
    doc = load_proposal(path)
    caps = C.load(capture_paths or sources(path))
    out = out or os.path.join(caps[0].dir, "review-%gx%g.html" % r)
    title = "hud-map review: %s" % os.path.basename(path)
    text, n = sheet(doc, caps, os.path.dirname(os.path.abspath(out)), r, title)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    return out, n
