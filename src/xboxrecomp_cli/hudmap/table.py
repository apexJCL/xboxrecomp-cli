"""The HUD table (config/hud.toml): the toolkit layer's rules, a writer and
a merge that keeps the file's comments.

`validate` is the toolkit parser's check (src/enhance/hud_anchor.c,
parse_table) in Python, message for message where it can be, so that
`hud-map emit --check` refuses what the game's mod would refuse at init.
It reports every error, where the parser stops at the first. What the
parser asks of the running game (a wrapper's hook slot) is emit's check
against the analysis, not this one.

tomllib has no writer, so `dumps` writes the schema's few shapes by hand:
strings, numbers, booleans and flat arrays, with addresses in hex. The
toolkit's reader (recomp_cfg.c) takes no inline tables and no nested
arrays, and no [[a.b]] beside a table [a]: table-form draws are
[[capture.draws]] blocks with no [capture] header, and `c_readable`
refuses what that reader would. Only a proposal's evidence is written
inline; emit strips it.
"""

import math
import re
import tomllib

VERSION = 1
KINDS = ("centre", "left", "right", "proportional", "world", "stretch")
# The parser's spellings: "center" is read as "centre".
KIND_NAMES = {k: k for k in KINDS} | {"center": "centre"}
# HUD_NAME_MAX: names are stored in 48-byte fields with their NUL.
NAME_MAX = 48
SOURCES = ("fixed", "memory", "target")
ORIGINS = ("centre", "center", "top-left")
YDIRS = ("down", "up")
TYPES = ("f32", "u32")
KEYS = {
    "canvas": (
        "w",
        "h",
        "origin",
        "y",
        "view",
        "target",
        "source",
        "w_va",
        "h_va",
        "view_va",
        "type",
        "mem_scale",
    ),
    "layout": ("stride", "x_off", "u_off", "prim"),
    "wrapper": ("va", "name"),
    "group": ("name", "kind", "ref_x", "park"),
    "site": ("ret_va", "element", "group", "kind", "note"),
}
# Keys a proposal adds to rows; never in a table.
PROPOSAL_KEYS = ("proposed", "evidence")
# Rows by identity: the key that names each array entry.
ROW_ID = {"site": "ret_va", "group": "name", "wrapper": "va"}
# Written in hex: these and any *_va key (the canvas's memory addresses,
# a draw row's fn or va, the short form's draws).
VA_KEYS = ("va", "ret_va", "w_va", "h_va", "view_va", "fn", "draws")


def _is_va_key(key):
    return key in VA_KEYS or key.endswith("_va")


def kind_of(s):
    """The canonical kind, or None (kind_parse)."""
    return KIND_NAMES.get(s) if isinstance(s, str) else None


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v):
    return (_int(v) or isinstance(v, float)) and math.isfinite(v)


def _u32(v):
    return _int(v) and 0 <= v <= 0xFFFFFFFF


def _dim_ok(v):
    return _num(v) and 0 < v <= 4096


def _pos_ok(v):
    return _num(v) and 0 <= v <= 4096


def flatten(doc, prefix=""):
    """[(dotted key, value)] as the toolkit's config reader names them:
    tables by dots, arrays of tables by index (site.0.ret_va), other
    arrays as one value."""
    out = []
    for k, v in doc.items():
        key = prefix + k
        if isinstance(v, dict):
            out += flatten(v, key + ".")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for i, x in enumerate(v):
                out += flatten(x, "%s.%d." % (key, i))
        else:
            out.append((key, v))
    return out


_AOT_RE = re.compile(r"^(wrapper|group|site)\.\d+\.(.+)$")


def check_keys(doc, proposal=False):
    """Errors for keys the parser does not know (check_keys)."""
    errs = []
    for k, _ in flatten(doc):
        if k == "version" or k == "capture.draws" or k.startswith("capture.draws."):
            continue
        head, _, rest = k.partition(".")
        if head in ("canvas", "layout") and rest in KEYS[head]:
            continue
        m = _AOT_RE.match(k)
        if m:
            sub = m.group(2)
            if sub in KEYS[m.group(1)]:
                continue
            if proposal and sub.split(".")[0] in PROPOSAL_KEYS:
                continue
        errs.append("unknown key %s" % k)
    return errs


def check_canvas(cv):
    """Errors in [canvas] (parse_canvas)."""
    if not isinstance(cv, dict):
        cv = {}
    src = cv.get("source", "fixed")
    if src not in SOURCES:
        return ["canvas.source=%s is not fixed, memory or target" % src]
    if src != "target" and not (_dim_ok(cv.get("w")) and _dim_ok(cv.get("h"))):
        return ["canvas: w and h must be numbers in (0, 4096]"]
    if "origin" in cv and cv["origin"] not in ORIGINS:
        return ["canvas.origin is not centre or top-left"]
    if "y" in cv and cv["y"] not in YDIRS:
        return ["canvas.y is not down or up"]
    if "view" in cv:
        v = cv["view"]
        if not isinstance(v, list) or len(v) != 4:
            return ["canvas.view is not [x, y, w, h]"]
        if not (_pos_ok(v[0]) and _pos_ok(v[1]) and _dim_ok(v[2]) and _dim_ok(v[3])):
            return ["canvas.view needs x, y in [0, 4096] and w, h in (0, 4096]"]
    if "target" in cv:
        t = cv["target"]
        if t == "frame":
            pass
        elif isinstance(t, list) and len(t) == 2:
            if not all(_int(x) and 0 < x <= 4096 for x in t):
                return ["canvas.target is not [w, h] in (0, 4096]"]
        else:
            return ['canvas.target is not "frame" or [w, h]']
    if src == "memory":
        if not (_u32(cv.get("w_va")) and _u32(cv.get("h_va"))):
            return ["canvas: source memory needs w_va and h_va"]
        if "view_va" in cv and not _u32(cv["view_va"]):
            return ["canvas.view_va is not an address"]
        if "type" in cv and cv["type"] not in TYPES:
            return ["canvas.type is not f32 or u32"]
        if "mem_scale" in cv and not _dim_ok(cv["mem_scale"]):
            return ["canvas.mem_scale is not a number in (0, 4096]"]
    elif any(k in cv for k in ("w_va", "h_va", "view_va")):
        return ['canvas: w_va, h_va and view_va need source = "memory"']
    return []


def check_layout(lay):
    """Errors in [layout] (parse_layout). Stricter than the parser on one
    point: a non-integer is refused, where the parser reads its default."""
    if not isinstance(lay, dict):
        lay = {}

    def get(k, default):
        v = lay.get(k, default)
        return v if _int(v) else None

    stride, x_off, prim = get("stride", -1), get("x_off", -1), get("prim", 4)
    if stride is None or not 4 <= stride <= 256:
        return ["layout.stride must be 4..256"]
    if x_off is None or x_off < 0 or x_off + 4 > stride:
        return ["layout.x_off must lie in the stride"]
    if "u_off" in lay:
        u = get("u_off", -1)
        if u is None or u < 0 or u + 4 > stride:
            return ["layout.u_off must lie in the stride"]
    if prim is None or not 1 <= prim <= 64:
        return ["layout.prim must be 1..64"]
    return []


def _name_ok(v):
    return isinstance(v, str) and 0 < len(v.encode()) < NAME_MAX


def _rows(doc, name):
    """The array's tables. Anything else there (group = ["x"]) is not a
    row: check_keys reports it as an unknown key."""
    v = doc.get(name, [])
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def check_rows(doc):
    """Errors in the groups, sites and wrappers (parse_table)."""
    errs = []
    groups = set()
    for i, g in enumerate(_rows(doc, "group")):
        name = g.get("name")
        if not _name_ok(name):
            errs.append("group %d: a name of 1..%d characters is needed" % (i, NAME_MAX - 1))
            continue
        if name in groups:
            errs.append("group %s: listed twice" % name)
            continue
        kind = kind_of(g.get("kind"))
        if kind is None:
            errs.append("group %s: unknown kind %s" % (name, g.get("kind", "(none)")))
            continue
        groups.add(name)
        if "ref_x" in g and not _num(g["ref_x"]):
            errs.append("group %s: ref_x is not a number" % name)
        if "park" in g:
            if kind != "world":
                errs.append("group %s: park is for world groups" % name)
            elif not (_num(g["park"]) and g["park"] > 0):
                errs.append("group %s: park is not a positive number" % name)
    seen = {}
    for i, s in enumerate(_rows(doc, "site")):
        ret = s.get("ret_va")
        if not _u32(ret) or not ret:
            errs.append("site %d: ret_va is not an address" % i)
            continue
        where = "site 0x%08X" % ret
        if not _name_ok(s.get("element")):
            errs.append("%s: an element of 1..%d characters is needed" % (where, NAME_MAX - 1))
        gn, kind = s.get("group"), s.get("kind")
        if gn is not None:
            if kind is not None:
                errs.append("%s: kind belongs to group %s" % (where, gn))
            elif not isinstance(gn, str):
                errs.append("%s: group is not a name" % where)
            elif gn not in groups:
                errs.append("%s: unknown group %s" % (where, gn))
        elif kind_of(kind) is None:
            errs.append(
                "%s: no group, and %s"
                % (where, "unknown kind %s" % kind if kind is not None else "no kind")
            )
        if ret in seen:
            errs.append("%s: listed twice" % where)
        seen[ret] = i
    for i, w in enumerate(_rows(doc, "wrapper")):
        va = w.get("va")
        if not _u32(va) or not va:
            errs.append("wrapper %d: va is not an address" % i)
    return errs


def validate(doc, proposal=False):
    """Every error the toolkit's parser would raise on doc (a parsed
    table), in its order. proposal: the rows may carry proposed and
    evidence."""
    v = doc.get("version")
    if not _int(v) or v != VERSION:
        return ["version must be %d" % VERSION]
    errs = check_keys(doc, proposal)
    errs += check_canvas(doc.get("canvas"))
    errs += check_layout(doc.get("layout"))
    return errs + check_rows(doc)


DRAW_KEYS = ("fn", "va", "sig", "name", "vtx", "count", "stride", "prim")
DRAWS_MAX = 64  # HUD_DRAWS_MAX: the toolkit drops the rest silently
_SPACE = " \t\n\v\f\r"
_DIGITS = {8: "01234567", 10: "0123456789", 16: "0123456789abcdefABCDEF"}


def c_ulong(s):
    """s read as C's strtoul(s, &end, 0) with *end required to be 0, as the
    toolkit reads a draw's address and a stack:N offset: leading white
    space, an optional +, then 0x hex, 0 octal or decimal, ASCII digits
    only, the whole string. None where the toolkit refuses (and for a
    minus, which strtoul would wrap)."""
    if not isinstance(s, str):
        return None
    i = 0
    while i < len(s) and s[i] in _SPACE:
        i += 1
    if i < len(s) and s[i] == "+":
        i += 1
    rest = s[i:]
    if rest[:2] in ("0x", "0X") and rest[2:3] and rest[2] in _DIGITS[16]:
        base, rest = 16, rest[2:]
    elif rest[:1] == "0":
        base = 8
    else:
        base = 10
    if not rest or any(c not in _DIGITS[base] for c in rest):
        return None
    return int(rest, base)


def _draw_entry(v):
    """A draw's entry as draws() gives it: "0x%08X" for an address in any
    spelling the toolkit reads, "sig:NAME" as it is, else v (or None)."""
    if _int(v):
        return "0x%08X" % v if 0 <= v <= 0xFFFFFFFF else str(v)
    if isinstance(v, str):
        if v.startswith("sig:"):
            return v
        n = c_ulong(v)
        return "0x%08X" % n if n is not None and n <= 0xFFFFFFFF else v
    return None


def draws(doc):
    """[capture] draws as [(entry, row)], as the toolkit reads them
    (hud_anchor.c parse_draws): entry is "0x%08X", "sig:NAME" (a
    hook_functions spelling), the string as written when it is neither,
    or None; row is the draw's own table or {}. The short form is a list
    of VAs and address or "sig:" strings; a [[capture.draws]] row names
    its function by fn, else va, else sig = "NAME". A [capture.draws]
    table (not an array) gives no draws, as in the toolkit."""
    cap = doc.get("capture")
    raw = cap.get("draws", []) if isinstance(cap, dict) else []
    if isinstance(raw, dict):
        return []
    out = []
    for d in raw if isinstance(raw, list) else [raw]:
        row = d if isinstance(d, dict) else {}
        if row:
            v = row.get("fn", row.get("va"))
            if v is None and isinstance(row.get("sig"), str):
                v = "sig:" + row["sig"]
        else:
            v = d
        out.append((_draw_entry(v), row))
    return out


def _loc_ok(v, const):
    """A vtx, count or stride location (draw_loc): "ecx", "edx",
    "stack:N" (N as strtoul reads it, a multiple of 4 below 4096), or
    with const an integer 0..0xFFFF."""
    if _int(v):
        return const and 0 <= v <= 0xFFFF
    if not isinstance(v, str):
        return False
    if v in ("ecx", "edx"):
        return True
    if not v.startswith("stack:"):
        return False
    n = c_ulong(v[6:])
    return n is not None and n % 4 == 0 and n < 4096


def _draw_va_ok(v):
    """draw_va: a sig: name (resolved by capture), or a nonzero 32-bit
    address."""
    if isinstance(v, str) and v.startswith("sig:"):
        return len(v) > 4
    if isinstance(v, str):
        v = c_ulong(v)
    return _int(v) and 0 < v <= 0xFFFFFFFF


def check_draws(doc):
    """Errors for [capture] draws rows the capture would skip or cut (the
    toolkit's parser skips them with a line and never fails the table;
    emit refuses them). A sig: draw is fine here: hud-map capture writes
    the run's copy with its VA."""
    cap = doc.get("capture")
    if isinstance(cap, dict) and isinstance(cap.get("draws"), dict):
        return ["capture.draws: draws is a table; use [[capture.draws]]"]
    errs = []
    ds = draws(doc)
    if len(ds) > DRAWS_MAX:
        errs.append("capture.draws: %d draws, the toolkit reads at most %d" % (len(ds), DRAWS_MAX))
    for i, (entry, row) in enumerate(ds):
        where = "capture.draws %d" % i
        if not _draw_va_ok(entry):
            errs.append('%s: fn is not an address, a "0x..." or a sig: name' % where)
        if not row:
            continue
        for k in row:
            if k not in DRAW_KEYS:
                errs.append("%s: unknown key %s" % (where, k))
        if "name" in row and not _name_ok(row["name"]):
            errs.append("%s: name is not a string of 1..%d bytes" % (where, NAME_MAX - 1))
        if "vtx" in row and not _loc_ok(row["vtx"], False):
            errs.append("%s: vtx is not ecx, edx or stack:N" % where)
        for k in ("count", "stride"):
            if k in row and not _loc_ok(row[k], True):
                errs.append("%s: %s is not ecx, edx, stack:N or a number" % (where, k))
        if ("vtx" in row) != ("count" in row):
            errs.append("%s: vtx and count go together" % where)
        if "prim" in row and not (_int(row["prim"]) and 1 <= row["prim"] <= 64):
            errs.append("%s: prim is not 1..64" % where)
    return errs


# ── writing ──────────────────────────────────────────────────────────────


_NAMED = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _str(s):
    """A basic string: backslash and quote escaped, \\b \\t \\n \\f \\r
    by name, any other control character as \\uXXXX (all of which the
    toolkit's reader takes)."""
    out = []
    for c in s:
        if c in '"\\':
            out.append("\\" + c)
        elif c in _NAMED:
            out.append(_NAMED[c])
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append("\\u%04X" % ord(c))
        else:
            out.append(c)
    return '"%s"' % "".join(out)


def value(v, key=""):
    if isinstance(v, bool):
        return "true" if v else "false"
    if _int(v):
        return "0x%08X" % v if _is_va_key(key) and 0 <= v <= 0xFFFFFFFF else str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else _str(str(v))
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        return "[%s]" % ", ".join(value(x, key) for x in v)
    if isinstance(v, dict):
        return "{ %s }" % ", ".join("%s = %s" % (_key(k), value(x, k)) for k, x in v.items())
    raise TypeError("no TOML for %r" % (v,))


def _key(k):
    return k if re.match(r"^[A-Za-z0-9_-]+$", k) else _str(k)


def body(row):
    """A table's key lines."""
    return ["%s = %s" % (_key(k), value(v, k)) for k, v in row.items()]


def block(header, row, aot=False):
    """One [header] or [[header]] block's lines."""
    return [("[[%s]]" if aot else "[%s]") % header] + body(row)


def capture_blocks(cap):
    """[capture] as [(header, aot, row)]: one [capture] block with draws as
    a flat array when every draw is a name or a VA, else one
    [[capture.draws]] block per draw (a name or VA becoming its fn), with
    no [capture] header: the reader refuses [[capture.draws]] beside
    [capture], and draws is the table's only capture key."""
    if not isinstance(cap, dict):
        return []
    raw = cap.get("draws", [])
    raw = raw if isinstance(raw, list) else [raw]
    if not any(isinstance(d, dict) for d in raw):
        return [("capture", False, cap)]
    rows = [d if isinstance(d, dict) else {"fn": d} for d in raw]
    return [("capture.draws", True, r) for r in rows]


def dumps(doc, head=()):
    """A table or proposal as TOML text: head (comment lines) first, then
    version, the canvas, the layout, the capture, the wrappers, the groups
    and the sites."""
    lines = ["# " + h if h else "#" for h in head]
    lines.append("version = %s" % value(doc.get("version", VERSION)))
    for name in ("canvas", "layout"):
        if isinstance(doc.get(name), dict):
            lines += [""] + block(name, doc[name])
    for header, aot, row in capture_blocks(doc.get("capture")):
        lines += [""] + block(header, row, aot=aot)
    for name in ("wrapper", "group", "site"):
        for row in _rows(doc, name):
            lines += [""] + block(name, row, aot=True)
    return "\n".join(lines) + "\n"


# ── merging into an existing file ────────────────────────────────────────

_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([A-Za-z0-9_.\-]+)\s*\]\]?\s*(#.*)?$")


class Block:
    """A slice of the file: the comments above it (lead), its header and
    its body, as lines."""

    def __init__(self, header=None, aot=False):
        self.lead, self.header, self.aot, self.lines = [], header, aot, []

    def kind(self):
        return self.header.split(".")[0] if self.header else None

    def text(self):
        return self.lead + ([self.head_line] if self.header else []) + self.lines

    def parsed(self):
        """The block's own row, read through tomllib."""
        try:
            doc = tomllib.loads(
                "\n".join(([self.head_line] if self.header else []) + self.lines) + "\n"
            )
        except tomllib.TOMLDecodeError:
            return None
        if not self.header:
            return doc
        node = doc
        for part in self.header.split("."):
            node = node.get(part, {})
            if isinstance(node, list):
                node = node[-1] if node else {}
        return node


def split(text):
    """The file as [Block]: the preamble (no header) first, then one block
    per table header. Comment and blank lines just above a header lead the
    block they describe."""
    blocks = [Block()]
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            cur = blocks[-1]
            lead = []
            while cur.lines and (not cur.lines[-1].strip() or cur.lines[-1].lstrip()[0] == "#"):
                lead.insert(0, cur.lines.pop())
            b = Block(m.group(2), aot=m.group(1) == "[[")
            b.head_line = line
            b.lead = lead
            blocks.append(b)
        else:
            blocks[-1].lines.append(line)
    return blocks


def join(blocks):
    lines = []
    for b in blocks:
        lines += b.text()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def _row_id(kind, row):
    k = ROW_ID.get(kind)
    return row.get(k) if k and isinstance(row, dict) else None


def strip_proposal(row):
    return {k: v for k, v in row.items() if k not in PROPOSAL_KEYS}


_KEYLINE_RE = re.compile(r"^\s*([A-Za-z0-9_-]+|\"[^\"]*\")\s*=\s*(.*)$")


def _trailing_comment(rest):
    """The comment after a one-line value (with its leading spaces), or
    "". None when the value continues on the next lines."""
    q = None
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if q:
            if c == "\\" and q == '"':
                i += 1
            elif c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "#":
            if depth == 0:
                j = i
                while j > 0 and rest[j - 1] in " \t":
                    j -= 1
                return rest[j:]
            # A comment inside an array over several lines: to its end.
            while i < len(rest) and rest[i] != "\n":
                i += 1
        i += 1
    return "" if depth == 0 and q is None else None


def rebody(lines, row):
    """A block's body lines for row, keeping the old body's comments: a
    key kept is rewritten in place with its trailing comment, a key gone
    is dropped, comment and blank lines stay where they are, new keys go
    after the last key. A body with a value over several lines is written
    afresh (its comments lost)."""
    new = dict((k, "%s = %s" % (_key(k), value(v, k))) for k, v in row.items())
    out, done, last = [], set(), -1
    for line in lines:
        m = _KEYLINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        k = m.group(1).strip('"')
        tail = _trailing_comment(m.group(2))
        if tail is None:
            return body(row)
        if k in new and k not in done:
            out.append(new[k] + tail)
            done.add(k)
            last = len(out) - 1
    rest = [new[k] for k in row if k not in done]
    at = last + 1
    if last < 0:
        # No key kept: the new keys before any trailing blank lines.
        at = len(out)
        while at > 0 and not out[at - 1].strip():
            at -= 1
    return out[:at] + rest + out[at:]


def _capture_changed(blocks, cap):
    """Replace every capture block (a [capture] or [[capture.draws]]) by
    cap's blocks, at the first one's place, else by the order of kinds.
    Returns whether the file changed."""
    try:
        cur = tomllib.loads(join(blocks)).get("capture")
    except tomllib.TOMLDecodeError:
        cur = None
    if cur == cap:
        return False
    at, lead = None, None
    for i in range(len(blocks) - 1, -1, -1):
        h = blocks[i].header or ""
        if h == "capture" or h.startswith("capture."):
            at, lead = i, blocks[i].lead
            del blocks[i]
    new = []
    for header, aot, row in capture_blocks(cap):
        nb = Block(header, aot=aot)
        nb.head_line = ("[[%s]]" if aot else "[%s]") % header
        nb.lead = [""]
        nb.lines = body(row)
        new.append(nb)
    if at is None:
        at = min(
            (i for i, b in enumerate(blocks) if b.kind() in ("wrapper", "group", "site")),
            default=len(blocks),
        )
    elif new and lead:
        # The comments above the first capture block lead the new ones.
        new[0].lead = lead
    blocks[at:at] = new
    return True


def merge(text, changes):
    """text with changes applied: changes is [(kind, row)] where kind is
    canvas, layout, capture, wrapper, group or site. A row whose identity
    (a site's ret_va, a group's name, a wrapper's va; the one block for
    the others) is in the file replaces that block's keys when it differs
    (rebody: its comments kept) and leaves it as it is otherwise; a new
    row goes after the last block of its kind. capture is the [capture]
    block and its [[capture.draws]], replaced as one. Returns (text,
    [(kind, id, "added" | "changed")])."""
    blocks = split(text) if text.strip() else [Block()]
    if not text.strip():
        blocks[0].lines = ["version = %d" % VERSION]
    report = []
    for kind, row in changes:
        row = strip_proposal(row)
        if kind == "capture":
            had = any((b.header or "").split(".")[0] == "capture" for b in blocks)
            if _capture_changed(blocks, row):
                report.append((kind, None, "changed" if had else "added"))
            continue
        rid = _row_id(kind, row)
        aot = kind in ROW_ID
        found = None
        for b in blocks:
            if b.header != kind:
                continue
            got = b.parsed()
            if not aot or (got is not None and _row_id(kind, got) == rid):
                found = (b, got)
                break
        if found:
            b, got = found
            if got == row:
                continue
            b.lines = rebody(b.lines, row)
            report.append((kind, rid, "changed"))
            continue
        nb = Block(kind, aot=aot)
        nb.head_line = ("[[%s]]" if aot else "[%s]") % kind
        nb.lead = [""]
        nb.lines = body(row)
        at = max((i for i, b in enumerate(blocks) if b.kind() == kind), default=None)
        if at is None:
            order = ("canvas", "layout", "capture", "wrapper", "group", "site")
            later = order[order.index(kind) + 1 :]
            at = min((i for i, b in enumerate(blocks) if b.kind() in later), default=len(blocks))
            blocks.insert(at, nb)
        else:
            # After the last block of its kind and that block's sub-tables.
            at += 1
            while at < len(blocks) and (blocks[at].header or "").startswith(kind + "."):
                at += 1
            blocks.insert(at, nb)
        # A blank line between a previous body and the new block.
        if (
            nb.lead == [""]
            and at > 0
            and blocks[at - 1].lines
            and not blocks[at - 1].lines[-1].strip()
        ):
            nb.lead = []
        report.append((kind, rid, "added"))
    return join(blocks), report


def c_readable(text):
    """What the toolkit's reader (recomp_cfg.c) refuses in text that
    tomllib takes: inline tables, nested arrays, and an array of tables
    [[a.b]] beside a table [a] (or [a.b.c] inside it)."""
    errs = []
    tables, aots = [], []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        n = i + 1
        i += 1
        m = _HEADER_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            (aots if m.group(1) == "[[" else tables).append((m.group(2), n))
            continue
        km = _KEYLINE_RE.match(line)
        if not km:
            continue
        val = km.group(2)
        # A value over several lines: join until its brackets close.
        while _trailing_comment(val) is None and i < len(lines):
            val += "\n" + lines[i]
            i += 1
        v = val.lstrip()
        if v.startswith("{"):
            errs.append("line %d: %s: inline tables are not supported" % (n, km.group(1)))
        elif v.startswith("["):
            depth, q, j = 0, None, 0
            while j < len(v):
                c = v[j]
                if q:
                    if c == "\\" and q == '"':
                        j += 1
                    elif c == q:
                        q = None
                elif c in "\"'":
                    q = c
                elif c == "#":
                    while j < len(v) and v[j] != "\n":
                        j += 1
                elif c in "[{":
                    depth += 1
                    if depth > 1:
                        errs.append(
                            "line %d: %s: nested arrays and inline tables are not supported"
                            % (n, km.group(1))
                        )
                        break
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
    for a, n in aots:
        for t, tn in tables:
            if t == a or a.startswith(t + ".") or t.startswith(a + "."):
                errs.append("line %d: [[%s]] beside the table [%s] (line %d)" % (n, a, t, tn))
                break
    return errs
