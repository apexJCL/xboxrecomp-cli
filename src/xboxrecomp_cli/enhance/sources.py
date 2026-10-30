"""Archive sources of xbr enhance hud: where the set's entries are read.

game.toml's [enhance.hud] source picks the reader:

  ipk   IPK1 pairs (ipk.py); an archive is a basename under game_files,
        `<name>.iph` beside `<name>.ipk`
  hook  a stdlib script in the game's repo prints the archive's index, the
        CLI reads the ranges itself; an archive is a path under
        game_files, matched case-insensitively per part (a dump copied
        from a disc or a Windows folder may spell `Data` or `data`)

Hook protocol 1: `<python> -I <repo>/<hook> index <absolute archive>`,
run in a fresh temporary folder, prints one JSON object

  {"xbr_archive_index": 1,
   "entries": [{"name": "data\\\\ui\\\\a.dds", "offset": 1234, "size": 512}]}

The hook reads data from the owner's dump, so it runs isolated (-I: no
user site-packages, no PYTHON* variables, no script folder on sys.path)
and away from both the dump and the repo. It gives ranges, not bytes, so
it can never hand the CLI a file outside the archive. Every entry's
`path` is its name lower-cased with `/`, for the patterns in game.toml.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

from ..manifest import rel_path_problem
from . import ipk

PROTOCOL = 1
STDOUT_MAX = 64 << 20
STDERR_TAIL = 4096
TIMEOUT = 60
CHUNK = 1 << 16


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


class IpkSource:
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


def resolve_nocase(root, rel):
    """root/rel with each part of rel matched case-insensitively when it
    is not there as spelt. A part that exists as spelt wins over names that
    differ from it only in case; otherwise OSError when no name matches or
    two do."""
    path = root
    for part in rel.split("/"):
        exact = os.path.join(path, part)
        if os.path.exists(exact):
            path = exact
            continue
        want = part.casefold()
        hits = [n for n in os.listdir(path) if n.casefold() == want]
        if not hits:
            raise FileNotFoundError("no such file: %s" % os.path.join(root, rel))
        if len(hits) > 1:
            raise OSError("%s: %s match %s" % (path, " and ".join(sorted(hits)), part))
        path = os.path.join(path, hits[0])
    return path


def hook_path(repo, hook):
    """The hook's real path: repo-relative and, after symlinks, a regular
    file inside the repo, else SourceError."""
    why = rel_path_problem(hook)
    if why:
        raise SourceError("enhance.hud.hook %r: %s" % (hook, why))
    top = os.path.realpath(repo)
    real = os.path.realpath(os.path.join(repo, hook))
    if os.path.commonpath([top, real]) != top:
        raise SourceError("enhance.hud.hook %s: resolves outside the repo (%s)" % (hook, real))
    if not os.path.isfile(real):
        raise SourceError("enhance.hud.hook %s: no such file in %s" % (hook, repo))
    return real


def _kill_tree(p):
    """Kill the hook and everything it started. Killing only the hook
    leaves a child that inherited stdout holding the pipe open, and the
    read (and so the timeout) would wait for that child. On POSIX the
    group outlives a reaped hook while a child is in it, so its id is
    still the group's."""
    if os.name == "nt":
        if p.poll() is None:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(p.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass  # the group is gone already
    if p.poll() is None:
        p.kill()


def run_hook(argv, timeout=None, cap=None):
    """(exit code, stdout bytes, stderr tail, why) of argv run in a fresh
    temporary folder; why is "" or what stopped it (the timeout, the cap).
    stdout is read while the hook runs, so a hook that prints too much is
    killed at the cap, not after it has filled the memory. The hook runs
    in its own process group (session on POSIX), and every way out (the
    timeout, the cap, Ctrl-C) kills the whole group."""
    timeout = timeout or TIMEOUT
    cap = cap or STDOUT_MAX
    out = []
    state = {"n": 0, "over": False}
    if os.name == "nt":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    tail = bytearray()
    with tempfile.TemporaryDirectory(prefix="xbr-hook-") as cwd:
        p = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **group,
        )

        def drain():
            # Only the last STDERR_TAIL bytes are kept: a hook that writes
            # gigabytes of errors costs neither memory nor disk.
            try:
                while True:
                    b = p.stderr.read1(CHUNK)
                    if not b:
                        return
                    tail.extend(b)
                    del tail[:-STDERR_TAIL]
            except (OSError, ValueError):
                return

        def pump():
            try:
                while True:
                    b = p.stdout.read1(CHUNK)
                    if not b:
                        return
                    state["n"] += len(b)
                    if state["n"] > cap:
                        state["over"] = True
                        return
                    out.append(b)
            except (OSError, ValueError):
                return  # the pipe was closed under the read after a kill

        t = threading.Thread(target=pump, daemon=True)
        e = threading.Thread(target=drain, daemon=True)
        why = ""
        try:
            t.start()
            e.start()
            t.join(timeout)
            if t.is_alive():
                why = "no index after %d s" % timeout
            elif state["over"]:
                why = "printed more than %d MB" % (cap >> 20)
            if not why:
                # stdout is closed: the hook is done, or about to be.
                try:
                    p.wait(timeout)
                except subprocess.TimeoutExpired:
                    why = "no exit after %d s" % timeout
        finally:
            # A hook that exited after closing stdout is left be; one that
            # timed out, overran or was interrupted goes with its children.
            if why or p.poll() is None:
                _kill_tree(p)
            p.stdout.close()
            t.join(5)
            rc = p.wait()
            e.join(5)
            p.stderr.close()
    return rc, b"".join(out), bytes(tail).decode("utf-8", "replace").strip(), why


def check_index(doc, size):
    """[Entry] of a protocol 1 index, or SourceError naming the first bad
    entry. size: the archive's, which every range must stay inside."""
    if not isinstance(doc, dict) or doc.get("xbr_archive_index") != PROTOCOL:
        raise SourceError("not an xbr archive index version %d" % PROTOCOL)
    rows = doc.get("entries")
    if not isinstance(rows, list):
        raise SourceError("the index has no entries list")
    out, seen = [], {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SourceError("entry %d is not an object" % i)
        name, off, n = r.get("name"), r.get("offset"), r.get("size")
        if not isinstance(name, str) or not name:
            raise SourceError("entry %d: name is not a non-empty string" % i)
        # bool is an int in Python: true would read as offset 1.
        for k, v in (("offset", off), ("size", n)):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise SourceError("entry %d (%s): %s is not a whole number >= 0" % (i, name, k))
        if off + n > size:
            raise SourceError(
                "entry %d (%s): %d bytes at %d run past the archive's %d" % (i, name, n, off, size)
            )
        e = Entry(name, off, n)
        if e.path in seen:
            raise SourceError("entry %d (%s): the same path as entry %d" % (i, name, seen[e.path]))
        seen[e.path] = i
        out.append(e)
    return out


class HookArchive:
    """One archive whose index a hook prints (protocol 1)."""

    def __init__(self, game_files, name, hook, repo):
        why = rel_path_problem(name)
        if why:
            raise SourceError("archive %r: %s" % (name, why))
        self.name = name
        self.path = os.path.abspath(resolve_nocase(game_files, name))
        if not os.path.isfile(self.path):
            raise SourceError("not a file")
        self.label = self.path
        self.hook = hook
        real = hook_path(repo, hook)
        size = os.path.getsize(self.path)
        rc, out, tail, why = run_hook([sys.executable, "-I", real, "index", self.path])
        what = "hook %s" % hook
        if why or rc:
            raise SourceError(
                "%s %s" % (what, why or "exited %d" % rc) + (":\n  " + tail if tail else "")
            )
        try:
            doc = json.loads(out)
        except ValueError as e:
            raise SourceError("%s: its output is not JSON (%s)" % (what, e)) from e
        try:
            self.entries = check_index(doc, size)
        except SourceError as e:
            raise SourceError("%s: %s" % (what, e)) from e

    def read(self, entry):
        try:
            with open(self.path, "rb") as f:
                f.seek(entry.offset)
                data = f.read(entry.size)
        except OSError as e:
            raise SourceError("%s: %s: %s" % (self.path, entry.name, e)) from e
        if len(data) != entry.size:
            raise SourceError(
                "%s: %s: %d of %d bytes" % (self.path, entry.name, len(data), entry.size)
            )
        return data


def label(game_files, name, cfg):
    """The path errors name for an archive before it is opened."""
    if cfg.get("source", "ipk") == "hook":
        return os.path.join(game_files, name)
    return os.path.join(game_files, name + ".iph")


def open_source(game_files, name, cfg, repo):
    """The archive `name` read by game.toml's [enhance.hud] source; OSError
    or SourceError when it cannot be opened."""
    if cfg.get("source", "ipk") == "hook":
        return HookArchive(game_files, name, cfg["hook"], repo)
    return IpkSource(game_files, name)
