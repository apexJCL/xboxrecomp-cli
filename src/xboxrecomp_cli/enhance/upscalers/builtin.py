"""The built-in upscalers: integer, reproducible across hosts, nothing to
install. `xbr` is scaler.py. `epx` is Eric's Pixel Expansion as Scale2x and
Scale3x (4x is 2x twice), `lanczos` a three-lobe filter through a fixed-point
kernel, `nearest` and `box` the plain ones. Exact equality and fixed-point sums
only, so two hosts write one pack.

The lanczos kernel table comes from exact rationals: a libm's sin may differ in
the last bit between hosts, and a weight on either side of a rounding boundary
would change a pack.
"""

import array
from fractions import Fraction
from math import gcd
from operator import mul

from .. import scaler
from .base import Param, Upscaler

LOBES = 3
FRACTION_BITS = 14
ONE = 1 << FRACTION_BITS
ROUND2 = 1 << (2 * FRACTION_BITS - 1)
# Enough for the overshoot of two passes of a three-lobe kernel.
CLAMP_OFFSET = 256
CLAMP = bytes(0 if v < 0 else 255 if v > 255 else v for v in range(-CLAMP_OFFSET, 768))
# A rational pi and enough series terms for 14 bits, whatever the argument
# reached: |x| <= LOBES, so terms past 40 are far under one unit.
PI = Fraction(314159265358979323846, 100000000000000000000)
SERIES_TERMS = 40


def _sinc(x):
    """sin(pi x) / (pi x) as an exact rational of the truncated series."""
    if x == 0:
        return Fraction(1)
    t = PI * x
    term = t
    total = t
    k = 1
    for _ in range(SERIES_TERMS):
        term = -term * t * t / ((k + 1) * (k + 2))
        # Keep the denominators bounded: 64 fractional bits outlast the table's 14.
        term = Fraction(int(term * (1 << 64)), 1 << 64)
        total += term
        k += 2
    return total / t


def _lanczos(x):
    if abs(x) >= LOBES:
        return Fraction(0)
    return _sinc(x) * _sinc(x / LOBES)


_KERNELS = {}


def kernel(src, dst):
    """[(first source index, [fixed-point weights])] per destination index of
    a resample from src to dst texels. Weights sum to ONE exactly: the rounding's
    residue goes on the largest. A shrink widens the filter by its ratio."""
    g = gcd(src, dst)
    key = (src // g, dst // g)
    phases = _KERNELS.get(key)
    sn, dn = key
    if phases is None:
        phases = []
        widen = max(Fraction(1), Fraction(sn, dn))
        for i in range(dn):
            # The destination texel's centre in source texels.
            centre = Fraction((2 * i + 1) * sn, 2 * dn) - Fraction(1, 2)
            reach = LOBES * widen
            first = int((centre - reach).__ceil__())
            last = int((centre + reach).__floor__())
            raw = [_lanczos((j - centre) / widen) for j in range(first, last + 1)]
            total = sum(raw)
            weights = [round(r / total * ONE) for r in raw]
            weights[weights.index(max(weights))] += ONE - sum(weights)
            # Trim the zero weights at the ends (a whole ratio's exact zeros).
            while weights and weights[0] == 0:
                weights.pop(0)
                first += 1
            while weights and weights[-1] == 0:
                weights.pop()
            phases.append((first, weights))
        _KERNELS[key] = phases
    out = []
    for i in range(dst):
        block, ph = divmod(i, dn)
        first, weights = phases[ph]
        out.append((first + block * sn, weights))
    return out


def _filter_line(line, table, margin):
    """One row or column: `line` is a list extended by `margin` copies of each
    end, so every tap exists."""
    return [
        sum(map(mul, weights, line[first + margin : first + margin + len(weights)]))
        for first, weights in table
    ]


def _extended(line, margin):
    line = list(line)
    return [line[0]] * margin + line + [line[-1]] * margin


def _resample_plane(plane, w, h, nw, nh, tab_x, tab_y, margin_x, margin_y):
    rows = [
        _filter_line(_extended(plane[y * w : (y + 1) * w], margin_x), tab_x, margin_x)
        for y in range(h)
    ]
    cols = [
        _filter_line(_extended(col, margin_y), tab_y, margin_y) for col in zip(*rows, strict=True)
    ]
    shift = 2 * FRACTION_BITS
    clamp = CLAMP
    off = CLAMP_OFFSET
    return bytes(clamp[((v + ROUND2) >> shift) + off] for row in zip(*cols) for v in row)


def _margin(table, n):
    lo = max(0, -min(f for f, _ in table))
    hi = max(0, max(f + len(ws) for f, ws in table) - n)
    return max(lo, hi)


def resample(w, h, rgba, nw, nh):
    """rgba at nw x nh through the fixed-point kernel. With alpha, the filter
    runs over premultiplied colour, so a transparent texel's colour counts for
    nothing; a texel left fully transparent takes colour 0."""
    tab_x, tab_y = kernel(w, nw), kernel(h, nh)
    mx, my = _margin(tab_x, w), _margin(tab_y, h)
    a = rgba[3::4]
    out = bytearray(nw * nh * 4)
    if a.count(255) == len(a):
        for c in range(3):
            out[c::4] = _resample_plane(rgba[c::4], w, h, nw, nh, tab_x, tab_y, mx, my)
        out[3::4] = b"\xff" * (nw * nh)
        return bytes(out)
    na = _resample_plane(a, w, h, nw, nh, tab_x, tab_y, mx, my)
    for c in range(3):
        pre = bytes((v * al + 127) // 255 for v, al in zip(rgba[c::4], a))
        p = _resample_plane(pre, w, h, nw, nh, tab_x, tab_y, mx, my)
        out[c::4] = bytes(min(255, (v * 255 + al // 2) // al) if al else 0 for v, al in zip(p, na))
    out[3::4] = na
    return bytes(out)


def _epx2(w, h, px):
    """Scale2x over 32-bit texels; the neighbours past an edge are the edge."""
    ow = w * 2
    out = [0] * (ow * h * 2)
    for y in range(h):
        row = y * w
        up = row - w if y else row
        dn = row + w if y < h - 1 else row
        o0 = y * 2 * ow
        o1 = o0 + ow
        for x in range(w):
            e = px[row + x]
            b, h_ = px[up + x], px[dn + x]
            d = px[row + x - 1] if x else e
            f = px[row + x + 1] if x < w - 1 else e
            if b != h_ and d != f:
                out[o0 + 2 * x] = d if d == b else e
                out[o0 + 2 * x + 1] = f if b == f else e
                out[o1 + 2 * x] = d if d == h_ else e
                out[o1 + 2 * x + 1] = f if h_ == f else e
            else:
                out[o0 + 2 * x] = out[o0 + 2 * x + 1] = e
                out[o1 + 2 * x] = out[o1 + 2 * x + 1] = e
    return out


def _epx3(w, h, px):
    """Scale3x over 32-bit texels."""
    ow = w * 3
    out = [0] * (ow * h * 3)
    for y in range(h):
        row = y * w
        up = row - w if y else row
        dn = row + w if y < h - 1 else row
        for x in range(w):
            xl = x - 1 if x else x
            xr = x + 1 if x < w - 1 else x
            a, b, c = px[up + xl], px[up + x], px[up + xr]
            d, e, f = px[row + xl], px[row + x], px[row + xr]
            g, h_, i = px[dn + xl], px[dn + x], px[dn + xr]
            o = (y * 3 * ow) + x * 3
            if b != h_ and d != f:
                e0 = d if d == b else e
                e1 = b if (d == b and e != c) or (b == f and e != a) else e
                e2 = f if b == f else e
                e3 = d if (d == b and e != g) or (d == h_ and e != a) else e
                e5 = f if (b == f and e != i) or (h_ == f and e != c) else e
                e6 = d if d == h_ else e
                e7 = h_ if (d == h_ and e != i) or (h_ == f and e != g) else e
                e8 = f if h_ == f else e
            else:
                e0 = e1 = e2 = e3 = e5 = e6 = e7 = e8 = e
            out[o : o + 3] = (e0, e1, e2)
            out[o + ow : o + ow + 3] = (e3, e, e5)
            out[o + 2 * ow : o + 2 * ow + 3] = (e6, e7, e8)
    return out


def _as_ints(rgba):
    return memoryview(rgba).cast("I").tolist()


def _as_bytes(px):
    # array keeps the host's order, the cast's own: the texels' bytes return as they were.

    return array.array("I", px).tobytes()


def epx(w, h, rgba, s):
    px = _as_ints(rgba)
    if s == 2:
        return _as_bytes(_epx2(w, h, px))
    if s == 3:
        return _as_bytes(_epx3(w, h, px))
    return _as_bytes(_epx2(w * 2, h * 2, _epx2(w, h, px)))


class Builtin(Upscaler):
    kind = "builtin"
    alpha = "rgba"
    deterministic = True
    batch = 1
    reproducible_scope = "any"
    licence = "part of this CLI"

    def identity(self):
        from ... import __version__

        return {"backend": "builtin", "name": self.name, "cli": __version__}


class Nearest(Builtin):
    name = "nearest"
    scales = (2, 3, 4)

    def upscale_one(self, w, h, rgba, scale, params):
        return scaler.scale_nearest(w, h, rgba, scale)


class Box(Builtin):
    """Each source texel's box at a whole scale, the same texels as `nearest`.
    A name of its own so a rule says what it means; the mips use this filter."""

    name = "box"
    scales = (2, 3, 4)

    def upscale_one(self, w, h, rgba, scale, params):
        return scaler.scale_nearest(w, h, rgba, scale)


class Xbr(Builtin):
    name = "xbr"
    scales = (2, 3, 4)
    params = (
        Param("level", "int", 2, 1, 2, "1: the 45-degree line alone; 2: also the 2:1 slopes"),
        Param(
            "eq", "int", scaler.EQ, 0, 10_000_000, "colour distance under which two texels match"
        ),
        Param(
            "weights",
            "ints",
            [scaler.WY, scaler.WU, scaler.WV, scaler.WA],
            0,
            1000,
            "distance weights of Y, U, V and alpha",
        ),
        Param("alpha_edge", "bool", True, None, None, "alpha counts as an edge (weights' fourth)"),
    )

    def upscale_one(self, w, h, rgba, scale, params):
        weights = tuple(params.get("weights") or (scaler.WY, scaler.WU, scaler.WV, scaler.WA))
        if not params.get("alpha_edge", True):
            weights = (*weights[:3], 0)
        return scaler.scale(
            w, h, rgba, scale, params.get("level", 2), params.get("eq", scaler.EQ), weights
        )


class Epx(Builtin):
    name = "epx"
    scales = (2, 3, 4)

    def upscale_one(self, w, h, rgba, scale, params):
        return epx(w, h, rgba, scale)


class Lanczos(Builtin):
    name = "lanczos"
    scales = (2, 3, 4)

    def upscale_one(self, w, h, rgba, scale, params):
        return resample(w, h, rgba, w * scale, h * scale)


ALL = (Nearest(), Box(), Xbr(), Epx(), Lanczos())
