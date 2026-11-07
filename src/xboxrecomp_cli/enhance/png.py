"""PNG read and write, stdlib only, for the files an owner edits and the
upscaling tools take and give back. An image is (width, height, rgba): RGBA
bytes, 4 per texel, rows top down, the alpha straight as the file holds it.

read takes every colour type and depth PNG has. Colour and gamma chunks are
ignored: the numbers in the file are the texels. 16-bit samples keep their
high byte, and a tRNS colour key or palette alpha becomes the alpha. Interlaced
files are refused with a message rather than decoded: no editor writes one
unless asked to.

write stores unfiltered rows through zlib: quick in pure Python, and the file
is bigger than an editor's. Its bytes depend on the zlib of this host; its
pixels never do.
"""

import contextlib
import os
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# What a texture pack's entry can reach, and over it a file is not a texture.
MAX_TEXELS = 16384 * 16384
COMPRESS_LEVEL = 6

GRAY, RGB, PALETTE, GRAY_ALPHA, RGBA = 0, 2, 3, 4, 6
CHANNELS = {GRAY: 1, RGB: 3, PALETTE: 1, GRAY_ALPHA: 2, RGBA: 4}
DEPTHS = {
    GRAY: (1, 2, 4, 8, 16),
    RGB: (8, 16),
    PALETTE: (1, 2, 4, 8),
    GRAY_ALPHA: (8, 16),
    RGBA: (8, 16),
}


class PngError(Exception):
    pass


def _ihdr(data):
    """(width, height, depth, colour type) from a file's first chunk."""
    if data[:8] != SIGNATURE:
        raise PngError("not a PNG file")
    if len(data) < 33 or data[12:16] != b"IHDR" or data[8:12] != b"\0\0\0\r":
        raise PngError("no PNG header")
    w, h, depth, colour, method, filt, lace = struct.unpack(">IIBBBBB", data[16:29])
    if not w or not h:
        raise PngError("an empty image")
    if w * h > MAX_TEXELS:
        raise PngError("%dx%d is over %d texels" % (w, h, MAX_TEXELS))
    if colour not in DEPTHS or depth not in DEPTHS[colour]:
        raise PngError("colour type %d at depth %d is not PNG" % (colour, depth))
    if method or filt:
        raise PngError("unknown compression or filter method")
    if lace:
        raise PngError("interlaced: save it again without interlacing")
    return w, h, depth, colour


def size(path):
    """(width, height) of a PNG file, read from its header alone."""
    with open(path, "rb") as f:
        return _ihdr(f.read(33))[:2]


def header(data):
    """(width, height) from a PNG's first bytes: what a peer sent, checked
    against the size it should be before anything is decompressed."""
    return _ihdr(data[:33])[:2]


def _chunks(data):
    """The chunks after the signature, up to IEND, with their CRCs checked."""
    off = 8
    while True:
        if off + 12 > len(data):
            raise PngError("cut short: no end chunk")
        n, kind = struct.unpack(">I4s", data[off : off + 8])
        end = off + 8 + n
        if end + 4 > len(data):
            raise PngError("cut short inside a %s chunk" % kind.decode("latin-1"))
        body = data[off + 8 : end]
        if struct.unpack(">I", data[end : end + 4])[0] != zlib.crc32(kind + body):
            raise PngError("bad checksum in a %s chunk" % kind.decode("latin-1"))
        if kind == b"IEND":
            return
        yield kind, body
        off = end + 4


def _inflate(idat, want):
    """The scanlines; a stream past `want` is refused before it is held."""
    z = zlib.decompressobj()
    try:
        raw = z.decompress(b"".join(idat), want + 1)
    except zlib.error as e:
        raise PngError("bad image data: %s" % e) from e
    if len(raw) != want:
        raise PngError("%d bytes of scanlines where the size needs %d" % (len(raw), want))
    return raw


def _unfilter(raw, stride, rows, bpp):
    """The rows without their filter bytes, joined."""
    out = bytearray(stride * rows)
    prev = bytes(stride)
    off = 0
    for y in range(rows):
        kind = raw[off]
        line = raw[off + 1 : off + 1 + stride]
        off += 1 + stride
        if kind == 1:
            cur = bytearray(line)
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif kind == 2:
            cur = bytes([(a + b) & 255 for a, b in zip(line, prev)])
        elif kind == 3:
            cur = bytearray(line)
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:
            cur = bytearray(line)
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - c - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                cur[i] = (cur[i] + pred) & 255
        elif kind == 0:
            cur = line
        else:
            raise PngError("unknown filter %d on row %d" % (kind, y))
        out[y * stride : (y + 1) * stride] = cur
        prev = cur
    return bytes(out)


def _samples_low(raw, stride, w, h, depth):
    """A row-padded 1, 2 or 4-bit image as one sample per byte, unscaled."""
    mask = (1 << depth) - 1
    per = 8 // depth
    # Each byte's samples, left to right: a table, since there are only 256.
    table = [bytes((v >> (8 - depth * (i + 1))) & mask for i in range(per)) for v in range(256)]
    out = bytearray()
    for y in range(h):
        row = raw[y * stride : (y + 1) * stride]
        out += b"".join([table[v] for v in row])[:w]
    return bytes(out)


def _palette_rgba(idx, plte, trns):
    if len(plte) % 3 or not plte:
        raise PngError("bad palette")
    n = len(plte) // 3
    if n > 256:
        raise PngError("a palette of %d colours, over PNG's 256" % n)
    if max(idx) >= n:
        raise PngError("a texel past the palette's %d colours" % n)
    pad = bytes(256 - n)
    out = bytearray(len(idx) * 4)
    for c in range(3):
        out[c::4] = idx.translate(plte[c::3] + pad)
    out[3::4] = idx.translate((trns + b"\xff" * 256)[:256])
    return bytes(out)


def _gray_rgba(v, alpha):
    out = bytearray(len(v) * 4)
    out[0::4] = out[1::4] = out[2::4] = v
    out[3::4] = alpha
    return bytes(out)


def _key16(trns, n):
    """The first n 16-bit samples of a tRNS colour key."""
    if len(trns) < 2 * n:
        raise PngError("bad transparency chunk")
    return struct.unpack(">%dH" % n, trns[: 2 * n])


def _to_rgba(raw, w, h, depth, colour, plte, trns):
    stride = (w * CHANNELS[colour] * depth + 7) // 8
    opaque = b"\xff" * (w * h)
    if colour == PALETTE:
        idx = raw if depth == 8 else _samples_low(raw, stride, w, h, depth)
        return _palette_rgba(idx, plte, trns)
    if depth < 8:
        v = _samples_low(raw, stride, w, h, depth)
        scale = 255 // ((1 << depth) - 1)
        alpha = opaque
        if trns:
            key = _key16(trns, 1)[0]
            alpha = bytes([0 if s == key else 255 for s in v])
        return _gray_rgba(bytes([s * scale for s in v]), alpha)
    step = depth // 8
    top = raw[0::step]  # the high byte of each sample
    if colour == GRAY:
        alpha = opaque
        if trns:
            key = _key16(trns, 1)[0]
            if step == 1:
                alpha = bytes([0 if s == key else 255 for s in top])
            else:
                got = struct.unpack(">%dH" % (w * h), raw)
                alpha = bytes([0 if s == key else 255 for s in got])
        return _gray_rgba(top, alpha)
    if colour == GRAY_ALPHA:
        return _gray_rgba(top[0::2], top[1::2])
    if colour == RGBA:
        return top
    out = bytearray(w * h * 4)
    out[0::4], out[1::4], out[2::4] = top[0::3], top[1::3], top[2::3]
    if not trns:
        out[3::4] = opaque
        return bytes(out)
    key = _key16(trns, 3)
    if step == 1:
        got = zip(top[0::3], top[1::3], top[2::3])
    else:
        vals = struct.unpack(">%dH" % (w * h * 3), raw)
        got = zip(vals[0::3], vals[1::3], vals[2::3])
    out[3::4] = bytes([0 if s == key else 255 for s in got])
    return bytes(out)


def decode(data):
    """(width, height, rgba) of a PNG file's bytes, or a PngError."""
    w, h, depth, colour = _ihdr(data)
    plte = trns = b""
    idat = []
    for kind, body in _chunks(data):
        if kind == b"IDAT":
            idat.append(body)
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind != b"IHDR" and not kind[0] & 0x20:
            raise PngError("unknown required chunk %s" % kind.decode("latin-1"))
    if not idat:
        raise PngError("no image data")
    if colour == PALETTE and not plte:
        raise PngError("no palette")
    bits = CHANNELS[colour] * depth
    stride = (w * bits + 7) // 8
    raw = _inflate(idat, (stride + 1) * h)
    raw = _unfilter(raw, stride, h, max(1, bits // 8))
    return w, h, _to_rgba(raw, w, h, depth, colour, plte, trns)


def read(path):
    """(width, height, rgba) of a PNG file. OSError or PngError."""
    with open(path, "rb") as f:
        return decode(f.read())


def _chunk(kind, body):
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def encode(w, h, rgba, alpha=True):
    """A PNG file's bytes. alpha False stores RGB and drops the alpha (for a
    tool that only takes RGB; a read gives it back as all 255)."""
    if not w or not h or len(rgba) != w * h * 4:
        raise PngError("%d bytes are not a %dx%d RGBA image" % (len(rgba), w, h))
    if w * h > MAX_TEXELS:
        raise PngError("%dx%d is over %d texels" % (w, h, MAX_TEXELS))
    if alpha:
        pixels, stride, colour = rgba, w * 4, RGBA
    else:
        rgb = bytearray(w * h * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = rgba[0::4], rgba[1::4], rgba[2::4]
        pixels, stride, colour = bytes(rgb), w * 3, RGB
    # Filter 0 on every row: b"\0" between the rows.
    rows = b"\0" + b"\0".join(pixels[y * stride : (y + 1) * stride] for y in range(h))
    head = struct.pack(">IIBBBBB", w, h, 8, colour, 0, 0, 0)
    return (
        SIGNATURE
        + _chunk(b"IHDR", head)
        + _chunk(b"IDAT", zlib.compress(rows, COMPRESS_LEVEL))
        + _chunk(b"IEND", b"")
    )


def write(path, w, h, rgba, alpha=True):
    """The file appears whole or not at all: a cut-short PNG under its final
    name would read as a file someone made."""
    data = encode(w, h, rgba, alpha)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def has_alpha(rgba):
    """Whether any texel is not fully opaque."""
    a = rgba[3::4]
    return a.count(255) != len(a)
