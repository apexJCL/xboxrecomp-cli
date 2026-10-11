"""MIXF containers and the XPR0 packed resources inside them, read-only.

Some games keep on-screen text as pre-rendered DXT images in `.mix`
files rather than as glyphs from a font atlas (e.g. BLiNX 2: the mission
objectives, their title, story captions). The game uploads each texture's data block
as it is, so its hash is the hash of the DXT bytes from there, as for a
DDS (dds.py).

Format (read from BLiNX 2's lng_us/mis_mix and media/mix, 2026-10-11),
little-endian:

  MIXF   "MIXF", u32 header size (0x20), u32 name table size (count x 32),
         u32 data offset, 3 x u32 zero, u32 count, then count names of
         32 bytes (NUL-padded), then at the data offset one XPR0
  XPR0   "XPR0", u32 total size, u32 header size, then 20-byte texture
         headers (u32 common, u32 data offset from the end of the header,
         u32 lock, u32 format, u32 size) up to a 0xFFFFFFFF terminator;
         the texels start at the XPR0 plus its header size
  format bits 0-1 the DMA context, 2 cube map, 3 border source, 4-7
         the dimensionality (2), 8-15 the colour format (0x0C DXT1, 0x0E
         DXT3, 0x0F DXT5), 16-19 the mip levels, 20-23 and 24-27 log2
         width and height; a non-zero size word means a linear texture

The textures are named after the MIX's name table, in order. The bytes
come from a dump, so names that are empty, not printable ASCII or hold a
path separator are replaced by their index, and a repeated name gets one.
"""

import struct

from . import dds

MIX_MAGIC = b"MIXF"
XPR_MAGIC = b"XPR0"
END = 0xFFFFFFFF
TYPE_TEXTURE = 4
FORMATS = {0x0C: b"DXT1", 0x0E: b"DXT3", 0x0F: b"DXT5"}


class XprError(Exception):
    pass


def is_mix(data):
    return data[:4] == MIX_MAGIC


def _names(raw):
    """The name table's strings, made safe to print and to use in a path."""
    out = []
    seen = set()
    for i, b in enumerate(raw):
        name = b.split(b"\0", 1)[0].decode("latin-1")
        if not name or not all(" " <= c <= "~" for c in name) or "/" in name or "\\" in name:
            name = "#%d" % i
        elif name in seen:
            name = "%s#%d" % (name, i)
        seen.add(name)
        out.append(name)
    return out


def parse_mix(data):
    """[(name, Dds or None, note)] of the textures in a MIXF image. A
    texture that can't be used comes back as None with the reason; a MIX
    with no texture as one ("", None, note)."""
    try:
        return _parse_mix(data)
    except struct.error as e:
        # A backstop: the bounds checks should leave no short read.
        raise XprError("MIXF truncated (%s)" % e) from e


def _parse_mix(data):
    if len(data) < 0x20 or not is_mix(data):
        raise XprError("not a MIXF file")
    hsize, nsize, doff = struct.unpack_from("<III", data, 4)
    count = struct.unpack_from("<I", data, 0x1C)[0]
    if hsize < 0x20:
        raise XprError("MIXF header size 0x%x is under 0x20" % hsize)
    if hsize + count * 32 > len(data) or nsize < count * 32:
        raise XprError("MIXF name table past the end")
    names = _names([data[hsize + 32 * i : hsize + 32 * (i + 1)] for i in range(count)])
    texs = parse_xpr(data, doff)
    if not texs:
        return [("", None, "skipped: no textures")]
    out = []
    for i, t in enumerate(texs):
        name = names[i] if i < len(names) else "#%d" % i
        out.append((name,) + t)
    return out


def parse_xpr(data, base):
    """[(Dds or None, note)] of the textures in the XPR0 at `base`."""
    if data[base : base + 4] != XPR_MAGIC:
        raise XprError("no XPR0 at 0x%x" % base)
    if base + 12 > len(data):
        raise XprError("XPR0 header at 0x%x truncated" % base)
    total, hsize = struct.unpack_from("<II", data, base + 4)
    if hsize < 12 or base + hsize > len(data):
        raise XprError("XPR0 header size 0x%x past the end" % hsize)
    if total < hsize or total > len(data) - base:
        raise XprError("XPR0 size 0x%x past the end" % total)
    texels = base + hsize
    out = []
    p = base + 12
    while p + 4 <= texels:
        common = struct.unpack_from("<I", data, p)[0]
        if common == END:
            break
        if (common >> 16) & 7 != TYPE_TEXTURE or p + 20 > texels:
            # Other resource kinds have other header sizes: stop rather
            # than misread the rest.
            out.append((None, "skipped: XPR resource type %d" % ((common >> 16) & 7)))
            break
        off, _lock, fmt, size = struct.unpack_from("<IIII", data, p + 4)
        p += 20
        if (fmt & 0xF4) != 0x20 or size:
            # Only 2D swizzled textures: a cube map, a volume or a linear
            # (size-word) layout would be misread as one.
            out.append((None, "skipped: not a 2D swizzled texture (format 0x%08X)" % fmt))
            continue
        color = (fmt >> 8) & 0xFF
        levels = max(1, (fmt >> 16) & 0xF)
        w, h = 1 << ((fmt >> 20) & 0xF), 1 << ((fmt >> 24) & 0xF)
        fourcc = FORMATS.get(color)
        if fourcc is None:
            out.append((None, "skipped: not DXT (format 0x%02X)" % color))
            continue
        n = dds.extent(fourcc, w, h, levels)
        if texels + off + n > len(data):
            out.append((None, "skipped: %s data truncated" % fourcc.decode()))
            continue
        out.append((dds.Dds(w, h, levels, fourcc, data[texels + off : texels + off + n]), ""))
    return out
