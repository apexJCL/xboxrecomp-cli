"""A TrueType reader and a scanline rasteriser, standard library only.

Reads what glyph rendering needs: `head` (units per em, loca format),
`maxp`, `hhea` and `hmtx` (advances), `loca` and `glyf` (simple and
composite outlines), `cmap` (formats 4 and 12), and `name` (the full
name a game's manifest finds its font by). A file is one font or a
collection (`ttcf`, as macOS ships PT Sans), whose faces share the file
and are picked by full name. No hinting and no CFF outlines: the HUD
atlas is drawn at 72-pixel cap heights, where hinting changes nothing a
player would see.

Rasterisation: the quadratic contours are flattened to segments, filled
with the non-zero winding rule on 4 sub-scanlines per pixel row with
exact horizontal coverage per span, into 8-bit coverage. The operations
are IEEE doubles in a fixed order, so the same font at the same size
gives the same bitmap on every host.
"""

import os
import struct
import sys

SS = 4  # sub-scanlines per pixel row
FLATTEN = 8  # segments per quadratic curve

ARG_1_AND_2_ARE_WORDS = 0x0001
ARGS_ARE_XY_VALUES = 0x0002
WE_HAVE_A_SCALE = 0x0008
MORE_COMPONENTS = 0x0020
WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
WE_HAVE_A_TWO_BY_TWO = 0x0080


class FontError(Exception):
    pass


def _f2dot14(v):
    return v / 16384.0


class Font:
    def __init__(self, data, offset=0):
        self.data = data
        self.tables = self._directory(offset)
        head = self._table("head")
        self.upem = struct.unpack_from(">H", head, 18)[0]
        self.loca_long = struct.unpack_from(">h", head, 50)[0] == 1
        self.num_glyphs = struct.unpack_from(">H", self._table("maxp"), 4)[0]
        hhea = self._table("hhea")
        self.ascender, self.descender = struct.unpack_from(">hh", hhea, 4)
        self.num_hmetrics = struct.unpack_from(">H", hhea, 34)[0]
        self.hmtx = self._table("hmtx")
        self.loca = self._read_loca()
        self.glyf = self._table("glyf")
        self.cmap = self._read_cmap()
        self.name = full_name(data, offset)
        self._outlines = {}

    # ---- tables ----------------------------------------------------------

    def _directory(self, base):
        # Table offsets are from the start of the file, in a collection too.
        d = self.data
        if len(d) < base + 12:
            raise FontError("not a TrueType font")
        tag = d[base : base + 4]
        if tag not in (b"\x00\x01\x00\x00", b"true"):
            raise FontError("not a TrueType font (CFF outlines?)")
        n = struct.unpack_from(">H", d, base + 4)[0]
        tables = {}
        for i in range(n):
            name, _, off, length = struct.unpack_from(">4sIII", d, base + 12 + 16 * i)
            if off + length > len(d):
                raise FontError("table %s is outside the file" % name.decode("latin-1"))
            tables[name.decode("latin-1")] = (off, length)
        return tables

    def _table(self, name):
        if name not in self.tables:
            raise FontError("font has no %s table" % name)
        off, length = self.tables[name]
        return self.data[off : off + length]

    def _read_loca(self):
        loca = self._table("loca")
        n = self.num_glyphs + 1
        if self.loca_long:
            return list(struct.unpack_from(">%dI" % n, loca, 0))
        return [2 * v for v in struct.unpack_from(">%dH" % n, loca, 0)]

    def _read_cmap(self):
        cmap = self._table("cmap")
        n = struct.unpack_from(">H", cmap, 2)[0]
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack_from(">HHI", cmap, 4 + 8 * i)
            fmt = struct.unpack_from(">H", cmap, off)[0]
            rank = {(3, 10): 4, (0, 4): 3, (3, 1): 2, (0, 3): 1}.get(
                (pid, eid), 0 if fmt in (4, 12) else -1
            )
            if rank >= 0 and (best is None or rank > best[0]):
                best = (rank, off, fmt)
        if best is None:
            raise FontError("font has no usable cmap subtable")
        _, off, fmt = best
        mapping = {}
        if fmt == 4:
            segx2 = struct.unpack_from(">H", cmap, off + 6)[0]
            seg = segx2 // 2
            ends = struct.unpack_from(">%dH" % seg, cmap, off + 14)
            starts = struct.unpack_from(">%dH" % seg, cmap, off + 16 + segx2)
            deltas = struct.unpack_from(">%dh" % seg, cmap, off + 16 + 2 * segx2)
            ro_base = off + 16 + 3 * segx2
            ros = struct.unpack_from(">%dH" % seg, cmap, ro_base)
            for s in range(seg):
                if starts[s] > ends[s]:
                    continue
                for c in range(starts[s], min(ends[s], 0xFFFE) + 1):
                    if ros[s] == 0:
                        g = (c + deltas[s]) & 0xFFFF
                    else:
                        p = ro_base + 2 * s + ros[s] + 2 * (c - starts[s])
                        if p + 2 > len(cmap):
                            continue
                        g = struct.unpack_from(">H", cmap, p)[0]
                        if g:
                            g = (g + deltas[s]) & 0xFFFF
                    if g:
                        mapping[c] = g
        elif fmt == 12:
            ngroups = struct.unpack_from(">I", cmap, off + 12)[0]
            for i in range(ngroups):
                sc, ec, sg = struct.unpack_from(">III", cmap, off + 16 + 12 * i)
                for c in range(sc, ec + 1):
                    mapping[c] = sg + (c - sc)
        else:
            raise FontError("cmap format %d not supported" % fmt)
        return mapping

    # ---- glyphs ----------------------------------------------------------

    def glyph_index(self, codepoint):
        return self.cmap.get(codepoint, 0)

    def advance(self, gid):
        i = min(gid, self.num_hmetrics - 1)
        return struct.unpack_from(">H", self.hmtx, 4 * i)[0]

    def outline(self, gid, depth=0):
        """Contours of (x, y, on_curve) in font units; [] for an empty glyph."""
        if gid in self._outlines:
            return self._outlines[gid]
        if gid >= self.num_glyphs or depth > 8:
            return []
        start, end = self.loca[gid], self.loca[gid + 1]
        if end <= start:
            self._outlines[gid] = []
            return []
        g = self.glyf[start:end]
        ncont = struct.unpack_from(">h", g, 0)[0]
        if ncont >= 0:
            contours = self._simple(g, ncont)
        else:
            contours = self._composite(g, depth)
        self._outlines[gid] = contours
        return contours

    def _simple(self, g, ncont):
        ends = struct.unpack_from(">%dH" % ncont, g, 10)
        npts = (ends[-1] + 1) if ncont else 0
        p = 10 + 2 * ncont
        ilen = struct.unpack_from(">H", g, p)[0]
        p += 2 + ilen
        flags = []
        while len(flags) < npts:
            f = g[p]
            p += 1
            flags.append(f)
            if f & 8:
                rep = g[p]
                p += 1
                flags.extend([f] * rep)
        flags = flags[:npts]
        xs, v = [], 0
        for f in flags:
            if f & 2:
                d = g[p]
                p += 1
                v += d if f & 16 else -d
            elif not f & 16:
                v += struct.unpack_from(">h", g, p)[0]
                p += 2
            xs.append(v)
        ys, v = [], 0
        for f in flags:
            if f & 4:
                d = g[p]
                p += 1
                v += d if f & 32 else -d
            elif not f & 32:
                v += struct.unpack_from(">h", g, p)[0]
                p += 2
            ys.append(v)
        contours, s = [], 0
        for e in ends:
            contours.append([(xs[i], ys[i], bool(flags[i] & 1)) for i in range(s, e + 1)])
            s = e + 1
        return contours

    def _composite(self, g, depth):
        p = 10
        contours = []
        while True:
            flags, gid = struct.unpack_from(">HH", g, p)
            p += 4
            if flags & ARG_1_AND_2_ARE_WORDS:
                a1, a2 = struct.unpack_from(">hh", g, p)
                p += 4
            else:
                a1, a2 = struct.unpack_from(">bb", g, p)
                p += 2
            xx, xy, yx, yy = 1.0, 0.0, 0.0, 1.0
            if flags & WE_HAVE_A_SCALE:
                xx = yy = _f2dot14(struct.unpack_from(">h", g, p)[0])
                p += 2
            elif flags & WE_HAVE_AN_X_AND_Y_SCALE:
                xx, yy = (_f2dot14(v) for v in struct.unpack_from(">hh", g, p))
                p += 4
            elif flags & WE_HAVE_A_TWO_BY_TWO:
                xx, xy, yx, yy = (_f2dot14(v) for v in struct.unpack_from(">hhhh", g, p))
                p += 8
            dx, dy = (a1, a2) if flags & ARGS_ARE_XY_VALUES else (0, 0)
            for c in self.outline(gid, depth + 1):
                contours.append(
                    [(x * xx + y * yx + dx, x * xy + y * yy + dy, on) for x, y, on in c]
                )
            if not flags & MORE_COMPONENTS:
                break
        return contours

    def bbox(self, gid):
        """(xmin, ymin, xmax, ymax) of the outline points in font units, or None."""
        pts = [(x, y) for c in self.outline(gid) for x, y, _ in c]
        if not pts:
            return None
        return (
            min(p[0] for p in pts),
            min(p[1] for p in pts),
            max(p[0] for p in pts),
            max(p[1] for p in pts),
        )

    def cap_height(self):
        """Units from the baseline to the top of H (then I, then E, then the
        ascender), the height a fitted atlas matches."""
        for ch in "HIE":
            b = self.bbox(self.glyph_index(ord(ch)))
            if b:
                return b[3]
        return self.ascender

    # ---- rasterisation ---------------------------------------------------

    def segments(self, gid, kx, ky, ox, oy):
        """Straight segments of the glyph in pixel space (y down): font
        units times kx across and ky down, origin at (ox, oy)."""
        segs = []
        for c in self.outline(gid):
            pts = [(ox + x * kx, oy - y * ky, on) for x, y, on in c]
            n = len(pts)
            if n < 2:
                continue
            # Start on an on-curve point (an implied one between two
            # off-curve points if the contour has none).
            start = next((i for i in range(n) if pts[i][2]), None)
            if start is None:
                mx = (pts[-1][0] + pts[0][0]) / 2
                my = (pts[-1][1] + pts[0][1]) / 2
                pts = [(mx, my, True)] + pts
                n += 1
                start = 0
            seq = pts[start:] + pts[:start] + [pts[start]]
            cur = seq[0][:2]
            ctrl = None
            for x, y, on in seq[1:]:
                if on:
                    if ctrl is None:
                        segs.append((cur[0], cur[1], x, y))
                    else:
                        segs.extend(_quad(cur, ctrl, (x, y)))
                        ctrl = None
                    cur = (x, y)
                elif ctrl is None:
                    ctrl = (x, y)
                else:
                    mid = ((ctrl[0] + x) / 2, (ctrl[1] + y) / 2)
                    segs.extend(_quad(cur, ctrl, mid))
                    cur = mid
                    ctrl = (x, y)
        return segs

    def render(self, gid, px_x, px_y, ox, oy, width, height):
        """8-bit coverage (width x height, row-major) of the glyph drawn at
        px_x pixels per em across and px_y down with its origin at (ox, oy),
        which may be fractional."""
        return rasterize(
            self.segments(gid, px_x / self.upem, px_y / self.upem, ox, oy), width, height
        )


def _quad(p0, c, p1):
    out = []
    px, py = p0
    for i in range(1, FLATTEN + 1):
        t = i / FLATTEN
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0]
        y = u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1]
        out.append((px, py, x, y))
        px, py = x, y
    return out


def rasterize(segs, width, height):
    """Non-zero winding fill of the segments into width x height coverage."""
    cov = bytearray(width * height)
    if not segs or width <= 0 or height <= 0:
        return cov
    edges = []
    for x0, y0, x1, y1 in segs:
        if y0 == y1:
            continue
        edges.append((min(y0, y1), max(y0, y1), x0, y0, x1, y1, 1 if y1 > y0 else -1))
    edges.sort()
    acc = [0.0] * width
    scale = 255.0 / SS
    for row in range(height):
        for i in range(width):
            acc[i] = 0.0
        touched = False
        for j in range(SS):
            ys = row + (j + 0.5) / SS
            xs = []
            for ymin, ymax, x0, y0, x1, y1, d in edges:
                if ymin > ys:
                    break
                if ymax <= ys:
                    continue
                xs.append((x0 + (ys - y0) * (x1 - x0) / (y1 - y0), d))
            if len(xs) < 2:
                continue
            xs.sort()
            wind = 0
            for i in range(len(xs) - 1):
                wind += xs[i][1]
                if wind == 0:
                    continue
                xa, xb = max(xs[i][0], 0.0), min(xs[i + 1][0], float(width))
                if xb <= xa:
                    continue
                touched = True
                ia, ib = int(xa), int(xb)
                if ia == ib:
                    acc[ia] += xb - xa
                else:
                    acc[ia] += ia + 1 - xa
                    for c in range(ia + 1, min(ib, width)):
                        acc[c] += 1.0
                    if ib < width:
                        acc[ib] += xb - ib
        if touched:
            base = row * width
            for i in range(width):
                v = acc[i]
                if v > 0:
                    cov[base + i] = min(255, int(v * scale + 0.5))
    return cov


def face_offsets(data):
    """The offset of each face's table directory: [0] for a single font."""
    if data[:4] != b"ttcf":
        return [0]
    if len(data) < 12:
        raise FontError("truncated font collection")
    n = struct.unpack_from(">I", data, 8)[0]
    if n == 0 or 12 + 4 * n > len(data):
        raise FontError("font collection of %d faces does not fit the file" % n)
    return list(struct.unpack_from(">%dI" % n, data, 12))


def full_name(data, offset=0):
    """A face's full name (name ID 4), read without parsing its outlines;
    '' when it has none. Windows (UTF-16) records win over Mac Roman."""
    try:
        n = struct.unpack_from(">H", data, offset + 4)[0]
        for i in range(n):
            tag, _, off, length = struct.unpack_from(">4sIII", data, offset + 12 + 16 * i)
            if tag == b"name":
                break
        else:
            return ""
        t = data[off : off + length]
        count, strings = struct.unpack_from(">HH", t, 2)
        best = ""
        for r in range(count):
            pid, _, _, nid, ln, no = struct.unpack_from(">6H", t, 6 + 12 * r)
            if nid != 4:
                continue
            raw = t[strings + no : strings + no + ln]
            if pid in (0, 3):
                return raw.decode("utf-16-be", "replace")
            if pid == 1 and not best:
                best = raw.decode("mac_roman", "replace")
        return best
    except struct.error:
        return ""


def _same(a, b):
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def load(path, name=""):
    """The font in path; in a collection, the face whose full name is name
    (any face of a one-font file is taken as it is: the user named it)."""
    with open(path, "rb") as f:
        data = f.read()
    offsets = face_offsets(data)
    if len(offsets) == 1:
        return Font(data, offsets[0])
    names = [full_name(data, o) for o in offsets]
    for o, n in zip(offsets, names):
        if name and _same(n, name):
            return Font(data, o)
    raise FontError(
        "a collection of %d faces (%s)%s"
        % (
            len(offsets),
            ", ".join(n or "?" for n in names),
            ", none of them %r" % name if name else "; name one in game.toml",
        )
    )


def font_dirs():
    """The host's font folders, user ones first."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return [
            os.path.join(home, "Library", "Fonts"),
            "/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/System/Library/Fonts",
        ]
    if os.name == "nt":
        out = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        out.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        return out
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return [
        os.path.join(data_home, "fonts"),
        os.path.join(home, ".fonts"),
        "/usr/local/share/fonts",
        "/usr/share/fonts",
    ]


def _file_names(path):
    """The full names of the faces in a font file, reading only the
    directories and name tables (a system collection can be tens of MB)."""
    with open(path, "rb") as f:

        def at(off, n):
            f.seek(off)
            b = f.read(n)
            if len(b) != n:
                raise FontError("truncated")
            return b

        head = at(0, 12)
        if head[:4] == b"ttcf":
            n = struct.unpack_from(">I", head, 8)[0]
            if n > 1024:
                raise FontError("not a font collection")
            offsets = struct.unpack(">%dI" % n, at(12, 4 * n))
        else:
            offsets = (0,)
        names = []
        for o in offsets:
            n = struct.unpack(">H", at(o + 4, 2))[0]
            directory = at(o + 12, 16 * n)
            for i in range(n):
                tag, _, off, length = struct.unpack_from(">4sIII", directory, 16 * i)
                if tag == b"name":
                    # full_name() reads a font laid out from offset 0: give it
                    # a one-table directory pointing at the table just read.
                    blob = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
                    blob += struct.pack(">4sIII", b"name", 0, 28, length)
                    names.append(full_name(blob + at(off, length)))
                    break
        return names


def find(name, dirs=None):
    """The first .ttf or .ttc under dirs with a face whose full name is
    name, or None."""
    for d in font_dirs() if dirs is None else dirs:
        for root, subdirs, files in os.walk(d):
            subdirs.sort()
            for fn in sorted(files):
                if not fn.lower().endswith((".ttf", ".ttc")):
                    continue
                path = os.path.join(root, fn)
                try:
                    names = _file_names(path)
                except (OSError, FontError, struct.error):
                    continue
                if any(_same(n, name) for n in names):
                    return path
    return None
