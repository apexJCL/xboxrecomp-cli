"""An XBE loaded at its virtual addresses: the Ninja data is VA-addressed,
so every pointer in it is read through the section table."""

import struct

from .. import xbe


class ImageError(ValueError):
    pass


class Image:
    def __init__(self, data):
        self.data = data
        self.sections = xbe.sections(data[: xbe.header_size(data)])
        self.base = xbe.read_header(data)["base"]

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls(f.read())

    def section(self, name):
        for s in self.sections:
            if s.name == name:
                return s
        raise ImageError("no section %r in the XBE" % name)

    def section_of(self, va):
        for s in self.sections:
            if s.va <= va < s.va + s.vsize:
                return s
        return None

    def contains(self, va, n=1):
        s = self.section_of(va)
        return s is not None and va + n <= s.va + s.vsize

    def read(self, va, n):
        """n bytes at va; bytes past a section's raw data (its BSS tail)
        read as zero, as the loader leaves them."""
        s = self.section_of(va)
        if s is None or va + n > s.va + s.vsize:
            raise ImageError("0x%08X+%d is outside every section" % (va, n))
        off = va - s.va
        raw = self.data[s.raw_off + off : s.raw_off + min(off + n, s.raw_size)]
        return raw + bytes(n - len(raw))

    def unpack(self, fmt, va):
        return struct.unpack(fmt, self.read(va, struct.calcsize(fmt)))
