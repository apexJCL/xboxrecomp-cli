"""benchdb plan and clean (design D11): the one deletion path for derived
data, which agents run themselves.

`plan` lists what may go and why, and what is refused and why, and writes
the plan to <store>/clean-plans/<id>.json. `clean <id>` makes a fresh plan
under <store>/benchdb-clean.lock and deletes only what both plans hold
with the same size and newest mtime: the plan file sits in a writable dir,
so nothing in it is trusted but the paths.

The guards (D11.3), each checked at plan and again at clean time:
  1. the parent's realpath is under a deletable root, the leaf is examined
     with lstat, a link is only unlinked, any other leaf must be its own
     realpath; the deleted path is always realpath(parent)/name;
  2. the path (relative to its root) matches the root's pattern, is not
     under or over a [never] path, and is the user's when the root says own;
  3. nothing in the reference text cites it (refs.collect, failing closed);
  4. a worktree has no tracked changes, and every untracked file has the
     same bytes somewhere under notes/<name>/ (--force-dirty skips this);
  5. it has an exit-code if it is a run, and its newest mtime is older than
     the root's days (or --older-than);
  6. in the store, D10 decides: a superseded run loses its heavy files, and
     a run dir goes whole only when every row of it is unparsed;
  7. a host tree's bench-logs/<stamp> runs each have a whole local copy.
A refusal names the rule. Nothing is renamed, moved or relinked to get
past one."""

import json
import math
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import time
import unicodedata

from . import ingest, retain, store
from . import refs as refs_mod
from .store import DbError, flock, now_iso

PLANS = "clean-plans"
CLEAN_LOG = "clean.log"
CLEAN_LOCK = "benchdb-clean.lock"
PLAN_MAX_AGE = 3600
# Guard 5's least age for --older-than without --force-young, in days.
MIN_OLDER_THAN = 1.0
LARGE_BYTES = 100 * 10**9
LARGE_SHARE = 0.20
PLAN_ID_RE = re.compile(r"^[0-9]{14}-[a-z]{6}$")
# The store's own files: never a sweep's, never an explicit path's.
STORE_OWN = {
    "bench.db",
    "bench.db-wal",
    "bench.db-shm",
    "bench.db.lock",
    CLEAN_LOCK,
    CLEAN_LOG,
    PLANS,
    "db-backup",
    "inbox",
    "golden-sessions.tsv",
}
HOST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host")
LIST_TIMEOUT = 600
# The apply waits up to an hour for the run lock, then deletes.
APPLY_TIMEOUT = 3600 + 1800


# ---- the pin index ------------------------------------------------------------


class Pins:
    """Guard 3 over a Refs: a stamp, its HHMMSS token, or (for a name with
    no stamp) the name as a word. Each answer names the citing file."""

    def __init__(self, refs, base=None):
        self.refs = refs
        self.base = base
        self.stamp, self.six = {}, {}
        for path, text in refs.files.items():
            for s in refs_mod.STAMP_RE.findall(text):
                self.stamp.setdefault(s, path)
            for s in refs_mod.SIX_RE.findall(text):
                self.six.setdefault(s, path)

    def where(self, path):
        if self.base and path.startswith(self.base.rstrip(os.sep) + os.sep):
            return os.path.relpath(path, self.base)
        return path

    def source(self, names, words=True):
        """The citing file and match for the first of names that is cited,
        or None. words=False matches stamps only (and names by full text
        only when they hold a /, as a path)."""
        for n in names:
            if not n:
                continue
            m = refs_mod.STAMP_RE.search(n)
            if m:
                st = m.group(1)
                if st in self.stamp:
                    return "%s cites %s" % (self.where(self.stamp[st]), st)
                if st[9:] in self.six:
                    return "%s cites %s (HHMMSS)" % (self.where(self.six[st[9:]]), st[9:])
                continue
            if not words and "/" not in n:
                continue
            pat = re.compile(r"(?<![\w.-])" + re.escape(n) + r"(?![\w-])")
            for path, text in self.refs.files.items():
                if pat.search(text):
                    return "%s names %s" % (self.where(path), n)
        return None


# ---- helpers ------------------------------------------------------------------


def under(p, base):
    base = base.rstrip(os.sep) or os.sep
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


# ---- the floor (D11.2): the refusals no benchdb.toml lifts --------------------
#
# Each entry is (kind, path). "tree": the path, anything below it and
# anything above it. "self": the path and anything above it (what is below
# may go by the roots). "bench": a bench base, where only
# <bench>/<game>/runs/<name> and below may go; the base, its game_files and
# its lock stay whatever a root's pattern says.

BENCH_RUN_RE = re.compile(r"[^/]+/runs/[^/]+(/.*)?")


def local_floor(cfg):
    home = os.path.expanduser("~")
    out = [("self", p) for p in (home, "/", "/tmp", "/var/tmp", "/private/tmp")]
    out += [("self", "/private/var/tmp"), ("self", cfg.dir)]
    out += [("tree", os.path.join(home, n)) for n in ("Games", ".recomp-mac.lock")]
    out += [("tree", "/Applications"), ("bench", os.path.join(home, "xbr-bench"))]
    if cfg.path:
        out.append(("tree", cfg.path))
    if cfg.db:
        d = os.path.dirname(cfg.db)
        out.append(("self", d))
        out += [("tree", cfg.db + x) for x in ("", "-wal", "-shm", "-journal")]
        out += [("tree", os.path.join(d, n)) for n in ("bench.db.lock", "db-backup")]
    for p in list(cfg.games.values()) + [cfg.toolkit, cfg.cli]:
        if p:
            out.append(("tree", p))
    # A floor path is matched as given and as its real path: /tmp is
    # /private/tmp on macOS, and a checkout may be reached through a link.
    real = [(k, spelling(os.path.realpath(p))) for k, p in out]
    return out + [x for x in real if x not in out]


def host_floor(h, roots, extra=()):
    """A host's floor, with ~ for its home (check_entry fills it from the
    listing; the apply scripts from $HOME). extra: bench_env_floor's."""
    out = list(extra)
    if h.os != "windows":
        out += [("self", p) for p in ("~", "/", "/tmp", "/var/tmp")]
        out += [("tree", "~/" + n) for n in ("Games", ".recomp-run.lock", ".steam")]
        out += [("tree", "~/.local/share/Steam"), ("tree", "~/xbr-bench/.bench-lock")]
        return out + [("bench", "~/xbr-bench")]
    drives = {"C:", "D:"} | {r.path[:2].upper() for r in roots if r.path[1:2] == ":"}
    for d in sorted(drives):
        out += [("self", d + "\\"), ("self", d + "\\xbr"), ("self", d + "\\Users")]
        out += [("tree", d + "\\xbr\\.bench-lock"), ("tree", d + "\\Windows")]
        out += [("tree", d + "\\Program Files"), ("tree", d + "\\Program Files (x86)")]
        out.append(("bench", d + "\\xbr\\bench"))
    return out


def bench_env_floor(cfg):
    """{host key: floor} from each game's scripts/bench.env: the BENCH_DIR
    a bench ships to is a bench base wherever it is (~/blx-bench, a data
    drive), and its game files, prefix and lock stay whole. A bench.env
    that does not read refuses the plan, as a missing pin source does."""
    from ..bench import config as bench_config

    home = os.path.expanduser("~")
    out = {}
    for d in sorted(cfg.games.values()):
        p = os.path.join(d, "scripts", "bench.env")
        if not os.path.isfile(p):
            continue
        try:
            env = bench_config.read_env_file(p, home)
        except (OSError, UnicodeDecodeError, bench_config.ConfigError) as e:
            raise DbError("cannot read %s for the bench floor: %s" % (p, e)) from e
        key = cfg.host_of_alias(env.get("BENCH_HOST", ""))
        if key is None:
            continue
        win = cfg.hosts[key].os == "windows"

        def host_path(v, win=win):
            # An unquoted ~ was expanded to this machine's home: it is the host's.
            if v == home or v.startswith(home + "/"):
                v = "~" + v[len(home) :]
            return v.replace("/", "\\") if win else v.rstrip("/")

        fl = out.setdefault(key, [])
        if env.get("BENCH_DIR"):
            fl.append(("bench", host_path(env["BENCH_DIR"])))
        for k in ("BENCH_GAME_FILES", "BENCH_PREFIX", "BENCH_LOCK"):
            if env.get(k):
                fl.append(("tree", host_path(env[k])))
    return out


def floor_hit(floor, p, sep="/", fold=False):
    """The floor path p is refused by, or None."""
    a = p.lower() if fold else p
    for kind, n in floor:
        b = n.lower() if fold else n
        if under_text(b, a, sep):
            return n
        if kind == "tree" and under_text(a, b, sep):
            return n
        if kind == "bench" and under_text(a, b, sep):
            rest = a[len(b.rstrip(sep)) + 1 :].replace(sep, "/")
            if not BENCH_RUN_RE.fullmatch(rest):
                return n
    return None


def _fold(n):
    return unicodedata.normalize("NFC", n).casefold()


def spelling(p, cache=None):
    """p as the disk spells it, each name read back from its parent's
    listing. APFS is case- and normalisation-insensitive, and realpath
    keeps the spelling given: runs/b3-x would miss a [never] or a pin on
    runs/B3-X. A name not listed (gone, unreadable) is kept as given."""
    cache = {} if cache is None else cache
    out = os.sep if os.path.isabs(p) else ""
    for n in [x for x in p.split(os.sep) if x]:
        d = out or "."
        names = cache.get(d)
        if names is None:
            try:
                names = os.listdir(d)
            except OSError:
                names = []
            cache[d] = names
        if n not in names:
            hits = [x for x in names if _fold(x) == _fold(n)]
            if len(hits) == 1:
                n = hits[0]
        out = os.path.join(out, n)
    return out


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def sh_path(p):
    """A host path for bash: a leading ~ is the remote $HOME."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + sh_quote(p[2:])
    return sh_quote(p)


def sh_array(name, items):
    return "%s=(%s)\n" % (name, " ".join(items))


def ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def ps_array(items):
    return "@(%s)" % ", ".join(items)


def host_text(name):
    """A host script without its #@ header lines."""
    with open(os.path.join(HOST_DIR, name), encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(True)
    i = 0
    while i < len(lines) and lines[i].startswith("#@"):
        i += 1
    return "".join(lines[i:])


def plan_id(now=None):
    """The time and six letters: neither STAMP_RE nor SIX_RE finds a stamp
    in it, so a plan never pins what it names."""
    t = time.strftime("%Y%m%d%H%M%S", time.localtime(now))
    return "%s-%s" % (t, "".join(secrets.choice(string.ascii_lowercase) for _ in range(6)))


def agent():
    return os.environ.get("BENCHDB_AGENT") or os.environ.get("USER") or "unknown"


def sha_tree(d):
    """The sha256 of every regular file under d (no link followed)."""
    out = set()
    if not os.path.isdir(d):
        return out
    for dp, _dirs, files in os.walk(d, followlinks=False):
        for n in files:
            p = os.path.join(dp, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out.add(refs_mod.sha256_file(p))
    return out


def git_dirs(top):
    """The repository checkouts under a worktree dir (a .git file or dir),
    without entering them or any link."""
    out = []
    for dp, dirs, files in os.walk(top, followlinks=False):
        if ".git" in files or ".git" in dirs:
            out.append(dp)
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dp, d))]
    return out


def main_of(checkout):
    """The main checkout of a linked worktree, from its .git file."""
    g = os.path.join(checkout, ".git")
    if not os.path.isfile(g):
        return None
    with open(g, encoding="utf-8", errors="replace") as f:
        text = f.read()
    gitdir = text.split("gitdir:", 1)[-1].strip()
    if "/.git/worktrees/" not in gitdir:
        return None
    return gitdir.split("/.git/worktrees/")[0]


# Untracked or ignored paths that are regenerable output, never an agent's
# work. A build* dir counts only with a CMakeCache.txt in it (refs.build_dir):
# build-notes/ is prose.
BUILD_OUTPUT_RE = re.compile(
    r"(^|/)(\.venv[^/]*|__pycache__|node_modules|[^/]*\.egg-info|\.pytest_cache"
    r"|\.ruff_cache|\.mypy_cache|xcuserdata)(/|$)|(^|/)\.DS_Store$|\.py[co]$"
)


def build_output(d, path):
    path = path.rstrip("/")
    if BUILD_OUTPUT_RE.search(path):
        return True
    parts = path.split("/")
    return any(refs_mod.build_dir(os.path.join(d, *parts[:i]), parts[i]) for i in range(len(parts)))


def pipeline_output(d):
    """A game checkout's regenerable pipeline dirs (gen and the stage
    outputs), relative to it with a trailing /; none for another repo."""
    if not os.path.isfile(os.path.join(d, "game.toml")):
        return ()
    from .. import manifest

    try:
        m = manifest.load(d).m
    except Exception:
        return ()
    out = [m["pipeline"]["gen"]] + [m["pipeline"]["out"] + "/" + x for x in refs_mod.STAGE_DIRS]
    return tuple(x.strip("/") + "/" for x in out)


def repo_state(d):
    """One checkout's porcelain: (paths to keep, nested checkouts), or why
    it must stay. -z keeps a name git would C-quote (résumé.md) as bytes."""
    st = subprocess.run(
        ["git", "-C", d, "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        + ["--ignored=matching"],
        capture_output=True,
    )
    if st.returncode != 0:
        return "git status failed in %s" % d
    regen = pipeline_output(d)
    keep, nested = [], []
    parts = st.stdout.split(b"\0")
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if not e:
            continue
        xy, path = e[:2].decode(errors="replace"), os.fsdecode(e[3:])
        if xy[0] in "RC":
            i += 1  # the source path of a rename or copy follows
        if xy not in ("??", "!!"):
            return "uncommitted changes in %s (%s %s)" % (d, xy, path)
        if build_output(d, path) or path.startswith(regen):
            continue
        p = os.path.join(d, path)
        if os.path.isdir(p) and not os.path.islink(p) and os.path.lexists(os.path.join(p, ".git")):
            nested.append(os.path.normpath(p))
            continue
        keep.append(path.rstrip("/") if path.endswith("/") else path)
    # A HEAD no branch, tag or remote holds (a detached commit) is lost
    # with the worktree; a branch survives git worktree remove.
    head = subprocess.run(
        ["git", "-C", d, "rev-parse", "-q", "--verify", "HEAD"], capture_output=True
    )
    if head.returncode == 0:
        refs = subprocess.run(
            ["git", "-C", d, "for-each-ref", "--contains", "HEAD", "--count=1"]
            + ["refs/heads", "refs/tags", "refs/remotes"],
            capture_output=True,
        )
        if refs.returncode != 0:
            return "git for-each-ref failed in %s" % d
        if not refs.stdout.strip():
            return "HEAD %s in %s is on no branch or tag (its commits would be lost)" % (
                head.stdout.decode().strip()[:12],
                d,
            )
    return keep, nested


def regular_files(p):
    """The regular files at or under p, no link followed or listed, and the
    checkouts (a dir holding .git) found inside, which are judged as their
    own repos."""
    if os.path.islink(p):
        return [], []
    if os.path.isfile(p):
        return [p], []
    out, nested = [], []
    for dp, dirs, files in os.walk(p, followlinks=False):
        if dp != p and os.path.lexists(os.path.join(dp, ".git")):
            nested.append(os.path.normpath(dp))
            dirs[:] = []
            continue
        for n in files:
            f = os.path.join(dp, n)
            if os.path.isfile(f) and not os.path.islink(f):
                out.append(f)
    return out, nested


def same_file(a, b):
    try:
        if os.path.islink(b) or os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    return refs_mod.sha256_file(a) == refs_mod.sha256_file(b)


def make_writable(top):
    """u+w on every entry that is not a link, never following one: an old
    read-only game_files copy goes with its tree, and a link to a read-only
    dir elsewhere leaves that dir as it was."""
    try:
        st = os.lstat(top)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    os.chmod(top, st.st_mode | stat.S_IWUSR, follow_symlinks=False)
    for dp, dirs, files in os.walk(top, followlinks=False):
        for n in dirs + files:
            p = os.path.join(dp, n)
            try:
                s = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISLNK(s.st_mode):
                os.chmod(p, s.st_mode | stat.S_IWUSR, follow_symlinks=False)


def disk(path):
    try:
        u = shutil.disk_usage(path)
        return u.total, u.free
    except OSError:
        return 0, 0


# ---- the plan -----------------------------------------------------------------


class Planner:
    """One plan's state: the config, the db, the pins, the items and the
    refusals."""

    def __init__(self, cfg, db, older_than=None, force_dirty=False, now=None, refs=None):
        self.cfg, self.db = cfg, db
        self.older_than = older_than
        self.force_dirty = force_dirty
        self.now = now if now is not None else time.time()
        self.refs = refs if refs is not None else refs_mod.collect(cfg)
        self.pins = Pins(self.refs, cfg.dir)
        self.items, self.refused = [], []
        self.seen = set()
        self.disks = {}
        self._decide = None
        self._gc_days = None
        self._floor = None
        self._names = {}
        self._bench_floor = None

    # ---- results -----------------------------------------------------------

    def add(self, root, path, rel, kind, size, mtime, rule, host="local"):
        if (host, path) in self.seen:
            return
        self.seen.add((host, path))
        self.items.append(
            dict(
                root=root.name,
                host=host,
                path=path,
                rel=rel,
                kind=kind,
                bytes=int(size),
                mtime=int(mtime),
                rule=rule,
                age=int(self.days(root) * 86400),
            )
        )

    def refuse(self, path, why, root=None, host="local"):
        if (host, path) in self.seen:
            return
        self.seen.add((host, path))
        self.refused.append(dict(root=root.name if root else None, host=host, path=path, why=why))

    def days(self, root):
        """Guard 5's age: --older-than, else the root's days, else 2 for a
        runs root and the longest [bench.gc] days of the games (7 with no
        game) for the store and the hosts."""
        if self.older_than is not None:
            return float(self.older_than)
        if root.days is not None:
            return root.days
        if root.kind == "runs":
            return 2.0
        if self._gc_days is None:
            ds = [
                float(ingest.settings(self.cfg, g)["bench"]["gc"]["days"]) for g in self.cfg.games
            ]
            self._gc_days = max(ds) if ds else 7.0
        return self._gc_days

    def young(self, root, mtime):
        d = self.days(root)
        return mtime > self.now - d * 86400, d

    # ---- local roots --------------------------------------------------------

    def local_roots(self):
        out = []
        for r in self.cfg.roots.values():
            if r.local:
                out.append((os.path.realpath(r.path), r))
        # The deepest root first: a store inside a runs root is the store.
        out.sort(key=lambda x: len(x[0]), reverse=True)
        return out

    def resolve(self, path):
        """Guard 1 for a local path: (root, resolved path, rel, lstat) or a
        refusal reason."""
        if ".." in path.replace("\\", "/").split("/"):
            return None, "a .. in the path"
        path = os.path.abspath(os.path.expanduser(path))
        parent = os.path.realpath(os.path.dirname(path))
        name = os.path.basename(path)
        res = os.path.join(parent, name)
        # Before any root, pattern, never or pin check, all of which compare
        # spellings: a variant would slip past every one of them.
        disk = spelling(res, self._names)
        if disk != res and os.path.lexists(res):
            return None, "spelled %s on disk" % disk
        root = None
        for base, r in self.local_roots():
            if res == base:
                return None, "the root %s itself" % r.name
            if under(parent, base):
                root = (base, r)
                break
        if root is None:
            return None, "outside the deletable roots"
        base, r = root
        if not r.deletable:
            return None, "under the reference-only root %s (%s)" % (r.name, r.kind)
        try:
            st = os.lstat(res)
        except OSError:
            return None, "does not exist"
        if not stat.S_ISLNK(st.st_mode) and os.path.realpath(res) != res:
            return None, "its real path is %s, not %s" % (os.path.realpath(res), res)
        rel = os.path.relpath(res, base).replace(os.sep, "/")
        return (r, res, rel, st), None

    def never_hit(self, host, path):
        """The never-path a local path is under or over: the built-in floor
        first, which no config lifts, then the config's [never]."""
        if self._floor is None:
            self._floor = local_floor(self.cfg)
        hit = floor_hit(self._floor, path)
        if hit:
            return "%s (built in)" % hit
        for n in self.cfg.never.get(host, []):
            nr = spelling(os.path.realpath(n), self._names) if host == "local" else n
            if under(path, nr) or under(nr, path):
                return n
        return None

    def check_local(self, path):
        """Every guard for one local path; adds an item or a refusal."""
        got, why = self.resolve(path)
        if got is None:
            # A .. path is shown as given: normalised, it would name another path.
            dotdot = ".." in path.replace("\\", "/").split("/")
            self.refuse(path if dotdot else os.path.abspath(path), why)
            return
        r, res, rel, st = got
        kind = "link" if stat.S_ISLNK(st.st_mode) else None
        first = rel.split("/")[0]
        if r.kind == "store" and first in STORE_OWN:
            if first == PLANS and rel.count("/") == 1 and kind is None:
                return self._old_plan(r, res, rel, st)
            self.refuse(res, "the store's own %s" % first, r)
            return
        if not r.matches(rel) and not (kind == "link" and r.kind == "store" and "/" not in rel):
            self.refuse(res, "%s does not match the %s root's pattern" % (rel, r.name), r)
            return
        hit = self.never_hit("local", res)
        if hit:
            self.refuse(res, "under or over the never-path %s" % hit, r)
            return
        if r.own and st.st_uid != os.getuid():
            self.refuse(res, "owned by uid %d, not this user" % st.st_uid, r)
            return
        if kind == "link":
            # A link is only unlinked and its target stays, so no tree rule
            # applies; but its own name can be cited (a link other tools
            # rely on), and one made just now is still in use.
            names = [rel, os.path.basename(res), os.path.relpath(res, self.cfg.dir)]
            src = self.pins.source(names, words=r.kind not in ("scratch",))
            if src:
                self.refuse(res, "pinned: %s" % src, r)
                return
            y, d = self.young(r, st.st_mtime)
            if y:
                self.refuse(res, "a link made within %g days" % d, r)
                return
            self.add(r, res, rel, "link", 0, st.st_mtime, "a link (unlinked, target kept)")
            return
        if r.kind == "worktrees":
            kind = "worktree"
            names = [rel, os.path.relpath(res, self.cfg.dir), res]
            src = self.pins.source(names, words=False)
        else:
            kind = "tree" if stat.S_ISDIR(st.st_mode) else "file"
            names = [rel, os.path.basename(res), os.path.relpath(res, self.cfg.dir)]
            src = self.pins.source(names, words=r.kind not in ("scratch",))
        if not src and kind == "tree":
            src = self.nested_pin(res, r)
        if src:
            self.refuse(res, "pinned: %s" % src, r)
            return
        if kind == "worktree" and not self.force_dirty:
            why = self.dirty(res)
            if why:
                self.refuse(res, why, r)
                return
        if r.kind == "store":
            why = self.store_dir(res, rel)
            if why:
                self.refuse(res, why, r)
                return
        if kind == "tree" and os.path.isfile(os.path.join(res, "run-info.txt")):
            if not os.path.isfile(os.path.join(res, "exit-code")):
                self.refuse(res, "a run with no exit-code (being written)", r)
                return
        mt = refs_mod.newest_mtime(res)
        y, d = self.young(r, mt)
        if y:
            self.refuse(res, "written within %g days" % d, r)
            return
        if kind == "worktree":
            rule = "a worktree with its notes kept, older than %g days" % d
        elif r.kind == "store" and self.has_rows(rel):
            rule = "an unparsed run older than %g days" % d
        else:
            rule = "older than %g days" % d
        self.add(r, res, rel, kind, refs_mod.allocated(res), mt, rule)

    def nested_pin(self, res, r):
        """Guard 3 below a tree: a topic dir (runs/ab-fade, a store copy)
        holds runs whose stamps a note cites while its own name cites
        nothing. Any stamped dir or file inside that is cited pins it all."""
        base = os.path.realpath(r.path)
        for dp, dirs, files in os.walk(res, followlinks=False):
            for n in dirs + files:
                if not refs_mod.STAMP_RE.search(n):
                    continue
                p = os.path.join(dp, n)
                src = self.pins.source(
                    [n, os.path.relpath(p, base), os.path.relpath(p, self.cfg.dir)],
                    words=False,
                )
                if src:
                    return "%s (in %s)" % (src, os.path.relpath(p, res))
        return None

    def _old_plan(self, r, res, rel, st):
        y, d = self.young(r, st.st_mtime)
        if y:
            self.refuse(res, "a plan written within %g days" % d, r)
            return
        self.add(r, res, rel, "plan", st.st_blocks * 512, st.st_mtime, "an old plan")

    def store_dir(self, res, rel):
        """Guard 6 for a whole store entry: None when it may go whole (an
        unparsed run, or a topic copy whose runs are all gone), else why."""
        like = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
        rows = self.db.sql(
            "SELECT status FROM runs WHERE relpath = ? OR relpath LIKE ? ESCAPE '\\'",
            (rel, like),
        )
        if rows and all(x["status"] == "unparsed" for x in rows):
            return None
        if rows:
            return "a run in the db: its large files go by retention (D10), its rows stay"
        if refs_mod.RUN_RE.match(os.path.basename(res)):
            return "a run not in the db: benchdb import first"
        if os.path.isdir(res) and not os.path.islink(res):
            for dp, dirs, _files in os.walk(res, followlinks=False):
                for n in dirs:
                    if refs_mod.RUN_RE.match(n):
                        return (
                            "holds runs not in the db (%s): benchdb import first"
                            % os.path.relpath(os.path.join(dp, n), res)
                        )
        return None

    def dirty(self, top):
        """Guard 4: why a worktree dir must stay, or None. Every file the
        delete would take must be committed on a branch, regenerable build
        output, or a copy (by sha) of a file under notes/<name>/ or of the
        same path in the main checkout."""
        notes = None
        for r in self.cfg.roots.values():
            if r.local and r.kind == "notes":
                notes = r.path
                break
        name = os.path.basename(top)
        have = None
        lost = []
        checkouts = git_dirs(top)
        queue = list(checkouts)
        while queue:
            d = queue.pop(0)
            got = repo_state(d)
            if isinstance(got, str):
                return got
            keep, nested = got
            queue += nested
            checkouts += nested
            main = main_of(d)
            regen = pipeline_output(d)
            for rel in keep:
                p = os.path.join(d, rel)
                if not os.path.lexists(p):
                    return "cannot read an untracked name in %s: %r" % (d, rel)
                files, more = regular_files(p)
                queue += more
                checkouts += more
                for f in files:
                    frel = os.path.relpath(f, d).replace(os.sep, "/")
                    # An ignored src/recomp/ holds gen/: judged file by file.
                    if build_output(d, frel) or frel.startswith(regen):
                        continue
                    if main and same_file(f, os.path.join(main, frel)):
                        continue
                    if have is None:
                        have = sha_tree(os.path.join(notes, name)) if notes else set()
                    if refs_mod.sha256_file(f) not in have:
                        lost.append(os.path.relpath(f, top))
        # Files beside the checkouts (wt/<name>/NOTES.md, a scratch dir) are
        # in no repo: they are kept only as copies under notes/<name>/.
        inside = {os.path.normpath(c) for c in checkouts}
        for dp, dirs, files in os.walk(top, followlinks=False):
            dirs[:] = [x for x in dirs if os.path.normpath(os.path.join(dp, x)) not in inside]
            for n in files:
                f = os.path.join(dp, n)
                if os.path.islink(f) or not os.path.isfile(f):
                    continue
                if have is None:
                    have = sha_tree(os.path.join(notes, name)) if notes else set()
                if refs_mod.sha256_file(f) not in have:
                    lost.append(os.path.relpath(f, top))
        if lost:
            return "untracked files not under notes/%s/: %s" % (
                name,
                ", ".join(sorted(lost)[:8]) + (" ..." if len(lost) > 8 else ""),
            )
        return None

    # ---- store sweeps and retention ----------------------------------------

    def decisions(self):
        if self._decide is None:

            def pinned(r):
                return self.pins.source([r["relpath"], r["relpath"].split("/")[-1]])

            self._decide = retain.decide(self.db, self.cfg, pinned=pinned, now=self.now)
        return self._decide

    def sweep_local(self, r):
        base = os.path.realpath(r.path)
        if not os.path.isdir(base):
            self.refuse(base, "the root %s does not exist" % r.name, r)
            return
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            if r.kind == "store":
                if n == PLANS and os.path.isdir(p) and not os.path.islink(p):
                    for x in sorted(os.listdir(p)):
                        self.check_local(os.path.join(p, x))
                    continue
                if n in STORE_OWN:
                    continue
                if not os.path.islink(p) and r.matches(n) and self.has_rows(n):
                    # A run in the db is retention's (below), not a whole delete.
                    if not self.all_unparsed(n):
                        continue
            elif not r.matches(n) and not os.path.islink(p):
                continue
            self.check_local(p)
        if r.kind == "store":
            self.retention(r, base)

    def has_rows(self, rel):
        return bool(self.db.sql("SELECT 1 FROM runs WHERE relpath = ? LIMIT 1", (rel,)))

    def all_unparsed(self, rel):
        rows = self.db.sql("SELECT status FROM runs WHERE relpath = ?", (rel,))
        return all(x["status"] == "unparsed" for x in rows)

    def retention(self, r, base):
        """Guard 6: the heavy files of the runs D10 lets go."""
        dec = self.decisions()
        gone = [rid for rid, (keep, _why) in dec.items() if not keep]
        if not gone:
            return
        q = (
            "SELECT a.relpath, a.kind, a.bytes, r.relpath AS run FROM artifacts a "
            "JOIN runs r ON r.id = a.run_id WHERE a.root = ? AND a.heavy = 1 "
            "AND a.state = 'present' AND a.run_id IN (%s) ORDER BY a.relpath"
        )
        run_mt = {}
        for i in range(0, len(gone), 500):
            chunk = gone[i : i + 500]
            for a in self.db.sql(q % ",".join("?" * len(chunk)), [r.name] + chunk):
                p = os.path.join(base, *a["relpath"].split("/"))
                # Guard 5 on the whole run: its start can be old while a
                # re-pull, a re-judge or a copied-in dump wrote it today.
                run = os.path.join(base, *a["run"].split("/"))
                if run not in run_mt:
                    run_mt[run] = refs_mod.newest_mtime(run) if os.path.isdir(run) else 0
                y, d = self.young(r, run_mt[run])
                if y:
                    self.refuse(p, "its run was written within %g days" % d, r)
                    continue
                got, why = self.resolve(p)
                if got is None:
                    if why != "does not exist":
                        self.refuse(p, why, r)
                    continue
                rr, res, rel, st = got
                if rr.name != r.name or not r.matches(rel.split("/")[0]):
                    self.refuse(res, "not a run's file in the %s root" % r.name, r)
                    continue
                hit = self.never_hit("local", res)
                if hit:
                    self.refuse(res, "under or over the never-path %s" % hit, r)
                    continue
                kind = "link" if stat.S_ISLNK(st.st_mode) else "heavy"
                mt = st.st_mtime if kind == "link" else refs_mod.newest_mtime(res)
                size = 0 if kind == "link" else refs_mod.allocated(res)
                self.add(r, res, rel, kind, size, mt, "superseded (D10): %s" % a["kind"])

    # ---- remote roots ------------------------------------------------------

    def host_roots(self, key):
        return [r for r in self.cfg.roots.values() if r.host == key]

    def check_remote(self, key, paths, sweep):
        """Guards for one host: its listing, then each entry. paths are the
        explicit host paths; sweep the deletable roots to list."""
        h = self.cfg.hosts[key]
        roots = [r for r in self.host_roots(key) if r.deletable]
        if not roots:
            for p in paths:
                self.refuse(p, "host %s has no deletable root" % key, host=key)
            return
        for p in paths:
            if ".." in p.replace("\\", "/").split("/"):
                self.refuse(p, "a .. in the path", host=key)
        paths = [p for p in paths if ".." not in p.replace("\\", "/").split("/")]
        try:
            rc, text = run_host(h, list_script(h, roots, sweep, paths))
        except (OSError, subprocess.SubprocessError, DbError) as e:
            rc, text = -1, str(e)
        if rc != 0:
            why = "host %s not reached (it is down, or up on the other OS)" % key
            for p in paths or [r.path for r in sweep]:
                self.refuse(p, why, host=key)
            return
        listing = parse_listing(text)
        resolved = {}
        for i, r in enumerate(roots):
            t = listing["roots"].get(i)
            if t:
                resolved[r.name] = t
                self.disks[(key, r.name)] = (t["kb"] * 1024, t["avail"] * 1024)
        for p in listing["missing"]:
            self.refuse(p, "does not exist", host=key)
        for p in listing["bad"]:
            self.refuse(p, "a tab or newline in the name", host=key)
        for e in listing["entries"]:
            # With no sweep, only the paths asked for: a listing line for
            # anything else is the host script misbehaving, never a target.
            if not sweep and e["given"] not in paths:
                continue
            self.check_entry(key, h, roots, resolved, e, listing)

    def check_entry(self, key, h, roots, resolved, e, listing):
        res = e["resolved"]
        win = h.os == "windows"
        sep = "\\" if win else "/"
        root = None
        for r in sorted(
            roots, key=lambda x: len(resolved.get(x.name, {}).get("path", "")), reverse=True
        ):
            base = resolved.get(r.name, {}).get("path")
            if not base:
                continue
            if res.lower() == base.lower() if win else res == base:
                self.refuse(res, "the root %s itself" % r.name, host=key)
                return
            pref = base.rstrip(sep) + sep
            if res.lower().startswith(pref.lower()) if win else res.startswith(pref):
                root = r
                break
        if root is None:
            self.refuse(res, "outside host %s's deletable roots" % key, host=key)
            return
        base = resolved[root.name]["path"]
        rel = res[len(base.rstrip(sep)) + 1 :].replace("\\", "/")
        if e["type"] != "l" and e["real"] != res:
            self.refuse(res, "its real path is %s, not %s" % (e["real"], res), root, key)
            return
        if not root.matches(rel):
            self.refuse(
                res, "%s does not match the %s root's pattern" % (rel, root.name), root, key
            )
            return
        home = listing.get("home", "~")
        if not win and not home.startswith("/"):
            self.refuse(res, "host %s's listing has no home" % key, root, key)
            return
        if self._bench_floor is None:
            self._bench_floor = bench_env_floor(self.cfg)
        floor = [
            (k, home + n[1:] if n.startswith("~") else n)
            for k, n in host_floor(h, roots, self._bench_floor.get(key, []))
        ]
        hit = floor_hit(floor, res, sep, fold=win)
        if hit:
            self.refuse(res, "under or over the never-path %s (built in)" % hit, root, key)
            return
        for n in self.cfg.never.get(key, []):
            nn = n.replace("~", home, 1) if n.startswith("~") else n
            a, b = (res.lower(), nn.lower()) if win else (res, nn)
            if under_text(a, b, sep) or under_text(b, a, sep):
                self.refuse(res, "under or over the never-path %s" % n, root, key)
                return
        if root.own and e["uid"] not in ("-", listing["uid"]):
            self.refuse(res, "owned by uid %s, not this user" % e["uid"], root, key)
            return
        stamps = listing["stamps"].get(res, {})
        names = [rel, rel.split("/")[-1]] + list(stamps)
        src = self.pins.source(names)
        if src:
            self.refuse(res, "pinned: %s" % src, root, key)
            return
        if e["type"] == "l":
            # As locally: the name is pin-checked above, the target stays.
            y, d = self.young(root, e["mtime"])
            if y:
                self.refuse(res, "a link made within %g days" % d, root, key)
                return
            self.add(root, res, rel, "link", 0, e["mtime"], "a link (unlinked)", host=key)
            return
        for st, s in sorted(stamps.items()):
            if not s["exit"]:
                self.refuse(res, "bench-logs/%s has no exit-code (being written)" % st, root, key)
                return
        if stamps:
            store = self.cfg.store()
            if store is None:
                self.refuse(res, "no local store to check its runs were pulled", root, key)
                return
            state = refs_mod.pulled_state(
                store.path,
                [{"name": n} for n in stamps],
                {n: listing["files"].get((res, n), []) for n in stamps},
                {n: s["sha"] for n, s in stamps.items()},
            )
            short = sorted(n for n, v in state.items() if v != 0)
            if short:
                self.refuse(
                    res,
                    "runs not pulled whole to the store: %s" % ", ".join(short[:6]),
                    root,
                    key,
                )
                return
        y = e["mtime"] > listing["now"] - self.days(root) * 86400
        if y:
            self.refuse(res, "written within %g days" % self.days(root), root, key)
            return
        self.add(
            root,
            res,
            rel,
            "tree" if e["type"] == "d" else "file",
            e["kb"] * 1024,
            e["mtime"],
            "older than %g days" % self.days(root)
            + (", its %d runs pulled whole" % len(stamps) if stamps else ""),
            host=key,
        )

    def result(self, args):
        total = sum(i["bytes"] for i in self.items)
        return dict(
            id=None,
            made=now_iso(),
            made_ts=self.now,
            args=args,
            items=self.items,
            refused=self.refused,
            bytes=total,
        )


def under_text(p, base, sep):
    base = base.rstrip(sep)
    return p == base or p.startswith(base + sep)


# ---- host scripts -------------------------------------------------------------


def list_script(h, roots, sweep, paths):
    """(command, stdin) of the host listing."""
    if h.os == "windows":
        pre = "$Roots = %s\n$Sweep = %s\n$Paths = %s\n" % (
            ps_array(ps_quote(r.path) for r in roots),
            ps_array("1" if r in sweep else "0" for r in roots),
            ps_array(ps_quote(p) for p in paths),
        )
        return ps_stdin(), host_text("clean_lib.ps1") + pre + host_text("clean_list.ps1")
    pre = sh_array("ROOTS", [sh_path(r.path) for r in roots])
    pre += sh_array("SWEEP", ["1" if r in sweep else "0" for r in roots])
    pre += sh_array("PATHS", [sh_path(p) for p in paths])
    pre += "home=$HOME\nprintf 'Z\\t%s\\n' \"$home\"\n"
    return "bash -s", pre + host_text("clean_list.sh")


def apply_script(h, items, roots, never, owner, extra_floor=()):
    """(command, stdin) of the host deletes for items (one host's)."""
    idx = {r.name: i for i, r in enumerate(roots)}
    floor = host_floor(h, roots, extra_floor)

    def floor_of(kind):
        return [n for k, n in floor if k == kind]

    if h.os == "windows":
        rows = []
        for it in items:
            r = roots[idx[it["root"]]]
            pat = "|".join(p.pattern for p in r.patterns)
            rows.append(
                "@{p=%s; r=%d; pat=%s; m=%d; age=%d}"
                % (ps_quote(it["path"]), idx[it["root"]], ps_quote(pat), it["mtime"], it["age"])
            )
        pre = "$Lock = %s\n$Owner = %s\n$Roots = %s\n$Never = %s\n" % (
            ps_quote(lock_path(h, roots)),
            ps_quote(owner),
            ps_array(ps_quote(r.path) for r in roots),
            ps_array(ps_quote(n) for n in list(never) + floor_of("tree")),
        )
        pre += "$NeverSelf = %s\n$Bench = %s\n$Items = %s\n" % (
            ps_array(ps_quote(n) for n in floor_of("self")),
            ps_array(ps_quote(n) for n in floor_of("bench")),
            ps_array(rows),
        )
        return ps_stdin(), host_text("clean_lib.ps1") + pre + host_text("clean_apply.ps1")
    pre = sh_array("ROOTS", [sh_path(r.path) for r in roots])
    pre += sh_array("NEVER", [sh_path(n) for n in list(never) + floor_of("tree")])
    pre += sh_array("NEVER_SELF", [sh_path(n) for n in floor_of("self")])
    pre += sh_array("BENCH", [sh_path(n) for n in floor_of("bench")])
    cols = {"P": [], "R": [], "PAT": [], "M": [], "AGE": [], "OWN": []}
    for it in items:
        r = roots[idx[it["root"]]]
        cols["P"].append(sh_quote(it["path"]))
        cols["R"].append(str(idx[it["root"]]))
        cols["PAT"].append(sh_quote("|".join(p.pattern for p in r.patterns)))
        cols["M"].append(str(it["mtime"]))
        cols["AGE"].append(str(it["age"]))
        cols["OWN"].append("1" if r.own else "0")
    for k, v in cols.items():
        pre += sh_array(k, v)
    return "bash -s", pre + host_text("clean_apply.sh")


def lock_path(h, roots):
    """The PC's run lock: D:\\xbr\\.bench-lock, beside its bench tree (the
    drive of the first root)."""
    drive = roots[0].path[:2] if roots and roots[0].path[1:2] == ":" else "D:"
    return drive + "\\xbr\\.bench-lock"


def ps_stdin():
    """cmd.exe runs this; it reads the script from stdin and runs it, so
    no command line holds the paths (and its length limit never bites)."""
    import base64

    boot = "$s = [Console]::In.ReadToEnd(); & ([scriptblock]::Create($s))"
    return (
        "powershell -NoProfile -NonInteractive -EncodedCommand %s"
        % base64.b64encode(boot.encode("utf-16-le")).decode()
    )


def run_host(h, cmd_stdin, timeout=LIST_TIMEOUT):
    """(rc, stdout) of a host script sent on ssh's stdin; a module function
    so the tests put a fake in its place."""
    command, script = cmd_stdin
    s = shutil.which("ssh")
    if not s:
        raise DbError("no ssh on PATH")
    r = subprocess.run(
        [s, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", h.ssh, command],
        input=script.encode(),
        capture_output=True,
        timeout=timeout,
    )
    return r.returncode, r.stdout.decode(errors="replace")


def parse_listing(text):
    out = dict(
        uid="-",
        now=time.time(),
        home="~",
        roots={},
        entries=[],
        stamps={},
        files={},
        missing=[],
        bad=[],
    )
    for line in text.splitlines():
        f = line.split("\t")
        tag = f[0]
        try:
            if tag == "H" and len(f) == 3:
                out["uid"], out["now"] = f[1], float(f[2])
            elif tag == "Z" and len(f) == 2:
                out["home"] = f[1]
            elif tag == "T" and len(f) == 5:
                out["roots"][int(f[1])] = dict(path=f[2], kb=int(f[3]), avail=int(f[4]))
            elif tag == "E" and len(f) == 8:
                out["entries"].append(
                    dict(
                        given=f[1],
                        resolved=f[2],
                        real=f[3],
                        type=f[4],
                        uid=f[5],
                        mtime=int(f[6] or 0),
                        kb=int(f[7] or 0),
                    )
                )
            elif tag == "S" and len(f) == 5:
                out["stamps"].setdefault(f[1], {})[f[2]] = dict(
                    sha=None if f[3] == "-" else f[3], exit=f[4] == "1"
                )
            elif tag == "F" and len(f) == 4:
                out["files"].setdefault((f[1], f[2]), []).append(f[3])
            elif tag == "M" and len(f) == 2:
                out["missing"].append(f[1])
            elif tag == "B" and len(f) == 2:
                out["bad"].append(f[1])
        except ValueError:
            continue
    return out


def parse_apply(text):
    """[(result, path, why)] of an apply script's lines."""
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if f[0] in ("deleted", "skipped", "failed") and len(f) >= 2:
            out.append((f[0], f[1], f[2] if len(f) > 2 else ""))
    return out


# ---- the verbs' bodies --------------------------------------------------------


def split_targets(cfg, targets):
    """(root names, local paths, {host: [paths]}) of plan's arguments: a
    root's name, host:path for a [hosts] key or ssh alias, else a local
    path."""
    roots, local, remote = [], [], {}
    for t in targets:
        if t in cfg.roots and not os.path.lexists(t):
            roots.append(t)
            continue
        head, sep, tail = t.partition(":")
        key = cfg.host_of_alias(head) if sep and len(head) > 1 else None
        if key and tail:
            remote.setdefault(key, []).append(tail)
        else:
            local.append(t)
    return roots, local, remote


def check_older_than(days, force_young):
    """--older-than below MIN_OLDER_THAN days would let guard 5 take a run
    being written or one ship touched seconds ago: only with --force-young.
    Never below 0 or not a number."""
    if days is None:
        return None
    try:
        days = float(days)
    except (TypeError, ValueError):
        raise DbError("--older-than %r is not a number of days" % (days,)) from None
    if not math.isfinite(days) or days < 0:
        raise DbError("--older-than %g: the days must be 0 or more" % days)
    if days < MIN_OLDER_THAN and not force_young:
        raise DbError(
            "--older-than %g is under the %g-day floor (a run being written or shipped "
            "would go): pass --force-young after checking" % (days, MIN_OLDER_THAN)
        )
    return days


def make_plan(
    cfg,
    db,
    targets=(),
    older_than=None,
    force_dirty=False,
    now=None,
    refs=None,
    force_young=False,
):
    """The plan of targets (as split_targets reads them); with none, the
    local store and runs roots. Raises DbError when the reference text
    cannot be read whole."""
    cfg.require("plan")
    older_than = check_older_than(older_than, force_young)
    pl = Planner(cfg, db, older_than, force_dirty, now, refs)
    roots, local, remote = split_targets(cfg, targets)
    if not targets:
        roots = [r.name for r in cfg.roots.values() if r.local and r.kind in ("store", "runs")]
    for p in local:
        pl.check_local(p)
    sweep_remote = {}
    for name in roots:
        r = cfg.roots[name]
        if not r.deletable:
            pl.refuse(r.path, "the %s root %s is reference-only" % (r.kind, name), r)
            continue
        if r.local:
            if r.kind in ("worktrees", "scratch"):
                # One at a time: a sweep would weigh every agent's tree.
                pl.refuse(r.path, "%s roots are cleaned by explicit path only" % r.kind, r)
                continue
            pl.sweep_local(r)
        else:
            sweep_remote.setdefault(r.host, []).append(r)
    for key in sorted(set(remote) | set(sweep_remote)):
        pl.check_remote(key, remote.get(key, []), sweep_remote.get(key, []))
    plan = pl.result(
        dict(
            targets=list(targets),
            older_than=older_than,
            force_dirty=bool(force_dirty),
            force_young=bool(force_young),
        )
    )
    if older_than is not None and older_than < MIN_OLDER_THAN:
        plan["warnings"] = [
            "--force-young: guard 5's age is %g days, under the %g-day floor"
            % (older_than, MIN_OLDER_THAN)
        ]
    plan["disks"] = {"%s:%s" % k: v for k, v in pl.disks.items()}
    return plan


def store_dir(cfg):
    r = cfg.store()
    if r is None:
        raise DbError("benchdb.toml has no local store root (for the plans and the journal)")
    return os.path.realpath(r.path)


def save_plan(cfg, plan, now=None):
    d = os.path.join(store_dir(cfg), PLANS)
    os.makedirs(d, exist_ok=True)
    while True:
        pid = plan_id(now)
        p = os.path.join(d, pid + ".json")
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            continue
    plan["id"] = pid
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1, sort_keys=True)
        f.write("\n")
    return pid, p


def load_plan(cfg, pid):
    if not PLAN_ID_RE.match(pid or ""):
        raise DbError("%r is not a plan id (YYYYMMDDHHMMSS-xxxxxx)" % pid)
    p = os.path.join(store_dir(cfg), PLANS, pid + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise DbError("plan %s: %s" % (pid, e)) from e


def key(it):
    return (it["host"], it["path"])


def intersect(old, fresh):
    """(kept items, [(item, why)] skipped): the old plan's items that the
    fresh plan holds with the same kind, size and newest mtime. The fresh
    plan's own fields (its rule) are the ones kept."""
    now = {key(i): i for i in fresh["items"]}
    why = {(r["host"], r["path"]): r["why"] for r in fresh["refused"]}
    kept, skipped = [], []
    for it in old.get("items", []):
        f = now.get(key(it))
        if f is None:
            skipped.append((it, why.get(key(it), "no longer planned")))
        elif (f["bytes"], f["mtime"], f["kind"]) != (
            it.get("bytes"),
            it.get("mtime"),
            it.get("kind"),
        ):
            skipped.append((f, "changed since the plan"))
        else:
            kept.append(f)
    return kept, skipped


def over_cap(cfg, items, disks):
    """Why the items need --large, or None: over 100 GB in all, or over
    20% of a root's disk."""
    total = sum(i["bytes"] for i in items)
    if total > LARGE_BYTES:
        return "%s in all is over %s" % (human(total), human(LARGE_BYTES))
    per = {}
    for i in items:
        per[(i["host"], i["root"])] = per.get((i["host"], i["root"]), 0) + i["bytes"]
    for (host, root), b in sorted(per.items()):
        if host == "local":
            r = cfg.roots.get(root)
            size, _free = disk(r.path) if r else (0, 0)
        else:
            size = disks.get("%s:%s" % (host, root), (0, 0))[0]
        if size and b > LARGE_SHARE * size:
            return "%s in %s is over %d%% of its disk (%s)" % (
                human(b),
                root,
                int(LARGE_SHARE * 100),
                human(size),
            )
    return None


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return ("%d %s" if u == "B" else "%.1f %s") % (n, u)
        n /= 1024.0
    return str(n)


def remove_local(cfg, it):
    """Deletes one local item; raises OSError on a failure. Returns the
    notes for the journal: a git worktree remove that failed (the rmtree
    and prune after it still clear the tree and its registration)."""
    p = it["path"]
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        os.unlink(p)
        return []
    mains, notes = set(), []
    if it["kind"] == "worktree":
        for d in git_dirs(p):
            m = main_of(d)
            if m:
                mains.add(m)
                r = subprocess.run(
                    ["git", "-C", m, "worktree", "remove", "--force", d], capture_output=True
                )
                if r.returncode != 0:
                    err = r.stderr.decode(errors="replace").strip().splitlines()
                    notes.append(
                        "git worktree remove %s exited %d%s"
                        % (d, r.returncode, ": " + err[-1] if err else "")
                    )
    if os.path.lexists(p):
        make_writable(p)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    for m in sorted(mains):
        subprocess.run(["git", "-C", m, "worktree", "prune"], capture_output=True)
    return notes


def clean(cfg, db, pid, large=False, force_dirty=False, now=None, refs=None, say=print):
    """Deletes the intersection of plan pid with a fresh plan. Returns
    {deleted, skipped, failed, bytes}."""
    cfg.require("clean")
    sd = store_dir(cfg)
    now = now if now is not None else time.time()
    with flock(os.path.join(sd, CLEAN_LOCK), exclusive=True):
        old = load_plan(cfg, pid)
        made = old.get("made_ts")
        if not isinstance(made, (int, float)) or now - made > PLAN_MAX_AGE or made > now + 60:
            raise DbError(
                "plan %s is over an hour old (or its time is unreadable): make a new one" % pid
            )
        args = old.get("args") or {}
        fresh = make_plan(
            cfg,
            db,
            args.get("targets") or (),
            args.get("older_than"),
            bool(args.get("force_dirty")) or force_dirty,
            now,
            refs,
            bool(args.get("force_young")),
        )
        kept, skipped = intersect(old, fresh)
        why = over_cap(cfg, kept, fresh.get("disks", {}))
        if why and not large:
            raise DbError("plan %s: %s; pass --large after checking it" % (pid, why))
        results = []
        for it, w in skipped:
            results.append((it, "refused:%s" % w))
        local = [i for i in kept if i["host"] == "local"]
        for it in local:
            try:
                notes = remove_local(cfg, it)
                if notes:
                    it = dict(it, rule="%s; %s" % (it.get("rule", ""), "; ".join(notes)))
                    for n in notes:
                        say("WARNING: %s" % n)
                results.append((it, "deleted"))
            except OSError as e:
                results.append((it, "failed:%s" % e))
        by_host = {}
        for it in kept:
            if it["host"] != "local":
                by_host.setdefault(it["host"], []).append(it)
        for hk, items in sorted(by_host.items()):
            results.extend(apply_remote(cfg, hk, items, pid, now))
        journal(cfg, db, sd, pid, results)
    out = dict(deleted=0, skipped=0, failed=0, bytes=0)
    for it, res in results:
        if res == "deleted":
            out["deleted"] += 1
            out["bytes"] += it.get("bytes") or 0
        elif res.startswith("failed"):
            out["failed"] += 1
        else:
            out["skipped"] += 1
        say(
            "%-8s %s%s  %s"
            % (res.split(":")[0], host_prefix(it), it["path"], res.partition(":")[2])
        )
    return out


def host_prefix(it):
    return "" if it.get("host", "local") == "local" else it["host"] + ":"


def apply_remote(cfg, hk, items, pid, now):
    h = cfg.hosts[hk]
    roots = [r for r in cfg.roots.values() if r.host == hk and r.deletable]
    never = cfg.never.get(hk, [])
    try:
        extra = bench_env_floor(cfg).get(hk, [])
        script = apply_script(h, items, roots, never, "bench benchdb-clean %s" % pid, extra)
        rc, text = run_host(h, script, APPLY_TIMEOUT)
    except (OSError, subprocess.SubprocessError, DbError) as e:
        return [(it, "failed:host %s: %s" % (hk, e)) for it in items]
    if rc == 75:
        return [(it, "failed:the run lock was held for an hour") for it in items]
    got = {}
    for res, path, why in parse_apply(text):
        got[path] = (res, why)
    out = []
    for it in items:
        res, why = got.get(it["path"], ("failed", "no line from the host (exit %d)" % rc))
        out.append(
            (
                it,
                res
                if res == "deleted"
                else "%s:%s" % ("refused" if res == "skipped" else res, why),
            )
        )
    return out


def journal(cfg, db, sd, pid, results):
    """A deletions row and a clean.log line per path, and the artifacts of
    the deleted paths marked deleted."""
    at = now_iso()
    who = agent()
    with open(os.path.join(sd, CLEAN_LOG), "a", encoding="utf-8") as f:
        for it, res in results:
            f.write(
                "\t".join(
                    [
                        at,
                        pid,
                        who,
                        res.replace("\t", " "),
                        "%s%s" % (host_prefix(it), it["root"]),
                        it["rel"],
                        str(it.get("bytes") or 0),
                        it.get("rule", ""),
                    ]
                )
                + "\n"
            )
    try:
        with db.write() as w:
            for it, res in results:
                w.deletion(
                    pid,
                    it["root"],
                    it["rel"],
                    it.get("rule", ""),
                    res,
                    bytes=it.get("bytes"),
                    agent=who,
                    at=at,
                )
                if res == "deleted":
                    w.sql(
                        "UPDATE artifacts SET state = 'deleted', seen = ? WHERE root = ? AND "
                        "(relpath = ? OR substr(relpath, 1, ?) = ?)",
                        (at, it["root"], it["rel"], len(it["rel"]) + 1, it["rel"] + "/"),
                    )
    except (DbError, OSError) as e:
        raise DbError("deleted, but the journal write failed (clean.log has it): %s" % e) from e


# ---- pins and gc --------------------------------------------------------------


def refresh_pins(cfg, db, refs=None):
    """The pins table from the reference text: each stamp and the file and
    line that cite it. Returns the row count."""
    refs = refs if refs is not None else refs_mod.collect(cfg)
    at = now_iso()
    rows = []
    for path, text in sorted(refs.files.items()):
        where = os.path.relpath(path, cfg.dir) if under(path, cfg.dir) else path
        for n, line in enumerate(text.splitlines(), 1):
            for s in refs_mod.STAMP_RE.findall(line):
                rows.append((s, "%s:%d" % (where, n)))
    with db.write() as w:
        w.sql("DELETE FROM pins")
        for s, src in sorted(set(rows)):
            w.pin(s, src, at)
    return len(set(rows))


def gc(cfg, db, say=print):
    """Reconcile the artifacts with the local disks, optimise, rotate the
    backups and vacuum when a quarter of the pages are free."""
    missing = 0
    roots = {r.name: r for r in cfg.roots.values() if r.local}
    present = db.sql("SELECT id, root, relpath FROM artifacts WHERE state = 'present'")
    gone = []
    for a in present:
        r = roots.get(a["root"])
        if r and not os.path.lexists(os.path.join(r.path, *a["relpath"].split("/"))):
            gone.append(a["id"])
    at = now_iso()
    with db.write() as w:
        for i in gone:
            w.sql("UPDATE artifacts SET state = 'missing', seen = ? WHERE id = ?", (at, i))
            missing += 1
        w.sql("PRAGMA optimize")
    say("benchdb gc: %d artifacts gone from disk marked missing" % missing)
    bd = os.path.join(os.path.dirname(db.path), store.BACKUPS)
    if os.path.isdir(bd):
        store.rotate(bd)
    pages = db.one("PRAGMA page_count")
    free = db.one("PRAGMA freelist_count")
    pc = list(pages.values())[0] if pages else 0
    fc = list(free.values())[0] if free else 0
    if pc and fc / pc > 0.25:
        with db.exclusive():
            c = store.connect(db.path)
            try:
                c.execute("VACUUM")
            finally:
                c.close()
        say("benchdb gc: vacuumed (%d of %d pages were free)" % (fc, pc))
    return missing


def table(plan, out_say=print):
    """gc's table style: one line per item and refusal, then the total."""
    for it in plan["items"]:
        out_say(
            "%-8s %10s  %s%s  (%s)"
            % ("remove", human(it["bytes"]), host_prefix(it), it["path"], it["rule"])
        )
    for r in plan["refused"]:
        out_say("%-8s %10s  %s%s  (%s)" % ("REFUSE", "", host_prefix(r), r["path"], r["why"]))
    for w in plan.get("warnings", []):
        out_say("WARNING: %s" % w)
    out_say(
        "plan %s: %d to remove, %s; %d refused"
        % (plan.get("id"), len(plan["items"]), human(plan["bytes"]), len(plan["refused"]))
    )
