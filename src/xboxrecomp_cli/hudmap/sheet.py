"""hud-map check's sheet: one HTML file per scenario and ratio, mod off
beside mod on, and what the trace says (design A15, step 5).

From the mod-on run's log:
- [hud.unlisted] 0xRET x LO..HI: a call no row names (only clipped);
- [hud.skew] 0xRET: a rotated primitive crossing its window, left as is;
- [hud.skip] 0xRET WHY: a call left untouched (a wrong prim, the restore
  cap); a site that shows one is fixed before the game's check passes;
- [aspect.program] ... x LO..HI on WxH ... lift: a lifted 2D batch. One
  reaching past the 4:3 columns is explained when the table has a row
  that moves sprites that way (left, proportional, stretch or world for
  the left pad; right and the same for the right one); else it is listed
  as unexplained.

Exit 1 (the failed count) on a skip, an unexplained lifted batch, or a run
that did not reach its last dump.
"""

import html
import os
import re

from . import table as T

UNLISTED_RE = re.compile(r"^\[hud\.unlisted\] 0x([0-9A-Fa-f]{8}) x (\S+)\.\.(\S+)")
SKEW_RE = re.compile(r"^\[hud\.skew\] 0x([0-9A-Fa-f]{8})")
SKIP_RE = re.compile(r"^\[hud\.skip\] 0x([0-9A-Fa-f]{8}) (.*)$")
PROGRAM_RE = re.compile(
    r"^\[aspect\.program\] ([0-9A-Fa-f]+) flip (\d+): .*? x (\S+)\.\.(\S+) on (\d+)x(\d+)"
    r".*-> \S+( lift)?"
)
FLIP_RE = re.compile(r"flip_(\d+)\.bmp$")
EDGE = 2.0
LEFT_KINDS = ("left", "proportional", "stretch", "world")
RIGHT_KINDS = ("right", "proportional", "stretch", "world")


def _f(s):
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_log(lines):
    """{unlisted: {ret: (lo, hi)}, skew: [ret], skip: {ret: why},
    lifted: [(program, flip, lo, hi, w)]} for lifted batches past the
    columns."""
    out = {"unlisted": {}, "skew": [], "skip": {}, "lifted": []}
    for line in lines:
        line = line.rstrip("\n")
        m = UNLISTED_RE.match(line)
        if m:
            out["unlisted"].setdefault(int(m.group(1), 16), (_f(m.group(2)), _f(m.group(3))))
            continue
        m = SKEW_RE.match(line)
        if m:
            ret = int(m.group(1), 16)
            if ret not in out["skew"]:
                out["skew"].append(ret)
            continue
        m = SKIP_RE.match(line)
        if m:
            out["skip"].setdefault(int(m.group(1), 16), m.group(2))
            continue
        m = PROGRAM_RE.match(line)
        if m and m.group(7):
            lo, hi, w = _f(m.group(3)), _f(m.group(4)), int(m.group(5))
            if lo < -EDGE or hi > w + EDGE:
                out["lifted"].append((m.group(1), int(m.group(2)), lo, hi, w))
    return out


def table_kinds(doc):
    """Every kind the table's rows use."""
    kinds = {T.kind_of(g.get("kind")) for g in T._rows(doc, "group")}
    kinds |= {T.kind_of(s.get("kind")) for s in T._rows(doc, "site")}
    return {k for k in kinds if k}


def explains(kinds, lo, hi, w):
    """Whether rows of these kinds can put a batch at lo..hi."""
    left = lo < -EDGE
    right = hi > w + EDGE
    return (not left or bool(kinds & set(LEFT_KINDS))) and (
        not right or bool(kinds & set(RIGHT_KINDS))
    )


def frames_of(d):
    """{flip: path} of a run's frame dumps."""
    out = {}
    fd = os.path.join(d, "frames")
    for n in sorted(os.listdir(fd)) if os.path.isdir(fd) else []:
        m = FLIP_RE.search(n)
        if m:
            out[int(m.group(1))] = os.path.join(fd, n)
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def render(scen, r, on, off, known, out_dir):
    """(html, failed) for one scenario at one ratio. on, off: (run dir,
    exit code, why)."""
    log = parse_log(_read(os.path.join(on[0], "game-stdio.log")))
    kinds = table_kinds(known)
    unexplained = [x for x in log["lifted"] if not explains(kinds, x[2], x[3], x[4])]
    fon, foff = frames_of(on[0]), frames_of(off[0])
    failed = len(log["skip"]) + len(unexplained) + sum(1 for x in (on, off) if x[2] != "done")
    e = html.escape
    p = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>hud-map check %s %s</title>" % (e(scen), e(r)),
        "<style>body{font:13px sans-serif;background:#222;color:#ddd}img{margin-right:8px}"
        "td,th{padding:2px 8px;text-align:left}.bad{color:#f66}</style></head><body>",
        "<h1>hud-map check: %s at %s</h1>" % (e(scen), e(r)),
        "<p>mod on: %s (exit %s); mod off: %s (exit %s)</p>" % (on[2], on[1], off[2], off[1]),
    ]

    def table(title, rows, bad=False):
        p.append(
            "<h2%s>%s (%d)</h2>" % (" class='bad'" if bad and rows else "", e(title), len(rows))
        )
        if rows:
            p.append(
                "<table>"
                + "".join(
                    "<tr>%s</tr>" % "".join("<td>%s</td>" % e(str(c)) for c in row) for row in rows
                )
                + "</table>"
            )

    table("[hud.skip] sites", [("0x%08X" % k, v) for k, v in sorted(log["skip"].items())], True)
    table(
        "lifted batches past the columns, no row explains",
        [(x[0], "flip %d" % x[1], "x %g..%g on %d" % (x[2], x[3], x[4])) for x in unexplained],
        True,
    )
    table(
        "[hud.unlisted] sites",
        [("0x%08X" % k, "x %g..%g" % v) for k, v in sorted(log["unlisted"].items())],
    )
    table("[hud.skew] sites", [("0x%08X" % k,) for k in log["skew"]])
    explained = [x for x in log["lifted"] if x not in unexplained]
    table(
        "lifted batches past the columns, explained by a row's kind",
        [(x[0], "flip %d" % x[1], "x %g..%g on %d" % (x[2], x[3], x[4])) for x in explained],
    )
    p.append("<h2>frames: mod off, mod on</h2>")
    for flip in sorted(set(fon) | set(foff)):
        imgs = []
        for src in (foff.get(flip), fon.get(flip)):
            if src:
                imgs.append("<img src='%s'>" % e(os.path.relpath(src, out_dir), quote=True))
            else:
                imgs.append("<span class='bad'>no dump</span>")
        p.append("<div><div>flip %d</div>%s</div>" % (flip, "".join(imgs)))
    p.append("</body></html>\n")
    return "\n".join(p), failed


def write(out_dir, scen, r, on, off, known):
    """The sheet's path and its failed count."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "check-%s.html" % r.replace(":", "x"))
    text, failed = render(scen, r, on, off, known, out_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, failed
