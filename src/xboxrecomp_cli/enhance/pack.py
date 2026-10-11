"""The texture pack file (toolkit docs/formats/texture-pack.md, "XRTP") and
the manifest written beside it for people.

  header  "XRTP", u32 version 1, u32 count, u32 scale, u64 index offset,
          u64 reserved
  index   count x 32 bytes sorted by hash: u64 hash, u32 w, u32 h,
          u32 levels, u32 flags, u64 payload offset
  payload per entry, one u32 A8R8G8B8 texel per pixel, little-endian:
          the bytes B, G, R, A in memory, as the runtime uploads them
"""

import hashlib
import json
import struct

MAGIC = b"XRTP"
VERSION = 1
HEADER = struct.Struct("<4sIIIQQ")
ENTRY = struct.Struct("<QIIIIQ")


class PackEntry:
    """`texels` is A8R8G8B8 little-endian bytes for every level, level 0
    first; `name` and `source` are for the manifest only."""

    __slots__ = ("hash", "width", "height", "levels", "texels", "name", "source", "guest")

    def __init__(self, hash_, width, height, texels, levels=1, name="", source="", guest=(0, 0)):
        self.hash = hash_
        self.width = width
        self.height = height
        self.levels = levels
        self.texels = texels
        self.name = name
        self.source = source
        self.guest = guest


def rgba_to_argb(rgba):
    """RGBA bytes to the pack's texel bytes (B, G, R, A per pixel)."""
    out = bytearray(len(rgba))
    out[0::4] = rgba[2::4]
    out[1::4] = rgba[1::4]
    out[2::4] = rgba[0::4]
    out[3::4] = rgba[3::4]
    return bytes(out)


def write(path, entries, scale):
    """Write the pack; returns its sha256. Entries are sorted by hash here,
    and two entries with one hash are an error (the index must be unique)."""
    entries = sorted(entries, key=lambda e: e.hash)
    for a, b in zip(entries, entries[1:]):
        if a.hash == b.hash:
            raise ValueError("two entries with the hash %016X (%s, %s)" % (a.hash, a.name, b.name))
    index_off = HEADER.size
    payload_off = index_off + len(entries) * ENTRY.size
    h = hashlib.sha256()
    with open(path, "wb") as f:
        head = HEADER.pack(MAGIC, VERSION, len(entries), scale, index_off, 0)
        f.write(head)
        h.update(head)
        off = payload_off
        for e in entries:
            rec = ENTRY.pack(e.hash, e.width, e.height, e.levels, 0, off)
            f.write(rec)
            h.update(rec)
            off += len(e.texels)
        for e in entries:
            f.write(e.texels)
            h.update(e.texels)
    return h.hexdigest()


def read_header(path):
    """(count, scale) of a pack, or a ValueError naming what is wrong: the
    package step's check before it copies a pack into a bundle."""
    with open(path, "rb") as f:
        head = f.read(HEADER.size)
    if len(head) < HEADER.size:
        raise ValueError("too short for a texture pack")
    magic, version, count, scale, index_off, _ = HEADER.unpack(head)
    if magic != MAGIC:
        raise ValueError("not a texture pack (bad magic)")
    if version != VERSION:
        raise ValueError("texture pack version %d, this CLI writes %d" % (version, VERSION))
    return count, scale


def manifest_text(entries, scale, kernel, tool, xbe_sha256, font=None):
    """hud-pack.toml: what the pack holds, for the owner (the runtime never
    reads it). Names archive entries and hashes; no texels."""
    lines = [
        "# Texture pack manifest, written by xbr enhance hud. The pack is made",
        "# from your own game files: keep it private.",
        "schema = 1",
        'tool = "%s"' % tool,
        "scale = %d" % scale,
        'kernel = "%s"' % kernel,
        'xbe_sha256 = "%s"' % xbe_sha256,
    ]
    if font:
        lines.append('font = "%s"' % font["name"])
        if font.get("face"):
            lines.append('font_face = "%s"' % font["face"].replace('"', "'"))
        lines.append('font_sha256 = "%s"' % font["sha256"])
    lines.append("entries = %d" % len(entries))
    lines.append("")
    for e in sorted(entries, key=lambda e: e.hash):
        lines.append("[[entry]]")
        lines.append('hash = "%016X"' % e.hash)
        # The name holds bytes from the dump (MIX names): an escaped JSON
        # string is a valid TOML basic string.
        lines.append("name = %s" % json.dumps(e.name.replace("\\", "/"), ensure_ascii=True))
        lines.append('source = "%s"' % e.source)
        lines.append("guest = [%d, %d]" % e.guest)
        lines.append("size = [%d, %d]" % (e.width, e.height))
        lines.append("levels = %d" % e.levels)
        lines.append('texels_sha256 = "%s"' % hashlib.sha256(e.texels).hexdigest())
        lines.append("")
    return "\n".join(lines)
