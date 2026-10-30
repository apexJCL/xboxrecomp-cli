"""hud-map propose: a table proposal from captures (design A15, step 2).

Sprites become elements by the address the layer names them by (the
outermost wrapper's return, else the hooked call's), elements become group
candidates when the same function draws them and their boxes touch, and
each gets a kind from where it is while it is on the canvas. The rules are
pure functions over guest-pixel boxes, one pytest each.

Every row the rules make is marked `proposed = true` with its evidence. A
site the game's table already lists is copied from the table as it is
(`proposed = false`), so emit never undoes a person's correction; the
rules' own reading is in its evidence.
"""

import os

from . import table as T

# Guest pixels: a box within this of both edges spans the canvas (H4's
# full-width test), and boxes this close touch.
EDGE = 2.0
TOUCH = 2.0
# A world element's centre must move this far on both axes.
MOVE = 2.0


# ── the rules (pure) ─────────────────────────────────────────────────────


def zero_area(box):
    """A primitive with no area (BLiNX 2's zero-height quads): never
    proposed."""
    return box is None or box[2] - box[0] <= 1e-6 or box[3] - box[1] <= 1e-6


def spans(box, tw):
    return box[0] <= EDGE and box[2] >= tw - EDGE


def on_canvas(box, tw):
    return box[2] > 0 and box[0] < tw


def beyond(box, tw):
    """Reaching past either edge."""
    return box[0] < -EDGE or box[2] > tw + EDGE


def third(box, tw):
    """left, centre or right, by the box's centre."""
    cx = (box[0] + box[2]) / 2
    if cx < tw / 3:
        return "left"
    if cx > 2 * tw / 3:
        return "right"
    return "centre"


def _monotonic(xs):
    d = [b - a for a, b in zip(xs, xs[1:]) if abs(b - a) > 1e-6]
    return all(x > 0 for x in d) or all(x < 0 for x in d)


def moves_freely(frames):
    """frames: [(frame, box)] in frame order. True when the centre moves
    on both axes and not monotonically in x: a projected marker, not a
    slide."""
    if len(frames) < 3:
        return False
    cx = [(b[0] + b[2]) / 2 for _, b in frames]
    cy = [(b[1] + b[3]) / 2 for _, b in frames]
    if max(cx) - min(cx) < MOVE or max(cy) - min(cy) < MOVE:
        return False
    return not _monotonic(cx)


def park_bound(boxes, tw):
    """A world group's park bound in guest pixels from the centre, or
    None: when some sprites sit far beyond the rest, outside the canvas,
    the middle of the widest such gap. Far: the gap is over a quarter of
    the canvas."""
    half = tw / 2
    d = sorted(max(abs(b[0] - half), abs(b[2] - half)) for b in boxes)
    best = None
    for near, far in zip(d, d[1:]):
        if far > half and far - near > tw / 4 and (best is None or far - near > best[1] - best[0]):
            best = (near, far)
    if best is None:
        return None
    return (max(best[0], half) + best[1]) / 2


def vote(kinds):
    """The most frequent kind; ties go to the first seen."""
    count = {}
    for k in kinds:
        count[k] = count.get(k, 0) + 1
    return max(count, key=lambda k: (count[k], -kinds.index(k))) if kinds else None


def kind_rule(frames, sprite_boxes, tw):
    """(kind, rule, park) for one element. frames: [(frame, union box)] in
    frame order; sprite_boxes: every sprite's box; boxes in guest
    pixels."""
    on = [b for _, b in frames if on_canvas(b, tw)]
    if any(spans(b, tw) for b in on):
        return "stretch", "spans the canvas", None
    if any(beyond(b, tw) for b in sprite_boxes) and moves_freely(frames):
        park = park_bound(sprite_boxes, tw)
        rule = "beyond the edges, moving on both axes"
        return "world", rule + (", some sprites parked far out" if park else ""), park
    if not on:
        return "centre", "never on the canvas: kept as unlisted", None
    k = vote([third(b, tw) for b in on])
    if len(on) < len(frames):
        return k, "slides in: %s third while on the canvas" % k, None
    return k, "in the %s third" % k, None


def union(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def touch(a, b, gap=TOUCH):
    return a[0] <= b[2] + gap and b[0] <= a[2] + gap and a[1] <= b[3] + gap and b[1] <= a[3] + gap


def group_candidates(elements, func_of):
    """[[element key, ...]] of two or more: elements drawn by the same
    function whose on-canvas boxes touch, joined transitively. elements:
    {key: box}; func_of: key -> function start or None (None never
    groups)."""
    keys = sorted(elements)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            fa, fb = func_of(a), func_of(b)
            if fa is None or fa != fb:
                continue
            if (
                elements[a] is not None
                and elements[b] is not None
                and touch(elements[a], elements[b])
            ):
                parent[find(a)] = find(b)
    out = {}
    for k in keys:
        out.setdefault(find(k), []).append(k)
    return sorted((v for v in out.values() if len(v) > 1), key=lambda v: v[0])


# ── building the proposal ────────────────────────────────────────────────


def _r(x):
    return round(x, 2) if isinstance(x, float) and x != int(x) else int(x)


class Element:
    def __init__(self, key):
        self.key = key
        self.sprites = []
        self.frames = {}  # (capture, frame) -> [px boxes]
        self.zero = 0
        self.nobox = 0
        self.chain = None
        self.ret = None

    def boxes(self):
        return [b for bs in self.frames.values() for b in bs]

    def per_frame(self):
        return [(k, union(v)) for k, v in sorted(self.frames.items())]


def collect(captures):
    """{name ret: Element}, and the canvas the boxes are measured on."""
    els = {}
    canvas = captures[0].canvas() if captures else None
    for ci, cap in enumerate(captures):
        for s in cap.sprites:
            key = s.name_ret()
            if key is None:
                continue
            e = els.setdefault(key, Element(key))
            if e.chain is None:
                e.chain, e.ret = s.chain, s.ret
            if s.bbox is None:
                e.nobox += 1
                continue
            box = cap.canvas(s.frame).to_px(s.bbox)
            if zero_area(box):
                e.zero += 1
                continue
            e.frames.setdefault((ci, s.frame), []).append(box)
    return els, canvas


def _units_box(canvas, box):
    a, b = canvas.to_units(box[0]), canvas.to_units(box[2])
    sy = canvas.h / canvas.view[3]
    oy = canvas.h / 2 if canvas.centre else 0.0
    c, d = (box[1] - canvas.view[1]) * sy - oy, (box[3] - canvas.view[1]) * sy - oy
    return [_r(a), _r(c), _r(b), _r(d)]


def fit_name(base, suffix=""):
    """base cut to fit the parser's name limit (NAME_MAX - 1 UTF-8 bytes)
    with suffix after it; the cut never splits a character."""
    room = T.NAME_MAX - 1 - len(suffix.encode())
    cut = base.encode()[:room].decode("utf-8", "ignore")
    return cut + suffix


def propose(captures, known=None, analysis=None):
    """The proposal (a dict in the table's schema plus proposed and
    evidence) and a list of notes for the user. known: the game's table,
    parsed, or None; analysis: an Analysis or None."""
    known = known or {}
    els, canvas = collect(captures)
    tw = canvas.tw
    listed = {s["ret_va"]: s for s in T._rows(known, "site") if T._u32(s.get("ret_va"))}
    notes = []
    doc = {"version": T.VERSION}
    if isinstance(known.get("canvas"), dict):
        doc["canvas"] = known["canvas"]
    else:
        doc["canvas"] = {k: v for k, v in canvas.raw.items() if k in T.KEYS["canvas"]}
        notes.append("canvas from the capture's first frame: check its source")
    if isinstance(known.get("layout"), dict):
        doc["layout"] = known["layout"]
    else:
        notes.append("no [layout]: add the vertex stride, x_off and prim before emit")
    for k in ("capture", "wrapper"):
        if known.get(k):
            doc[k] = known[k]

    rows, boxes, kinds = {}, {}, {}
    for key in sorted(els):
        e = els[key]
        if not e.frames:
            why = "zero-area" if e.zero else "no boxes (level 0 without a vertex location)"
            notes.append("0x%08X: %d sprites, %s: not proposed" % (key, e.zero + e.nobox, why))
            continue
        frames = e.per_frame()
        kind, rule, park = kind_rule(frames, e.boxes(), tw)
        on = [b for _, b in frames if on_canvas(b, tw)] or [b for _, b in frames]
        box = union(on)
        func = analysis.function(key) if analysis else None
        ev = {
            "frames": len(frames),
            "first": frames[0][0][1],
            "last": frames[-1][0][1],
            "box": _units_box(canvas, box),
            "rule": rule,
        }
        if e.zero:
            ev["zero_area"] = e.zero
        if e.chain:
            ev["chain"] = ["0x%08X" % c for c in e.chain]
        if func:
            ev["function"] = func[2] or "sub_%08X" % func[0]
        if key in listed:
            row = dict(listed[key])
            row["proposed"] = False
            ev["listed"] = True
            ev["rule_kind"] = kind
            row["evidence"] = ev
        else:
            row = {"ret_va": key, "element": "e_%08X" % key, "kind": kind, "proposed": True}
            if park is not None:
                park = _r(canvas.to_units(tw / 2 + park) - canvas.to_units(tw / 2))
                ev["park"] = park
            row["evidence"] = ev
        rows[key], boxes[key], kinds[key] = row, box, (kind, park)

    def func_of(key):
        if key in listed or not analysis:
            return None
        f = analysis.function(key)
        return f[0] if f else None

    groups = []
    taken = {g["name"] for g in T._rows(known, "group") if isinstance(g.get("name"), str)}
    for members in group_candidates(boxes, func_of):
        f = analysis.function(members[0])
        base = f[2] or "sub_%08X" % f[0]
        name, n = fit_name(base), 2
        while name in taken:
            name = fit_name(base, "_%d" % n)
            n += 1
        taken.add(name)
        ks = [kinds[m][0] for m in members]
        if len(set(ks)) == 1:
            kind, rule = ks[0], "its elements' kind"
        else:
            kind = third(union([boxes[m] for m in members]), tw)
            rule = "mixed kinds: the %s third of the joined box" % kind
        g = {"name": name, "kind": kind, "proposed": True}
        parks = [kinds[m][1] for m in members if kinds[m][1] is not None]
        if kind == "world" and parks:
            g["park"] = min(parks)
        g["evidence"] = {
            "rule": "drawn by one function, boxes touching; " + rule,
            "elements": ["e_%08X" % m for m in members],
        }
        groups.append(g)
        for m in members:
            rows[m].pop("kind", None)
            rows[m]["group"] = name
    # A site's own group has no park bound (the parser's implicit group):
    # a parked world element gets a group of one.
    for key, row in rows.items():
        kind, park = kinds[key]
        if row.get("proposed") and "kind" in row and kind == "world" and park is not None:
            name = row["element"]
            taken.add(name)
            groups.append(
                {
                    "name": name,
                    "kind": "world",
                    "park": row["evidence"].pop("park"),
                    "proposed": True,
                    "evidence": {"rule": "a world element with a park bound"},
                }
            )
            del row["kind"]
            row["group"] = name
    used = {r.get("group") for r in rows.values()}
    known_groups = [g for g in T._rows(known, "group") if g.get("name") in used]
    if known_groups or groups:
        doc["group"] = known_groups + groups
    if rows:
        doc["site"] = [rows[k] for k in sorted(rows)]
    return doc, notes


def write(doc, notes, path, sources):
    head = (
        [
            "hud-map proposal: not the game's table. Correct kinds, groups and",
            "names here, run review again, then emit (it refuses rows still",
            "proposed unless --accept-proposed).",
            "Captures (one per line):",
        ]
        + ["  " + str(src) for src in sources]
        + [""]
        + notes
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(T.dumps(doc, head=head))
    return path


def default_out(capture_path):
    d = capture_path if os.path.isdir(capture_path) else os.path.dirname(capture_path)
    return os.path.join(d, "proposal.toml")
