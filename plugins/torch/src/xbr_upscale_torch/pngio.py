"""8-bit RGB and RGBA PNG, stdlib only: what the CLI hands over and takes back.
A separate package has no claim on the CLI's reader, and needs no other depth."""

import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


class PngError(Exception):
    pass


def _chunks(data):
    if data[:8] != SIG:
        raise PngError("not a PNG")
    off = 8
    while off + 8 <= len(data):
        n, kind = struct.unpack_from(">I4s", data, off)
        yield kind, data[off + 8 : off + 8 + n]
        off += 12 + n


def _unfilter(raw, stride, bpp, h):
    out = bytearray(stride * h)
    prev = bytearray(stride)
    o = 0
    for y in range(h):
        f = raw[o]
        row = bytearray(raw[o + 1 : o + 1 + stride])
        o += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + (a + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                row[i] = (row[i] + pred) & 255
        elif f:
            raise PngError("filter %d" % f)
        out[y * stride : (y + 1) * stride] = row
        prev = row
    return bytes(out)


def decode(data):
    """(width, height, channels, bytes), top down."""
    idat = []
    w = h = channels = 0
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise PngError("only 8-bit RGB or RGBA, not interlaced")
            channels = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
    if not w:
        raise PngError("no header")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise PngError(str(e)) from e
    stride = w * channels
    if len(raw) != (stride + 1) * h:
        raise PngError("%d bytes for a %dx%d image" % (len(raw), w, h))
    return w, h, channels, _unfilter(raw, stride, channels, h)


def encode(w, h, channels, pixels):
    def chunk(kind, body):
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    stride = w * channels
    raw = b"".join(b"\0" + pixels[y * stride : (y + 1) * stride] for y in range(h))
    ctype = 2 if channels == 3 else 6
    return (
        SIG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
