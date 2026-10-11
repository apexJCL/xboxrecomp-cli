"""A deterministic integer BC3 (DXT5) encoder for texture pack entries.

A pack entry with XRTP_FLAG_BC3 holds BC3 blocks instead of A8R8G8B8
texels: a quarter of the bytes on disk and in VRAM, and the runtime uploads
them as they are. The sources are DXT3/DXT5 already, so the loss is small,
and anti-aliased text survives it.

Integer arithmetic only, so two hosts write one pack. Per 4x4 block:

  alpha   the endpoints are the block's largest and smallest alpha (the
          eight-value mode, a0 > a1); each texel takes the nearest of the
          eight values, the lower index on a tie
  colour  a range fit: the endpoints are opposite corners of the colour
          box of the texels that are not fully transparent (all sixteen if
          every one is), the corners picked by the sign of each channel's
          covariance with the widest one, rounded to 5:6:5 and ordered so
          c0 > c1 (the four-colour mode); each texel takes the
          nearest of the four colours the decoder makes, the lower index on
          a tie

The palettes are computed as the toolkit's decoder (d3d8_dxt_decode_texel)
and dds.decode_level compute them, so the index choice minimises the error
of what is actually drawn. Edge blocks of an image whose size is not a
multiple of 4 repeat the last row and column.
"""

BLOCK_BYTES = 16


def _q5(v):
    return (v * 31 + 127) // 255


def _q6(v):
    return (v * 63 + 127) // 255


def _x5(v):
    return (v << 3) | (v >> 2)


def _x6(v):
    return (v << 2) | (v >> 4)


def _alpha_block(alphas):
    a0, a1 = max(alphas), min(alphas)
    if a0 == a1:
        return bytes((a0, a1, 0, 0, 0, 0, 0, 0))
    table = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    bits = 0
    for k, a in enumerate(alphas):
        best, best_d = 0, 256
        for i, t in enumerate(table):
            d = a - t if a >= t else t - a
            if d < best_d:
                best, best_d = i, d
        bits |= best << (3 * k)
    return bytes((a0, a1)) + bits.to_bytes(6, "little")


def _color_block(px):
    """px: 16 (r, g, b, a). Four-colour mode, which BC3 always uses."""
    solid = [p for p in px if p[3]] or px
    hi = [max(p[c] for p in solid) for c in range(3)]
    lo = [min(p[c] for p in solid) for c in range(3)]
    # The box's main diagonal fits colours that rise together; a channel
    # that falls as the others rise takes the other diagonal. The sign of
    # its covariance with the widest channel decides (sums times n, so it
    # stays integer).
    n = len(solid)
    sums = [sum(p[c] for p in solid) for c in range(3)]
    ref = max(range(3), key=lambda c: hi[c] - lo[c])
    for c in range(3):
        if c != ref:
            cov = sum((n * p[ref] - sums[ref]) * (n * p[c] - sums[c]) for p in solid)
            if cov < 0:
                hi[c], lo[c] = lo[c], hi[c]
    c0 = (_q5(hi[0]) << 11) | (_q6(hi[1]) << 5) | _q5(hi[2])
    c1 = (_q5(lo[0]) << 11) | (_q6(lo[1]) << 5) | _q5(lo[2])
    if c0 < c1:
        c0, c1 = c1, c0
    if c0 == c1:
        return c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + bytes(4)
    e0 = (_x5(c0 >> 11), _x6((c0 >> 5) & 0x3F), _x5(c0 & 0x1F))
    e1 = (_x5(c1 >> 11), _x6((c1 >> 5) & 0x3F), _x5(c1 & 0x1F))
    pal = (
        e0,
        e1,
        tuple((2 * e0[i] + e1[i]) // 3 for i in range(3)),
        tuple((e0[i] + 2 * e1[i]) // 3 for i in range(3)),
    )
    bits = 0
    for k, p in enumerate(px):
        best, best_d = 0, 1 << 30
        for i, c in enumerate(pal):
            dr, dg, db = p[0] - c[0], p[1] - c[1], p[2] - c[2]
            d = dr * dr + dg * dg + db * db
            if d < best_d:
                best, best_d = i, d
        bits |= best << (2 * k)
    return c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + bits.to_bytes(4, "little")


def encode_block(px):
    """16 bytes of BC3 for 16 (r, g, b, a) texels in row order."""
    return _alpha_block([p[3] for p in px]) + _color_block(px)


def size(w, h):
    """Bytes of one BC3 level of w x h."""
    return ((w + 3) // 4) * ((h + 3) // 4) * BLOCK_BYTES


def encode(w, h, rgba):
    """BC3 blocks of a w x h RGBA image (one level), row of blocks by row."""
    if len(rgba) != w * h * 4:
        raise ValueError("%dx%d needs %d RGBA bytes, got %d" % (w, h, w * h * 4, len(rgba)))
    out = bytearray()
    for by in range(0, h, 4):
        rows = [min(by + y, h - 1) for y in range(4)]
        for bx in range(0, w, 4):
            cols = [min(bx + x, w - 1) for x in range(4)]
            px = []
            for y in rows:
                base = y * w
                for x in cols:
                    o = (base + x) * 4
                    px.append((rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]))
            out += encode_block(px)
    return bytes(out)
