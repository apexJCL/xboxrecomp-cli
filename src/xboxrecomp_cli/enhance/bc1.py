"""A deterministic integer BC1 (DXT1) encoder for a texture pack's opaque
entries: half of BC3's bytes, for the textures that have no alpha to keep.

The colour fit is bc3.py's, so two hosts write one pack. A block here is always
the four-colour form, c0 > c1: the other order is DXT1's punch-through mode,
whose index 3 decodes transparent, and an entry goes to BC1 because it is
opaque. So a flat block, where the fit gives c0 == c1, is written with c0 one
above c1 and the index that decodes the wanted colour exactly.
"""

from .bc3 import _color_block

BLOCK_BYTES = 8


def encode_block(px):
    """8 bytes of BC1 for 16 (r, g, b, a) texels in row order. Every texel is
    fitted: alpha plays no part, unlike BC3's colour half."""
    block = _color_block([(p[0], p[1], p[2], 255) for p in px])
    c0 = int.from_bytes(block[:2], "little")
    c1 = int.from_bytes(block[2:4], "little")
    if c0 != c1:
        return block
    if c0:
        return c0.to_bytes(2, "little") + (c0 - 1).to_bytes(2, "little") + bytes(4)
    # Black: index 1 is c1, which keeps its value when c0 has to rise.
    return b"\x01\x00\x00\x00\x55\x55\x55\x55"


def size(w, h):
    """Bytes of one BC1 level of w x h."""
    return ((w + 3) // 4) * ((h + 3) // 4) * BLOCK_BYTES


def encode(w, h, rgba):
    """BC1 blocks of a w x h RGBA image (one level), row of blocks by row.
    Edge blocks of a size not a multiple of 4 repeat the last row and column."""
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
