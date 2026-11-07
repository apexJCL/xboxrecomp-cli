"""The `loose` source: .dds, .xpr and .xbx files under globs of game_files.

Patterns are fnmatch over the path below game_files, lower case with `/`
(`data/textures/*`). A file reached through a link that leaves game_files is
not the dump's, and is left out.
"""

import fnmatch
import os

from .base import Entry, Source, SourceError

EXTENSIONS = (".dds", ".xpr", ".xbx")


def _inside(root, real):
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:
        # Windows: a link to another drive has no path in common.
        return False


class LooseSource(Source):
    def __init__(self, game_files, patterns):
        self.name = "loose"
        self.label = game_files
        self._root = os.path.realpath(game_files)
        if not os.path.isdir(self._root):
            raise SourceError("%s: no such folder" % game_files)
        pats = [p.lower().replace("\\", "/") for p in patterns]
        rows = []
        for top, dirs, files in os.walk(self._root):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(top, f)
                rel = os.path.relpath(full, self._root).replace(os.sep, "/")
                low = rel.lower()
                if not low.endswith(EXTENSIONS):
                    continue
                if not any(fnmatch.fnmatchcase(low, p) for p in pats):
                    continue
                real = os.path.realpath(full)
                if not _inside(self._root, real):
                    continue
                rows.append(Entry(rel, 0, os.path.getsize(real)))
        self.entries = rows

    def read(self, entry):
        path = os.path.join(self._root, entry.name.replace("/", os.sep))
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            raise SourceError("%s: %s" % (path, e)) from e
