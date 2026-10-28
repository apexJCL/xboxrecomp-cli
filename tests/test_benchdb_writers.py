"""The live writers (design D7) on temp trees: what each one leaves on disk
and in the db, the inbox fallback, and that a broken db never changes a
command's outcome. pull runs against a fake ssh."""

import base64
import hashlib
import json
import os
import types

import pytest
from benchdb_util import (
    GAMES,
    GOLDEN_TXT,
    PROTON_INFO,
    SHA_A,
    WIN_HOST,
    flip_log,
    game_dir,
    open_db,
    run_dir,
)

from xboxrecomp_cli.benchdb import live, pull, record, store
from xboxrecomp_cli.benchdb.store import DbError

INBOX_HOST = """
[hosts.hostB]
os = "windows"
ssh = "hostb"
markers = ['^os: +windows']
inbox = 'D:/xbr/benchdb-inbox'

[hosts.hostC]
os = "linux-proton"
ssh = "hostc"
inbox = "/srv/u/benchdb-inbox"
"""


@pytest.fixture
def ws(d, monkeypatch):
    game_dir(d)
    db = open_db(d, GAMES + WIN_HOST, monkeypatch)
    live._warned.clear()
    return d, db


def logs(d):
    return os.path.join(d, "bench-logs")


# ---- run, session, tests --------------------------------------------------------


def test_run_writes_record_then_rows(ws):
    d, db = ws
    p = run_dir(logs(d), "20261025-072135", PROTON_INFO, golden=GOLDEN_TXT, log=flip_log())
    rid = live.run(p, "demo", host_alias="carlos@hosta", kind="golden", scenario="menu")
    doc = record.read(os.path.join(p, record.RUN))
    assert doc["record"] == "run" and doc["host"] == "hostA" and doc["scenario"] == "menu"
    r = db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert r["host"] == "hostA" and r["kind"] == "golden" and r["scenario"] == "menu"
    # A second call merges into the record and updates the same row.
    assert live.run(p, "demo", kind="golden", secs=40.0) == rid
    assert record.read(os.path.join(p, record.RUN))["scenario"] == "menu"
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 1


def test_run_without_a_config_writes_the_record_only(d):
    p = run_dir(d, "20261025-072135", PROTON_INFO)
    assert live.run(p, "demo", kind="golden") is None
    assert record.read(os.path.join(p, record.RUN))["kind"] == "golden"


def test_run_warns_once_when_the_db_breaks(ws, monkeypatch, capsys):
    d, _db = ws

    def broken(*a, **k):
        raise DbError("disk on fire")

    monkeypatch.setattr(store, "open", broken)
    for stamp in ("20261025-072135", "20261025-072136"):
        p = run_dir(logs(d), stamp, PROTON_INFO)
        assert live.run(p, "demo") is None
        assert os.path.isfile(os.path.join(p, record.RUN))
    err = capsys.readouterr().err
    assert err.count("disk on fire") == 2  # one per run: each names its own run
    live.run(p, "demo")
    assert capsys.readouterr().err == ""


def test_run_warns_on_a_field_json_cannot_write(ws, capsys):
    """live.run: a field json cannot write (a TypeError, not an OSError)
    warns and returns None; it never fails the bench."""
    d, _db = ws
    p = run_dir(logs(d), "20261025-072135", PROTON_INFO)
    assert live.run(p, "demo", kind="golden", bad=object()) is None
    assert "the run record of" in capsys.readouterr().err


def test_session_falls_back_to_its_runs_build(ws):
    d, db = ws
    p = run_dir(logs(d), "20261025-072135", PROTON_INFO, log=flip_log())
    live.run(p, "demo", kind="golden", scenario="menu")
    u = live.session(
        logs(d),
        "demo",
        "golden",
        host="hostA",
        backend="d3d11",
        tests="pass",
        only="menu",
        rc=0,
        verdicts={"menu": (p, "pass"), "story": (None, "SKIP")},
    )
    assert os.path.isfile(os.path.join(logs(d), record.SESSIONS, "%s.json" % u))
    s = db.one("SELECT * FROM sessions WHERE uuid = ?", (u,))
    assert s["source"] == "live" and s["host"] == "hostA" and s["rc"] == 0
    run = db.one("SELECT * FROM runs")
    assert s["build_id"] == run["build_id"] and s["build_id"] is not None
    v = {x["scenario"]: x for x in db.sql("SELECT * FROM verdicts")}
    assert v["menu"]["run_id"] == run["id"] and v["story"]["run_id"] is None


def test_tests_pass(ws):
    d, db = ws
    p = os.path.join(d, "demo", "tests-pass-k1.txt")
    with open(p, "w") as f:
        f.write("key: k1\npassed: 2026-10-14 17:26:15\nhost: hostA\n")
    assert live.tests_pass(p, cwd=d) == "added"
    assert live.tests_pass(p, cwd=d) == "updated"
    assert db.one("SELECT host FROM test_passes WHERE key = 'k1'")["host"] == "hostA"


# ---- matrix -----------------------------------------------------------------


def test_matrix_run(ws):
    d, db = ws
    p = run_dir(os.path.join(d, "runs", "mx"), "arm-A-rep1", extra={"run.json": "{}"})
    row = {
        "arm": "A",
        "rep": 1,
        "backend": "metal",
        "flips": 2300,
        "steady_low1_fps": 55.5,
        "p99_ms": 21.0,
        "hitches": 2,
        "ok": True,
    }
    info = {
        "started": "2026-10-25T07:45:07",
        "exe": "/srv/u/demo/build/demo_recomp",
        "exe_sha256": SHA_A,
        "scene_name": "menu",
    }
    rid = live.matrix(p, row, info)
    r = db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert r["kind"] == "matrix" and r["status"] == "ok" and r["stamp"] == "20261025-074507"
    assert r["backend"] == "metal" and r["scenario"] == "menu" and r["game"] == "demo"
    perf = {x["window"]: x for x in db.sql("SELECT * FROM perf WHERE run_id = ?", (rid,))}
    assert perf["steady"]["flips"] == 2300 and perf["steady"]["low1_fps"] == 55.5
    assert perf["run"]["ms_p99"] == 21.0 and perf["run"]["hitches"] == 2
    m = {x["name"] for x in db.sql("SELECT * FROM metrics WHERE run_id = ?", (rid,))}
    assert "flips" in m and "rep" not in m and "ok" not in m


# ---- builds: Timer, gen verbs, inbox ------------------------------------------


def test_timer_records_a_failed_step(ws):
    d, db = ws
    bdir = os.path.join(d, "demo", "build")
    os.makedirs(bdir)
    t = live.Timer("demo", "mac", "build")
    t.timed("configure", lambda: None)
    with pytest.raises(RuntimeError):
        t.timed("compile", lambda: (_ for _ in ()).throw(RuntimeError("cc")))
    u = t.finish(bdir, ok=False, cwd=d)
    doc = record.read(os.path.join(bdir, record.BUILD))
    assert doc["uuid"] == u and doc["ok"] == 0 and doc["failed_step"] == "compile"
    assert [s[0] for s in doc["steps"]] == ["configure", "compile"]
    b = db.one("SELECT * FROM builds WHERE uuid = ?", (u,))
    assert b["ok"] == 0
    steps = db.sql("SELECT * FROM build_steps WHERE build_id = ? ORDER BY seq", (b["id"],))
    assert [s["step"] for s in steps] == ["configure", "compile"]
    assert steps[1]["detail"] == "failed"


def test_gen_verb_records_beside_the_output(ws, monkeypatch):
    d, db = ws
    out = os.path.join(d, "demo", "analysis")
    os.makedirs(out)
    g = types.SimpleNamespace(slug="demo", root=os.path.join(d, "demo"), out=out)
    seen = []
    live.gen_verb(g, "analyze", [("parse", lambda: seen.append(1)), ("abi", lambda: None)])
    doc = record.read(os.path.join(out, live.GEN_RECORD % "analyze"))
    assert seen == [1] and doc["command"] == "analyze" and doc["ok"] == 1
    assert [s[0] for s in doc["steps"]] == ["parse", "abi"]
    assert db.one("SELECT command FROM builds WHERE uuid = ?", (doc["uuid"],))["command"] == (
        "analyze"
    )
    with pytest.raises(ValueError):
        live.gen_verb(g, "recomp", [("emit", lambda: int("x"))])
    assert record.read(os.path.join(out, live.GEN_RECORD % "recomp"))["ok"] == 0


def test_all_times_its_recomp(monkeypatch):
    """`all` runs analyze, recomp and build: its recomp is a gen verb too,
    so a regen through `all` leaves a row like the standalone verb."""
    from xboxrecomp_cli import build as build_mod
    from xboxrecomp_cli import main, pipeline

    verbs = []
    monkeypatch.setattr(main, "cmd_analyze", lambda a, more=(): verbs.append("analyze"))
    monkeypatch.setattr(main.host, "g", lambda: types.SimpleNamespace())
    monkeypatch.setattr(main.host, "step", lambda *a: None)
    monkeypatch.setattr(main.resources, "build_need", lambda d: None)
    monkeypatch.setattr(build_mod, "default_build_target", lambda: "macos")
    monkeypatch.setattr(build_mod, "build_dir", lambda t: "b")
    monkeypatch.setattr(build_mod, "build", lambda *a, **k: verbs.append("build"))
    monkeypatch.setattr(main, "_toolkit_dir_or_none", lambda: None)

    def gen_verb(g, verb, stages, tk=None):
        assert [n for n, _ in stages] == ["recomp"]
        assert stages[0][1] is pipeline.stage_recomp
        verbs.append(verb)

    monkeypatch.setattr(live, "gen_verb", gen_verb)
    main.cmd_all(types.SimpleNamespace(system_tools=False, jobs=0))
    assert verbs == ["analyze", "recomp", "build"]


def test_build_without_a_db_goes_to_the_inbox(d, monkeypatch):
    bdir = os.path.join(d, "build")
    os.makedirs(bdir)
    ib = os.path.join(d, "inbox")
    monkeypatch.setenv("BENCHDB_INBOX", ib)
    monkeypatch.setenv("BENCHDB_HOST", "hostB")
    t = live.Timer("demo", "windows", "build")
    assert t.doc["host"] == "hostB"
    u = t.finish(bdir, ok=True, cwd=d)
    copy = record.read(os.path.join(ib, "%s.json" % u))
    assert copy["host"] == "hostB" and copy["record"] == "build"
    # The build dir keeps its own record either way.
    assert record.read(os.path.join(bdir, record.BUILD))["uuid"] == u


# The PC's own benchdb.toml above its checkouts: it names itself and its
# inbox, and has no db (D7.6).
PC_HOST = """host = "hostB"

[hosts.hostB]
os = "windows"
inbox = '{inbox}'
"""


def pc_host(d):
    """d/pc: a game checkout under a host config without a db; returns
    (game root, inbox dir)."""
    pc = os.path.join(d, "pc")
    root = game_dir(pc)
    ib = os.path.join(pc, "benchdb-inbox")
    with open(os.path.join(pc, "benchdb.toml"), "w") as f:
        f.write(PC_HOST.format(inbox=ib))
    return root, ib


def test_host_config_sends_every_writer_to_its_inbox(d):
    """A build, a package and a gen verb on a host whose benchdb.toml
    names it: each record carries the host key and lands in its inbox,
    with no BENCHDB_* environment (the PC's builds used to have none)."""
    root, ib = pc_host(d)
    bdir = os.path.join(root, "build-win")
    os.makedirs(bdir)
    t = live.Timer("demo", "windows", "build", cwd=root)
    assert t.doc["host"] == "hostB"
    t.timed("cmake --build", lambda: None)
    u = t.finish(bdir, ok=True, cwd=root)
    copy = record.read(os.path.join(ib, "%s.json" % u))
    assert copy["host"] == "hostB" and copy["kind"] == "incremental"
    assert copy["secs"] is not None and copy["steps"][0][0] == "cmake --build"

    p = live.Timer("demo", "windows", "package", kind="package", cwd=root)
    p.doc.update(ended=live.now(), secs=1.5, ok=1)
    pu = live.package(os.path.join(root, "dist", "x.package-record.json"), p.doc, cwd=root)
    assert record.read(os.path.join(ib, "%s.json" % pu))["host"] == "hostB"

    g = types.SimpleNamespace(slug="demo", root=root, out=os.path.join(root, "analysis"))
    os.makedirs(g.out)
    live.gen_verb(g, "recomp", [("recomp", lambda: None)])
    gu = record.read(os.path.join(g.out, live.GEN_RECORD % "recomp"))["uuid"]
    assert record.read(os.path.join(ib, "%s.json" % gu))["host"] == "hostB"


def test_environment_beats_the_host_config(d, monkeypatch):
    root, ib = pc_host(d)
    other = os.path.join(d, "other-inbox")
    monkeypatch.setenv("BENCHDB_INBOX", other)
    monkeypatch.setenv("BENCHDB_HOST", "hostC")
    assert live.this_host(root) == ("hostC", other)
    monkeypatch.delenv("BENCHDB_HOST")
    assert live.this_host(root) == ("hostB", ib)
    # Outside any config: this machine, no inbox.
    assert live.this_host(os.path.join(d)) == ("local", None)


def test_a_relative_inbox_is_relative_to_the_config(d, monkeypatch):
    root, ib = pc_host(d)
    with open(os.path.join(d, "pc", "benchdb.toml"), "w") as f:
        f.write(PC_HOST.format(inbox="benchdb-inbox"))
    # From another cwd the inbox still lands beside the config.
    monkeypatch.chdir(d)
    assert live.this_host(root) == ("hostB", ib)


def test_a_broken_host_config_never_fails_the_build(d, capsys):
    root, _ib = pc_host(d)
    with open(os.path.join(d, "pc", "benchdb.toml"), "a") as f:
        f.write("\n[bogus]\n")
    t = live.Timer("demo", "windows", "build", cwd=root)
    assert t.doc["host"] == "local"
    assert "benchdb.toml" in capsys.readouterr().err
    # Landing the record warns once, not again for the inbox lookup.
    bdir = os.path.join(root, "build-win")
    os.makedirs(bdir)
    live._warned.clear()
    t.finish(bdir, ok=True, cwd=root)
    assert capsys.readouterr().err.count("benchdb: warning") == 1


def test_build_merges_into_ships_record(ws):
    d, db = ws
    bdir = os.path.join(d, "demo", "build")
    os.makedirs(bdir)
    record.write(os.path.join(bdir, record.BUILD), {"uuid": "u1", "shipped": {"at": "x"}})
    live.build(bdir, {"uuid": "u1", "game": "demo", "target": "mac", "ok": 1}, cwd=d)
    doc = record.read(os.path.join(bdir, record.BUILD))
    assert doc["shipped"] == {"at": "x"} and doc["target"] == "mac"
    # A new build's uuid replaces the old record instead of mixing them.
    live.build(bdir, {"uuid": "u2", "game": "demo", "target": "mac", "ok": 1}, cwd=d)
    assert "shipped" not in record.read(os.path.join(bdir, record.BUILD))


# ---- package record and prune journal -----------------------------------------


def test_package_record(ws, monkeypatch):
    from xboxrecomp_cli import package

    d, db = ws
    root = os.path.join(d, "demo")
    dist = os.path.join(root, "dist")
    os.makedirs(dist)
    bundle = os.path.join(dist, "demo-mac-v1.zip")
    with open(bundle, "wb") as f:
        f.write(b"z" * 10)
    monkeypatch.setattr(package, "g", lambda: types.SimpleNamespace(slug="demo", root=root))
    monkeypatch.setattr(
        package,
        "RECORD",
        {
            "kind": "full",
            "made": True,
            "version": "v1",
            "outs": [bundle],
            "manifest_sha256": "f" * 64,
        },
    )
    a = types.SimpleNamespace(out=dist, target="mac")
    t = live.Timer("demo", "mac", "package", kind="package")
    u = package.record_package(a, t, [["stage", 1.0, None]], ok=True)
    assert u == record.derived_uuid("bundle", "demo", "mac", "f" * 64)
    doc = record.read(bundle + package.PACKAGE_RECORD)
    assert doc["kind"] == "package" and doc["full_bytes"] == 10 and doc["version"] == "v1"
    b = db.one("SELECT * FROM builds WHERE uuid = ?", (u,))
    assert b["kind"] == "package"
    # A kept bundle adds nothing; a failure writes the failed- record.
    monkeypatch.setattr(package, "RECORD", {})
    assert package.record_package(a, t, [], ok=True) is None
    package.record_package(a, t, [], ok=False)
    assert record.read(os.path.join(dist, "failed-mac" + package.PACKAGE_RECORD))["ok"] == 0


def test_deletions_journal(ws):
    d, db = ws
    p = os.path.join(d, "bench-logs", "20261025-072135")
    live.deletions(d, "package-prune", [(p, 123, "old bundle", "deleted")])
    r = db.one("SELECT * FROM deletions")
    assert r["plan"] == "package-prune" and r["root"] == "store"
    assert r["relpath"] == "20261025-072135" and r["bytes"] == 123


# ---- golden provenance ---------------------------------------------------------


def test_provenance_build_line(d, monkeypatch):
    from xboxrecomp_cli import golden_run

    bdir = os.path.join(d, "build")
    os.makedirs(bdir)
    exe = os.path.join(bdir, "demo_recomp")
    with open(exe, "w") as f:
        f.write("x")
    record.write(os.path.join(bdir, record.BUILD), {"uuid": "u1", "exe_sha256": SHA_A})
    lines = golden_run.provenance_lines({"exe": exe, "exe_sha256": SHA_A})
    assert "build:  u1" in lines
    assert any(x.startswith("cli:") for x in lines)
    # A rebuilt exe no longer matches the record: no build line.
    assert not any(
        x.startswith("build:")
        for x in golden_run.provenance_lines({"exe": exe, "exe_sha256": "c" * 64})
    )


# ---- pull ---------------------------------------------------------------------


def listing(files):
    out = []
    for name, data in files:
        out.append(
            "%s %s %s" % (name, hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode())
        )
    return "\n".join(out) + "\n"


def build_doc(uuid, host="hostB"):
    return json.dumps(
        {
            "record": "build",
            "uuid": uuid,
            "game": "demo",
            "target": "windows",
            "ok": 1,
            "host": host,
        }
    ).encode()


@pytest.fixture
def pws(d, monkeypatch):
    game_dir(d)
    db = open_db(d, GAMES + INBOX_HOST, monkeypatch)
    live._warned.clear()
    return d, db


class FakeSsh:
    def __init__(self, listings, rc=0, raises=None):
        self.listings, self.rc, self.raises, self.calls = listings, rc, raises, []

    def __call__(self, h, command):
        self.calls.append((h.key, command))
        if self.raises:
            raise self.raises
        if len([c for c in self.calls if c[0] == h.key]) == 1:
            return self.rc, self.listings.get(h.key, "")
        return 0, ""


def test_pull_imports_then_deletes(pws, monkeypatch):
    d, db = pws
    good = build_doc("u-b1")
    fake = FakeSsh(
        {"hostB": listing([("u-b1.json", good), ("junk.json", b"not json")])},
    )
    monkeypatch.setattr(pull, "ssh", fake)
    said = []
    res = pull.pull(db.cfg, "hostB", say=said.append, db=db)
    assert res == {"hostB": (1, 1)}
    assert db.one("SELECT host FROM builds WHERE uuid = 'u-b1'")["host"] == "hostB"
    assert os.path.isfile(os.path.join(logs(d), "inbox", "hostB", "u-b1.json"))
    # One list, one delete; the delete names only the imported file.
    assert len(fake.calls) == 2
    script = base64.b64decode(fake.calls[1][1].split()[-1]).decode("utf-16-le")
    assert "u-b1.json" in script and "junk.json" not in script
    assert hashlib.sha256(good).hexdigest() in script


def test_pc_build_reaches_the_db_through_its_inbox(pws, monkeypatch):
    """The whole PC path: a build under the PC's host config writes its
    inbox copy; pull lists it over (fake) ssh, imports it with its host,
    kind and secs, then deletes it there."""
    d, db = pws
    root, ib = pc_host(d)
    bdir = os.path.join(root, "build-win")
    os.makedirs(bdir)
    t = live.Timer("demo", "windows", "build", cwd=root)
    t.doc["kind"] = "full"
    t.timed("configure", lambda: None)
    u = t.finish(bdir, ok=True, cwd=root)
    name = "%s.json" % u
    with open(os.path.join(ib, name), "rb") as f:
        data = f.read()
    fake = FakeSsh({"hostB": listing([(name, data)])})
    monkeypatch.setattr(pull, "ssh", fake)
    assert pull.pull(db.cfg, "hostB", say=lambda m: None, db=db) == {"hostB": (1, 0)}
    b = db.one("SELECT * FROM builds WHERE uuid = ?", (u,))
    assert b["host"] == "hostB" and b["kind"] == "full" and b["command"] == "build"
    assert b["secs"] is not None and b["ok"] == 1
    assert db.one("SELECT step FROM build_steps WHERE build_id = ?", (b["id"],))["step"] == (
        "configure"
    )
    script = base64.b64decode(fake.calls[1][1].split()[-1]).decode("utf-16-le")
    assert name in script


def test_a_record_that_fails_halfway_leaves_no_rows(pws, monkeypatch):
    from xboxrecomp_cli.benchdb import ingest

    _d, db = pws
    real = ingest.ingest_record

    def flaky(w, cfg, p):
        got = real(w, cfg, p)
        if os.path.basename(p) == "u-half.json":
            raise store.DbError("failed after its build row")
        if os.path.basename(p) == "u-odd.json":
            raise ValueError("an odd field")
        return got

    monkeypatch.setattr(ingest, "ingest_record", flaky)
    files = [(n + ".json", build_doc(n)) for n in ("u-half", "u-odd", "u-ok")]
    fake = FakeSsh({"hostB": listing(files)})
    monkeypatch.setattr(pull, "ssh", fake)
    said = []
    assert pull.pull(db.cfg, "hostB", say=said.append, db=db) == {"hostB": (1, 2)}
    uuids = [r["uuid"] for r in db.sql("SELECT uuid FROM builds ORDER BY uuid")]
    assert uuids == ["u-ok"]
    script = base64.b64decode(fake.calls[1][1].split()[-1]).decode("utf-16-le")
    assert "u-ok.json" in script and "u-half.json" not in script


def test_pull_skips_a_bad_sha_and_an_unreachable_host(pws, monkeypatch):
    _d, db = pws
    data = build_doc("u-c1", host="hostC")
    bad = "u-c1.json %s %s\n" % ("0" * 64, base64.b64encode(data).decode())
    fake = FakeSsh({"hostB": "", "hostC": bad}, rc=0)
    monkeypatch.setattr(pull, "ssh", fake)
    said = []
    res = pull.pull(db.cfg, say=said.append, db=db)
    assert res == {"hostB": (0, 0), "hostC": (0, 0)}
    assert any("sha256 does not match" in s for s in said)
    assert len(fake.calls) == 2  # no delete for either host
    assert db.one("SELECT count(*) AS n FROM builds")["n"] == 0
    monkeypatch.setattr(pull, "ssh", FakeSsh({}, rc=255))
    said.clear()
    assert pull.pull(db.cfg, "hostC", say=said.append, db=db) == {}
    assert "not reached (ssh exit 255)" in said[0]
    monkeypatch.setattr(pull, "ssh", FakeSsh({}, raises=OSError("no route")))
    assert pull.pull(db.cfg, "hostC", say=said.append, db=db) == {}


def test_pull_host_errors(pws):
    _d, db = pws
    with pytest.raises(DbError, match="no host"):
        pull.hosts_to_pull(db.cfg, "nope")
    with pytest.raises(DbError, match="no inbox"):
        pull.hosts_to_pull(db.cfg, "hostA")
    assert [h.key for h in pull.hosts_to_pull(db.cfg)] == ["hostB", "hostC"]


def test_parse_listing():
    data = b'{"a": 1}'
    ok = listing([("x.json", data)])
    files, bad = pull.parse_listing(ok + "../evil.json 00 AA\n\n" + "y.json %s !!!\n" % ("a" * 64))
    assert files == [("x.json", hashlib.sha256(data).hexdigest(), data)]
    assert len(bad) == 2


def test_scripts_shape(pws):
    _d, db = pws
    win, posix = db.cfg.hosts["hostB"], db.cfg.hosts["hostC"]
    cmd = pull.list_script(win)
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    ps = base64.b64decode(cmd.split()[-1]).decode("utf-16-le")
    assert "'D:/xbr/benchdb-inbox'" in ps and "Get-FileHash" in ps
    sh = pull.list_script(posix)
    assert "'/srv/u/benchdb-inbox'" in sh and "sha256sum" in sh
    dl = pull.delete_script(posix, {"it's.json": "a" * 64})
    assert "'it'\\''s.json'" in dl and dl.endswith("; true")
    wd = pull.delete_script(win, {"b.json": "b" * 64, "a.json": "a" * 64})
    ps = base64.b64decode(wd.split()[-1]).decode("utf-16-le")
    items = "@{n='a.json'; s='%s'}; @{n='b.json'; s='%s'}" % ("a" * 64, "b" * 64)
    assert "foreach ($i in @(%s))" % items in ps
    assert "Join-Path $d $i.n;" in ps and "-eq $i.s)" in ps and "$i[" not in ps


def test_pull_verb(pws, monkeypatch, capsys):
    from xboxrecomp_cli.benchdb import cli

    d, db = pws
    monkeypatch.setattr(pull, "ssh", FakeSsh({"hostC": listing([("u.json", b"{}")])}))
    rc = cli.main(["--config", os.path.join(d, "benchdb.toml"), "pull", "--host", "hostC"])
    assert rc == 1  # the record has no 'record' field: it failed to import
    assert "1 failed" in capsys.readouterr().out


def test_pull_quietly_never_raises(pws, monkeypatch, capsys):
    d, _db = pws
    monkeypatch.setattr(pull, "pull", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert pull.pull_quietly(d, "hostc") is None
    assert "inbox pull" in capsys.readouterr().err
    # A host without an inbox, or one not in the config: nothing to pull.
    assert pull.pull_quietly(d, "hosta") is None
    assert pull.pull_quietly(d, "elsewhere") is None


def test_bench_golden_only_records_its_session(ws, capsys):
    """bench golden --only story: `only` is a list there, and the session row
    is recorded with it joined (a list failed the insert, so the session was
    never recorded)."""
    from xboxrecomp_cli.bench import golden as bg

    d, db = ws
    run_dir(logs(d), "20261025-072135", PROTON_INFO, log=flip_log())
    b = types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            game_dir=d, host="carlos@hosta", game=types.SimpleNamespace(slug="demo")
        ),
        record_run=lambda stamp, **k: live.run(os.path.join(logs(d), stamp), "demo", **k),
    )
    notes = {"story": (None, None, None, None, "pass")}
    runs = {"story": ("20261025-072135", None, None)}
    bg.record_session(b, "golden", None, "pass", ["story", "attract"], 0, runs, notes)
    assert "not recorded" not in capsys.readouterr().err
    s = db.one("SELECT * FROM sessions")
    assert s["only"] == "story,attract" and s["rc"] == 0
    v = db.one("SELECT * FROM verdicts")
    assert v["scenario"] == "story" and v["run_id"] == db.one("SELECT id FROM runs")["id"]
