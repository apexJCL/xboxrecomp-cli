"""The `dump` source: a directory the runtime wrote under
RECOMP_DEBUG=tex_dump=<dir> (the toolkit's docs/formats/texture-dump.md).

  <hash>.json    the sidecar: size, format, layout, wrap, filters, ...
  <hash>.xtd     a 64-byte header, then the decoded A8R8G8B8 levels (B, G,
                 R, A bytes), level 0 first; linear textures have none
  counts.json    {hash: uploads}; may lag the sidecars, so a hash it does
                 not name was still uploaded once
  volatile.json  [{offset, width, height, hashes}]: addresses whose hash
                 kept changing

The file name's hash is the key the runtime's pack looks up, so this source
alone gives a full inventory for a game nobody wrote a reader for. A run cut
short leaves files half written: those go in `problems`, a texture whose .xtd
is bad counts as having no texels, and the rest of the dump stays usable.
"""

import json
import os
import re
import struct

from .base import Entry, Source, SourceError

XTD_HEADER = struct.Struct("<4sIQ8IQQ")
XTD_MAGIC = b"XRTD"
XTD_VERSION = 1
HASH_NAME = re.compile(r"^[0-9A-Fa-f]{16}$")
FLAG_SWIZZLED, FLAG_LINEAR, FLAG_DXT, FLAG_PALETTE = 1, 2, 4, 8


def hash_text(h):
    """The form every file and inventory row spells a hash in."""
    return "%016X" % h


class Xtd:
    """A parsed .xtd: `levels_rgba()` gives each level's texels."""

    __slots__ = (
        "hash",
        "width",
        "height",
        "levels",
        "format_id",
        "pitch",
        "flags",
        "first_flip",
        "data",
    )

    def __init__(self, data):
        if len(data) < XTD_HEADER.size:
            raise SourceError("too short for a texture dump header")
        magic, version, h, w, ht, levels, fmt, pitch, flags, flip, off, count, _ = (
            XTD_HEADER.unpack_from(data, 0)
        )
        if magic != XTD_MAGIC:
            raise SourceError("not a texture dump file")
        if version != XTD_VERSION:
            raise SourceError("texture dump version %d, this reads %d" % (version, XTD_VERSION))
        if not w or not ht or not levels:
            raise SourceError("empty texture")
        if off < XTD_HEADER.size or off + count * 4 != len(data):
            raise SourceError("%d texels at %d do not fill its %d bytes" % (count, off, len(data)))
        self.hash = hash_text(h)
        self.width = w
        self.height = ht
        self.levels = levels
        self.format_id = fmt
        self.pitch = pitch
        self.flags = flags
        self.first_flip = flip
        self.data = data[off:]

    def levels_bgra(self):
        """[(width, height, bytes)] level 0 first, B, G, R, A per texel."""
        out = []
        w, h, off = self.width, self.height, 0
        for _ in range(self.levels):
            n = w * h * 4
            if off + n > len(self.data):
                raise SourceError("level %dx%d past the end of the texels" % (w, h))
            out.append((w, h, self.data[off : off + n]))
            off += n
            w, h = max(1, w // 2), max(1, h // 2)
        return out

    def alpha_used(self):
        """Whether level 0 holds a texel that is not fully opaque."""
        n = self.width * self.height * 4
        a = self.data[3:n:4]
        return a.count(255) != len(a)


class Texture:
    """One sidecar. `meta` is the sidecar's own dict; the keys a reader may
    rely on are checked here, the rest (the magnification, say) pass through."""

    __slots__ = ("hash", "meta", "path", "uses")

    def __init__(self, hash_, meta, path, uses):
        self.hash = hash_
        self.meta = meta
        self.path = path  # the .xtd, or None for a sidecar with no texels
        self.uses = uses

    def read(self):
        """The parsed .xtd; SourceError when there is none or it is bad."""
        if not self.path:
            raise SourceError("%s has no dumped texels" % self.hash)
        try:
            with open(self.path, "rb") as f:
                x = Xtd(f.read())
        except (OSError, SourceError) as e:
            raise SourceError("%s: %s" % (self.path, e)) from e
        if x.hash != self.hash:
            raise SourceError("%s: its header says %s" % (self.path, x.hash))
        return x


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


class DumpSource(Source):
    """`textures` maps each hash to its Texture; `volatile` is the set of
    hashes volatile.json marks, `volatile_addresses` its row count."""

    def __init__(self, directory, name=None):
        self.directory = directory
        self.name = name or os.path.basename(os.path.normpath(directory))
        self.label = directory
        self.problems = []
        self.textures = {}
        self.volatile = set()
        self.volatile_addresses = 0
        if not os.path.isdir(directory):
            raise SourceError("%s: no such folder" % directory)
        counts = self._counts()
        self._volatile()
        for f in sorted(os.listdir(directory)):
            stem, ext = os.path.splitext(f)
            if ext != ".json" or not HASH_NAME.match(stem):
                continue
            h = stem.upper()
            path = os.path.join(directory, f)
            try:
                meta = _load_json(path)
            except (OSError, ValueError) as e:
                self.problems.append("%s: %s" % (f, e))
                continue
            why = self._bad(meta, h)
            if why:
                self.problems.append("%s: %s" % (f, why))
                continue
            xtd = os.path.join(directory, stem + ".xtd")
            if not os.path.isfile(xtd):
                if meta.get("texels"):
                    self.problems.append("%s: says it has texels, no %s.xtd" % (f, stem))
                xtd = None
            tex = Texture(h, meta, xtd, counts.get(h, 1))
            if xtd:
                try:
                    tex.read().levels_bgra()
                except SourceError as e:
                    self.problems.append(str(e))
                    tex.path = None
            self.textures[h] = tex
        self.entries = [
            Entry(h + ".xtd", 0, os.path.getsize(t.path))
            for h, t in sorted(self.textures.items())
            if t.path
        ]

    @staticmethod
    def _bad(meta, h):
        if not isinstance(meta, dict):
            return "not an object"
        if str(meta.get("hash", "")).upper() != h:
            return "its hash is %r" % meta.get("hash")
        for k in ("width", "height"):
            if not _whole(meta.get(k)):
                return "%s is not a whole number above 0" % k
        return ""

    def _counts(self):
        path = os.path.join(self.directory, "counts.json")
        if not os.path.exists(path):
            return {}
        try:
            doc = _load_json(path)
        except (OSError, ValueError) as e:
            self.problems.append("counts.json: %s" % e)
            return {}
        if not isinstance(doc, dict):
            self.problems.append("counts.json: not an object")
            return {}
        return {str(k).upper(): v for k, v in doc.items() if _whole(v)}

    def _volatile(self):
        path = os.path.join(self.directory, "volatile.json")
        if not os.path.exists(path):
            return
        try:
            doc = _load_json(path)
        except (OSError, ValueError) as e:
            self.problems.append("volatile.json: %s" % e)
            return
        if not isinstance(doc, list):
            self.problems.append("volatile.json: not a list")
            return
        for row in doc:
            hashes = row.get("hashes") if isinstance(row, dict) else None
            if isinstance(hashes, list):
                self.volatile_addresses += 1
                self.volatile.update(str(h).upper() for h in hashes)

    def read(self, entry):
        t = self.textures[entry.name[:16].upper()]
        try:
            with open(t.path, "rb") as f:
                return f.read()
        except OSError as e:
            raise SourceError("%s: %s" % (t.path, e)) from e
