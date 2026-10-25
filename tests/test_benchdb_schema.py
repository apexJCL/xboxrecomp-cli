"""The db's schema and store (design D5, D6, D9): create, migrate, refuse a
newer db and a network file system, WAL kept, upserts on the natural keys,
the read-only sql(), the daily backup and its rotation, and the rebuild."""

import datetime
import os
import sqlite3
from contextlib import closing as contextlib_closing

import pytest
from benchdb_util import open_db, workspace

from xboxrecomp_cli.benchdb import schema, store


@pytest.fixture(autouse=True)
def local_fs(monkeypatch):
    monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))


def test_create_is_latest_and_wal(d):
    db = open_db(d)
    assert os.path.isfile(db.path)
    assert db.one("PRAGMA user_version")["user_version"] == schema.LATEST
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    names = {r["name"] for r in db.sql("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("runs", "builds", "build_steps", "sessions", "verdicts", "deletions", "releases"):
        assert t in names
    # The config's roots and hosts, and this machine as local.
    assert {r["name"] for r in db.sql("SELECT name FROM roots")} == {"store", "runs"}
    hosts = {r["key"]: r["os"] for r in db.sql("SELECT key, os FROM hosts")}
    assert hosts["hostA"] == "linux-proton" and "local" in hosts
    assert db.one("SELECT host FROM host_names WHERE seen = 'Box1'")["host"] == "hostA"


def test_reopen_is_a_noop_and_keeps_wal(d):
    open_db(d)
    db = open_db(d)
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    assert not os.path.isdir(os.path.join(db.dir, store.BACKUPS)) or not [
        n for n in os.listdir(os.path.join(db.dir, store.BACKUPS)) if n.startswith("bench-pre-")
    ]


def test_migrate_from_each_version(d, monkeypatch):
    """Each older version is backed up and moved on, step by step."""
    v2 = "CREATE TABLE extra (k TEXT PRIMARY KEY);"
    v3 = "ALTER TABLE extra ADD COLUMN v TEXT;"
    p = os.path.join(d, "m.db")
    c = sqlite3.connect(p, isolation_level=None)
    assert schema.migrate(c) == [1]
    c.execute("INSERT INTO meta (k, v) VALUES ('a', 'b')")
    monkeypatch.setattr(schema, "MIGRATIONS", [(1, schema.V1), (2, v2), (3, v3)])
    monkeypatch.setattr(schema, "LATEST", 3)
    bdir = os.path.join(d, "bk")
    assert schema.migrate(c, bdir) == [2, 3]
    assert schema.version(c) == 3
    assert os.path.isfile(os.path.join(bdir, "bench-pre-v2.db"))
    old = sqlite3.connect(os.path.join(bdir, "bench-pre-v2.db"))
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    assert old.execute("SELECT v FROM meta WHERE k = 'a'").fetchone()[0] == "b"
    c.execute("INSERT INTO extra (k, v) VALUES ('x', 'y')")
    # From 2 straight to 3.
    p2 = os.path.join(d, "m2.db")
    c2 = sqlite3.connect(p2, isolation_level=None)
    monkeypatch.setattr(schema, "MIGRATIONS", [(1, schema.V1), (2, v2)])
    monkeypatch.setattr(schema, "LATEST", 2)
    schema.migrate(c2)
    monkeypatch.setattr(schema, "MIGRATIONS", [(1, schema.V1), (2, v2), (3, v3)])
    monkeypatch.setattr(schema, "LATEST", 3)
    assert schema.migrate(c2, bdir) == [3]
    assert os.path.isfile(os.path.join(bdir, "bench-pre-v3.db"))


def test_failed_migration_rolls_back(d, monkeypatch):
    p = os.path.join(d, "m.db")
    c = sqlite3.connect(p, isolation_level=None)
    schema.migrate(c)
    monkeypatch.setattr(
        schema, "MIGRATIONS", [(1, schema.V1), (2, "CREATE TABLE t2 (a); CREATE TABLE runs (a);")]
    )
    monkeypatch.setattr(schema, "LATEST", 2)
    with pytest.raises(sqlite3.OperationalError):
        schema.migrate(c)
    assert schema.version(c) == 1
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 't2'").fetchone()


def test_newer_db_opens_read_only(d, capsys):
    db = open_db(d)
    c = sqlite3.connect(db.path)
    c.execute("PRAGMA user_version = %d" % (schema.LATEST + 1))
    c.close()
    with pytest.raises(schema.SchemaError):
        schema.migrate(sqlite3.connect(db.path, isolation_level=None))
    again = store.open(cfg=db.cfg)
    assert again.readonly and "newer" in again.note
    assert "read-only" in capsys.readouterr().err
    with pytest.raises(store.DbError, match="read-only"):
        with again.write():
            pass
    assert again.one("SELECT count(*) AS n FROM roots")["n"] == 2


def test_network_fs_refused(d, monkeypatch):
    cfg = workspace(d)
    monkeypatch.setattr(store, "FS_CHECK", lambda p: (False, "on a nfs mount (/n)"))
    with pytest.raises(store.DbError, match="nfs"):
        store.open(cfg=cfg)
    assert not os.path.exists(cfg.db)


def test_linux_mounts_parse(d, monkeypatch, tmp_path):
    """The Linux check reads the deepest mount holding the dir."""
    mounts = tmp_path / "mounts"
    net = os.path.join(os.path.realpath(d), "net")
    mounts.write_text("/dev/a / ext4 rw 0 0\nsrv:/x %s nfs4 rw 0 0\n" % net)
    os.makedirs(os.path.join(d, "net", "sub"))
    real_open = open

    def fake_open(p, *a, **k):
        return real_open(str(mounts) if p == "/proc/self/mounts" else p, *a, **k)

    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr("builtins.open", fake_open)
    ok, why = store.local_fs(os.path.join(d, "net", "sub"))
    assert not ok and "nfs4" in why
    assert store.local_fs(d) == (True, "")


def test_upsert_on_natural_keys(d):
    db = open_db(d)
    with db.write() as w:
        b = w.build("u-1", "mygame", "windows", "full", steps=[("recomp", 10.0), ("build", 5.0)])
        r = w.run("mygame", "store", "20261020-101010", "20261020-101010", "golden", build=b)
        w.perf(r, "run", "run-info", flips=100, flips_per_s=50.0)
        s = w.session("s-1", "mygame", "golden", "2026-10-20T10:10:10Z", build=b)
        w.verdict(s, "boot", "pass", run=r)
    with db.write() as w:
        b2 = w.build("u-1", "mygame", "windows", "full", steps=[("build", 7.0)], secs=7.0)
        r2 = w.run("mygame", "store", "20261020-101010", "20261020-101010", "golden", build=b2)
        w.perf(r2, "run", "run-info", flips=120)
        s2 = w.session("s-1", "mygame", "golden", "2026-10-20T10:10:10Z", rc=0)
        w.verdict(s2, "boot", "FAIL", run=r2)
    assert (b, r, s) == (b2, r2, s2)
    assert db.one("SELECT count(*) AS n FROM builds")["n"] == 1
    assert db.sql("SELECT step, secs FROM build_steps") == [{"step": "build", "secs": 7.0}]
    assert db.one("SELECT flips, flips_per_s FROM perf") == {"flips": 120, "flips_per_s": 50.0}
    assert db.one("SELECT word FROM verdicts")["word"] == "FAIL"
    assert db.one("SELECT rc FROM sessions")["rc"] == 0


def test_unknown_column_refused(d):
    db = open_db(d)
    with pytest.raises(store.DbError, match="no column"):
        with db.write() as w:
            w.build("u-1", "mygame", "windows", "full", nonsense=1)
    assert db.one("SELECT count(*) AS n FROM builds")["n"] == 0


def test_failed_write_rolls_back(d):
    db = open_db(d)
    with pytest.raises(RuntimeError):
        with db.write() as w:
            w.meta("a", "1")
            raise RuntimeError("boom")
    assert db.one("SELECT count(*) AS n FROM meta")["n"] == 0


def test_a_rollback_never_masks_the_error(d):
    """L12: after SQLite has rolled back on its own, the second ROLLBACK
    fails; the error the caller sees is still the one raised."""
    db = open_db(d)
    with pytest.raises(RuntimeError, match="boom"):
        with db.write() as w:
            w.meta("a", "1")
            w.conn.execute("ROLLBACK")
            raise RuntimeError("boom")


def test_a_held_lock_times_out(d, monkeypatch):
    """L11: a holder that hangs makes a writer fail after LOCK_TIMEOUT, not
    wait for ever (a bench's warn-only write would hang the bench)."""
    import fcntl
    import signal

    db = open_db(d)
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)

    def stuck(*_a):
        raise TimeoutError("the writer waited for ever")

    old = signal.signal(signal.SIGALRM, stuck)
    fd = os.open(db.lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        signal.alarm(5)
        with pytest.raises(store.DbError, match="held by another benchdb"):
            with db.write() as w:
                w.meta("a", "1")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
        os.close(fd)
    assert db.one("SELECT count(*) AS n FROM meta")["n"] == 0


def test_sql_is_read_only(d):
    db = open_db(d)
    with pytest.raises(sqlite3.OperationalError):
        db.sql("INSERT INTO meta (k, v) VALUES ('a', 'b')")
    with pytest.raises(sqlite3.OperationalError):
        db.sql("DELETE FROM roots")
    assert db.one("SELECT count(*) AS n FROM roots")["n"] == 2


def test_write_takes_the_shared_lock(d, monkeypatch):
    db = open_db(d)
    seen = []
    real = store.flock

    def spy(path, exclusive):
        seen.append((os.path.basename(path), exclusive))
        return real(path, exclusive)

    monkeypatch.setattr(store, "flock", spy)
    with db.write() as w:
        w.meta("a", "1")
    with db.exclusive():
        pass
    assert seen == [(store.LOCK, False), (store.LOCK, True)]


def test_daily_backup_and_rotation(d):
    db = open_db(d)
    bdir = os.path.join(db.dir, store.BACKUPS)
    today = datetime.date.today().strftime("%Y%m%d")
    assert os.path.isfile(os.path.join(bdir, "bench-%s.db" % today))
    # 40 days of copies: 7 daily, then the newest of each of 4 older weeks.
    for n in os.listdir(bdir):
        os.remove(os.path.join(bdir, n))
    start = datetime.date(2026, 9, 1)
    days = [start + datetime.timedelta(days=i) for i in range(40)]
    for day in days:
        open(os.path.join(bdir, "bench-%s.db" % day.strftime("%Y%m%d")), "w").close()
    open(os.path.join(bdir, "bench-pre-v2.db"), "w").close()
    store.rotate(bdir)
    kept = sorted(os.listdir(bdir))
    assert "bench-pre-v2.db" in kept
    dated = [n for n in kept if n != "bench-pre-v2.db"]
    assert len(dated) == 11
    newest = sorted(days, reverse=True)
    assert all("bench-%s.db" % x.strftime("%Y%m%d") in dated for x in newest[:7])


def test_daily_backup_has_a_temp_of_its_own(d):
    """L10: the copy goes through a temp file of its own, so another
    writer's (here a stale one at the old fixed name) never collides."""
    db = open_db(d)
    bdir = os.path.join(db.dir, store.BACKUPS)
    day = datetime.date(2026, 9, 1)
    p = os.path.join(bdir, "bench-%s.db" % day.strftime("%Y%m%d"))
    os.makedirs(p + ".tmp")
    with contextlib_closing(store.connect(db.path)) as conn:
        assert db.backup_daily(conn, today=day) == p
    assert os.path.isfile(p)
    assert not [n for n in os.listdir(bdir) if n.startswith(".bench-")]


def test_rebuild_keeps_deletions_and_releases(d):
    db = open_db(d)
    with db.write() as w:
        w.meta("old", "1")
        w.release("mygame", "v1", "abc", "published")
        w.deletion("p1", "store", "20261001-000000", "old", "deleted", bytes=5)

    def fill(tmp):
        with tmp.write() as w:
            w.meta("new", "2")
        # A deletion journalled on the live db while the rebuild ran.
        with db.write() as w:
            w.deletion("p2", "store", "20261002-000000", "old", "deleted")

    reader = store.connect(db.path, readonly=True)
    db.rebuild(fill)
    assert db.sql("SELECT k FROM meta") == [{"k": "new"}]
    assert db.one("SELECT tag FROM releases")["tag"] == "v1"
    assert [r["plan"] for r in db.sql("SELECT plan FROM deletions ORDER BY id")] == ["p1", "p2"]
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    # A connection open across the rebuild reads the new db.
    assert reader.execute("SELECT k FROM meta").fetchall()[0][0] == "new"
    reader.close()
    assert not os.path.exists(db.path + ".rebuild")
