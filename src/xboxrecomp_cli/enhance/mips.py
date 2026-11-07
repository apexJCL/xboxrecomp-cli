"""The mip chain of an upscaled texture (D4): the base is the upscaler's, the
levels below it are made here, by a 2x2 box in integers.

The filter is alpha-weighted: a transparent texel's colour says nothing, and
averaged straight it bleeds into the edge of a cutout as it shrinks. So colour
is averaged weighted by alpha (the premultiplied average, then unpremultiplied
with rounding) and alpha is averaged plain. An odd side halves by floor, and
each destination texel takes the box of its two or four sources, the last row
or column of an odd side being dropped, as the runtime's level sizes demand.
"""

from . import classes


def down2(w, h, rgba):
    """(w2, h2, rgba) of the next level; a 1x1 stays as it is."""
    nw, nh = max(1, w // 2), max(1, h // 2)
    sx = 2 if w > 1 else 1
    sy = 2 if h > 1 else 1
    n = sx * sy
    half = n // 2
    opaque = n * 255
    taps = [(dy * w + dx) * 4 for dy in range(sy) for dx in range(sx)]
    out = bytearray(nw * nh * 4)
    o = 0
    for y in range(nh):
        row = y * sy * w * 4
        for x in range(nw):
            base = row + x * sx * 4
            at = [base + t for t in taps]
            weights = [rgba[i + 3] for i in at]
            total = sum(weights)
            if total == opaque or total == 0:
                # Plain: nothing to weigh, and over all-transparent texels the
                # colour stays the area's, for a bilinear neighbour to pull in.
                for ch in range(3):
                    out[o + ch] = (sum(rgba[i + ch] for i in at) + half) // n
            else:
                for ch in range(3):
                    out[o + ch] = (
                        sum(rgba[i + ch] * a for i, a in zip(at, weights)) + total // 2
                    ) // total
            out[o + 3] = (total + half) // n
            o += 4
    return nw, nh, bytes(out)


def chain(w, h, rgba, mips, source_levels=1):
    """[(w, h, rgba)] level 0 first: `full` to 1x1, `keep` the source's level
    count, `none` the base alone."""
    n = classes.chain_levels(w, h, mips, source_levels)
    out = [(w, h, rgba)]
    for _ in range(n - 1):
        w, h, rgba = down2(w, h, rgba)
        out.append((w, h, rgba))
    return out
