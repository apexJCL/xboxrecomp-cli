"""The db: open(), short write transactions, read-only queries, the locks,
the daily backup and `import --rebuild` (design D6, D7, D9).

- WAL, busy_timeout 30 s, isolation_level None and BEGIN IMMEDIATE: several
  agents write at once and readers never block.
- A connection per write: db.write() opens one and closes it after its
  commit, so a 40 min golden never holds one across the game run.
- <db dir>/bench.db.lock is taken shared by every write and exclusive by
  migration, rebuild and VACUUM. It is never the Mac's build or run lock.
- The db must be on a local file system: SQLite over NFS, SMB or sshfs
  breaks WAL's locking."""

import builtins
import contextlib
import datetime
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

from . import config as config_mod
from . import schema

BUSY_MS = 30000
LOCK = "bench.db.lock"
# Writes are short transactions; a rebuild's swap the longest hold.
LOCK_TIMEOUT = 300.0
BACKUPS = "db-backup"
KEEP_DAILY = 7
KEEP_WEEKLY = 4
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "afpfs", "webdav", "9p"}


class DbError(Exception):
    pass


def host_os():
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux-native"


def local_fs(d):
    """(ok, why) for directory d. The stdlib has no statfs on macOS, so
    `df -l` answers there: it lists only local file systems and fails for
    a network mount. Linux reads the mount's type from /proc/self/mounts."""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["df", "-l", d], capture_output=True, text=True)
        except OSError as e:
            return False, "df -l failed: %s" % e
        if r.returncode != 0 or len(r.stdout.strip().splitlines()) < 2:
            return False, "not on a local file system (df -l %s)" % d
        return True, ""
    if sys.platform.startswith("linux"):
        try:
            with builtins.open("/proc/self/mounts") as f:
                mounts = [line.split() for line in f]
        except OSError:
            return True, ""
        real = os.path.realpath(d)
        best, fstype = "", ""
        for m in mounts:
            if len(m) < 3:
                continue
            mp = m[1].replace("\\040", " ")
            if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, fstype = mp, m[2]
        if fstype in NETWORK_FS:
            return False, "on a %s mount (%s)" % (fstype, best)
        return True, ""
    return True, ""


# The check open() runs; tests replace it.
FS_CHECK = local_fs


@contextlib.contextmanager
def flock(path, exclusive, timeout=None):
    """fcntl.flock on path, shared or exclusive, for the with block. A
    holder that hangs never hangs every writer (and every bench's
    warn-only write) with it: past timeout seconds this raises DbError."""
    import fcntl

    timeout = LOCK_TIMEOUT if timeout is None else timeout
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise DbError(
                        "%s is held by another benchdb for over %g s" % (path, timeout)
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def rollback(conn):
    """ROLLBACK that never masks the error being raised: after some errors
    SQLite has already rolled back, and a second ROLLBACK fails."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(
            "file:%s?mode=ro" % path, uri=True, timeout=BUSY_MS / 1000, isolation_level=None
        )
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(path, timeout=BUSY_MS / 1000, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_MS)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


class Writer:
    """The write API inside one db.write() transaction. Every method upserts
    on its table's natural key (D5), so a re-import is a no-op."""

    def __init__(self, conn, db):
        self.conn = conn
        self.db = db

    def sql(self, text, params=()):
        return rows(self.conn.execute(text, params))

    def one(self, text, params=()):
        r = self.conn.execute(text, params).fetchone()
        return None if r is None else (r[0] if len(r) == 1 else dict(r))

    def upsert(self, table, key, fields):
        """INSERT ... ON CONFLICT(key) DO UPDATE; returns the rowid."""
        cols = self.db.columns(table)
        bad = sorted(set(fields) - cols)
        if bad:
            raise DbError("%s has no column %s" % (table, ", ".join(bad)))
        names = list(fields)
        upd = [n for n in names if n not in key]
        sql = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) " % (
            table,
            ", ".join(names),
            ", ".join("?" * len(names)),
            ", ".join(key),
        )
        sql += (
            "DO UPDATE SET " + ", ".join("%s = excluded.%s" % (n, n) for n in upd)
            if upd
            else "DO NOTHING"
        )
        r = self.conn.execute(sql + " RETURNING rowid", [fields[n] for n in names]).fetchone()
        if r is not None:
            return r[0]
        where = " AND ".join("%s IS ?" % k for k in key)
        return self.conn.execute(
            "SELECT rowid FROM %s WHERE %s" % (table, where), [fields[k] for k in key]
        ).fetchone()[0]

    # ---- the D7 API ----------------------------------------------------------

    def build(self, uuid, game, target, kind, lineage="unknown", steps=None, **f):
        bid = self.upsert(
            "builds",
            ("uuid",),
            dict(uuid=uuid, game=game, target=target, kind=kind, lineage=lineage, **f),
        )
        if steps is not None:
            self.conn.execute("DELETE FROM build_steps WHERE build_id = ?", (bid,))
            for seq, st in enumerate(steps):
                step, secs, detail = (tuple(st) + (None,))[:3]
                self.conn.execute(
                    "INSERT INTO build_steps (build_id, seq, step, secs, detail) VALUES (?,?,?,?,?)",
                    (bid, seq, step, secs, detail),
                )
        return bid

    def run(self, game, root, relpath, stamp, kind, status="ok", build=None, **f):
        if build is not None:
            f["build_id"] = build
        return self.upsert(
            "runs",
            ("root", "relpath"),
            dict(game=game, root=root, relpath=relpath, stamp=stamp, kind=kind, status=status, **f),
        )

    def clear_run(self, run):
        """Drops a run's parsed child rows before it is parsed again."""
        for t in ("perf", "metrics", "frame_checks"):
            self.conn.execute("DELETE FROM %s WHERE run_id = ?" % t, (run,))

    def perf(self, run, window, source, **f):
        return self.upsert(
            "perf",
            ("run_id", "window", "source"),
            dict(run_id=run, window=window, source=source, **f),
        )

    def metric(self, run, name, value, unit=None):
        return self.upsert(
            "metrics", ("run_id", "name"), dict(run_id=run, name=name, value=value, unit=unit)
        )

    def frame_check(self, run, frame, result, **f):
        return self.upsert(
            "frame_checks", ("run_id", "frame"), dict(run_id=run, frame=frame, result=result, **f)
        )

    def artifact(self, run, root, relpath, kind, heavy, state="present", build=None, **f):
        f.setdefault("seen", now_iso())
        return self.upsert(
            "artifacts",
            ("root", "relpath"),
            dict(
                run_id=run,
                build_id=build,
                root=root,
                relpath=relpath,
                kind=kind,
                heavy=int(bool(heavy)),
                state=state,
                **f,
            ),
        )

    def session(self, uuid, game, kind, started, source="live", build=None, **f):
        if build is not None:
            f["build_id"] = build
        return self.upsert(
            "sessions",
            ("uuid",),
            dict(uuid=uuid, game=game, kind=kind, started=started, source=source, **f),
        )

    def verdict(self, session, scenario, word, run=None):
        return self.upsert(
            "verdicts",
            ("session_id", "scenario"),
            dict(session_id=session, scenario=scenario, run_id=run, word=word),
        )

    def test_pass(self, key, **f):
        return self.upsert("test_passes", ("key",), dict(key=key, **f))

    def release(self, game, tag, game_sha, state, **f):
        return self.upsert(
            "releases",
            ("game", "tag"),
            dict(game=game, tag=tag, game_sha=game_sha, state=state, **f),
        )

    def pin(self, stamp, source, refreshed):
        return self.upsert(
            "pins", ("stamp", "source"), dict(stamp=stamp, source=source, refreshed=refreshed)
        )

    def deletion(self, plan, root, relpath, reason, result, bytes=None, agent=None, at=None):
        self.conn.execute(
            "INSERT INTO deletions (plan, at, agent, root, relpath, bytes, reason, result) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (plan, at or now_iso(), agent, root, relpath, bytes, reason, result),
        )

    def host(self, key, os_, label=None):
        return self.upsert("hosts", ("key",), dict(key=key, os=os_, label=label))

    def host_name(self, seen, host):
        return self.upsert("host_names", ("seen",), dict(seen=seen, host=host))

    def root(self, name, host, path, kind, deletable):
        return self.upsert(
            "roots",
            ("name",),
            dict(name=name, host=host, path=path, kind=kind, deletable=int(bool(deletable))),
        )

    def meta(self, k, v):
        return self.upsert("meta", ("k",), dict(k=k, v=v))


def now_iso():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Db:
    def __init__(self, path, cfg, readonly=False, note=""):
        self.path = path
        self.cfg = cfg
        self.readonly = readonly
        self.note = note
        self.dir = os.path.dirname(path)
        self.lock_path = os.path.join(self.dir, LOCK)
        self._columns = {}

    def columns(self, table):
        if table not in self._columns:
            with contextlib.closing(connect(self.path, readonly=True)) as c:
                self._columns[table] = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
        return self._columns[table]

    @contextlib.contextmanager
    def write(self):
        """One short BEGIN IMMEDIATE ... COMMIT on its own connection,
        under the shared lock."""
        if self.readonly:
            raise DbError("the db is open read-only%s" % (": " + self.note if self.note else ""))
        with flock(self.lock_path, exclusive=False):
            conn = connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield Writer(conn, self)
                    conn.execute("COMMIT")
                except BaseException:
                    rollback(conn)
                    raise
                self.backup_daily(conn)
            finally:
                conn.close()

    @contextlib.contextmanager
    def exclusive(self):
        with flock(self.lock_path, exclusive=True):
            yield

    def sql(self, text, params=()):
        """A read on its own mode=ro connection: a write in text fails."""
        with contextlib.closing(connect(self.path, readonly=True)) as c:
            return rows(c.execute(text, params))

    def one(self, text, params=()):
        r = self.sql(text, params)
        return r[0] if r else None

    def query(self, name, **filters):
        from . import queries

        return queries.run(self, name, **filters)

    # ---- backups ---------------------------------------------------------------

    def backup_daily(self, conn, today=None):
        """The day's first write leaves <db dir>/db-backup/bench-YYYYMMDD.db;
        7 daily and 4 weekly copies are kept. A failure only warns."""
        today = today or datetime.date.today()
        d = os.path.join(self.dir, BACKUPS)
        p = os.path.join(d, "bench-%s.db" % today.strftime("%Y%m%d"))
        if os.path.exists(p):
            return None
        tmp = None
        try:
            os.makedirs(d, exist_ok=True)
            # A temp file of its own: two writers on the day's first write
            # would interleave pages in one fixed name and install a torn copy.
            fd, tmp = tempfile.mkstemp(prefix=".bench-", suffix=".tmp", dir=d)
            os.close(fd)  # SQLite takes an empty file as an empty db
            schema.backup_to(conn, tmp)
            os.replace(tmp, p)
            tmp = None
            rotate(d)
        except (OSError, sqlite3.Error) as e:
            print("benchdb: WARNING: daily backup failed: %s" % e, file=sys.stderr)
            return None
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return p

    # ---- rebuild ---------------------------------------------------------------

    def rebuild(self, fill, refill=None):
        """import --rebuild: fill(tmp_db) builds a temp db from the records
        on disk with no lock held; deletions and releases are copied in
        first (disk cannot recreate them); then, under the exclusive lock,
        refill(tmp_db, since) re-reads what disk gained during the fill (a
        bench that ended, a pull), the journal and pins written meanwhile
        are copied, and the temp db is copied into the live one with
        Connection.backup(), which writes through SQLite's pager and WAL, so
        a reader sees the old or the new db, never a mix. Never a rename
        (D9)."""
        tmp_path = self.path + ".rebuild"
        junk = [tmp_path + s for s in ("", "-wal", "-shm", ".lock")]
        for p in junk:
            if os.path.exists(p):
                os.remove(p)
        c = connect(tmp_path)
        try:
            schema.migrate(c)
        finally:
            c.close()
        tmp = Db(tmp_path, self.cfg)
        # Its own lock: the refill writes it while this holds the live
        # lock exclusively, and a shared take of that would wait on itself.
        tmp.lock_path = tmp_path + ".lock"
        load_config(tmp, self.cfg)
        copy_kept(tmp_path, self.path, ("releases", "deletions"), replace=True)
        # mtimes are whole seconds on some file systems, and a record
        # written in the second the fill started may be read or not.
        since = time.time() - 2
        fill(tmp)
        with self.exclusive():
            if refill is not None:
                refill(tmp, since)
            # What the live db gained since the copy above: the journal,
            # a release, the pins a refresh wrote.
            copy_kept(tmp_path, self.path, ("deletions", "releases"), replace=False)
            copy_kept(tmp_path, self.path, ("pins",), replace=True)
            with contextlib.closing(connect(tmp_path)) as src:
                dst = connect(self.path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
        for p in junk:
            if os.path.exists(p):
                os.remove(p)


def touched_since(path, since):
    """Whether a file, or a run dir by its own mtime and its top level's
    (record.json and run-info land there by a rename), changed since."""
    try:
        if os.path.getmtime(path) >= since:
            return True
        if os.path.isdir(path):
            with os.scandir(path) as it:
                return any(e.stat(follow_symlinks=False).st_mtime >= since for e in it)
    except OSError:
        return True
    return False


def copy_kept(dst_path, src_path, tables, replace):
    """Copies the rows disk cannot recreate from src's tables into dst's.
    ATTACH is refused inside a transaction, so it comes first."""
    with contextlib.closing(connect(dst_path)) as c:
        c.execute("ATTACH DATABASE ? AS live", (src_path,))
        c.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                if replace:
                    c.execute("INSERT OR REPLACE INTO main.%s SELECT * FROM live.%s" % (t, t))
                else:
                    c.execute("INSERT OR IGNORE INTO main.%s SELECT * FROM live.%s" % (t, t))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("DETACH DATABASE live")


def rotate(d, keep_daily=KEEP_DAILY, keep_weekly=KEEP_WEEKLY):
    """Keeps the newest keep_daily bench-YYYYMMDD.db copies, and of the
    older ones the newest per ISO week for keep_weekly weeks. Only this
    tool's own copies are touched (bench-pre-v*.db are kept)."""
    dated = []
    for n in os.listdir(d):
        if len(n) == len("bench-20260101.db") and n.startswith("bench-") and n.endswith(".db"):
            try:
                dated.append((datetime.datetime.strptime(n[6:14], "%Y%m%d").date(), n))
            except ValueError:
                continue
    dated.sort(reverse=True)
    keep = {n for _, n in dated[:keep_daily]}
    weeks = []
    for day, n in dated[keep_daily:]:
        wk = day.isocalendar()[:2]
        if wk not in weeks and len(weeks) < keep_weekly:
            weeks.append(wk)
            keep.add(n)
    for _, n in dated:
        if n not in keep:
            os.remove(os.path.join(d, n))


def load_config(db, cfg):
    """Mirrors the config's roots and hosts (and this machine as 'local')."""
    with db.write() as w:
        w.host("local", host_os(), platform.node())
        for h in cfg.hosts.values():
            w.host(h.key, h.os, h.label or None)
            for n in h.names:
                w.host_name(n, h.key)
        for r in cfg.roots.values():
            w.root(r.name, r.host, r.path, r.kind, r.deletable)


def open(path=None, cfg=None, readonly=False, cwd=None):  # noqa: A001 (benchdb.open)
    """The Db, created or migrated. path defaults to the config's db. A db
    newer than this CLI opens read-only with a note; one on a network file
    system is refused."""
    cfg = cfg if cfg is not None else config_mod.load(cwd=cwd)
    path = path or cfg.db
    if not path:
        raise DbError("no db: benchdb.toml has no db key, and none was given")
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    if not readonly:
        os.makedirs(d, exist_ok=True)
    ok, why = FS_CHECK(d if os.path.isdir(d) else os.path.dirname(d))
    if not ok:
        raise DbError("refusing the db at %s: %s" % (path, why))
    if readonly:
        if not os.path.exists(path):
            raise DbError("no db at %s (run benchdb init)" % path)
        return Db(path, cfg, readonly=True)
    with flock(os.path.join(d, LOCK), exclusive=True):
        c = connect(path)
        try:
            v = schema.version(c)
            if v > schema.LATEST:
                note = "schema %d is newer than this CLI's %d" % (v, schema.LATEST)
                print("benchdb: %s: opened read-only" % note, file=sys.stderr)
                return Db(path, cfg, readonly=True, note=note)
            schema.migrate(c, os.path.join(d, BACKUPS))
        finally:
            c.close()
    db = Db(path, cfg)
    if cfg.present:
        load_config(db, cfg)
    return db
