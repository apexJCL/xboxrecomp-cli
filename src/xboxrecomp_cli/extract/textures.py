"""Stage textures: the texlist's names, the files they name in the dump,
and PNG output.

A texlist is (names*, count), each name a fixed-size NUL-padded field
([extract] texname_size). [extract] textures says where a name's DDS is:
a path pattern under the dump ("media/{name}.dds", matched without case)
or an IPK1 archive and an entry pattern ("ipk:media:dds/{name}.dds").
"""

import os
import struct
import zlib

from ..enhance import dds as dds_mod
from ..enhance import ipk
from ..enhance.sources import resolve_nocase

DDPF_ALPHAPIXELS = 0x1


class TextureError(ValueError):
    pass


def texlist(image, va, name_size):
    if not va:
        return []
    p, n = image.unpack("<II", va)
    if n > 0x4000:
        raise TextureError("texlist 0x%08X: %d names, not a texlist" % (va, n))
    return [
        image.read(p + name_size * i, name_size).split(b"\0", 1)[0].decode("latin-1")
        for i in range(n)
    ]


def _mask_channel(v, mask):
    if not mask:
        return None
    shift = (mask & -mask).bit_length() - 1
    top = mask >> shift
    return ((v & mask) >> shift) * 255 // top


def decode(data):
    """(width, height, RGBA bytes, format label) of a DDS file's top level:
    DXT1/3/5, or any uncompressed RGB(A) the pixel format's masks describe."""
    d = dds_mod.parse(data)
    if d.dxt:
        w, h, rgba = dds_mod.decode(d)
        return w, h, rgba, d.fourcc.decode()
    if d.fourcc:
        raise TextureError("unsupported DDS format %r" % d.fourcc.decode("latin-1"))
    pf_flags, _, bits, rm, gm, bm, am = struct.unpack_from("<II5I", data, 80)
    if bits not in (16, 32):
        raise TextureError("unsupported %d-bit DDS" % bits)
    if not pf_flags & DDPF_ALPHAPIXELS:
        am = 0
    w, h = d.width, d.height
    step = bits // 8
    raw = d.data
    if len(raw) < w * h * step:
        raise TextureError("DDS data truncated")
    fmt = "<%d%s" % (w * h, "I" if bits == 32 else "H")
    px = struct.unpack_from(fmt, raw, 0)
    out = bytearray(w * h * 4)
    for i, v in enumerate(px):
        a = _mask_channel(v, am)
        out[4 * i] = _mask_channel(v, rm) or 0
        out[4 * i + 1] = _mask_channel(v, gm) or 0
        out[4 * i + 2] = _mask_channel(v, bm) or 0
        out[4 * i + 3] = 255 if a is None else a
    label = "%s%d" % ("A" if am else "X", bits)
    return w, h, bytes(out), label


def png(w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4 : (y + 1) * w * 4] for y in range(h))

    def chunk(t, b):
        c = struct.pack(">I", len(b)) + t + b
        return c + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, 6)),
            chunk(b"IEND", b""),
        )
    )


# Alpha at or above this is an edge or compression artefact on opaque art,
# not glass: within 6% of opaque, and the M13 glass floor bottoms out at 119.
TRANSLUCENT_BELOW = 240


def alpha_kind(rgba):
    """'opaque', 'mask' (alpha only 0 or 255), 'translucent' (never 0,
    somewhere below TRANSLUCENT_BELOW), 'near-opaque' (never 0, never below
    it) or 'blend'."""
    a = set(rgba[3::4])
    if a <= {255}:
        return "opaque"
    if a <= {0, 255}:
        return "mask"
    if 0 not in a:
        return "translucent" if min(a) < TRANSLUCENT_BELOW else "near-opaque"
    return "blend"


def file_name(name):
    """True when a texlist name is a plain file name. Names come from the
    game's data and end up in paths, so a separator or a ".." is refused
    rather than followed."""
    return bool(name) and not any(c in name for c in "/\\\0:") and name not in (".", "..")


def parse_source(spec):
    """("loose", pattern) or ("ipk", archive, pattern) of [extract]
    textures; ValueError naming the problem."""
    if spec.startswith("ipk:"):
        parts = spec.split(":", 2)
        if len(parts) != 3 or not parts[1] or "{name}" not in parts[2]:
            raise ValueError('"ipk:<archive>:<entry pattern with {name}>"')
        return ("ipk", parts[1], parts[2].replace("\\", "/").lower())
    if "{name}" not in spec:
        raise ValueError("a path pattern with {name}, or ipk:<archive>:<pattern>")
    return ("loose", spec)


class Source:
    """Reads a texture's DDS bytes by texlist name; None when missing."""

    def __init__(self, game_files, spec):
        self.kind = parse_source(spec)
        self.game_files = game_files
        self.archive = None
        if self.kind[0] == "ipk":
            self.archive = ipk.Archive(game_files, self.kind[1])
            self.by_path = {e.path: e for e in self.archive.entries}

    def read(self, name):
        if not file_name(name):
            return None
        if self.kind[0] == "ipk":
            e = self.by_path.get(self.kind[2].format(name=name).lower())
            return self.archive.read(e) if e else None
        try:
            path = resolve_nocase(self.game_files, self.kind[1].format(name=name))
        except OSError:
            return None
        with open(path, "rb") as f:
            return f.read()

    def label(self, name):
        if self.kind[0] == "ipk":
            return "%s.ipk:%s" % (self.kind[1], self.kind[2].format(name=name))
        return os.path.join(self.game_files, self.kind[1].format(name=name))
