"""The reference text: what cites a run (design D11 guard 3). A run, host
tree or worktree that any of it names is pinned and never deleted.

It is read over every game of benchdb.toml and every worktree of each
game's repository, and it fails closed: when git cannot list a game's
worktrees, or a required file is missing in a main checkout, the whole
plan is refused, because a partial reference text deletes evidence.

The matching is bench gc's: the full relpath, the run name, its stamp,
and the bare HHMMSS token of the stamp (which may over-protect; that is
intended). A name with no stamp (a worktree, a scratch dir) is pinned
when the text names it as a word."""

import fnmatch
import hashlib
import os
import re
import subprocess

from .store import DbError

STAMP_RE = re.compile(r"(?<!\d)(\d{8}-\d{6})(?!\d)")
SIX_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# ASCII only, as the host scripts' classes: a Unicode digit would pass \d
# and then fail strptime.
RUN_RE = re.compile(r"^([0-9]{8}-[0-9]{6})(-[A-Za-z0-9_.-]+)?$")
REF_SUFFIXES = (".md", ".json", ".txt")
SKIP_DIRS = {".git", "bench-logs", "third_party", ".venv", "node_modules"}
MAX_REF_FILE = 8 << 20
# The pipeline's stage outputs under pipeline.out (pipeline.py).
STAGE_DIRS = ("disasm", "func_id", "abi", "recomp", "ghidra")


def protection(text, names, include_named=False):
    """name -> reason for entries `text` protects by reference or by name.
    Without include_named, a name with no stamp is protected outright (gc's
    "named evidence")."""
    stamps = set(STAMP_RE.findall(text))
    six = set(SIX_RE.findall(text))
    hit = {}
    for n in names:
        m = STAMP_RE.search(n)
        if m:
            st = m.group(1)
            if st in stamps:
                hit[n] = "referenced"
            elif st[9:] in six:
                hit[n] = "referenced (HHMMSS)"
        elif not include_named:
            hit[n] = "named evidence"
        elif re.search(r"(?<![\w.-])" + re.escape(n) + r"(?![\w-])", text):
            hit[n] = "referenced (name)"
    return hit


def allocated(path):
    """Allocated bytes under path (st_blocks): sparse disk images in a run
    hold gigabytes of holes, which st_size counts. No link is followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512
    total = st.st_blocks * 512
    for d, dirs, files in os.walk(path, followlinks=False):
        for n in dirs + files:
            try:
                total += os.lstat(os.path.join(d, n)).st_blocks * 512
            except OSError:
                pass
    return total


def newest_mtime(path):
    """The newest mtime of path and everything under it, never following a
    link (a link counts by its own lstat)."""
    try:
        best = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    if os.path.islink(path) or not os.path.isdir(path):
        return best
    for d, dirs, files in os.walk(path, followlinks=False):
        for n in dirs + files:
            try:
                best = max(best, os.lstat(os.path.join(d, n)).st_mtime)
            except OSError:
                pass
    return best


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def worktrees(root):
    """The repository's worktrees, the main checkout first; refuses when
    git cannot list them (a partial reference text deletes evidence)."""
    r = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"], capture_output=True)
    if r.returncode != 0:
        raise DbError(
            "cannot list the worktrees of %s (%s); refusing: the reference text would be "
            "partial" % (root, r.stderr.decode(errors="replace").strip())
        )
    out = []
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("worktree "):
            out.append(line[len("worktree ") :])
    if not out:
        raise DbError("no worktrees listed for %s; refusing" % root)
    return out


class Refs:
    """The files read and their text."""

    def __init__(self):
        self.files = {}  # realpath -> text
        self.skipped_big = []
        self.missing = []
        self.worktrees = 0
        self.exclude = []  # realpaths that are never refs ([refs] exclude)

    def excluded(self, path):
        real = os.path.realpath(path)
        return any(real == x or real.startswith(x.rstrip(os.sep) + os.sep) for x in self.exclude)

    def add(self, path):
        real = os.path.realpath(path)
        if real in self.files or self.excluded(real):
            return
        # Fails closed: a source that cannot be read whole (a permission
        # error, EIO, a file gone mid-walk, one too big to hold) goes to
        # missing, which refuses the plan, never to a silent gap in the text.
        try:
            if os.path.getsize(real) > MAX_REF_FILE:
                self.skipped_big.append(os.path.normpath(path))
                self.missing.append(
                    "%s (over %d MB: list it in [refs] exclude or the game's refs_exclude)"
                    % (os.path.normpath(path), MAX_REF_FILE >> 20)
                )
                return
            with open(real, errors="replace") as f:
                self.files[real] = f.read()
        except OSError as e:
            self.missing.append("%s (%s)" % (os.path.normpath(path), e.strerror or e))

    def text(self):
        return "\n".join(self.files.values())

    def citing(self, name):
        """The files whose text alone protects name (empty: not pinned)."""
        out = []
        for path, text in self.files.items():
            if protection(text, [name], include_named=True):
                out.append(path)
        return out


def _excluded(path, patterns):
    p = os.path.normpath(path)
    return any(fnmatch.fnmatch(p, pat) for pat in patterns)


def build_dir(d, name):
    """A CMake build tree (build, build-win, ...): output, never prose. A
    spec dir named build-resources holds no CMakeCache.txt and is read."""
    return name.startswith("build") and os.path.isfile(os.path.join(d, name, "CMakeCache.txt"))


def _walk(top, suffixes, skip, excl, refs):
    def unreadable(e):
        refs.missing.append("%s (%s)" % (e.filename or top, e.strerror or e))

    for d, dirs, files in os.walk(top, onerror=unreadable, followlinks=False):
        dirs[:] = sorted(
            x
            for x in dirs
            if x not in SKIP_DIRS
            and not build_dir(d, x)
            and os.path.normpath(os.path.join(d, x)) not in skip
            and not refs.excluded(os.path.join(d, x))
        )
        for n in sorted(files):
            p = os.path.join(d, n)
            if os.path.islink(p) or (suffixes and not n.endswith(suffixes)):
                continue
            if not _excluded(p, excl):
                refs.add(p)


def read_refs(game, main_root, others, extra=(), refs=None):
    """Refs over the main checkout (every source required) and the other
    worktrees (missing paths skipped). game is a loaded manifest."""
    m = game.m
    gc = m["bench"]["gc"]
    refs = refs if refs is not None else Refs()
    for i, root in enumerate([main_root] + list(others)):
        main = i == 0
        refs.worktrees += 1
        excl = [os.path.normpath(os.path.join(root, pat)) for pat in gc["refs_exclude"]]
        # Generated output, never docs: gen/ and the pipeline's stage dirs,
        # whose hex and counts would protect runs by their HHMMSS at random.
        out = m["pipeline"]["out"]
        skip = {os.path.normpath(os.path.join(root, m["pipeline"]["gen"]))}
        skip |= {os.path.normpath(os.path.join(root, out, d)) for d in STAGE_DIRS}
        required = [m["golden"]["json"]] if m["golden"]["json"] else []
        required += [m["golden"]["audio"]] if m["golden"]["audio"] else []
        required += ["TASKS.md", "openspec"] + list(gc["refs"])
        # The pipeline's own inputs cite runs too: a seed's "source" names
        # the bench run that observed it.
        required += list(m["pipeline"]["seeds"])
        required += [m["pipeline"]["spin_waits"]] if m["pipeline"]["spin_waits"] else []
        for rel in required:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                if main:
                    refs.missing.append(p)
                continue
            if os.path.isdir(p):
                _walk(p, REF_SUFFIXES, skip, excl, refs)
            elif not _excluded(p, excl):
                refs.add(p)
        for n in sorted(os.listdir(root)) if os.path.isdir(root) else ():
            if n == "OPEN_QUESTIONS.md" or (n.startswith("RESUME") and n.endswith(".md")):
                refs.add(os.path.join(root, n))
    for e in extra:
        if os.path.isdir(e):
            _walk(e, None, set(), [], refs)
        elif os.path.isfile(e):
            refs.add(e)
        else:
            refs.missing.append(e)
    return refs


def protectors(refs, names):
    """{name: [files naming it]}, by protection()'s matching."""
    out = {}
    for path, text in refs.files.items():
        for n in protection(text, names):
            out.setdefault(n, []).append(path)
    return out


def collect(cfg):
    """The reference text of every [games] entry and every worktree of its
    repository, plus benchdb.toml's [refs] less its exclude. Raises
    DbError (failing closed) on a git or missing-file failure."""
    from .. import manifest

    refs = Refs()
    refs.exclude = [os.path.realpath(x) for x in cfg.refs_exclude]
    for slug, d in sorted(cfg.games.items()):
        try:
            g = manifest.load(d)
        except (manifest.ManifestError, OSError) as e:
            raise DbError(
                "game %s: %s; refusing: the reference text would be partial" % (slug, e)
            ) from e
        wts = worktrees(d)
        read_refs(g, wts[0], wts[1:], refs=refs)
    for e in cfg.refs:
        if os.path.isdir(e):
            _walk(e, None, set(), [], refs)
        elif os.path.isfile(e):
            refs.add(e)
        else:
            refs.missing.append(e)
    if refs.missing:
        raise DbError(
            "reference sources missing, refusing (a partial reference text deletes evidence): %s"
            % ", ".join(refs.missing)
        )
    return refs


def pulled_state(store, runs, files, sha):
    """{name: number of host files the local copy lacks} (0: a whole copy;
    None: no local copy, or another run-info.txt or no exit-code). runs is
    [{name}], files {name: [relpath]} and sha {name: run-info sha256}."""
    out = {}
    for r in runs:
        n = r["name"]
        loc = os.path.join(store, n)
        ri = os.path.join(loc, "run-info.txt")
        if not os.path.isdir(loc) or os.path.islink(loc):
            out[n] = None
            continue
        if sha.get(n) is None or not os.path.isfile(ri) or sha256_file(ri) != sha[n]:
            out[n] = None
            continue
        if not os.path.isfile(os.path.join(loc, "exit-code")):
            out[n] = None
            continue
        miss = 0
        for rel in files.get(n, []):
            if rel.startswith("frames/") or re.match(r"^steam-.*\.log$", rel):
                continue
            p = os.path.join(loc, rel)
            if not any(os.path.exists(p + s) for s in ("", ".zst", ".gz")):
                miss += 1
        out[n] = miss
    return out
