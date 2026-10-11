"""IPK1 archives: the `.iph` index and the `.ipk` data file, read-only.

Format (reversed in the BLiNX 2 HUD spike, 2026-10-06):

  .iph  "IPK1", u32 alignment, u32 count, u32 total .ipk size, then
        count entries of 80 bytes: char name[64], u32 flags (1 = LZSS),
        u32 stored size, u32 offset in the .ipk, u32 uncompressed size
  LZSS  Okumura's: a 4096-byte ring initialised to zero, the write
        pointer starting at 4096 - 18, 18-byte longest match, a flag byte
        per 8 items with a set bit meaning a literal, a pair as
        (low offset byte, high offset nibble | length - 3)

Entry names use the game's `\\` separators; `Entry.path` is the same name
with `/`, lower-cased, for the patterns in game.toml.
"""

import os
import struct

HEADER = struct.Struct("<4sIII")
ENTRY = struct.Struct("<64sIIII")
MAGIC = b"IPK1"
FLAG_LZSS = 1

RING = 4096
MATCH_MAX = 18
THRESHOLD = 2


class Entry:
    __slots__ = ("name", "flags", "stored", "offset", "size")

    def __init__(self, name, flags, stored, offset, size):
        self.name = name
        self.flags = flags
        self.stored = stored
        self.offset = offset
        self.size = size

    @property
    def path(self):
        return self.name.replace("\\", "/").lower()

    @property
    def compressed(self):
        return bool(self.flags & FLAG_LZSS)


class ArchiveError(Exception):
    pass


def read_index(data):
    """The entries of an .iph image."""
    if len(data) < HEADER.size:
        raise ArchiveError("too short for an IPK1 index")
    magic, align, count, total = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ArchiveError("not an IPK1 index")
    need = HEADER.size + count * ENTRY.size
    if len(data) < need:
        raise ArchiveError(
            "index truncated: %d entries need %d bytes, have %d" % (count, need, len(data))
        )
    out = []
    for i in range(count):
        name, flags, stored, offset, size = ENTRY.unpack_from(data, HEADER.size + i * ENTRY.size)
        out.append(Entry(name.split(b"\0", 1)[0].decode("latin-1"), flags, stored, offset, size))
    return out


def lzss_decode(src, out_size):
    """Okumura LZSS, exactly out_size bytes."""
    ring = bytearray(RING)
    r = RING - MATCH_MAX
    out = bytearray()
    n = len(src)
    i = 0
    flags = 0
    while len(out) < out_size:
        flags >>= 1
        if not flags & 0x100:
            if i >= n:
                break
            flags = src[i] | 0xFF00
            i += 1
        if flags & 1:
            if i >= n:
                break
            c = src[i]
            i += 1
            out.append(c)
            ring[r] = c
            r = (r + 1) & (RING - 1)
        else:
            if i + 1 >= n:
                break
            lo, hi = src[i], src[i + 1]
            i += 2
            pos = lo | ((hi & 0xF0) << 4)
            length = (hi & 0x0F) + THRESHOLD + 1
            for k in range(length):
                c = ring[(pos + k) & (RING - 1)]
                out.append(c)
                ring[r] = c
                r = (r + 1) & (RING - 1)
    if len(out) < out_size:
        raise ArchiveError("LZSS stream ended after %d of %d bytes" % (len(out), out_size))
    del out[out_size:]
    return bytes(out)


class Archive:
    """One archive: `<dir>/<name>.iph` + `<dir>/<name>.ipk`."""

    def __init__(self, directory, name):
        self.name = name
        self.iph = os.path.join(directory, name + ".iph")
        self.ipk = os.path.join(directory, name + ".ipk")
        with open(self.iph, "rb") as f:
            self.entries = read_index(f.read())

    def read(self, entry):
        with open(self.ipk, "rb") as f:
            f.seek(entry.offset)
            data = f.read(entry.stored)
        if len(data) != entry.stored:
            raise ArchiveError(
                "%s: %s: %d of %d bytes" % (self.ipk, entry.name, len(data), entry.stored)
            )
        if entry.compressed:
            return lzss_decode(data, entry.size)
        return data[: entry.size]
