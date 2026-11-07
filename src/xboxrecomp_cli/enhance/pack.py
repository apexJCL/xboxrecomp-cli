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

Version 3 (write's version=3) adds the kind `world`, FLAG_BC1, chains of any
of the three encodings (level 0 first, each level halving, floor, minimum 1;
a level under 4x4 is still whole blocks) and turns the reserved u32 into
`classes`, one bit per kind present, which the runtime only logs. `xbr enhance
hud` keeps writing version 2, its bytes unchanged.
"""

import hashlib
import json
import os
import struct

MAGIC = b"XRTP"
VERSION = 2
VERSION_CHAINS = 3
VERSIONS = (1, 2, 3)
HEADER = struct.Struct("<4sIIIQQ")
ENTRY = struct.Struct("<QIIIIQ")

KIND_ART, KIND_FONT, KIND_TEXT, KIND_WORLD = 0, 1, 2, 3
KINDS = ("art", "font", "text")
ALL_KINDS = KINDS + ("world",)
KIND_MASK = 0x3
FLAG_BC3 = 0x10
FLAG_BC1 = 0x20
MAX_SIDE = 16384
MAX_COUNT = 1 << 20


class PackEntry:
    """`texels` is A8R8G8B8 little-endian bytes for every level, level 0
    first, or BC3 or BC1 blocks when `bc3` or `bc1`; `name` and `source` are
    for the manifest only. An entry Spill.add wrote has texels None and
    its payload's place in the spill file instead."""

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
        "bc1",
        "spilled",
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
        bc1=False,
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
        self.bc1 = bc1
        self.spilled = None  # (offset, length, sha256) in the spill file

    @property
    def flags(self):
        return self.kind | (FLAG_BC3 if self.bc3 else 0) | (FLAG_BC1 if self.bc1 else 0)

    @property
    def nbytes(self):
        return self.spilled[1] if self.spilled else len(self.texels)

    @property
    def texels_sha256(self):
        return self.spilled[2] if self.spilled else hashlib.sha256(self.texels).hexdigest()


class Spill:
    """The payloads of a pack being made, appended in the order they are
    made, so the entries can drop their texels; write(spill=) copies them
    out in hash order."""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "wb")
        self._off = 0

    def add(self, entry, texels):
        entry.spilled = (self._off, len(texels), hashlib.sha256(texels).hexdigest())
        entry.texels = None
        self._f.write(texels)
        self._off += len(texels)

    def close(self):
        if not self._f.closed:
            self._f.close()

    def discard(self):
        self.close()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def rgba_to_argb(rgba):
    """RGBA bytes to the pack's texel bytes (B, G, R, A per pixel)."""
    out = bytearray(len(rgba))
    out[0::4] = rgba[2::4]
    out[1::4] = rgba[1::4]
    out[2::4] = rgba[0::4]
    out[3::4] = rgba[3::4]
    return bytes(out)


def _check_entry(e, version):
    """What the runtime's reader refuses, refused here with the entry's name."""
    if e.bc3 and e.bc1:
        raise ValueError("entry %s has two block formats" % e.name)
    block = e.bc3 or e.bc1
    # The runtime refuses partial blocks: D3D11 creates BC textures in whole
    # blocks only.
    if block and (e.width % 4 or e.height % 4):
        raise ValueError(
            "%s entry %s is %dx%d x%d"
            % ("BC3" if e.bc3 else "BC1", e.name, e.width, e.height, e.levels)
        )
    if version < VERSION_CHAINS:
        if e.bc1 or e.kind == KIND_WORLD or (e.bc3 and e.levels != 1):
            raise ValueError(
                "entry %s needs texture pack version %d (BC1, world or a BC chain)"
                % (e.name, VERSION_CHAINS)
            )


def write(path, entries, scale, text_scale=0, spill=None, version=VERSION):
    """Write the pack; returns its sha256. Entries are sorted by hash here,
    and two entries with one hash are an error (the index must be unique).
    `text_scale` is what text entries were upscaled by, 0 when it is
    `scale`; the runtime only logs it. spill: the closed Spill file the
    entries' payloads are in (each entry's `spilled`), copied in hash
    order in COPY_CHUNK pieces, then removed; the bytes are the same as
    from texels held in memory. version: 2, or 3 for what `build` makes."""
    entries = sorted(entries, key=lambda e: e.hash)
    for a, b in zip(entries, entries[1:]):
        if a.hash == b.hash:
            raise ValueError("two entries with the hash %016X (%s, %s)" % (a.hash, a.name, b.name))
    for e in entries:
        _check_entry(e, version)
    index_off = HEADER.size
    payload_off = index_off + len(entries) * ENTRY.size
    h = hashlib.sha256()
    # A new file renamed over the old one, never the old one rewritten:
    # package's pack store may hold a hard link to it (a kept pack's bytes).
    tmp = path + ".tmp"
    try:
        word = text_scale
        if version >= VERSION_CHAINS:
            word |= _classes(entries) << 32
        _write_body(tmp, entries, scale, word, spill, h, index_off, payload_off, version)
        os.replace(tmp, path)
    except BaseException:
        # Disk full, a short spill file or Ctrl-C: a KUF-sized pack.tmp is
        # gigabytes, so it never outlives the failed write.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if spill is not None:
        os.remove(spill)
    return h.hexdigest()


def _classes(entries):
    bits = 0
    for e in entries:
        bits |= 1 << e.kind
    return bits


def _write_body(tmp, entries, scale, word, spill, h, index_off, payload_off, version):
    with open(tmp, "wb") as f:
        head = HEADER.pack(MAGIC, version, len(entries), scale, index_off, word)
        f.write(head)
        h.update(head)
        off = payload_off
        for e in entries:
            rec = ENTRY.pack(e.hash, e.width, e.height, e.levels, e.flags, off)
            f.write(rec)
            h.update(rec)
            off += e.nbytes
        if spill is None:
            for e in entries:
                f.write(e.texels)
                h.update(e.texels)
        else:
            with open(spill, "rb") as src:
                for e in entries:
                    _copy_range(src, e.spilled[0], e.spilled[1], f, h, e.name)


COPY_CHUNK = 1 << 20


def _copy_range(src, off, n, dst, h, name):
    src.seek(off)
    while n:
        b = src.read(min(n, COPY_CHUNK))
        if not b:
            raise ValueError("spill file ends inside %s's payload" % name)
        dst.write(b)
        h.update(b)
        n -= len(b)


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
        raise ValueError("texture pack version %d, this CLI reads 1 to 3" % version)
    return count, scale


def level_sizes(w, h, levels):
    out = []
    for _ in range(levels):
        out.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return out


def payload_bytes(w, h, levels, flags=0):
    """Bytes of an entry's chain: whole blocks at every level, however small."""
    n = 0
    for lw, lh in level_sizes(w, h, levels):
        blocks = ((lw + 3) // 4) * ((lh + 3) // 4)
        n += blocks * 16 if flags & FLAG_BC3 else blocks * 8 if flags & FLAG_BC1 else lw * lh * 4
    return n


def read_index(path):
    """(version, scale, text scale, classes, rows) by the runtime's reader's
    rules (nv2a_tex_pack.c), or a ValueError naming the first it breaks. A row
    is (hash, w, h, levels, flags, payload offset). The build checks what it
    wrote with this before saying it is done."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEADER.size)
        if len(head) < HEADER.size:
            raise ValueError("too short for a texture pack")
        magic, version, count, scale, index_off, word = HEADER.unpack(head)
        if magic != MAGIC or version not in VERSIONS:
            raise ValueError("bad magic or version %d" % version)
        text_scale, classes = word & 0xFFFFFFFF, word >> 32
        if version == 2 and classes:
            raise ValueError("version 2 with a reserved word set")
        if count > MAX_COUNT or index_off < HEADER.size:
            raise ValueError("count %d or index offset %d out of range" % (count, index_off))
        if index_off + count * ENTRY.size > size:
            raise ValueError("the index runs past the end")
        f.seek(index_off)
        raw = f.read(count * ENTRY.size)
    rows = []
    prev = None
    kinds = 4 if version >= VERSION_CHAINS else 3
    known = KIND_MASK | FLAG_BC3 | (FLAG_BC1 if version >= VERSION_CHAINS else 0)
    for i in range(count):
        h, w, ht, levels, flags, off = ENTRY.unpack_from(raw, i * ENTRY.size)
        where = "entry %016X" % h
        if prev is not None and h <= prev:
            raise ValueError("%s: the index is not strictly ascending" % where)
        prev = h
        if not (1 <= w <= MAX_SIDE and 1 <= ht <= MAX_SIDE):
            raise ValueError("%s: %dx%d" % (where, w, ht))
        if not 1 <= levels <= max(w, ht).bit_length():
            raise ValueError("%s: %d levels of %dx%d" % (where, levels, w, ht))
        if flags & ~known or (flags & KIND_MASK) >= kinds:
            raise ValueError("%s: flags 0x%x" % (where, flags))
        if flags & FLAG_BC3 and flags & FLAG_BC1:
            raise ValueError("%s: two block formats" % where)
        if flags & (FLAG_BC3 | FLAG_BC1) and (w % 4 or ht % 4):
            raise ValueError("%s: block-compressed, not a multiple of 4" % where)
        if off < HEADER.size or off + payload_bytes(w, ht, levels, flags) > size:
            raise ValueError("%s: its payload runs past the end" % where)
        rows.append((h, w, ht, levels, flags, off))
    return version, scale, text_scale, classes, rows


def kind_stats(entries):
    """{kind name: (entries, bc3 entries, payload bytes)} for every kind a
    version 2 pack holds, and `world` when an entry has it."""
    stats = {k: [0, 0, 0] for k in KINDS}
    for e in entries:
        s = stats.setdefault(ALL_KINDS[e.kind], [0, 0, 0])
        s[0] += 1
        s[1] += 1 if e.bc3 else 0
        s[2] += e.nbytes
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
    # Each kind a v2 pack holds, then `world` when an entry has it.
    for k, (n, bc3, size) in stats.items():
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
        lines.append('kind = "%s"' % ALL_KINDS[e.kind])
        if e.bc3:
            lines.append("bc3 = true")
        lines.append('texels_sha256 = "%s"' % e.texels_sha256)
        lines.append("")
    return "\n".join(lines)
