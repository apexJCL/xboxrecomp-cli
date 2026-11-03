"""Save sources (openspec change save-import, D2 and D4): a folder or a
zip, opened read only, as one tree of files below the save root, the
folder that holds UDATA/ (and TDATA/) as the Xbox's E: drive does.

The save root is found by the layout, not by the name of what was passed:
- the shallowest folder holding UDATA/ (a dashboard copy, an E/ folder,
  a recomp install's hdd/, the in-game menu's saves zip with its hdd/);
- a UDATA folder itself;
- else a folder named like a title ID (8 hex digits) whose saves sit
  directly under it, taken as UDATA/<tid>.
Save folders found anywhere else are "no title": listed, never imported.

Every name below the root follows one rule set, the FATX one the in-game
menu's saves zip shares (toolkit src/enhance/saves_zip.c, sz_name_ok), so
a zip either side accepts the other accepts too. Links are never followed
and special files are never read. A name that breaks a rule is kept as a
rejected entry with its reason: the importer rejects the save that holds
it and imports the others.
"""

import os
import re
import stat
import zipfile

# Caps (D4), the menu's saves zip's (saves_zip.h): Xbox saves are
# kilobytes, so these only bound what a crafted source can make us read.
MAX_FILE = 16 << 20
MAX_SAVE = 64 << 20
MAX_TOTAL = 64 << 20
MAX_ENTRIES = 4096
# Components below the save root, UDATA included: the menu counts
# hdd/UDATA/<tid>/... against 8, so 7 here keeps every save we accept
# importable there.
MAX_DEPTH = 7
NAME_MAX = 42  # a FATX name's bytes
# How far above the save root a source may wrap it (a zip of a folder of
# an hdd folder).
MAX_WRAP = 3
MANIFEST = "xbr-saves.toml"
SAVE_META = "SaveMeta.xbx"

TITLE_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_BAD_CHARS = set('*?"<>|')
_RESERVED = {"con", "prn", "aux", "nul"}


class SourceError(Exception):
    """The source as a whole cannot be read: nothing is imported."""


def name_problem(name):
    """None when name may be a component under the save root, else why."""
    if not name:
        return "an empty name"
    if name in (".", ".."):
        return "a '.' or '..' folder"
    for ch in name:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            return "a control character in a name"
        if ch == "\\":
            return "a backslash in a name"
        if ch == "/":
            return "a '/' in a name"
        if ch == ":":
            return "a drive letter or ':' in a name"
        if ch in _BAD_CHARS:
            return "a character Windows refuses in a name"
    try:
        n = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return "a name that is not UTF-8"
    if n > NAME_MAX:
        return "a name over 42 bytes"
    if name[-1] in ". ":
        return "a name ending in a dot or space"
    stem = name.split(".", 1)[0].lower()
    if stem in _RESERVED or (
        len(stem) == 4 and stem[:3] in ("com", "lpt") and stem[3] in "123456789"
    ):
        return "a Windows device name"
    return None


class Entry:
    """One file below the save root: path is its components from the root
    (("UDATA", "4d530065", "<save id>", "file")), as the source spells
    them."""

    def __init__(self, path, size, mtime, reader):
        self.path = path
        self.size = size
        self.mtime = mtime
        self._reader = reader

    def read(self):
        data = self._reader()
        if len(data) != self.size:
            raise SourceError("%s changed while it was read" % "/".join(self.path))
        return data


class Tree:
    """What a source holds below its save root.

    files: [Entry]; dirs: every folder (a tuple of components), so an
    empty save folder is seen; rejected: [(path, why)] for names and
    entries that broke a rule; loose: save folders outside UDATA/<tid>
    (paths from the source's top); manifest: the text of xbr-saves.toml
    beside the root, or None."""

    def __init__(self, kind, path):
        self.kind = kind
        self.path = path
        self.where = ""  # the save root inside the source, for the plan
        self.files = []
        self.dirs = set()
        self.rejected = []
        self.loose = []
        self.manifest = None
        self._close = None

    def close(self):
        if self._close:
            self._close()
            self._close = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---- raw listings: (components from the top, kind, size, mtime, reader) ----
# kind: "file", "dir", or a rejection reason (a link, a special file).


def _walk_folder(top):
    """A folder's raw listing, its own name first in every path (so a folder
    named UDATA or like a title ID places itself). lstat only: a link is a
    rejected entry, never followed, and that includes a junction."""
    out = []
    base = os.path.basename(os.path.normpath(top)) or "root"

    def reject_link(st):
        if stat.S_ISLNK(st.st_mode):
            return "a link (never followed)"
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return "a junction or reparse point (never followed)"
        return None

    def walk(path, comps, depth):
        try:
            it = list(os.scandir(path))
        except OSError as e:
            raise SourceError("%s: %s" % (path, e.strerror or e)) from e
        for de in it:
            if len(out) >= MAX_ENTRIES:
                raise SourceError(
                    "%s: more than %d files, too many for saves: pass the folder that "
                    "holds UDATA" % (top, MAX_ENTRIES)
                )
            c = comps + (de.name,)
            try:
                st = de.stat(follow_symlinks=False)
            except OSError as e:
                out.append((c, "unreadable (%s)" % (e.strerror or e), 0, 0, None))
                continue
            why = reject_link(st)
            if why:
                out.append((c, why, 0, 0, None))
            elif stat.S_ISDIR(st.st_mode):
                out.append((c, "dir", 0, st.st_mtime, None))
                if depth + 1 > MAX_DEPTH + MAX_WRAP:
                    out.append((c + ("...",), "folders nested too deep", 0, 0, None))
                else:
                    walk(de.path, c, depth + 1)
            elif stat.S_ISREG(st.st_mode):
                p = de.path
                out.append((c, "file", st.st_size, st.st_mtime, _file_reader(p)))
            else:
                out.append((c, "not a regular file", 0, 0, None))

    try:
        st = os.lstat(top)
    except OSError as e:
        raise SourceError("%s: %s" % (top, e.strerror or e)) from e
    if reject_link(st):
        raise SourceError("%s is a link: pass the folder it points to" % top)
    out.append(((base,), "dir", 0, st.st_mtime, None))
    walk(top, (base,), 1)
    return out


def _file_reader(path):
    def read():
        with open(path, "rb") as f:
            return f.read(MAX_FILE + 1)

    return read


def _walk_zip(zf, path):
    out = []
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise SourceError("%s: more than %d entries, too many for saves" % (path, MAX_ENTRIES))
    for info in infos:
        name = info.filename
        is_dir = name.endswith("/")
        raw = name.rstrip("/")
        comps = tuple(raw.split("/")) if raw else ()
        mtime = _zip_mtime(info)
        mode = info.external_attr >> 16
        if not comps:
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            out.append(((name,), "an absolute path", 0, 0, None))
            continue
        if "\\" in name:
            out.append((comps, "a backslash in a name", 0, 0, None))
            continue
        if stat.S_ISLNK(mode):
            out.append((comps, "a link (never followed)", 0, 0, None))
            continue
        if info.flag_bits & 1:
            out.append((comps, "an encrypted entry", 0, 0, None))
            continue
        if is_dir:
            out.append((comps, "dir", 0, mtime, None))
            continue
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            out.append((comps, "compressed with a method this importer does not read", 0, 0, None))
            continue
        if info.file_size > MAX_FILE:
            out.append((comps, "a file over 16 MiB", 0, 0, None))
            continue
        out.append((comps, "file", info.file_size, mtime, _zip_reader(zf, info)))
    return out


def _zip_mtime(info):
    import calendar

    try:
        return calendar.timegm(info.date_time + (0, 0, -1))
    except (OverflowError, ValueError):
        return 0


def _zip_reader(zf, info):
    def read():
        try:
            with zf.open(info) as f:
                return f.read(MAX_FILE + 1)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as e:
            # A CRC mismatch lands here: the zip was edited or is damaged.
            raise SourceError("%s: %s" % (info.filename, e)) from e

    return read


# ---- the save root ----


def _dirs_of(raw):
    """Every folder of a listing, implied ones included (a zip may list only
    files)."""
    dirs = set()
    for comps, kind, *_ in raw:
        stop = len(comps) if kind == "dir" else len(comps) - 1
        for i in range(1, stop + 1):
            dirs.add(comps[:i])
    return dirs


def _find_root(raw, dirs):
    """(root, mode): root the components of the save root's folder (or of
    the folder standing for UDATA/<tid>'s parent); mode "root" when it holds
    UDATA/, "titles" when its title folders stand for UDATA/<tid>."""
    files = {c for c, kind, *_ in raw if kind == "file"}
    udata = sorted((d for d in dirs if d[-1].lower() == "udata"), key=len)
    if udata:
        depth = len(udata[0])
        roots = sorted({d[:-1] for d in udata if len(d) == depth})
        if len(roots) > 1:
            raise SourceError(
                "it holds several save roots (%s): pass one of them"
                % ", ".join("/".join(r) or "." for r in roots)
            )
        return roots[0], "root"
    titled = []
    for d in dirs:
        if TITLE_RE.match(d[-1]) and any(
            len(f) == len(d) + 2 and f[: len(d)] == d and f[-1].lower() == SAVE_META.lower()
            for f in files
        ):
            titled.append(d)
    if titled:
        depth = min(len(d) for d in titled)
        roots = sorted({d[:-1] for d in titled if len(d) == depth})
        if len(roots) > 1:
            raise SourceError(
                "it holds title folders in several places (%s): pass one of them"
                % ", ".join("/".join(r) or "." for r in roots)
            )
        return roots[0], "titles"
    return None, None


def _loose_saves(raw):
    """Folders holding a SaveMeta.xbx, as paths from the top."""
    out = set()
    for comps, kind, *_ in raw:
        if kind == "file" and comps[-1].lower() == SAVE_META.lower():
            out.add(comps[:-1])
    return sorted(out)


def _build(tree, raw):
    dirs = _dirs_of(raw)
    root, mode = _find_root(raw, dirs)
    if root is None:
        tree.loose = _loose_saves(raw)
        if not tree.loose:
            raise SourceError("no saves found (no UDATA folder, title folder or SaveMeta.xbx)")
        return tree
    if len(root) > MAX_WRAP:
        raise SourceError("its saves sit too deep (%s): pass that folder" % "/".join(root))
    n = len(root)
    tree.where = "/".join(root)

    def rel(comps):
        if comps[:n] != root or len(comps) == n:
            return None
        r = comps[n:]
        return ("UDATA",) + r if mode == "titles" else r

    for comps, kind, _size, _mtime, reader in raw:
        if kind == "file" and len(comps) == n + 1 and comps[-1] == MANIFEST and mode == "root":
            tree.manifest = reader
    if tree.manifest is None and n >= 1:
        # The menu's zip: xbr-saves.toml beside hdd/.
        for comps, kind, _size, _mtime, reader in raw:
            if kind == "file" and comps == root[:-1] + (MANIFEST,):
                tree.manifest = reader
    for comps, kind, size, mtime, reader in raw:
        r = rel(comps)
        if r is None:
            if kind == "file" and comps[-1].lower() == SAVE_META.lower():
                tree.loose.append(comps[:-1])
            elif kind not in ("file", "dir"):
                # An unsafe entry (an absolute name, a link) outside the root
                # is never read either, but the listing says so.
                tree.rejected.append((comps, kind))
            continue
        top = r[0].lower()
        if top not in ("udata", "tdata"):
            continue  # beside the trees (a TitleCache, the manifest): never imported
        why = None
        for c in r:
            why = name_problem(c)
            if why:
                break
        if not why and len(r) > MAX_DEPTH:
            why = "folders nested too deep"
        if not why and kind not in ("file", "dir"):
            why = kind
        if why:
            tree.rejected.append((r, why))
            continue
        if kind == "dir":
            tree.dirs.add(r)
            continue
        tree.files.append(Entry(r, size, mtime, reader))
    # Two names that differ only by case are one name on the Xbox (and on
    # NTFS and APFS), at any level: ABC and abc, or 4D530065 and 4d530065,
    # would merge into one folder. Nothing under either is taken.
    spell = {}
    for path in [e.path for e in tree.files] + list(tree.dirs):
        for i in range(1, len(path) + 1):
            spell.setdefault(tuple(c.lower() for c in path[:i]), set()).add(path[:i])
    clash = {k for k, v in spell.items() if len(v) > 1}

    def clashes(path):
        return any(tuple(c.lower() for c in path[:i]) in clash for i in range(1, len(path) + 1))

    for e in [e for e in tree.files if clashes(e.path)]:
        tree.files.remove(e)
        tree.rejected.append((e.path, "names that differ only by case"))
    tree.dirs = {d for d in tree.dirs if not clashes(d)}
    for e in tree.files:
        for i in range(1, len(e.path)):
            tree.dirs.add(e.path[:i])
    total = sum(e.size for e in tree.files)
    if total > MAX_TOTAL:
        raise SourceError("it holds %d MiB of saves, over the 64 MiB cap" % (total >> 20))
    tree.loose = sorted(set(tree.loose))
    return tree


def open_source(path):
    """A Tree for a folder or a zip (SourceError when it is neither, or
    cannot be read). Close it (or use it in a with) for a zip."""
    if not os.path.lexists(path):
        raise SourceError("%s: not found" % path)
    if os.path.isdir(path):
        tree = Tree("folder", path)
        return _build(tree, _walk_folder(path))
    if not os.path.isfile(path):
        raise SourceError("%s: not a folder or a zip" % path)
    if not zipfile.is_zipfile(path):
        raise SourceError(
            "%s: not a folder or a zip (disk and memory-unit images come in a later release)" % path
        )
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise SourceError("%s: %s" % (path, e)) from e
    tree = Tree("zip", path)
    tree._close = zf.close
    try:
        return _build(tree, _walk_zip(zf, path))
    except BaseException:
        tree.close()
        raise
