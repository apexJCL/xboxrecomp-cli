"""benchdb.toml: the workspace's private settings (roots, hosts, refs, the
db path). It never lives in a repository. benchdb takes $BENCHDB_CONFIG
when it is set, else finds it by walking up from the cwd, the way git
finds .git.

  db = "bench-logs/bench.db"            # relative to this file's dir
  host = "local"                        # this machine: 'local' or a [hosts] key

  [roots.store]                         # one table per root
  path = "bench-logs"
  host = "local"                        # or a [hosts] key
  kind = "store"                        # see KINDS
  pattern = ['[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?']   # deletable leaves
  days = 7                              # guard 5's age (default: 2 for runs,
                                        # else the games' longest [bench.gc] days)
  own = false                           # the leaf must be the user's
  lock = "flock"                        # host roots: flock | bench-lock

  [never]
  local = ["game_files"]                # per host: paths no rule deletes
  hostA = ["~/xbr-bench/mygame/game_files"]

  [refs]
  paths = ["notes"]                     # more pin sources
  exclude = ["notes/bench-db"]          # never pin sources

  [release]
  export_maps = "notes/public"          # <slug>.map: <public> <private> <date>
  toolkit_map = "notes/public-toolkit-map/map.txt"   # <private> <public>
  toolkit = "xboxrecomp"                # the private toolkit repo
  cli = "xboxrecomp-cli"                # the CLI repo

  [hosts.hostA]
  os = "linux-proton"
  ssh = "hosta"                         # the ssh alias (default: the key)
  markers = ['^proton:\\s+/srv/u/']     # regexes over a run-info, first
  names = []                            # the run-info's host: line (exact)
  inbox = ""                            # the host's inbox dir

  [games]
  mygame = "mygame"                     # slug = game dir

Every relative local path is relative to the config file's directory.
Without a config, the clean verbs are refused (D3).

A build host without the db (the PC under Windows) keeps a config of its
own above its checkouts that names itself and its inbox, so its builds,
packages and gen verbs copy their records there for `benchdb pull`:

  host = "pc"
  [hosts.pc]
  os = "windows"
  inbox = 'D:\\xbr\\benchdb-inbox'"""

import os
import re
import tomllib

ENV = "BENCHDB_CONFIG"
NAME = "benchdb.toml"
DELETABLE = ("store", "runs", "worktrees", "scratch", "host-scratch")
KINDS = DELETABLE + ("inbox", "checkout", "notes")
OSES = ("macos", "windows", "linux-proton", "linux-native")
LOCKS = ("", "flock", "bench-lock")
TABLE_KEYS = {
    "roots": {"path", "host", "kind", "deletable", "pattern", "days", "own", "lock", "df"},
    "hosts": {"os", "ssh", "markers", "names", "inbox", "label"},
}
REFS_KEYS = {"paths", "exclude"}
RELEASE_KEYS = {"export_maps", "toolkit_map", "toolkit", "cli"}


class ConfigError(Exception):
    pass


def find(cwd=None):
    """The path of benchdb.toml, or None: $BENCHDB_CONFIG when it is set
    (an explicit choice beats a file that happens to be above the cwd),
    else the nearest one walking up from cwd."""
    env = os.environ.get(ENV)
    if env:
        if not os.path.isfile(env):
            raise ConfigError("%s=%s: no such file" % (ENV, env))
        return os.path.abspath(env)
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        p = os.path.join(d, NAME)
        if os.path.isfile(p):
            return p
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return None


class Root:
    def __init__(self, name, path, host, kind, deletable, patterns, days, own, lock, df):
        self.name, self.path, self.host, self.kind = name, path, host, kind
        self.deletable, self.patterns, self.days = deletable, patterns, days
        self.own, self.lock, self.df = own, lock, df

    @property
    def local(self):
        return self.host == "local"

    def matches(self, leaf):
        return any(p.fullmatch(leaf) for p in self.patterns)


class Host:
    def __init__(self, key, os_, ssh, markers, names, inbox, label):
        self.key, self.os, self.ssh = key, os_, ssh
        self.markers, self.names, self.inbox, self.label = markers, names, inbox, label


class Config:
    """A parsed benchdb.toml. path is None for the empty config (no file):
    queries work on a db given by path, and the clean verbs refuse."""

    def __init__(self, path=None, doc=None):
        self.path = path
        self.dir = os.path.dirname(path) if path else os.getcwd()
        doc = doc or {}
        errors = []
        known = {"db", "host", "roots", "never", "refs", "release", "hosts", "games"}
        for k in doc:
            if k not in known:
                errors.append("%s: unknown key" % k)
        self.db = self.local_path(doc["db"]) if "db" in doc else None
        # A misspelt key (nevr, paterns) is an error, never a silent default.
        for table, keys in TABLE_KEYS.items():
            for name, t in sorted(doc.get(table, {}).items()):
                if not isinstance(t, dict):
                    errors.append("%s.%s: not a table" % (table, name))
                    continue
                for k in sorted(set(t) - keys):
                    errors.append("%s.%s.%s: unknown key" % (table, name, k))
        for table, keys in (("refs", REFS_KEYS), ("release", RELEASE_KEYS)):
            for k in sorted(set(doc.get(table, {})) - keys):
                errors.append("%s.%s: unknown key" % (table, k))
        self.hosts = {}
        for key, h in sorted(doc.get("hosts", {}).items()):
            if not isinstance(h, dict):
                continue
            o = h.get("os", "")
            if o not in OSES:
                errors.append("hosts.%s.os: %r is not one of %s" % (key, o, ", ".join(OSES)))
            try:
                markers = [re.compile(m, re.M) for m in h.get("markers", [])]
            except re.error as e:
                errors.append("hosts.%s.markers: %s" % (key, e))
                markers = []
            self.hosts[key] = Host(
                key,
                o,
                h.get("ssh", key),
                markers,
                list(h.get("names", [])),
                h.get("inbox", ""),
                h.get("label", ""),
            )
        # Which machine reads this file: the writers' host key, and the
        # inbox their records go to when the file names no db.
        self.this_host = doc.get("host", "local")
        if not isinstance(self.this_host, str):
            # A table or list would be unhashable in the hosts lookup.
            errors.append("host: %r is not a string" % (self.this_host,))
            self.this_host = "local"
        elif self.this_host != "local" and self.this_host not in self.hosts:
            errors.append("host: %r is not local or a [hosts] key" % (self.this_host,))
        self.roots = {}
        for name, r in sorted(doc.get("roots", {}).items()):
            if not isinstance(r, dict):
                continue
            kind = r.get("kind", "")
            host = r.get("host", "local")
            if kind not in KINDS:
                errors.append("roots.%s.kind: %r is not one of %s" % (name, kind, ", ".join(KINDS)))
            if host != "local" and host not in self.hosts:
                errors.append("roots.%s.host: %r is not local or a [hosts] key" % (name, host))
            if "path" not in r:
                errors.append("roots.%s.path: required" % name)
                continue
            deletable = bool(r.get("deletable", kind in DELETABLE))
            if deletable and kind not in DELETABLE:
                errors.append("roots.%s: a %s root is never deletable" % (name, kind))
            pats = r.get("pattern", [])
            pats = [pats] if isinstance(pats, str) else list(pats)
            try:
                patterns = [re.compile(p) for p in pats]
            except re.error as e:
                errors.append("roots.%s.pattern: %s" % (name, e))
                patterns = []
            if deletable and not patterns:
                errors.append("roots.%s.pattern: a deletable root needs one" % name)
            lock = r.get("lock", "")
            if lock not in LOCKS:
                errors.append("roots.%s.lock: %r is not flock or bench-lock" % (name, lock))
            days = r.get("days")
            if days is not None:
                ok = isinstance(days, (int, float)) and not isinstance(days, bool)
                if not ok or not days >= 1:
                    # Guard 5's floor: under a day, a run being written or
                    # shipped would go (only plan --force-young goes lower).
                    errors.append("roots.%s.days: %r is not a number of 1 or more" % (name, days))
                    days = None
            path = self.local_path(r["path"]) if host == "local" else r["path"]
            self.roots[name] = Root(
                name,
                path,
                host,
                kind,
                deletable,
                patterns,
                float(days) if days is not None else None,
                bool(r.get("own", False)),
                lock,
                r.get("df", ""),
            )
        self.never = {}
        for host, paths in doc.get("never", {}).items():
            if host != "local" and host not in self.hosts:
                errors.append("never.%s: not local or a [hosts] key" % host)
                continue
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                errors.append("never.%s: not a list of paths" % host)
                continue
            self.never[host] = [self.local_path(p) if host == "local" else p for p in paths]
        refs = doc.get("refs", {})
        self.refs = [self.local_path(p) for p in refs.get("paths", [])]
        self.refs_exclude = [self.local_path(p) for p in refs.get("exclude", [])]
        rel = doc.get("release", {})
        self.export_maps = self.local_path(rel["export_maps"]) if rel.get("export_maps") else None
        self.toolkit_map = self.local_path(rel["toolkit_map"]) if rel.get("toolkit_map") else None
        self.toolkit = self.local_path(rel["toolkit"]) if rel.get("toolkit") else None
        self.cli = self.local_path(rel["cli"]) if rel.get("cli") else None
        self.games = {k: self.local_path(v) for k, v in sorted(doc.get("games", {}).items())}
        if errors:
            raise ConfigError("%s: %s" % (path or NAME, "; ".join(errors)))

    def local_path(self, p):
        p = os.path.expanduser(p)
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(self.dir, p))

    @property
    def present(self):
        return self.path is not None

    def require(self, verb):
        """Refuses a deletion verb without a config file."""
        if not self.present:
            raise ConfigError(
                "benchdb %s: no %s (looked up from %s and in $%s); the clean verbs need one"
                % (verb, NAME, os.getcwd(), ENV)
            )

    def store(self):
        """The first local store root (where the db's locks, plans and
        backups live), or None."""
        for r in self.roots.values():
            if r.kind == "store" and r.local:
                return r
        return None

    def locate(self, path):
        """(root, relpath) of a local absolute path, by the deepest root
        that holds it; None when no root does (a path outside every root is
        refused when written, D4)."""
        p = os.path.normpath(os.path.abspath(path))
        best = None
        for r in self.roots.values():
            if not r.local:
                continue
            for base in {r.path, os.path.realpath(r.path)}:
                if p == base or p.startswith(base.rstrip(os.sep) + os.sep):
                    rel = os.path.relpath(p, base).replace(os.sep, "/")
                    if best is None or len(base) > len(best[2]):
                        best = (r.name, rel, base)
        return (best[0], best[1]) if best else None

    def host_of_alias(self, alias):
        for h in self.hosts.values():
            if h.ssh == alias or h.key == alias:
                return h.key
        return None


def load(path=None, cwd=None):
    """The workspace Config: path, else the one find() names, else the
    empty Config."""
    path = path or find(cwd)
    if not path:
        return Config()
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError("%s: %s" % (path, e)) from e
    return Config(os.path.abspath(path), doc)


def game_settings(game):
    """A game's [bench.db], [bench.db.regress] and [release] from its
    loaded manifest (defaults filled in by manifest.validate)."""
    m = game.m
    return {"db": m["bench"]["db"], "release": m["release"], "gc": m["bench"]["gc"]}
