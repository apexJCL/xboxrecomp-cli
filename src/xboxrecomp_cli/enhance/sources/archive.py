"""The `archive` source: IPK1 pairs under game_files (ipk.py)."""

import os

from .. import ipk
from .base import Source, SourceError

# An .ipk inside an .ipk: a game's archive may hold them two deep.
NEST_MAX = 2


class IpkSource(Source):
    """ipk.Archive with its errors as SourceError, the same text."""

    def __init__(self, game_files, name):
        self.name = name
        self.label = os.path.join(game_files, name + ".iph")
        try:
            self._arc = ipk.Archive(game_files, name)
        except ipk.ArchiveError as e:
            raise SourceError(str(e)) from e
        self.entries = self._arc.entries

    def read(self, entry):
        try:
            return self._arc.read(entry)
        except ipk.ArchiveError as e:
            raise SourceError(str(e)) from e

    def items(self):
        """(path, bytes), walking into nested .ipk images: `a.ipk/b.dds`."""
        for e in self.entries:
            yield from _walk(e.path, self.read(e), 0)


class _Nested:
    """An IPK1 image in memory: the index at its start, the data after."""

    def __init__(self, data):
        self.data = data
        self.entries = ipk.read_index(data)

    def read(self, e):
        d = self.data[e.offset : e.offset + e.stored]
        if len(d) != e.stored:
            raise ipk.ArchiveError("%s: %d of %d bytes" % (e.name, len(d), e.stored))
        return ipk.lzss_decode(d, e.size) if e.compressed else d[: e.size]


def _walk(path, data, depth):
    if not path.endswith(".ipk") or depth >= NEST_MAX:
        yield path, data
        return
    try:
        arc = _Nested(data)
        # Read every entry before yielding any: a bad image stays one entry.
        rows = [(e.path, arc.read(e)) for e in arc.entries]
    except ipk.ArchiveError:
        yield path, data
        return
    for sub, d in rows:
        yield from _walk(path + "/" + sub, d, depth + 1)
