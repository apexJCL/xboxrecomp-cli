"""DDS files with DXT1, DXT3 or DXT5 texels, decoded to RGBA.

The 128-byte header is Microsoft's (DDS_HEADER: size 124, flags, height,
width, pitch or linear size, depth, mip count, DDS_PIXELFORMAT with the
FourCC). The data block follows the header and is what the game uploads
to the console as it is, so a DXT texture's hash is the hash of exactly
`extent(fourcc, w, h, levels)` bytes from there.

Images are (width, height, bytes) with 4 bytes RGBA per pixel, row-major.
"""

import struct

MAGIC = b"DDS "
HEADER = 128
DDSD_MIPMAPCOUNT = 0x20000
DDPF_FOURCC = 0x4
BLOCK_BYTES = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16}


class DdsError(Exception):
    pass


class Dds:
    __slots__ = ("width", "height", "levels", "fourcc", "data")

    def __init__(self, width, height, levels, fourcc, data):
        self.width = width
        self.height = height
        self.levels = levels
        self.fourcc = fourcc
        self.data = data

    @property
    def dxt(self):
        return self.fourcc in BLOCK_BYTES


def extent(fourcc, w, h, levels):
    """Bytes of `levels` DXT levels, each halving (floor, minimum 1)."""
    bb = BLOCK_BYTES[fourcc]
    n = 0
    for _ in range(levels):
        n += ((w + 3) // 4) * ((h + 3) // 4) * bb
        w = max(1, w // 2)
        h = max(1, h // 2)
    return n


def parse(data):
    """A Dds whose `data` is the level bytes the hash covers (a DXT file),
    or the raw block after the header for other formats."""
    if len(data) < HEADER or data[:4] != MAGIC:
        raise DdsError("not a DDS file")
    size, flags, height, width = struct.unpack_from("<IIII", data, 4)
    if size != 124:
        raise DdsError("bad DDS header size %d" % size)
    mips = struct.unpack_from("<I", data, 28)[0]
    pf_flags, fourcc = struct.unpack_from("<I4s", data, 80)
    levels = mips if (flags & DDSD_MIPMAPCOUNT) and mips else 1
    if not width or not height:
        raise DdsError("empty texture")
    if (pf_flags & DDPF_FOURCC) and fourcc in BLOCK_BYTES:
        n = extent(fourcc, width, height, levels)
        if len(data) < HEADER + n:
            raise DdsError(
                "%s data truncated: %d of %d bytes" % (fourcc.decode(), len(data) - HEADER, n)
            )
        return Dds(width, height, levels, fourcc, data[HEADER : HEADER + n])
    return Dds(width, height, levels, fourcc if pf_flags & DDPF_FOURCC else b"", data[HEADER:])


def _rgb565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _colors(c0, c1, opaque_only):
    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    cols = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
    if c0 > c1 or opaque_only:
        cols.append(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255))
        cols.append(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255))
    else:
        cols.append(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255))
        cols.append((0, 0, 0, 0))
    return cols


def decode_level(fourcc, w, h, block):
    """One DXT level to RGBA bytes."""
    bb = BLOCK_BYTES[fourcc]
    bw, bh = (w + 3) // 4, (h + 3) // 4
    out = bytearray(w * h * 4)
    off = 0
    for by in range(bh):
        for bx in range(bw):
            if fourcc == b"DXT1":
                c0, c1, idx = struct.unpack_from("<HHI", block, off)
                cols = _colors(c0, c1, False)
                alphas = None
            elif fourcc == b"DXT3":
                a = struct.unpack_from("<Q", block, off)[0]
                c0, c1, idx = struct.unpack_from("<HHI", block, off + 8)
                cols = _colors(c0, c1, True)
                alphas = [((a >> (4 * k)) & 0xF) * 17 for k in range(16)]
            else:
                a0, a1 = block[off], block[off + 1]
                bits = int.from_bytes(block[off + 2 : off + 8], "little")
                c0, c1, idx = struct.unpack_from("<HHI", block, off + 8)
                cols = _colors(c0, c1, True)
                if a0 > a1:
                    table = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
                else:
                    table = (
                        [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255]
                    )
                alphas = [table[(bits >> (3 * k)) & 7] for k in range(16)]
            off += bb
            for k in range(16):
                px, py = bx * 4 + (k & 3), by * 4 + (k >> 2)
                if px >= w or py >= h:
                    continue
                r, g, b, a = cols[(idx >> (2 * k)) & 3]
                if alphas is not None:
                    a = alphas[k]
                o = (py * w + px) * 4
                out[o] = r
                out[o + 1] = g
                out[o + 2] = b
                out[o + 3] = a
    return bytes(out)


def decode(dds):
    """Level 0 of a DXT Dds as (w, h, rgba)."""
    if not dds.dxt:
        raise DdsError("not a DXT texture")
    return dds.width, dds.height, decode_level(dds.fourcc, dds.width, dds.height, dds.data)
