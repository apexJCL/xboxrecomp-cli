"""import: the player's dump into game_files/, from an Xbox disc image or
a folder.

`import PATH`, `package <target> --dump PATH` and the bare `<game> PATH`
all come here. The order is the point: the source is classified by its
content, the XBE's title is checked and every name and the free space
are judged before anything under the game root changes, so a wrong disc
costs nothing. Then the tree is built in game_files.partial/ beside the
target and renamed into place, and a marker (.xbr-dump.json) records
where it came from so a rerun is a size check. A game_files/ that holds
something else is replaced only with --replace, and the old tree is
removed only once the new one is in place (game_files.old/ meanwhile).

The image reader is the toolkit's (xdvdfs.py, kept as upstream wrote
it); the checks it leaves out (names, loops, depth) are here."""

import datetime
import errno
import json
import os
import shutil
import stat
import time

from . import __version__, doctor, host, xbe
from .host import CliError
from .package import lib as pkg_lib
from .xdvdfs import SECTOR, Xiso, XisoError

MARKER = ".xbr-dump.json"
MARKER_SCHEMA = 1
# Room left on the volume after the import: the build and the bundle need
# space too, and a disk filled to the last byte fails in odd places.
MARGIN = 256 << 20
MAX_DEPTH = 32
# What a file manager leaves in a folder: never part of a dump.
JUNK = (".DS_Store", "Thumbs.db", "desktop.ini")
PROGRESS_FILES = 100
PROGRESS_BYTES = 256 << 20


def g():
    return host.g()


def partial_dir():
    return g().game_files + ".partial"


def old_dir():
    return g().game_files + ".old"


def gf_rel():
    return g().rel(g().game_files)


def xbe_name():
    """The XBE's path inside the dump: [xbe] path relative to [data]
    game_files ('default.xbe' for most games)."""
    G = g()
    rel = os.path.relpath(G.xbe, G.game_files)
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise CliError(
            "import needs game.toml's [xbe] path inside [data] game_files "
            "(%s is not under %s/)" % (G.rel(G.xbe), gf_rel())
        )
    return rel.replace(os.sep, "/")


def size_text(n):
    if n >= 1000**3:
        return "%.1f GB" % (n / 1000**3)
    return "%.1f MB" % (n / 1000**2)


def progress_text(done, total):
    """'1.2 of 3.1 GB': both in the total's unit."""
    unit, div = ("GB", 1000**3) if total >= 1000**3 else ("MB", 1000**2)
    return "%.1f of %.1f %s" % (done / div, total / div, unit)


def ignored(rel):
    """Files in game_files/ that are not the dump: the pipeline's and dev
    runs' (data.game_files_exclude), the marker and file managers'."""
    parts = rel.replace("\\", "/").split("/")
    return parts[0] in g().m["data"]["game_files_exclude"] or rel == MARKER or parts[-1] in JUNK


def counts_empty(path):
    """Missing, or holding only what ignored() names at its top level."""
    if not os.path.isdir(path):
        return True
    return all(ignored(n) for n in os.listdir(path))


def kept_names(gf):
    """The game_files_exclude entries at gf's top level: the saves a run
    left (UDATA/, TDATA/) and the pipeline's files. An import carries them
    into the new tree rather than removing them with the old one."""
    if os.path.islink(gf) or not os.path.isdir(gf):
        return []
    exclude = g().m["data"]["game_files_exclude"]
    return sorted(n for n in os.listdir(gf) if n in exclude)


def tree_bytes(path):
    if not os.path.isdir(path) or os.path.islink(path):
        return os.lstat(path).st_size
    return sum(
        os.lstat(os.path.join(dp, f)).st_size
        for dp, _, fs in os.walk(path, onerror=walk_error)
        for f in fs
    )


def walk_error(e):
    """os.walk's onerror: an unreadable folder stops the walk by name
    rather than leaving its files out of the dump."""
    raise CliError("cannot read %s: %s" % (e.filename, e.strerror or e)) from e


def file_size(p):
    try:
        return os.stat(p).st_size
    except OSError as e:
        if os.path.islink(p):
            raise CliError("%s is a link to %s, which is not there" % (p, os.readlink(p))) from e
        raise CliError("cannot read %s: %s" % (p, e.strerror or e)) from e


# ── the source ───────────────────────────────────────────────────────────


def classify(path, name=None):
    """'folder' or 'image', judged by content (any file extension), or a
    CliError that says what the path is not. name is the XBE's path inside
    the dump; `new` passes it, as no game is loaded there yet."""
    name = name or xbe_name()
    if os.path.isdir(path):
        if not os.path.isfile(os.path.join(path, *name.split("/"))):
            raise CliError(
                "no %s in %s: the folder must be the disc's root (the one that holds %s)"
                % (name, path, name)
            )
        return "folder"
    if not os.path.isfile(path):
        raise CliError("%s: no such file or folder" % path)
    try:
        Xiso(path).close()
    except (XisoError, OSError) as e:
        raise CliError(
            "%s is not an Xbox disc image (no XDVDFS descriptor at any known "
            "partition base) and not a folder; an archive (.zip, .7z, .rar) must be "
            "extracted first, and a .cue/.bin set needs its .bin" % path
        ) from e
    return "image"


def check_name(name, where):
    """One path component read from an image, before it names a file:
    the reader decodes whatever the image masters."""
    rule = None
    if name in ("", ".", ".."):
        rule = "an empty, '.' or '..' name"
    elif any(c in name for c in "/\\\0"):
        rule = "a path separator or NUL in the name"
    elif pkg_lib.BAD_CHARS.search(name):
        rule = "a character Windows refuses"
    elif pkg_lib.RESERVED.match(name):
        rule = "a name Windows reserves"
    elif name[-1] in ". ":
        rule = "a name ending in a dot or a space"
    if rule:
        raise CliError("the image holds %r in /%s: %s" % (name, where, rule))


def safe_dest(root, rel):
    """root/rel, refused unless its real path lies under root's."""
    base = os.path.realpath(root)
    dest = os.path.join(root, *rel.split("/"))
    real = os.path.realpath(dest)
    if not real.startswith(base + os.sep):
        raise CliError("the image's %s would be written outside %s" % (rel, root))
    return dest


class Source:
    """A disc image or a folder: its XBE header, its file listing, and
    copying one of its files."""

    def __init__(self, path, xbe=None):
        self.path = os.path.abspath(path)
        self.real = os.path.realpath(path)
        self.name = os.path.basename(self.path.rstrip(os.sep)) or self.path
        self.kind = classify(self.path, xbe)
        self.iso = Xiso(self.real) if self.kind == "image" else None
        self._listing = None
        self.dirs = []  # every directory, so empty ones are made too

    def close(self):
        if self.iso:
            self.iso.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def describe(self):
        return "disc image" if self.kind == "image" else "folder"

    def xbe_header(self, name=None):
        # The header and certificate sit in the first 64 KiB; the reader's
        # read() would take the whole XBE.
        return self.xbe_read(0, 0x10000, name)

    def xbe_read(self, off, n, name=None):
        """Up to n bytes of the XBE at file offset off, read in place: the
        header, or one section `new` scans."""
        name = name or xbe_name()
        if self.kind == "folder":
            with open(os.path.join(self.real, *name.split("/")), "rb") as f:
                f.seek(off)
                return f.read(n)
        parts = name.split("/")
        try:
            e = self.iso.find(parts[-1], "/".join(parts[:-1]))
        except XisoError:
            e = None
        if e is None or e.is_dir:
            raise CliError("%s: no %s on this disc image" % (self.path, name))
        if off >= e.size:
            return b""
        f = self._seek(e)
        f.seek(off, os.SEEK_CUR)
        return f.read(min(e.size - off, n))

    def _seek(self, handle):
        f = self.iso._f
        f.seek(self.iso.base + handle.sector * SECTOR)
        return f

    def listing(self):
        """[(rel, size, handle)]: every file, nothing skipped (the
        manifest's exclusions apply to bundles, not to the dump)."""
        if self._listing is None:
            self._listing = self._walk_image() if self.iso else self._walk_folder()
        return self._listing

    def _walk_image(self):
        # The reader's walk() follows directory entries as the image
        # masters them: a directory listed under itself would recurse
        # forever, so the walk here keeps the sectors it has entered.
        out, seen = [], set()

        def visit(sector, size, parts):
            where = "/".join(parts)
            if len(parts) > MAX_DEPTH:
                raise CliError(
                    "the image nests directories deeper than %d at /%s" % (MAX_DEPTH, where)
                )
            if size == 0:
                # An empty directory: mastering tools may point several at
                # one sector, which is not a loop.
                return
            if sector in seen:
                raise CliError(
                    "the image's directory /%s loops (sector %d seen twice)" % (where, sector)
                )
            seen.add(sector)
            names = {}
            for e in self.iso._read_dir(sector, size):
                check_name(e.name, where)
                key = e.name.lower()
                if key in names:
                    raise CliError(
                        "the image holds %r and %r in /%s: names that differ only by case "
                        "overwrite each other" % (names[key], e.name, where)
                    )
                names[key] = e.name
                if e.is_dir:
                    self.dirs.append("/".join(parts + [e.name]))
                    visit(e.sector, e.size, parts + [e.name])
                else:
                    out.append(("/".join(parts + [e.name]), e.size, e))

        visit(self.iso.root_sector, self.iso.root_size, [])
        return out

    def _walk_folder(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(self.real, onerror=walk_error):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, self.real)
            for d in dirnames:
                if os.path.islink(os.path.join(dirpath, d)):
                    raise CliError(
                        "%s is a link to a folder: copy the folder there instead"
                        % os.path.join(dirpath, d)
                    )
                self.dirs.append(
                    d if rel_dir == "." else os.path.join(rel_dir, d).replace(os.sep, "/")
                )
            for f in sorted(filenames):
                p = os.path.join(dirpath, f)
                rel = f if rel_dir == "." else os.path.join(rel_dir, f).replace(os.sep, "/")
                # A link to a file is copied as the file it points at.
                out.append((rel, file_size(p), os.path.realpath(p)))
        return out

    def put(self, handle, dest, clone):
        if self.iso:
            self.iso.extract(handle, dest)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            pkg_lib.copy_file(handle, dest, clone)

    def sha256(self, handle):
        if not self.iso:
            return host.sha256_path(handle)
        import hashlib

        h = hashlib.sha256()
        # The reader reads a whole file at once; a dump's largest are
        # hundreds of MB, so this reads in chunks from its handle.
        f = self._seek(handle)
        left = handle.size
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                raise CliError("%s: the image ends inside %s" % (self.path, handle.name))
            h.update(b)
            left -= len(b)
        return h.hexdigest()

    def dump_totals(self):
        """(files, bytes) of the dump proper: what ignored() lets through."""
        files = [(r, s) for r, s, _ in self.listing() if not ignored(r)]
        return len(files), sum(s for _, s in files)

    def xbe_sha256(self):
        """The folder's XBE sha256 (an image's identity does not need it)."""
        return host.sha256_path(os.path.join(self.real, *xbe_name().split("/")))

    def identity(self, xbe_sha256=None):
        """What 'the same source' means for a rerun: an image's path,
        size and mtime; a folder's path, file count, bytes and XBE."""
        if self.iso:
            st = os.stat(self.real)
            return {
                "source": self.real,
                "source_size": st.st_size,
                "source_mtime": int(st.st_mtime),
            }
        files, total = self.dump_totals()
        sha = xbe_sha256 or self.xbe_sha256()
        return {"source": self.real, "files": files, "bytes": total, "xbe_sha256": sha}


def verify_title(src):
    """The certificate's title ID against [xbe] title_id; another title is
    refused by name."""
    G = g()
    data = src.xbe_header()
    try:
        h = xbe.read_header(data)
    except xbe.XbeError as e:
        raise CliError("%s: %s: %s" % (src.path, xbe_name(), e)) from e
    want = G.m["xbe"]["title_id"]
    if h["title_id"] != want:
        title = '"%s"' % h["title"] if h["title"] else "an XBE with an empty title"
        raise CliError(
            "%s: this is %s (0x%08X), not %s (0x%08X)"
            % (src.path, title, h["title_id"], G.name, want)
        )
    return h


def _inside(a, b):
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def check_source_place(src):
    """A source that is, sits in, or holds game_files/ (or its .partial):
    a copy would recurse, and --replace would delete the source."""
    G = g()
    base = os.path.join(
        os.path.realpath(os.path.dirname(G.game_files)), os.path.basename(G.game_files)
    )
    for target in (base, base + ".partial"):
        if _inside(src.real, target):
            raise CliError(
                "%s: the source is inside this game's %s/; pass the disc image or "
                "the folder it came from" % (src.path, os.path.basename(target))
            )
        if _inside(target, src.real):
            raise CliError(
                "%s: the source contains this game's %s/; pass the disc's own folder"
                % (src.path, os.path.basename(target))
            )


def volume_of(path):
    """The mount point holding path, for the free-space message."""
    path = os.path.realpath(path)
    dev = os.stat(path).st_dev
    while True:
        up = os.path.dirname(path)
        if up == path or os.stat(up).st_dev != dev:
            return path
        path = up


def same_volume(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def check_space(src, total, replacing):
    G = g()
    need = total + MARGIN
    # A folder on the game's own volume is cloned on APFS: no space taken.
    if src.kind == "folder" and host.host_os() == "macos" and same_volume(src.real, G.root):
        need = MARGIN
    free = shutil.disk_usage(G.root).free
    if free < need:
        raise CliError(
            "needs %s free on %s; %s free%s"
            % (
                size_text(need),
                volume_of(G.root),
                size_text(free),
                " (the old %s/ is removed after the swap)" % gf_rel() if replacing else "",
            )
        )


# ── the marker ───────────────────────────────────────────────────────────


def read_marker(gf=None):
    p = os.path.join(gf or g().game_files, MARKER)
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def marker_date(m):
    """The import's date on this computer's clock (the marker keeps UTC)."""
    when = str(m.get("when", "?"))
    try:
        t = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return when[:10]
    return t.replace(tzinfo=datetime.UTC).astimezone().strftime("%Y-%m-%d")


def same_source(m, ident):
    return all(m.get(k) == v for k, v in ident.items())


IDENTITY_WORDS = {
    "source": "path",
    "source_size": "size",
    "source_mtime": "modification time",
    "files": "file count",
    "bytes": "byte total",
    "xbe_sha256": "XBE",
}


def identity_diff(m, ident):
    """'its size and modification time differ': what makes a source not
    the one the marker names (a moved or re-downloaded image is refused,
    and this says why)."""
    words = [IDENTITY_WORDS.get(k, k) for k, v in ident.items() if m.get(k) != v]
    return "this source's %s differ%s" % (" and ".join(words), "s" if len(words) == 1 else "")


def write_marker(src, ident, xbe_sha256, gf=None):
    files, total = src.dump_totals()
    m = {"schema": MARKER_SCHEMA, "format": src.kind}
    m.update(ident)
    # A folder's identity holds its own XBE's hash; an image's marker
    # records the one now in place.
    m.setdefault("xbe_sha256", xbe_sha256)
    if src.iso:
        m["base"] = src.iso.base
    m.update(
        {
            "files": files,
            "bytes": total,
            "cli": __version__,
            "when": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    with open(os.path.join(gf or g().game_files, MARKER), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(m, indent=1) + "\n")
    return m


# ── comparing a source with game_files/ ──────────────────────────────────


def tree_files(root):
    """{rel: size} of the files under root that are the dump."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        for f in filenames:
            rel = f if rel_dir == "." else os.path.join(rel_dir, f).replace(os.sep, "/")
            if not ignored(rel):
                out[rel] = file_size(os.path.join(dirpath, f))
    return out


def compare(src, gf, hashes=False):
    """The differences between the source's files and gf's, as lines;
    ignored() files on either side do not count. Without hashes (the
    size pass) a name that differs only by case matches, as it does on
    the filesystems the game runs on; with them (--check) it is named."""
    have = tree_files(gf)
    folded = {r.lower(): r for r in have}
    out = []
    for rel, size, handle in src.listing():
        if ignored(rel):
            continue
        local = rel if rel in have else folded.get(rel.lower())
        if local is None:
            out.append("missing: %s" % rel)
            continue
        have.pop(local)
        if local != rel and hashes:
            out.append("name differs in case: %s here, %s in the source" % (local, rel))
        have_size = os.path.getsize(os.path.join(gf, *local.split("/")))
        if have_size != size:
            out.append("size differs: %s (%d here, %d in the source)" % (rel, have_size, size))
        elif hashes and host.sha256_path(os.path.join(gf, *local.split("/"))) != src.sha256(handle):
            out.append("content differs: %s" % rel)
    out += ["extra: %s" % r for r in sorted(have)]
    return out


# ── recovery and removal ─────────────────────────────────────────────────


def _rmtree_onexc(fn, path, exc):
    # Files copied off a disc are read-only, and Windows will not unlink
    # those: make it writable and try once more.
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWRITE | stat.S_IEXEC)
        fn(path)
    except OSError:
        raise exc from None


def rmtree(path):
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.lexists(path):
        shutil.rmtree(path, onexc=_rmtree_onexc)


def rename(a, b, tries=5):
    """os.rename, retried: on Windows an indexer or antivirus holding a
    handle on the tree refuses a rename for a moment."""
    for i in range(tries):
        try:
            os.rename(a, b)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.2)


def recover():
    """What an interrupted --replace left: game_files.old/ is put back when
    game_files/ is missing, removed when game_files/ holds a marker or is
    the link --link makes (each is written before the rename that puts it
    in place, so it marks a finished swap), and left otherwise. Never
    deleted while game_files/ is absent."""
    gf, old = g().game_files, old_dir()
    if not os.path.lexists(old):
        return
    if not os.path.lexists(gf):
        rename(old, gf)
        host.say(
            "%s/: put back from %s/ (an earlier import stopped midway)"
            % (gf_rel(), os.path.basename(old))
        )
    elif os.path.islink(gf) or read_marker() is not None:
        host.say("removing %s/ (left by an earlier import, which finished)" % g().rel(old))
        remove_old()
    else:
        host.say(
            "%s/ and %s/ both exist (an earlier import stopped midway); keeping both"
            % (gf_rel(), g().rel(old))
        )


def remove_old():
    """Removes game_files.old/ once the new tree is in place; a failure
    leaves it, said, for the next import or the player."""
    old = old_dir()
    try:
        rmtree(old)
    except OSError as e:
        host.say(
            "could not remove %s/ (%s): remove it later; the next import removes it too"
            % (g().rel(old), e.strerror or e)
        )


def refuse_old():
    """A writing import beside a game_files.old/ that recover() kept: the
    swap would need that name, and either tree may be the player's."""
    G = g()
    raise CliError(
        "%s/ is left from an import that stopped before it finished, beside a %s/ "
        "with no marker, and either may hold your dump: remove %s/ to keep %s/, or "
        "remove %s/ and the next run puts %s/ back; then run the import again"
        % (G.rel(old_dir()), gf_rel(), G.rel(old_dir()), gf_rel(), gf_rel(), G.rel(old_dir()))
    )


def leftover_note():
    """doctor's and package's line for a sibling an interrupted import left."""
    left = [d for d in (partial_dir(), old_dir()) if os.path.lexists(d)]
    if not left:
        return None
    return "%s left by an interrupted import: run '%s import PATH' again, or remove it" % (
        ", ".join(g().rel(d) + "/" for d in left),
        host.cli_name(),
    )


def origin_note():
    """doctor's ' (imported from X, date)' or ' (linked to X)', or ''."""
    gf = g().game_files
    if os.path.islink(gf):
        return " (linked to %s)" % os.readlink(gf)
    m = read_marker()
    if m:
        return " (imported from %s, %s)" % (
            os.path.basename(str(m.get("source", "?"))),
            marker_date(m),
        )
    return ""


# ── the import ───────────────────────────────────────────────────────────


class Import:
    """One import, judged (prepare) before anything is written (run).
    verdict: 'fresh' (place the files), 'same' (nothing to do), 'adopt'
    (write the marker only), 'link'."""

    def __init__(self, src, verdict, replace, link):
        self.src, self.verdict, self.replace, self.link = src, verdict, replace, link
        self.total = sum(s for _, s, _ in src.listing()) if verdict == "fresh" else 0

    @property
    def writes(self):
        return self.verdict != "same"

    def plan_why(self):
        """The import's step in package's plan."""
        if self.verdict == "adopt":
            return "%s: files in place, marker only" % self.src.name
        if self.verdict == "link":
            return "%s: link" % self.src.name
        return "%s, %s, %s, %d files" % (
            self.src.name,
            "ISO" if self.src.iso else "folder",
            size_text(self.total),
            self.src.dump_totals()[0],
        )

    def run(self, warn_unknown=True):
        try:
            if self.verdict == "link":
                self._link()
            elif self.verdict == "adopt":
                self._adopt()
            elif self.verdict == "fresh":
                self._place()
        finally:
            self.src.close()
        record_import()
        if self.verdict == "link":
            return
        if warn_unknown:
            sha, known = doctor.dump_state()
            if sha and not known:
                host.say("WARNING: %s" % doctor.unknown_dump_note(sha))

    def _adopt(self):
        write_marker(self.src, self.src.identity(), host.sha256_path(g().xbe))
        host.say("%s/: matches %s; marker written" % (gf_rel(), self.src.name))
        # A game_files.old/ that recover() kept now sits beside a marked
        # tree: the same rule removes it.
        recover()

    def _carry(self, gf, partial):
        """The old tree's kept_names() into the new one, copied (the old
        tree stays whole until the swap); a name the source also has is
        the source's."""
        kept, replaced = [], []
        for n in kept_names(gf):
            a, b = os.path.join(gf, n), os.path.join(partial, n)
            if os.path.lexists(b):
                replaced.append(n)
            elif os.path.islink(a):
                os.symlink(os.readlink(a), b)
                kept.append(n)
            elif os.path.isdir(a):
                shutil.copytree(a, b, symlinks=True)
                kept.append(n)
            else:
                shutil.copy2(a, b)
                kept.append(n)
        if kept:
            host.say("kept from the old %s/: %s" % (gf_rel(), ", ".join(kept)))
        if replaced:
            host.say(
                "replaced by the source's own: %s (the old %s/ held them too)"
                % (", ".join(replaced), gf_rel())
            )

    def _place(self):
        G = g()
        gf, partial, old = G.game_files, partial_dir(), old_dir()
        files = self.src.listing()
        n = self.src.dump_totals()[0]
        host.say(
            "import: %s (%s, %s, %d files) into %s/"
            % (self.src.name, self.src.describe(), size_text(self.total), n, gf_rel())
        )
        clone = pkg_lib._clonefile() if self.src.kind == "folder" else None
        moved = False
        try:
            os.makedirs(partial)
            for rel in self.src.dirs:
                os.makedirs(safe_dest(partial, rel), exist_ok=True)
            done_n = done_b = 0
            next_b = PROGRESS_BYTES
            for rel, size, handle in files:
                self.src.put(handle, safe_dest(partial, rel), clone)
                done_b += size
                if ignored(rel):
                    continue
                done_n += 1
                if done_n % PROGRESS_FILES == 0 or done_b >= next_b:
                    while next_b <= done_b:
                        next_b += PROGRESS_BYTES
                    host.say(
                        "imported %d/%d files, %s" % (done_n, n, progress_text(done_b, self.total))
                    )
            # extract() raises on a short read; this catches a full disk
            # whose write failed late, or a file that changed under a copy.
            for rel, size, _ in files:
                got = os.path.getsize(os.path.join(partial, *rel.split("/")))
                if got != size:
                    raise CliError(
                        "%s: %d bytes written, %d expected (is the disk full?)" % (rel, got, size)
                    )
            self._carry(gf, partial)
            # The marker goes in before the swap, so the rename that puts
            # the tree in place is the one step that finishes the import:
            # a crash after it leaves a marked game_files/, which recover()
            # trusts.
            sha = host.sha256_path(os.path.join(partial, *xbe_name().split("/")))
            write_marker(self.src, self.src.identity(), sha, partial)
            if os.path.islink(gf):
                os.unlink(gf)
            elif os.path.isdir(gf):
                if os.listdir(gf):
                    rename(gf, old)
                    moved = True
                else:
                    os.rmdir(gf)
            rename(partial, gf)
            moved = False
        except XisoError as e:
            raise CliError("%s: %s" % (self.src.path, e)) from e
        except OSError as e:
            full = e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
            raise CliError(
                "import stopped: %s%s"
                % (
                    e,
                    " (free on %s: %s)"
                    % (volume_of(G.root), size_text(shutil.disk_usage(G.root).free))
                    if full
                    else "",
                )
            ) from e
        finally:
            if moved and not os.path.lexists(gf):
                rename(old, gf)
            if os.path.lexists(partial):
                rmtree(partial)
        remove_old()
        host.say(
            "%s/: imported from %s (%d files, %s)"
            % (gf_rel(), self.src.name, n, size_text(self.total))
        )

    def _link(self):
        gf, old = g().game_files, old_dir()
        try:
            if os.path.islink(gf):
                os.unlink(gf)
            elif os.path.isdir(gf):
                rename(gf, old)
            os.symlink(self.src.real, gf, target_is_directory=True)
        except OSError as e:
            if os.path.lexists(old) and not os.path.lexists(gf):
                rename(old, gf)
            raise CliError("--link: %s" % e) from e
        remove_old()
        host.say("%s -> %s" % (gf_rel(), self.src.real))
        host.say(
            "--link: the pipeline writes %s into %s unless pipeline.analysis_json points elsewhere"
            % (os.path.basename(g().analysis_json), self.src.real)
        )


def refuse_existing(why):
    raise CliError(
        "%s; rerun with --replace to replace it, or run %s alone to build from it"
        % (why, host.cli_name())
    )


def prepare(path, replace=False, link=False):
    """Every check, in order, before anything under the game root is
    written: recovery of an interrupted import, the source's kind, its
    place, its title, a stale .partial, the names and the free space,
    then what to do with the game_files/ already there."""
    G = g()
    gf = G.game_files
    if link and host.host_os() == "windows":
        raise CliError("--link: use a copy on Windows (no link is made there)")
    recover()
    src = Source(path)
    try:
        check_source_place(src)
        verify_title(src)
        if link:
            if src.kind != "folder":
                raise CliError("--link takes a folder, not a disc image")
            if os.path.islink(gf) and os.readlink(gf) == src.real:
                host.say("%s/: already linked to %s; nothing to do" % (gf_rel(), src.real))
                return Import(src, "same", replace, link)
            if not counts_empty(gf) or os.path.islink(gf):
                if not replace:
                    refuse_existing("%s/ already holds a dump" % gf_rel())
            elif kept_names(gf) and not replace:
                # A link has no tree to carry them into.
                raise CliError(
                    "%s/ holds %s, which --link would remove: move them into %s first, "
                    "or rerun with --replace to remove them"
                    % (gf_rel(), ", ".join(kept_names(gf)), src.real)
                )
            if os.path.lexists(old_dir()):
                refuse_old()
            return Import(src, "link", replace, link)
        if os.path.lexists(partial_dir()):
            host.say("removing %s/ (left by an interrupted import)" % G.rel(partial_dir()))
            rmtree(partial_dir())
        src.listing()  # the names, loops and depth, before any write
        verdict = "fresh" if replace else judge(src)
        if verdict == "fresh":
            if os.path.lexists(old_dir()):
                refuse_old()
            total = sum(s for _, s, _ in src.listing())
            total += sum(tree_bytes(os.path.join(gf, n)) for n in kept_names(gf))
            check_space(src, total, not counts_empty(gf))
        return Import(src, verdict, replace, link)
    except BaseException:
        src.close()
        raise


def judge(src):
    """What the game_files/ already there means for this source, without
    --replace: 'fresh', 'same', 'adopt', or a refusal."""
    G = g()
    gf = G.game_files
    if os.path.islink(gf):
        refuse_existing("%s is a link to %s" % (gf_rel(), os.readlink(gf)))
    if counts_empty(gf):
        return "fresh"
    m = read_marker()
    if m is not None:
        ident = src.identity()
        if not same_source(m, ident):
            refuse_existing(
                "%s/ holds the dump imported from %s on %s (%s)"
                % (
                    gf_rel(),
                    os.path.basename(str(m.get("source", "?"))),
                    marker_date(m),
                    identity_diff(m, ident),
                )
            )
        diffs = compare(src, gf)
        if diffs:
            raise CliError(
                "%s/ differs from %s, which it was imported from (%s); rerun with "
                "--replace to import it again" % (gf_rel(), src.name, diffs[0])
            )
        host.say(
            "%s/: already imported from %s on %s; nothing to do"
            % (gf_rel(), src.name, marker_date(m))
        )
        return "same"
    if compare(src, gf):
        refuse_existing("%s/ already holds a dump that was put there by hand" % gf_rel())
    return "adopt"


def check(path):
    """import --check: the source against game_files/ by name, size and
    sha256; writes nothing. Returns the number of differences."""
    G = g()
    with Source(path) as src:
        check_source_place(src)
        verify_title(src)
        if not os.path.isdir(G.game_files):
            raise CliError("no %s/ to check against" % gf_rel())
        n = sum(1 for r, _, _ in src.listing() if not ignored(r))
        host.say("check: %s (%s) against %s/, %d files" % (src.name, src.describe(), gf_rel(), n))
        diffs = compare(src, G.game_files, hashes=True)
    for d in diffs:
        host.say("  " + d)
    host.say(
        "check: %s"
        % ("%d differences" % len(diffs) if diffs else "every file matches (name, size, sha256)")
    )
    return len(diffs)


def record_import():
    """The import's build-state entry (buildstate, D1): what package and
    enhance hud chain on. A run that changed nothing keeps the old key."""
    from . import buildstate
    from .package import incr, plib

    incr.record_import(buildstate.State(g().root), plib())


def run_import(path, replace=False, link=False, check_only=False):
    """`import PATH`: 0 when game_files/ holds the source's dump."""
    if check_only:
        return 1 if check(path) else 0
    prepare(path, replace, link).run()
    return 0
