"""An integer xBR kernel (Hyllian's edge-directed pixel-art scaler, the
family xBRZ and ScaleFX grew from) for 2x and 4x, with alpha.

For every pixel E and each of its four corners, the kernel decides from
the 5x5 neighbourhood whether an edge runs across that corner (the
weighted colour distance across the corner's diagonal is smaller than
along it), picks the neighbour colour the edge belongs to, and fills the
part of E's output block that lies beyond the edge line with a blend of
E and that colour, weighted by how much of each output pixel lies beyond
the line. Level 1 is the 45-degree line through the midpoints of the two
cell edges; level 2 recognises a 2:1 slope on either side and uses that
line instead. Everything is integer: the colour distance is the usual
YUV-weighted one scaled to integers (alpha counts as a fourth channel,
weighted like luma, so a glyph's edge against transparency is an edge),
the decisions compare integer sums, and the pixel weights are exact
rationals computed once per scale (the areas the lines cut from each
output pixel), applied with integer rounding. Two runs give the same
bytes on any machine; results do not depend on how textures are split
across processes, since a texture is one unit of work.

Images are (w, h, rgba bytes). `scale(w, h, rgba, s)` returns the
upscaled rgba bytes, s in (2, 4).
"""

from fractions import Fraction

KERNEL = "xbr-lv2-int"

# Distance weights (Y, U, V of the reference shader; alpha like luma) on
# channels scaled by 1000, and the equality threshold (about six luma
# levels: DXT blocks encode one flat colour slightly differently from one
# block to the next, and that is not an edge).
WY, WU, WV, WA = 48, 7, 6, 48
EQ = 300_000

# Offsets of the neighbourhood, bottom-right orientation (x right, y down).
_OFF = {
    "B": (0, -1),
    "C": (1, -1),
    "D": (-1, 0),
    "E": (0, 0),
    "F": (1, 0),
    "G": (-1, 1),
    "H": (0, 1),
    "I": (1, 1),
    "F4": (2, 0),
    "I4": (2, 1),
    "H5": (0, 2),
    "I5": (1, 2),
}
# Lines, in the unit square of E, beyond which the corner colour wins:
# a*x + b*y > c. lv1: through (1, .5) and (.5, 1). left: through (0, 1)
# and (1, .5). up: through (1, 0) and (.5, 1).
_LINES = {"lv1": (2, 2, 3), "left": (1, 2, 2), "up": (2, 1, 2)}


def _rot(off, k):
    """Rotate an offset 90 degrees clockwise k times: BR -> BL -> TL -> TR."""
    x, y = off
    for _ in range(k):
        x, y = -y, x
    return x, y


def _clip_area(x0, y0, x1, y1, a, b, c):
    """Area of the rectangle where a*x + b*y > c (exact)."""
    poly = [
        (Fraction(x0), Fraction(y0)),
        (Fraction(x1), Fraction(y0)),
        (Fraction(x1), Fraction(y1)),
        (Fraction(x0), Fraction(y1)),
    ]
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp = a * p[0] + b * p[1] - c
        fq = a * q[0] + b * q[1] - c
        if fp > 0:
            out.append(p)
        if (fp > 0) != (fq > 0) and fp != fq:
            t = fp / (fp - fq)
            out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
    if len(out) < 3:
        return Fraction(0)
    area = Fraction(0)
    for i in range(len(out)):
        p, q = out[i], out[(i + 1) % len(out)]
        area += p[0] * q[1] - q[0] * p[1]
    return abs(area) / 2


_TABLES = {}


def _tables(s):
    """Per line kind and corner: [(px, py, num, den)] for the output pixels
    of E's s x s block that the line cuts into, num/den the cut fraction."""
    if s in _TABLES:
        return _TABLES[s]
    tabs = {}
    for kind, (a, b, c) in _LINES.items():
        base = []
        for py in range(s):
            for px in range(s):
                area = _clip_area(
                    Fraction(px, s),
                    Fraction(py, s),
                    Fraction(px + 1, s),
                    Fraction(py + 1, s),
                    a,
                    b,
                    c,
                )
                frac = area * s * s
                if frac > 0:
                    base.append((px, py, frac))
        for k in range(4):
            rows = []
            for px, py, frac in base:
                x, y = px, py
                for _ in range(k):
                    x, y = s - 1 - y, x
                rows.append((x, y, frac.numerator, frac.denominator))
            tabs[kind, k] = rows
    _TABLES[s] = tabs
    return tabs


def scale(w, h, rgba, s):
    if s not in (2, 4):
        raise ValueError("scale must be 2 or 4")
    pw, ph = w + 4, h + 4
    n = pw * ph
    ys = [0] * n
    us = [0] * n
    vs = [0] * n
    als = [0] * n
    # Edge-replicated padding of two pixels, so every neighbour exists.
    for py in range(ph):
        sy = min(max(py - 2, 0), h - 1)
        for px in range(pw):
            sx = min(max(px - 2, 0), w - 1)
            o = (sy * w + sx) * 4
            r, g, b, a = rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]
            i = py * pw + px
            ys[i] = 299 * r + 587 * g + 114 * b
            us[i] = -169 * r - 331 * g + 500 * b
            vs[i] = 500 * r - 419 * g - 81 * b
            als[i] = a * 1000

    def df(i, j):
        return (
            abs(ys[i] - ys[j]) * WY
            + abs(us[i] - us[j]) * WU
            + abs(vs[i] - vs[j]) * WV
            + abs(als[i] - als[j]) * WA
        )

    tabs = _tables(s)
    # Index deltas of the neighbourhood per corner, in the padded image.
    deltas = []
    for k in range(4):
        d = {}
        for name, off in _OFF.items():
            x, y = _rot(off, k)
            d[name] = y * pw + x
        deltas.append(d)
    ow = w * s
    out = bytearray(ow * h * s * 4)
    for y in range(h):
        for x in range(w):
            e = (y + 2) * pw + (x + 2)
            o = (y * s * ow + x * s) * 4
            er, eg, eb, ea = rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4]
            # The block starts as E everywhere.
            px4 = bytes((er, eg, eb, ea))
            for by in range(s):
                row = o + by * ow * 4
                out[row : row + s * 4] = px4 * s
            for k in range(4):
                d = deltas[k]
                E = e
                B, C, D, F = e + d["B"], e + d["C"], e + d["D"], e + d["F"]
                G, H, II = e + d["G"], e + d["H"], e + d["I"]
                F4, I4, H5, I5 = e + d["F4"], e + d["I4"], e + d["H5"], e + d["I5"]
                if df(E, F) < EQ and df(E, H) < EQ:
                    continue
                # The lv2 interpolation restriction: not across a corner
                # that two equal neighbours already make flat.
                fb, fc = df(F, B) < EQ, df(F, C) < EQ
                hd, hg = df(H, D) < EQ, df(H, G) < EQ
                ei = df(E, II) < EQ
                if not (
                    (not fb and not fc)
                    or (not hd and not hg)
                    or (
                        ei
                        and (
                            (df(F, F4) >= EQ and df(F, I4) >= EQ)
                            or (df(H, H5) >= EQ and df(H, I5) >= EQ)
                        )
                    )
                    or df(E, G) < EQ
                    or df(E, C) < EQ
                ):
                    continue
                edge = df(E, C) + df(E, G) + df(II, H5) + df(II, F4) + 4 * df(H, F)
                inner = df(H, D) + df(H, I5) + df(F, I4) + df(F, B) + 4 * df(E, II)
                if edge >= inner:
                    continue
                left = 2 * df(F, G) <= df(H, C) and df(E, G) >= EQ and df(D, G) >= EQ
                up = 2 * df(H, C) <= df(F, G) and df(E, C) >= EQ and df(B, C) >= EQ
                kind = "left" if left and not up else "up" if up and not left else "lv1"
                src = F if df(E, F) <= df(E, H) else H
                # src is a padded index: back to the image's rgba.
                sy, sx = divmod(src, pw)
                sy = min(max(sy - 2, 0), h - 1)
                sx = min(max(sx - 2, 0), w - 1)
                so = (sy * w + sx) * 4
                nr, ng, nb, na = rgba[so], rgba[so + 1], rgba[so + 2], rgba[so + 3]
                for px, py, num, den in tabs[kind, k]:
                    po = o + (py * ow + px) * 4
                    cr, cg, cb, ca = out[po], out[po + 1], out[po + 2], out[po + 3]
                    inv = den - num
                    out[po] = (cr * inv + nr * num + den // 2) // den
                    out[po + 1] = (cg * inv + ng * num + den // 2) // den
                    out[po + 2] = (cb * inv + nb * num + den // 2) // den
                    out[po + 3] = (ca * inv + na * num + den // 2) // den
    return bytes(out)


def scale_nearest(w, h, rgba, s):
    """Nearest-neighbour s x, for cells the fitted font leaves alone and
    for tests."""
    ow = w * s
    out = bytearray(ow * h * s * 4)
    for y in range(h):
        row = bytearray()
        for x in range(w):
            row += rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4] * s
        for k in range(s):
            o = ((y * s + k) * ow) * 4
            out[o : o + ow * 4] = row
    return bytes(out)
