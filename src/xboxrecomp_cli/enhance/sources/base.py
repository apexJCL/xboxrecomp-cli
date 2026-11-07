"""What every texture source shares: an index of named entries and a read.

A source has `name` (what the inventory calls it), `label` (the path errors
name), `entries` (each with `name` and a lower-cased `/` `path`) and
`read(entry)`; `items()` yields (path, bytes) for whoever wants them all.
"""


class SourceError(Exception):
    pass


class Entry:
    """One stored range; the IPK1 entry's fields a reader needs."""

    __slots__ = ("name", "offset", "size")

    def __init__(self, name, offset, size):
        self.name = name
        self.offset = offset
        self.size = size

    @property
    def path(self):
        return self.name.replace("\\", "/").lower()


class Source:
    name = ""
    label = ""
    entries = ()

    def read(self, entry):
        raise NotImplementedError

    def items(self):
        for e in self.entries:
            yield e.path, self.read(e)
