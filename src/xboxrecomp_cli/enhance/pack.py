"""The texture pack file (toolkit docs/formats/texture-pack.md, "XRTP") and
the manifest written beside it for people.

  header  "XRTP", u32 version 2, u32 count, u32 scale, u64 index offset,
          u32 text scale (0: the header scale), u32 reserved 0
  index   count x 32 bytes sorted by hash: u64 hash, u32 w, u32 h,
          u32 levels, u32 flags, u64 payload offset
  payload per entry, one u32 A8R8G8B8 texel per pixel, little-endian:
          the bytes B, G, R, A in memory, as the runtime uploads them; or,
          with FLAG_BC3, BC3 blocks of one level, rows of blocks top down

The flags carry the entry's kind in bits 0-1 (art, font, text), which the
runtime switches off per kind, and FLAG_BC3. Version 1 had no flags and no
text scale; read_header still accepts it.
"""

import hashlib
import json
import os
import struct

MAGIC = b"XRTP"
VERSION = 2
VERSIONS = (1, 2)
HEADER = struct.Struct("<4sIIIQQ")
ENTRY = struct.Struct("<QIIIIQ")

KIND_ART, KIND_FONT, KIND_TEXT = 0, 1, 2
KINDS = ("art", "font", "text")
FLAG_BC3 = 0x10


class PackEntry:
    """`texels` is A8R8G8B8 little-endian bytes for every level, level 0
    first, or BC3 blocks of one level when `bc3`; `name` and `source` are
    for the manifest only."""

    __slots__ = (
        "hash",
        "width",
        "height",
        "levels",
        "texels",
        "name",
        "source",
        "guest",
        "kind",
        "bc3",
    )

    def __init__(
        self,
        hash_,
        width,
        height,
        texels,
        levels=1,
        name="",
        source="",
        guest=(0, 0),
        kind=KIND_ART,
        bc3=False,
    ):
        self.hash = hash_
        self.width = width
        self.height = height
        self.levels = levels
        self.texels = texels
        self.name = name
        self.source = source
        self.guest = guest
        self.kind = kind
        self.bc3 = bc3

    @property
    def flags(self):
        return self.kind | (FLAG_BC3 if self.bc3 else 0)


def rgba_to_argb(rgba):
    """RGBA bytes to the pack's texel bytes (B, G, R, A per pixel)."""
    out = bytearray(len(rgba))
    out[0::4] = rgba[2::4]
    out[1::4] = rgba[1::4]
    out[2::4] = rgba[0::4]
    out[3::4] = rgba[3::4]
    return bytes(out)


def write(path, entries, scale, text_scale=0):
    """Write the pack; returns its sha256. Entries are sorted by hash here,
    and two entries with one hash are an error (the index must be unique).
    `text_scale` is what text entries were upscaled by, 0 when it is
    `scale`; the runtime only logs it."""
    entries = sorted(entries, key=lambda e: e.hash)
    for a, b in zip(entries, entries[1:]):
        if a.hash == b.hash:
            raise ValueError("two entries with the hash %016X (%s, %s)" % (a.hash, a.name, b.name))
    for e in entries:
        # The runtime refuses a BC3 entry of partial blocks: D3D11 creates
        # BC textures in whole blocks only.
        if e.bc3 and (e.width % 4 or e.height % 4 or e.levels != 1):
            raise ValueError("BC3 entry %s is %dx%d x%d" % (e.name, e.width, e.height, e.levels))
    index_off = HEADER.size
    payload_off = index_off + len(entries) * ENTRY.size
    h = hashlib.sha256()
    # A new file renamed over the old one, never the old one rewritten:
    # package's pack store may hold a hard link to it (a kept pack's bytes).
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        head = HEADER.pack(MAGIC, VERSION, len(entries), scale, index_off, text_scale)
        f.write(head)
        h.update(head)
        off = payload_off
        for e in entries:
            rec = ENTRY.pack(e.hash, e.width, e.height, e.levels, e.flags, off)
            f.write(rec)
            h.update(rec)
            off += len(e.texels)
        for e in entries:
            f.write(e.texels)
            h.update(e.texels)
    os.replace(tmp, path)
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
    if version not in VERSIONS:
        raise ValueError("texture pack version %d, this CLI reads 1 and 2" % version)
    return count, scale


def kind_stats(entries):
    """{kind name: (entries, bc3 entries, payload bytes)} for every kind."""
    stats = {k: [0, 0, 0] for k in KINDS}
    for e in entries:
        s = stats[KINDS[e.kind]]
        s[0] += 1
        s[1] += 1 if e.bc3 else 0
        s[2] += len(e.texels)
    return {k: tuple(v) for k, v in stats.items()}


def manifest_text(
    entries,
    scale,
    kernel,
    tool,
    xbe_sha256,
    font=None,
    text_scale=0,
    languages=(),
    made=None,
):
    """hud-pack.toml: what the pack holds, for the owner (the runtime never
    reads it). Names archive entries and hashes; no texels. made: the
    pack's sha256 and the settings package records as the pack's
    made_with, to make the same pack again ({pack_sha256, only, config})."""
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
    lines.append("languages = [%s]" % ", ".join('"%s"' % x for x in languages))
    lines.append("entries = %d" % len(entries))
    if made:
        lines.append('pack_sha256 = "%s"' % made["pack_sha256"])
        lines.append("only = [%s]" % ", ".join(json.dumps(x) for x in made.get("only", ())))
        lines.append('config = "%s"' % made["config"])
    lines.append("")
    stats = kind_stats(entries)
    for k in KINDS:
        n, bc3, size = stats[k]
        lines.append("[kinds.%s]" % k)
        lines.append("entries = %d" % n)
        lines.append("scale = %d" % (text_scale or scale if k == "text" else scale))
        lines.append("bc3 = %d" % bc3)
        lines.append("bytes = %d" % size)
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
        lines.append('kind = "%s"' % KINDS[e.kind])
        if e.bc3:
            lines.append("bc3 = true")
        lines.append('texels_sha256 = "%s"' % hashlib.sha256(e.texels).hexdigest())
        lines.append("")
    return "\n".join(lines)
