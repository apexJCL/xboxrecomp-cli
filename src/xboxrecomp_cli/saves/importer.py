"""Classify a source's saves, plan their import, back up what is there,
stage, re-sign and rename into place (openspec change save-import, D3 and
D4).

Nothing here loses a save: the target's saves for every title touched are
backed up first, as a saves zip the in-game menu's Import saves restores
(toolkit 25bce308, src/enhance/saves_zip.c), and checked by reading it
back; a backup that fails stops the import before anything is written.
New saves are written whole in a staging folder inside the target, then
renamed in; a replaced save is renamed aside first and put back if the run
stops between the two renames (recover()).
"""

import contextlib
import datetime
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import stat
import zipfile

from . import procs, sources
from .sources import MANIFEST, SAVE_META, TITLE_RE, SourceError

# The kernel's XboxSignatureKey (toolkit src/kernel/kernel_bridge.c, beside
# XboxHDKey): 16 zero bytes. Every recompiled game signs under it, so a
# save moves between recomp installs as it is, and a save from a console
# or an emulator with its own keys loads once re-signed under it.
ZERO_KEY = bytes(16)
DIGEST = 20  # HMAC-SHA1
STAGING = ".save-import-tmp"
LOCK = ".save-import.lock"
UNDO_FAILED = "UNDO-FAILED.txt"
SAVES_FORMAT = 1  # the menu's saves zip format (saves_zip.h)
SIGNINGS = ("resign", "none")
TDATA_MODES = ("ignore", "optional", "required")
# Files a title folder may hold beside its save folders (the dashboard's
# title name and icons): written only where absent, never replaced.
EMULATOR_DIRS = {"emudisk": ("Cxbx-Reloaded", procs.CXBXR), "xemu": ("xemu", procs.XEMU)}


class SaveError(Exception):
    """The import cannot go ahead; nothing was written."""

    def __init__(self, msg, code="saves"):
        super().__init__(msg)
        self.code = code


class Config:
    """A game's [saves] values and the paths the import needs, from
    game.toml (from_game); tests build one directly."""

    def __init__(
        self,
        name,
        slug,
        exe,
        title_ids,
        signing="",
        tdata="optional",
        signed=(),
        data=None,
        game_files="",
        version="",
    ):
        self.name = name
        self.slug = slug
        self.exe = exe
        self.title_ids = list(title_ids)
        self.signing = signing
        self.tdata = tdata
        self.signed = [dict(r) for r in signed]
        self.data = dict(data or {})
        self.game_files = game_files
        self.version = version

    @classmethod
    def from_game(cls, G):
        from .. import VERSION

        m = G.m
        s = m.get("saves") or {}
        return cls(
            name=G.name,
            slug=G.slug,
            exe=m["build"]["exe"],
            title_ids=s.get("title_ids") or [m["xbe"]["title_id"]],
            signing=s.get("signing", ""),
            tdata=s.get("tdata", "optional"),
            signed=s.get("signed", []),
            data=m["data"],
            game_files=G.game_files,
            version=VERSION,
        )


# ---- signing ----


def recipes_for(cfg, rel):
    """The [[saves.signed]] entries whose glob (below UDATA/<tid>/, any
    case) matches rel, a "save id/file" path."""
    low = rel.lower()
    return [r for r in cfg.signed if fnmatch.fnmatchcase(low, r["glob"].lower())]


def resign(data, recipes):
    """data re-signed under the zero key: each recipe's HMAC-SHA1 over its
    data range written at its digest. Idempotent: a save this build wrote
    comes back byte for byte."""
    b = bytearray(data)
    for r in recipes:
        s, e = r["data"]
        d = r["digest"]
        b[d : d + DIGEST] = hmac.new(ZERO_KEY, bytes(b[s:e]), hashlib.sha1).digest()
    return bytes(b)


def signed_ok(data, recipes):
    return all(
        hmac.compare_digest(
            data[r["digest"] : r["digest"] + DIGEST],
            hmac.new(ZERO_KEY, data[r["data"][0] : r["data"][1]], hashlib.sha1).digest(),
        )
        for r in recipes
    )


# ---- SaveMeta.xbx ----


def meta_value(data, key):
    """A key=value line of a SaveMeta.xbx or TitleMeta.xbx (UTF-16LE, an
    optional BOM), or ""."""
    if data[:2] == b"\xff\xfe":
        data = data[2:]
    text = data[: len(data) & ~1].decode("utf-16-le", "replace")
    for line in text.replace("\r", "").split("\n"):
        k, sep, v = line.partition("=")
        if sep and k.strip().lower() == key.lower():
            return v.strip().strip("\x00")
    return ""


# ---- classification ----


class Save:
    def __init__(self, tid, folder, sid):
        self.tid = tid  # int
        self.folder = folder  # the title folder's spelling in the source
        self.sid = sid
        self.files = []  # Entry, path ("UDATA", folder, sid, ...)
        self.dirs = set()  # below the save folder, as tuples from it
        self.name = ""
        self.nocopy = False
        self.size = 0
        self.why = ""  # set: not importable
        self.signing = "none"  # resign | none | unknown
        self.signed_here = None  # resign: already signed for this build
        self.action = ""  # import | replace | skip | refuse (plan)
        self.note = ""

    @property
    def key(self):
        return "%08x/%s" % (self.tid, self.sid)

    def as_json(self):
        return {
            "title_id": "%08x" % self.tid,
            "id": self.sid,
            "name": self.name,
            "bytes": self.size,
            "files": len(self.files),
            "signing": self.signing,
            "signed_here": self.signed_here,
            "nocopy": self.nocopy,
            "status": "invalid" if self.why else "ok",
            "why": self.why,
            "action": self.action,
            "note": self.note,
        }


class Contents:
    """What a source (or a target) holds, sorted out by title."""

    def __init__(self):
        self.saves = []  # Save, for this game's title IDs
        self.foreign = {}  # tid string -> save count, another game's
        self.tdata = {}  # tid -> [Entry] (path from TDATA/<tid>/)
        self.title_files = {}  # tid -> [Entry]
        self.loose = []  # "no title" save folders
        self.notes = []  # (path, why) for what is never imported
        self.manifest = None  # parsed xbr-saves.toml, or None


def _tid_of(name):
    return int(name, 16) if TITLE_RE.match(name) else None


def classify(tree, cfg):
    c = Contents()
    ids = set(cfg.title_ids)
    saves = {}
    for e in tree.files:
        p = e.path
        top = p[0].lower()
        if len(p) < 3:
            c.notes.append(("/".join(p), "a file outside any title folder"))
            continue
        tid = _tid_of(p[1])
        if tid is None:
            c.notes.append(("/".join(p), "not under a title folder (8 hex digits)"))
            continue
        if tid not in ids:
            if top == "udata" and len(p) >= 4:
                c.foreign.setdefault("%08x" % tid, set()).add(p[2].lower())
            continue
        if top == "tdata":
            c.tdata.setdefault(tid, []).append(e)
            continue
        if len(p) == 3:
            c.title_files.setdefault(tid, []).append(e)
            continue
        k = (tid, p[2].lower())
        s = saves.get(k)
        if s is None:
            s = saves[k] = Save(tid, p[1], p[2])
        s.files.append(e)
    for d in tree.dirs:
        if len(d) >= 3 and d[0].lower() == "udata":
            tid = _tid_of(d[1])
            if tid in ids:
                k = (tid, d[2].lower())
                if k not in saves:
                    saves[k] = Save(tid, d[1], d[2])
                if len(d) > 3:
                    saves[k].dirs.add(d[3:])
            elif tid is not None:
                c.foreign.setdefault("%08x" % tid, set()).add(d[2].lower())
    c.foreign = {t: len(v) for t, v in sorted(c.foreign.items())}
    for path, why in tree.rejected:
        if (
            len(path) >= 3
            and path[0].lower() == "udata"
            and _tid_of(path[1]) in ids
            and (_tid_of(path[1]), path[2].lower()) in saves
            and len(path) > 3
        ):
            s = saves[(_tid_of(path[1]), path[2].lower())]
            s.why = s.why or "%s (%s)" % (why, "/".join(path[3:]))
        else:
            c.notes.append(("/".join(path), why))
    for s in saves.values():
        _check_save(s, cfg)
    c.saves = sorted(saves.values(), key=lambda s: (s.tid, s.sid.lower()))
    c.loose = ["/".join(x) for x in tree.loose]
    if tree.manifest is not None:
        c.manifest = _check_manifest(tree, c, cfg)
    return c


def _check_save(s, cfg):
    s.size = sum(e.size for e in s.files)
    meta = [e for e in s.files if len(e.path) == 4 and e.path[3].lower() == SAVE_META.lower()]
    if not s.why and not meta:
        s.why = "no SaveMeta.xbx: not a save folder"
    if not s.why and s.size > sources.MAX_SAVE:
        s.why = "over 64 MiB"
    if meta and not s.why:
        try:
            data = meta[0].read()
        except SourceError as e:
            s.why = str(e)
            return
        s.name = meta_value(data, "Name")
        s.nocopy = meta_value(data, "NoCopy") == "1"
    if cfg.signing == "":
        s.signing = "unknown"
    elif cfg.signing == "none":
        s.signing = "none"
    else:
        s.signing = "none"
        for e in s.files:
            rs = recipes_for(cfg, "/".join(e.path[2:]))
            if not rs:
                continue
            s.signing = "resign"
            if e.size == rs[0]["size"] and not s.why:
                try:
                    ok = signed_ok(e.read(), rs)
                except SourceError as err:
                    s.why = str(err)
                    continue
                s.signed_here = ok if s.signed_here is None else s.signed_here and ok
            if e.size != rs[0]["size"] and not s.why:
                s.why = "%s is %d bytes, not the %d this game's saves are" % (
                    "/".join(e.path[3:]),
                    e.size,
                    rs[0]["size"],
                )


def parse_manifest(text):
    """xbr-saves.toml as the menu writes it: key = "string" | integer."""
    import tomllib

    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SourceError("its xbr-saves.toml cannot be read (%s)" % e) from e


def _check_manifest(tree, c, cfg):
    """The menu's manifest, checked as the menu checks it, so a zip that
    lost or gained files on the way is refused (SourceError)."""
    raw = tree.manifest()
    if len(raw) > 4096:
        raise SourceError("its xbr-saves.toml is too big")
    m = parse_manifest(raw.decode("utf-8", "replace"))
    if m.get("kind") != "saves":
        raise SourceError("it is not a saves export (xbr-saves.toml kind %r)" % m.get("kind"))
    fmt = m.get("format")
    if not isinstance(fmt, int) or fmt < 1:
        raise SourceError("its xbr-saves.toml has a bad format")
    if fmt > SAVES_FORMAT:
        raise SourceError("it comes from a newer build (saves format %d); update xbr" % fmt)
    tid = m.get("title_id")
    if not isinstance(tid, str) or not TITLE_RE.match(tid):
        raise SourceError("its xbr-saves.toml has a bad title_id")
    folders = {
        p[1].lower() for p in (e.path for e in tree.files) if len(p) >= 2 and TITLE_RE.match(p[1])
    }
    if folders - {tid.lower()}:
        raise SourceError(
            "its xbr-saves.toml says title %s but it holds %s"
            % (tid.lower(), ", ".join(sorted(folders - {tid.lower()})))
        )
    files = len(tree.files)
    size = sum(e.size for e in tree.files)
    # With entries rejected (named apart) the counts cannot agree; those
    # are reported, and the saves holding them refused, on their own.
    if not tree.rejected and (m.get("files") != files or m.get("bytes") != size):
        raise SourceError(
            "it is incomplete: xbr-saves.toml lists %s files and %s bytes, it holds %d and %d"
            % (m.get("files"), m.get("bytes"), files, size)
        )
    return m


# ---- the target ----


def _expand(path):
    path = re.sub(
        r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda x: os.environ.get(x.group(1), x.group(0)), path
    )
    return os.path.expanduser(os.path.expandvars(path))


def default_target(cfg, host_os, env=None):
    """(hdd folder, why): where the installed game keeps its saves, as its
    launcher sets RECOMP_HDD_DIR: <data>/hdd. SaveError when it is not
    known here."""
    env = os.environ if env is None else env
    var = cfg.data.get("dir_env", "")
    if var and env.get(var):
        return os.path.join(env[var], "hdd"), "$%s" % var
    if host_os == "windows":
        base = cfg.data.get("windows", "")
        if "%" in _expand(base):
            raise SaveError("LOCALAPPDATA is not set: pass --to <the game's hdd folder>", "target")
        return os.path.join(_expand(base), "hdd"), "the Windows data folder"
    if host_os == "macos":
        return os.path.join(_expand(cfg.data.get("macos", "")), "hdd"), "the macOS data folder"
    base = _expand(cfg.data.get("steamos", ""))
    if not os.path.isdir(base):
        raise SaveError(
            "%s is not installed at %s: pass --to <the game's hdd folder>" % (cfg.name, base),
            "target",
        )
    return os.path.join(base, "hdd"), "the SteamOS install"


def _ci_child(parent, name):
    """parent's child named name in any case, or None."""
    try:
        for n in os.listdir(parent):
            if n.lower() == name.lower():
                return n
    except OSError:
        pass
    return None


def _is_link(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def target_tree(target, kind, tid):
    """(folder path or None, {lowered name: name}) of target/<kind>/<tid>."""
    k = _ci_child(target, kind)
    if not k:
        return None, {}
    t = _ci_child(os.path.join(target, k), "%08x" % tid)
    if not t:
        return None, {}
    p = os.path.join(target, k, t)
    try:
        names = {n.lower(): n for n in os.listdir(p)}
    except OSError:
        names = {}
    return p, names


def _walk_target(top):
    """[(rel components, abs path, is_dir)] of a target tree; SaveError on
    a link anywhere in it (D4: never write through one)."""
    out = []

    def walk(path, rel):
        for de in sorted(os.scandir(path), key=lambda d: d.name):
            p = os.path.join(path, de.name)
            if _is_link(p):
                raise SaveError(
                    "%s is a link: the import never writes through one; move the real "
                    "folder there" % p,
                    "link",
                )
            r = rel + (de.name,)
            if de.is_dir(follow_symlinks=False):
                out.append((r, p, True))
                walk(p, r)
            elif de.is_file(follow_symlinks=False):
                out.append((r, p, False))

    if top and os.path.isdir(top):
        walk(top, ())
    return out


def safety(cfg, target, source, procs_list=None):
    """Refusals (SaveError) and warnings ([str]) before anything is written."""
    warnings = []
    # normcase: Windows paths compare without case (and with either slash).
    real = os.path.normcase(os.path.realpath(target))
    parts = real.replace("\\", "/").split("/")
    if any(p.lower().endswith(".app") for p in parts):
        raise SaveError(
            "%s is inside an app bundle; the game keeps saves in its data folder" % target, "target"
        )
    if cfg.game_files:
        gf = os.path.normcase(os.path.realpath(cfg.game_files))
        if real == gf or real.startswith(gf.rstrip(os.sep) + os.sep):
            raise SaveError(
                "%s is inside the game files; the game keeps saves in its data folder" % target,
                "target",
            )
    for kind in ("UDATA", "TDATA"):
        k = _ci_child(target, kind)
        if k and _is_link(os.path.join(target, k)):
            raise SaveError(
                "%s is a link: the import never writes through one" % os.path.join(target, k),
                "link",
            )
        if k:
            for tid in cfg.title_ids:
                t = _ci_child(os.path.join(target, k), "%08x" % tid)
                if t and _is_link(os.path.join(target, k, t)):
                    raise SaveError(
                        "%s is a link: the import never writes through one"
                        % os.path.join(target, k, t),
                        "link",
                    )
    if procs_list is None:
        procs_list = procs.list_processes(cfg.exe)
    games = procs.game_running(procs_list, cfg.exe)
    if games is None:
        warnings.append(
            "could not check whether %s is running: quit it before importing, or it "
            "writes its old saves back over these" % cfg.name
        )
    elif games:
        raise SaveError(
            "%s is running (pid %s): quit it first, or it writes its old saves back over "
            "these" % (cfg.name, ", ".join(str(p) for p, _ in games)),
            "running",
        )
    if source:
        comps = [p.lower() for p in os.path.realpath(source).replace("\\", "/").split("/")]
        for d, (label, names) in EMULATOR_DIRS.items():
            if any(d in c for c in comps):
                emu = procs.emulator_running(procs_list, names)
                if emu:
                    raise SaveError(
                        "%s is running (pid %s) and its disk holds the source: close it "
                        "first so the saves are whole" % (label, ", ".join(str(p) for p, _ in emu)),
                        "emulator",
                    )
    if cfg.game_files:
        legacy = _ci_child(cfg.game_files, "UDATA")
        if legacy:
            for tid in cfg.title_ids:
                old = _ci_child(os.path.join(cfg.game_files, legacy), "%08x" % tid)
                if old and not target_tree(target, "UDATA", tid)[0]:
                    warnings.append(
                        "older builds kept saves in %s; this one reads %s. Import that "
                        "folder too if those are the saves you play"
                        % (os.path.join(cfg.game_files, legacy), target)
                    )
    return warnings


# ---- the plan ----


class Plan:
    def __init__(self, source, target, contents):
        self.source = source
        self.target = target
        self.contents = contents
        self.saves = []  # Save with action set
        self.tdata = []  # (tid, Entry, action, dest rel)
        self.title_files = []  # (tid, Entry, dest rel)
        self.warnings = []
        self.backup = None  # set by run()
        self.backup_dir = ""

    def writes(self):
        return [s for s in self.saves if s.action in ("import", "replace")] or [
            t for t in self.tdata if t[2] in ("import", "replace")
        ]

    def as_json(self):
        c = self.contents
        return {
            "source": self.source,
            "target": self.target,
            "backup": self.backup,
            "backup_dir": self.backup_dir,
            "saves": [s.as_json() for s in self.saves],
            "tdata": [
                {"title_id": "%08x" % t, "path": "/".join(e.path[2:]), "action": a}
                for t, e, a, _ in self.tdata
            ],
            "title_files": ["/".join(e.path[1:]) for _, e, _ in self.title_files],
            "foreign": c.foreign,
            "no_title": c.loose,
            "ignored": [{"path": p, "why": w} for p, w in c.notes],
            "warnings": self.warnings,
        }


def make_plan(source, target, contents, cfg, only=(), replace=False, with_tdata=False):
    p = Plan(source, target, contents)
    if cfg.signing == "":
        p.warnings.append(
            "game.toml has no [saves] signing: the saves go in as they are; a save from a "
            "console or an emulator may not load"
        )
    if contents.manifest and contents.manifest.get("keys", "zero") != "zero":
        p.warnings.append(
            "these saves were exported with other console keys; they are re-signed for "
            "this build where game.toml says how"
        )
    want = {x.lower() for x in only}
    known = {s.sid.lower() for s in contents.saves}
    missing = sorted(want - known)
    if missing:
        raise SaveError("no save %s in the source" % ", ".join(missing), "save")
    tids = set()
    for s in contents.saves:
        if want and s.sid.lower() not in want:
            continue
        p.saves.append(s)
        if s.why:
            s.action = "refuse"
            continue
        tids.add(s.tid)
        _, names = target_tree(target, "UDATA", s.tid)
        if s.sid.lower() in names:
            s.action = "replace" if replace else "skip"
            if not replace:
                s.note = "already there (--replace overwrites it, after the backup)"
        else:
            s.action = "import"
    take_tdata = cfg.tdata == "required" or (cfg.tdata == "optional" and with_tdata)
    for tid, entries in sorted(contents.tdata.items()):
        if not take_tdata:
            if cfg.tdata == "optional" and entries:
                p.warnings.append(
                    "the source has the title's own data (TDATA, %d files); --with-tdata "
                    "brings it too" % len(entries)
                )
            continue
        tdir, _ = target_tree(target, "TDATA", tid)
        for e in sorted(entries, key=lambda e: e.path):
            rel = e.path[2:]
            exists = tdir and _ci_exists(tdir, rel)
            action = "import" if not exists else ("replace" if replace else "skip")
            p.tdata.append((tid, e, action, rel))
            tids.add(tid)
    if cfg.tdata == "required":
        for tid in sorted({s.tid for s in p.saves if s.action in ("import", "replace")}):
            if tid not in contents.tdata:
                p.warnings.append(
                    "the source has no TDATA for %08x, which this game keeps beside its "
                    "saves; they may not load" % tid
                )
    for tid in sorted(tids):
        udir, names = target_tree(target, "UDATA", tid)
        for e in contents.title_files.get(tid, []):
            if e.path[2].lower() not in names:
                p.title_files.append((tid, e, (e.path[2],)))
    return p


def _ci_exists(top, rel):
    p = top
    for c in rel:
        n = _ci_child(p, c)
        if n is None:
            return False
        p = os.path.join(p, n)
    return True


# ---- backup ----


def backup_dir(target):
    """<data>/config/exports/save-backups: where the menu's Import saves
    puts its backups (the user dir is <data>/config), so a CLI backup sits
    beside them and the menu restores it."""
    return os.path.join(
        os.path.dirname(os.path.abspath(target)), "config", "exports", "save-backups"
    )


def _manifest_text(cfg, tid, files, size, now):
    def q(s):
        s = "".join(ch for ch in s if ord(ch) >= 0x20 and ord(ch) != 0x7F)
        return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')

    return (
        "# Saves exported by the in-game menu (xboxrecomp)\n"
        "format = %d\nkind = %s\ntitle_id = %s\ntitle = %s\ncreated = %s\nkeys = %s\n"
        "files = %d\nbytes = %d\ntoolkit = %s\n"
        % (
            SAVES_FORMAT,
            q("saves"),
            q("%08x" % tid),
            q(cfg.name),
            q(now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            q("zero"),
            files,
            size,
            q("xbr %s" % cfg.version if cfg.version else "xbr"),
        )
    )


def write_backup(cfg, target, tid, dest_dir, now, multi=False):
    """One title's saves in target as a menu saves zip in dest_dir, checked
    by reading it back. None when there is nothing to back up."""
    trees = []
    for kind in ("UDATA", "TDATA"):
        top, _ = target_tree(target, kind, tid)
        if top:
            trees.append((kind, _walk_target(top)))
    if not any(any(not d for _, _, d in t) for _, t in trees):
        return None
    files = []
    dirs = []
    try:
        for kind, walked in trees:
            base = "hdd/%s/%08x/" % (kind, tid)
            dirs.append(base)
            for rel, path, is_dir in walked:
                name = base + "/".join(rel)
                if is_dir:
                    dirs.append(name + "/")
                else:
                    with open(path, "rb") as f:
                        files.append((name, f.read(), os.lstat(path).st_mtime))
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise SaveError(
            "the saves there could not be read for the backup (%s); nothing was imported" % e,
            "backup",
        ) from e
    size = sum(len(d) for _, d, _ in files)
    stem = "%s-saves-backup-%s" % (cfg.slug, now.strftime("%Y%m%d-%H%M%S"))
    if multi:
        stem += "-%08x" % tid
    for n in range(1, 1000):
        path = os.path.join(dest_dir, stem + (".zip" if n == 1 else "-%d.zip" % n))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
            break
        except FileExistsError:
            continue
        except OSError as e:
            raise SaveError(
                "the backup %s could not be made (%s); nothing was imported" % (path, e), "backup"
            ) from e
    else:
        raise SaveError("no free backup name in %s" % dest_dir, "backup")
    try:
        with os.fdopen(fd, "wb") as raw:
            with zipfile.ZipFile(raw, "w", zipfile.ZIP_STORED) as z:
                _zput(z, MANIFEST, _manifest_text(cfg, tid, len(files), size, now).encode(), now)
                for name in ["hdd/", "hdd/UDATA/", "hdd/TDATA/"]:
                    if any(d.startswith(name) for d in dirs):
                        _zput(z, name, b"", now)
                for d in dirs:
                    _zput(z, d, b"", now)
                for name, data, mtime in files:
                    _zput(z, name, data, datetime.datetime.fromtimestamp(mtime, datetime.UTC))
            raw.flush()
            os.fsync(raw.fileno())
        _verify_backup(path, files)
    except Exception as e:
        # A half-written backup is no backup: never leave one to be trusted.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise SaveError(
            "the backup %s failed (%s); nothing was imported" % (path, e), "backup"
        ) from e
    return path


def _zput(z, name, data, when):
    t = when.timetuple()[:6]
    if t[0] < 1980:
        t = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, t)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    # Unix modes, as the menu's reader checks them: a folder or a file.
    info.external_attr = (0o40755 << 16) | 0x10 if name.endswith("/") else 0o100644 << 16
    z.writestr(info, data)


def _verify_backup(path, files):
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise SaveError("%s does not read back (%s)" % (path, bad), "backup")
        for name, data, _ in files:
            if z.read(name) != data:
                raise SaveError("%s does not read back (%s)" % (path, name), "backup")


# ---- writing ----


def recover(target):
    """Finish what a stopped run left in the staging folder: a save renamed
    aside whose replacement never landed goes back; the rest is removed.
    [str] of what was put back.

    An aside copy still carries its .json record until its run finished
    (execute() drops the records before removing staging). One whose place
    is taken again cannot be told apart from a failed undo, so it is never
    deleted: the import refuses and names it."""
    stage = os.path.join(target, STAGING)
    back = []
    if not os.path.isdir(stage) or _is_link(stage):
        return back
    if os.path.lexists(os.path.join(stage, UNDO_FAILED)):
        raise SaveError(
            "a stopped import could not be undone; the saves it replaced are in %s (see %s). "
            "Move them back by hand, then delete that folder"
            % (stage, os.path.join(stage, UNDO_FAILED)),
            "stage",
        )
    for n in sorted(os.listdir(stage)):
        if not n.startswith("old-") or n.endswith(".json"):
            continue
        meta = os.path.join(stage, n + ".json")
        old = os.path.join(stage, n)
        if not os.path.lexists(meta):
            continue  # its run finished: the record went first
        try:
            with open(meta, encoding="utf-8") as f:
                rel = json.load(f)
        except (OSError, ValueError):
            rel = None
        if rel is None or not isinstance(rel, list) or not all(isinstance(c, str) for c in rel):
            if os.path.lexists(old):
                raise SaveError(
                    "a stopped import left %s with a damaged record; move it back by hand, "
                    "then delete %s" % (old, stage),
                    "stage",
                )
            continue
        dest = os.path.join(target, *rel)
        if os.path.lexists(old) and os.path.lexists(dest):
            raise SaveError(
                "a stopped import left %s, the old copy of %s, and something is there again; "
                "keep whichever is right, then delete %s" % (old, dest, stage),
                "stage",
            )
        if os.path.lexists(old):
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(old, dest)
            except OSError as e:
                raise SaveError(
                    "a stopped import left %s, which could not go back to %s (%s); move it "
                    "back by hand, then delete %s" % (old, dest, e, stage),
                    "stage",
                ) from e
            back.append(dest)
    try:
        shutil.rmtree(stage)
    except OSError as e:
        raise SaveError("%s could not be removed (%s)" % (stage, e), "stage") from e
    return back


def _stage_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _file_data(cfg, e, rel_in_title):
    data = e.read()
    if cfg.signing == "resign":
        rs = recipes_for(cfg, rel_in_title)
        if rs:
            data = resign(data, rs)
    return data


class Run:
    """The import's writes, in order, with what is needed to undo them."""

    def __init__(self, target):
        self.target = target
        self.stage = os.path.join(target, STAGING)
        self.n = 0
        self.done = []  # (dest, aside or None, installed): what _undo reverses

    def slot(self, kind):
        self.n += 1
        return os.path.join(self.stage, "%s-%d" % (kind, self.n))

    def aside(self, rel):
        """Rename target/rel into the staging folder, recording where it
        came from first."""
        old = self.slot("old")
        with open(old + ".json", "w", encoding="utf-8") as f:
            json.dump(list(rel), f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(os.path.join(self.target, *rel), old)
        return old

    def put(self, new, dest, replace):
        """Rename new to dest, moving what is at dest aside first; each
        step is recorded as soon as it is done."""
        aside = self.aside(_rel(self.target, dest)) if replace else None
        step = [dest, aside, False]
        self.done.append(step)
        os.rename(new, dest)
        step[2] = True


def execute(plan, cfg, now=None):
    """Back up, stage, rename in. Returns {"backup": [paths], "recovered": [...]}."""
    target = plan.target
    now = now or datetime.datetime.now(datetime.UTC)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise SaveError("%s could not be made (%s)" % (target, e), "write") from e
    with _lock(target):
        return _execute(plan, cfg, target, now)


@contextlib.contextmanager
def _lock(target):
    """One import at a time per target: a second run's recover() would take
    the first one's staging folder away mid-swap."""
    path = os.path.join(target, LOCK)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            break
        except FileExistsError:
            stale = _stale_lock(path)
            if stale is None or not _take_over(path, stale):
                raise SaveError(
                    "another import into %s is running (%s); wait for it, or delete that "
                    "file if none is" % (target, path),
                    "busy",
                ) from None
        except OSError as e:
            raise SaveError("%s could not be locked (%s)" % (target, e), "write") from e
    else:
        raise SaveError("%s could not be locked" % target, "busy")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("%d %s\n" % (os.getpid(), socket.gethostname()))
        yield
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _stale_lock(path):
    """The lock's text when it was left by a run that died (its pid is gone
    on this host), else None. Unknown is never stale."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        pid, host = text.split()[:2]
        pid = int(pid)
    except (OSError, ValueError):
        return None
    if host != socket.gethostname():
        return None
    if os.name == "nt":
        # os.kill on Windows terminates; ask the process list instead. An
        # empty list is a failed listing (this process is always in it).
        ps = procs.list_processes("")
        if not ps or all(p != os.getpid() for p, _, _ in ps):
            return None
        return text if all(p != pid for p, _, _ in ps) else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return text
    except OSError:
        return None
    return None


def _take_over(path, stale):
    """Move a dead run's lock aside so the O_EXCL create can be retried.
    The rename is atomic, so of two runs taking over only one moves it; the
    text is checked after the move in case a live run's lock was moved
    instead, and that one goes back."""
    aside = "%s.stale-%d-%s" % (path, os.getpid(), os.urandom(4).hex())
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return True  # another run moved it: race for the create
    except OSError:
        return False
    try:
        with open(aside, encoding="utf-8") as f:
            ok = f.read() == stale
    except OSError:
        ok = False
    if not ok:
        with contextlib.suppress(OSError):
            if not os.path.lexists(path):
                os.rename(aside, path)
        return False
    with contextlib.suppress(OSError):
        os.unlink(aside)
    return True


def _execute(plan, cfg, target, now):
    recovered = recover(target)
    todo = [s for s in plan.saves if s.action in ("import", "replace")]
    tdata = [t for t in plan.tdata if t[2] in ("import", "replace")]
    if not todo and not tdata and not plan.title_files:
        return {"backup": [], "recovered": recovered}
    tids = sorted({s.tid for s in todo} | {t[0] for t in tdata} | {t[0] for t in plan.title_files})
    bdir = plan.backup_dir or backup_dir(target)
    backups = []
    for tid in tids:
        b = write_backup(cfg, target, tid, bdir, now, multi=len(tids) > 1)
        if b:
            backups.append(b)
    plan.backup = backups
    run = Run(target)
    backup_note = ", ".join(backups) or "not needed"
    try:
        staged = _stage(run, cfg, todo, tdata, plan.title_files)
    except (OSError, SourceError) as e:
        with contextlib.suppress(OSError):
            shutil.rmtree(run.stage)
        raise SaveError(
            "the saves could not be staged (%s); nothing in place was touched. The backup is %s"
            % (e, backup_note),
            "write",
        ) from e
    try:
        _swap(run, target, staged)
    except OSError as e:
        failed = _undo(run)
        if failed:
            raise SaveError(
                "a save could not be put in place (%s), and undoing the swap failed too (%s). "
                "The saves it replaced are in %s; the backup is %s"
                % (e, failed, run.stage, backup_note),
                "write",
            ) from e
        with contextlib.suppress(OSError):
            shutil.rmtree(run.stage)
        raise SaveError(
            "a save could not be put in place (%s); every save there is as it was. "
            "The backup is %s" % (e, backup_note),
            "write",
        ) from e
    # Every swap landed: the records go first, so a stop here leaves aside
    # copies that recover() knows it may delete.
    for _, aside, _ in run.done:
        if aside:
            with contextlib.suppress(OSError):
                os.unlink(aside + ".json")
    shutil.rmtree(run.stage)
    return {"backup": backups, "recovered": recovered}


def _stage(run, cfg, todo, tdata, title_files):
    """Everything new, written whole before anything in place moves."""
    os.makedirs(run.stage, exist_ok=True)
    staged = []
    for s in todo:
        new = run.slot("new")
        os.makedirs(new)
        for d in sorted(s.dirs):
            os.makedirs(os.path.join(new, *d), exist_ok=True)
        for e in s.files:
            _stage_write(os.path.join(new, *e.path[3:]), _file_data(cfg, e, "/".join(e.path[2:])))
        staged.append(("save", s, new))
    for tid, e, action, rel in tdata:
        new = run.slot("new")
        _stage_write(os.path.join(new, rel[-1]), e.read())
        staged.append(("tdata", (tid, rel, action), os.path.join(new, rel[-1])))
    for tid, e, rel in title_files:
        new = run.slot("new")
        _stage_write(os.path.join(new, rel[-1]), e.read())
        staged.append(("title", (tid, rel), os.path.join(new, rel[-1])))
    return staged


def _swap(run, target, staged):
    for kind, item, new in staged:
        if kind == "save":
            s = item
            tdir = _ensure(target, "UDATA", s.tid)
            _, names = target_tree(target, "UDATA", s.tid)
            have = names.get(s.sid.lower())
            run.put(new, os.path.join(tdir, have or s.sid), bool(have))
        elif kind == "tdata":
            tid, rel, action = item
            tdir = _ensure(target, "TDATA", tid)
            parent = tdir
            for c in rel[:-1]:
                n = _ci_child(parent, c)
                parent = os.path.join(parent, n or c)
                os.makedirs(parent, exist_ok=True)
            have = _ci_child(parent, rel[-1])
            run.put(new, os.path.join(parent, have or rel[-1]), bool(have))
        else:
            tid, rel = item
            tdir = _ensure(target, "UDATA", tid)
            if not _ci_child(tdir, rel[-1]):
                run.put(new, os.path.join(tdir, rel[-1]), False)


def _undo(run):
    """Put every swap of this run back, newest first: the installed copy
    returns to staging, the old one to its place. "" when all went back;
    otherwise the errors, with a marker that stops the next run's
    recover() from deleting the old copies."""
    errors = []
    for dest, aside, installed in reversed(run.done):
        try:
            if installed:
                os.rename(dest, run.slot("undo"))
            if aside:
                os.rename(aside, dest)
        except OSError as e:
            errors.append("%s: %s" % (dest, e))
    if errors:
        with contextlib.suppress(OSError):
            with open(os.path.join(run.stage, UNDO_FAILED), "w", encoding="utf-8") as f:
                f.write("\n".join(errors) + "\n")
    return "; ".join(errors)


def _rel(target, path):
    return tuple(os.path.relpath(path, target).split(os.sep))


def _ensure(target, kind, tid):
    k = _ci_child(target, kind) or kind
    os.makedirs(os.path.join(target, k), exist_ok=True)
    t = _ci_child(os.path.join(target, k), "%08x" % tid) or "%08x" % tid
    p = os.path.join(target, k, t)
    os.makedirs(p, exist_ok=True)
    return p


# ---- listing the target ----


def target_contents(target, cfg):
    tree = sources.Tree("folder", target)
    for tid in cfg.title_ids:
        for kind in ("UDATA", "TDATA"):
            top, _ = target_tree(target, kind, tid)
            if not top:
                continue
            for rel, path, is_dir in _walk_target(top):
                comps = (kind, "%08x" % tid) + rel
                if is_dir:
                    tree.dirs.add(comps)
                else:
                    st = os.lstat(path)
                    tree.files.append(
                        sources.Entry(comps, st.st_size, st.st_mtime, sources._file_reader(path))
                    )
    return classify(tree, cfg)


def stale_staging(target):
    return os.path.isdir(os.path.join(target, STAGING))
