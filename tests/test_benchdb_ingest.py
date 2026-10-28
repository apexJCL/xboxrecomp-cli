"""ingest_run and the other importers (design D7, D12) on synthetic run
dirs, TSVs and records: what each format yields, partial and unparsed
runs, idempotent re-import, the TSV re-key, build links and artifacts."""

import json
import os

import pytest
from benchdb_util import (
    GAME_SHA,
    GAMES,
    GOLDEN_TXT,
    MAC_INFO,
    PROTON_INFO,
    SHA_A,
    SHA_B,
    WIN_HOST,
    WINDOWS_INFO,
    flip_log,
    game_dir,
    open_db,
    run_dir,
)

from xboxrecomp_cli.benchdb import ingest as ingest_mod
from xboxrecomp_cli.benchdb import record, store


@pytest.fixture
def ws(d, monkeypatch):
    game_dir(d)
    db = open_db(d, GAMES + WIN_HOST, monkeypatch)
    return d, db, db.cfg


def store_dir(d):
    return os.path.join(d, "bench-logs")


def ingest(db, cfg, path, **kw):
    with db.write() as w:
        return ingest_mod.ingest_run(w, cfg, path, **kw)


def test_proton_run(ws):
    d, db, cfg = ws
    p = run_dir(
        store_dir(d),
        "20261025-072135",
        PROTON_INFO,
        golden=GOLDEN_TXT,
        log=flip_log(),
        extra={"exit-code": "0\n", "frames/": "", "steam-default.log": "x\n"},
    )
    rid, how, status, problems = ingest(db, cfg, p)
    assert (how, status, problems) == ("added", "ok", [])
    r = db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert r["game"] == "demo" and r["host"] == "hostA"
    assert r["backend"] == "d3d11" and r["kind"] == "golden" and r["scenario"] == "menu"
    assert r["started"] == "2026-10-25T14:21:35Z"
    assert r["exit_code"] == 0 and r["lock_wait_secs"] == 3.0
    b = db.one("SELECT * FROM builds WHERE id = ?", (r["build_id"],))
    assert b["kind"] == "unknown" and b["exe_sha256"] == SHA_A
    assert b["game_sha"] == GAME_SHA and b["lineage"] == "integration"
    assert b["ended"] == "2026-10-25T14:21:34Z"
    perf = {x["source"]: x for x in db.sql("SELECT * FROM perf WHERE run_id = ?", (rid,))}
    assert perf["run-info"]["flips"] == 2063
    assert perf["flip-trace"]["hitches"] == 1
    assert len(db.sql("SELECT * FROM frame_checks WHERE run_id = ?", (rid,))) == 2
    # A Proton golden run's verdict comes from the session TSV, not golden.txt.
    assert db.sql("SELECT * FROM sessions") == []
    arts = {a["relpath"].split("/")[-1]: a for a in db.sql("SELECT * FROM artifacts")}
    assert arts["frames"]["kind"] == "frames" and arts["frames"]["heavy"] == 1
    assert arts["steam-default.log"]["kind"] == "proton-log"
    assert arts["run-info.txt"]["sha256"]


def test_reimport_is_a_noop(ws):
    d, db, cfg = ws
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO, log=flip_log())
    ingest(db, cfg, p)
    counts = {
        t: db.one("SELECT count(*) AS n FROM %s" % t)["n"]
        for t in ("runs", "perf", "builds", "artifacts")
    }
    rid, how, _s, _p = ingest(db, cfg, p)
    assert how == "updated"
    assert counts == {
        t: db.one("SELECT count(*) AS n FROM %s" % t)["n"]
        for t in ("runs", "perf", "builds", "artifacts")
    }


def test_stand_in_build_knows_where_it_was_built(ws, monkeypatch):
    """A run that predates the build records gets a kind-unknown stand-in
    with no secs (nothing timed that build), but its built: line names the
    machine: this one is 'local', a cross build for a Windows exe. A
    re-import fills the host into a stand-in made before it was known."""
    d, db, cfg = ws
    monkeypatch.setattr(ingest_mod.platform, "node", lambda: "hostm.lan")
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO, log=flip_log())
    rid, *_ = ingest(db, cfg, p)
    b = db.one("SELECT b.* FROM builds b JOIN runs r ON r.build_id = b.id WHERE r.id = ?", (rid,))
    assert b["kind"] == "unknown" and b["secs"] is None
    assert b["host"] == "local" and b["cross"] == 1

    # The same run before the fix: a stand-in without its host.
    with db.write() as w:
        w.conn.execute("UPDATE builds SET host = NULL, cross = NULL WHERE id = ?", (b["id"],))
    ingest(db, cfg, p)
    b = db.one("SELECT * FROM builds WHERE id = ?", (b["id"],))
    assert b["host"] == "local" and b["cross"] == 1

    # A name the config does not know leaves it unknown.
    monkeypatch.setattr(ingest_mod.platform, "node", lambda: "elsewhere")
    with db.write() as w:
        w.conn.execute("UPDATE builds SET host = NULL WHERE id = ?", (b["id"],))
    ingest(db, cfg, p)
    assert db.one("SELECT host FROM builds WHERE id = ?", (b["id"],))["host"] is None


def test_synced_tree_was_built_on_the_run_host(ws):
    """Before BENCH_BUILD=local the bench host compiled a synced tree: the
    `synced: ... from <mac>:<dir>` line names where the sources came from,
    and the run's host is where the exe was built."""
    d, db, cfg = ws
    info = PROTON_INFO.replace(
        "built:  shipped (not built by bench): 2026-10-25T14:21:34Z on hostm from /srv/u/demo"
        " (BENCH_BUILD=local)",
        "built:  synced: 2026-10-25T14:01:09Z from hostm:/srv/u/demo",
    )
    assert "synced:" in info
    p = run_dir(store_dir(d), "20261025-072135", info, log=flip_log())
    rid, *_ = ingest(db, cfg, p)
    b = db.one("SELECT b.* FROM builds b JOIN runs r ON r.build_id = b.id WHERE r.id = ?", (rid,))
    assert b["kind"] == "unknown" and b["host"] == "hostA" and b["cross"] == 1


def test_mac_run_was_built_on_the_run_host(d, monkeypatch):
    """A Mac run has no built: line: the Mac runs the exe it built in
    place, so the stand-in build's host is the run's own, a native build."""
    game_dir(d)
    db = open_db(d, GAMES + WIN_HOST + '\n[hosts.hostM]\nos = "macos"\n', monkeypatch)
    info = MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws=d)
    assert "built:" not in info
    p = run_dir(store_dir(d), "20261025-074507-metal-menu", info, log=flip_log())
    rid, *_ = ingest(db, db.cfg, p, host="hostM")
    b = db.one("SELECT b.* FROM builds b JOIN runs r ON r.build_id = b.id WHERE r.id = ?", (rid,))
    assert b["kind"] == "unknown" and b["target"] == "macos"
    assert (b["host"], b["cross"]) == ("hostM", 0)


def test_hand_written_built_line(ws, monkeypatch):
    """Mid-October runs carry a hand-written line: "<exe> built on <name>
    <iso> (...)". A name is looked up as a shipped line's; "the Mac" is the
    one macOS host."""
    d, db, cfg = ws
    shipped = (
        "built:  shipped (not built by bench): 2026-10-25T14:21:34Z on hostm from /srv/u/demo"
        " (BENCH_BUILD=local)"
    )
    monkeypatch.setattr(ingest_mod.platform, "node", lambda: "hostm.lan")
    for stamp, line, want in (
        (
            "20261015-070000",
            "built:  demo.exe built on hostm 2026-10-15T14:39:44Z (cross)",
            "local",
        ),
        (
            "20261015-080000",
            "built:  demo.exe built on the Mac 2026-10-15T01:57:21Z (cross)",
            "local",
        ),
    ):
        info = PROTON_INFO.replace(shipped, line).replace(
            SHA_A, stamp.replace("-", "") + SHA_A[14:]
        )
        p = run_dir(store_dir(d), stamp, info, log=flip_log())
        rid, *_ = ingest(db, cfg, p)
        b = db.one(
            "SELECT b.* FROM builds b JOIN runs r ON r.build_id = b.id WHERE r.id = ?", (rid,)
        )
        assert (b["host"], b["cross"]) == (want, 1), line


def test_timed_build_keeps_its_host(ws):
    """Only a stand-in is filled: a recorded build's host is its own."""
    d, db, cfg = ws
    with db.write() as w:
        w.build("u-1", "demo", "windows", "full", exe_sha256=SHA_A, host="hostA", secs=9.0)
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO, log=flip_log())
    ingest(db, cfg, p)
    b = db.one("SELECT host, secs FROM builds WHERE uuid = 'u-1'")
    assert b == {"host": "hostA", "secs": 9.0}


def test_windows_run_local_time_and_marker(ws):
    d, db, cfg = ws
    import datetime

    # The PC runs at UTC-7: its local 01:30:48 is 08:30:48 UTC.
    utc = datetime.datetime(2026, 10, 25, 8, 30, 48, tzinfo=datetime.UTC)
    launch = {"started": "2026-10-25T01:30:48", "unix_us": int(utc.timestamp()) * 10**6}
    p = run_dir(
        store_dir(d),
        "20261025-003048",
        WINDOWS_INFO,
        extra={"launch.json": json.dumps(launch)},
    )
    rid, _how, status, problems = ingest(db, cfg, p)
    r = db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert r["host"] == "hostB"
    assert r["secs"] == 41.0 and r["exit_code"] == 0
    assert status == "ok", problems
    # The offset comes from launch.json.
    assert r["started"] == "2026-10-25T08:30:48Z"
    b = db.one("SELECT * FROM builds WHERE id = ?", (r["build_id"],))
    assert b["exe_sha256"] == SHA_B and b["game_dirty"] == 1
    assert b["lineage"] == "branch:feat/x"


def test_mac_golden_run_makes_a_session(ws):
    d, db, cfg = ws
    info = MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws=d)
    p = run_dir(store_dir(d), "20261025-074507-metal-menu", info, golden=GOLDEN_TXT, log=flip_log())
    rid, _how, status, problems = ingest(db, cfg, p, host="hostA")
    r = db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert r["kind"] == "golden-run" and r["backend"] == "metal" and r["scenario"] == "menu"
    assert r["game"] == "demo"
    # The env's workspace paths are normalised (no private path in the db).
    assert d not in r["env"] and "<store>/20261025-074507-metal-menu/hdd" in r["env"]
    s = db.one("SELECT * FROM sessions")
    assert s["kind"] == "golden-run" and s["source"] == "golden.txt"
    v = db.one("SELECT * FROM verdicts")
    assert (v["scenario"], v["word"], v["run_id"]) == ("menu", "pass", rid)
    assert status == "ok", problems


def test_mem_line_becomes_metrics(ws):
    d, db, cfg = ws
    info = MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws=d)
    log = flip_log() + (
        "[MEM] exit ram_mib=812.4 ram_peak_mib=905.1 commit_mib=n/a commit_peak_mib=n/a"
        " gpu_mib=301.0 gpu_peak_mib=350.2 gpu_nl_mib=n/a gpu_nl_peak_mib=n/a"
        " ram_src=phys_footprint gpu_src=metal why=sigint\n"
    )
    p = run_dir(store_dir(d), "20261025-074508-metal-menu", info, golden=GOLDEN_TXT, log=log)
    rid, _how, status, problems = ingest(db, cfg, p, host="hostA")
    assert status == "ok", problems
    got = {
        r["name"]: (r["value"], r["unit"])
        for r in db.sql("SELECT name, value, unit FROM metrics WHERE run_id = ?", (rid,))
        if r["name"].endswith("_mib")
    }
    assert got == {
        "ram_mib": (812.4, "MiB"),
        "ram_peak_mib": (905.1, "MiB"),
        "gpu_mib": (301.0, "MiB"),
        "gpu_peak_mib": (350.2, "MiB"),
    }


def test_mem_sample_only_is_kept_apart(ws):
    # Killed before the exit line: the last sample is stored, but not under
    # the names regress compares.
    d, db, cfg = ws
    info = MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws=d)
    log = flip_log() + (
        "[MEM] sample t=30.0 ram_mib=700.0 ram_peak_mib=750.0 gpu_mib=50.0"
        " gpu_peak_mib=55.0 ram_src=phys_footprint gpu_src=metal\n"
    )
    p = run_dir(store_dir(d), "20261025-074509-metal-menu", info, golden=GOLDEN_TXT, log=log)
    rid, _how, status, problems = ingest(db, cfg, p, host="hostA")
    assert status == "ok", problems
    got = {
        r["name"]: r["value"]
        for r in db.sql("SELECT name, value FROM metrics WHERE run_id = ?", (rid,))
        if r["name"].endswith("_mib")
    }
    assert got == {
        "mem_sample_ram_mib": 700.0,
        "mem_sample_ram_peak_mib": 750.0,
        "mem_sample_gpu_mib": 50.0,
        "mem_sample_gpu_peak_mib": 55.0,
    }


def test_unparsed_and_partial(ws):
    d, db, cfg = ws
    p = run_dir(store_dir(d), "20261025-080000", extra={"game-stdio.log": "boot\n"})
    _rid, _how, status, problems = ingest(db, cfg, p)
    assert status == "unparsed" and "no run-info.txt" in problems
    p = run_dir(store_dir(d), "20261025-080100", "host: Box1 2026-10-25T08:01:00Z\nflips: 9\n")
    _rid, _how, status, problems = ingest(db, cfg, p, game="demo")
    assert status == "partial"
    assert "no exe line" in problems


def test_unknown_host_is_a_problem(ws):
    d, db, cfg = ws
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO.replace("Box1", "Nobody"))
    _rid, _how, status, problems = ingest(db, cfg, p)
    assert status == "partial" and "host unknown" in problems


def test_outside_roots_and_bad_names_refused(ws, tmp_path):
    d, db, cfg = ws
    p = run_dir(str(tmp_path / "elsewhere"), "20261025-072135", PROTON_INFO)
    with pytest.raises(store.DbError, match="outside every root"):
        ingest(db, cfg, p)
    p = run_dir(store_dir(d), "notastamp", PROTON_INFO)
    with pytest.raises(store.DbError, match="not a run dir"):
        ingest(db, cfg, p)


def test_artifacts_marked_missing(ws):
    d, db, cfg = ws
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO, extra={"frames/": ""})
    ingest(db, cfg, p)
    os.rmdir(os.path.join(p, "frames"))
    ingest(db, cfg, p)
    a = db.one("SELECT state FROM artifacts WHERE relpath LIKE '%/frames'")
    assert a["state"] == "missing"


def test_run_links_build_record_by_uuid(ws):
    d, db, cfg = ws
    doc = {
        "record": "build",
        "schema": record.SCHEMA,
        "uuid": "u-1",
        "game": "demo",
        "target": "windows",
        "kind": "full",
        "exe_sha256": SHA_A,
        "secs": 120.5,
        "steps": [["recomp", 30.0, None], {"step": "compile", "secs": 90.5}],
        "trees": {"game": {"sha": GAME_SHA, "branch": "main", "dirty": 0}},
    }
    with db.write() as w:
        bid, how = ingest_mod.ingest_build(w, cfg, doc)
    assert how == "added"
    steps = db.sql("SELECT step, secs FROM build_steps WHERE build_id = ? ORDER BY seq", (bid,))
    assert [s["step"] for s in steps] == ["recomp", "compile"]
    info = PROTON_INFO.replace("built:  exe:", "built:  build: u-1\nbuilt:  exe:")
    p = run_dir(store_dir(d), "20261025-072135", info)
    rid, *_ = ingest(db, cfg, p)
    assert db.one("SELECT build_id FROM runs WHERE id = ?", (rid,))["build_id"] == bid
    # And by exe sha alone, when the run names no uuid.
    p = run_dir(store_dir(d), "20261025-072200", PROTON_INFO)
    rid, *_ = ingest(db, cfg, p)
    assert db.one("SELECT build_id FROM runs WHERE id = ?", (rid,))["build_id"] == bid
    with pytest.raises(store.DbError):
        with db.write() as w:
            ingest_mod.ingest_build(w, cfg, {"uuid": "x"})


def test_tsv_links_runs_and_rekeys(ws):
    d, db, cfg = ws
    tsv = os.path.join(store_dir(d), ingest_mod.TSV)
    with open(tsv, "w") as f:
        f.write(
            "2026-10-25 07:34:10\tgolden\ttests=pass\tonly=menu\t"
            "menu=20261025-072135:pass\trc=0\thost=posix\n"
        )
    # The TSV first, before its run: game unknown.
    with db.write() as w:
        assert ingest_mod.ingest_tsv(w, tsv) == (1, 0)
    s = db.one("SELECT * FROM sessions")
    assert s["game"] == "unknown" and s["uuid"].startswith("tsv:unknown:")
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO)
    rid, *_ = ingest(db, cfg, p)
    with db.write() as w:
        ingest_mod.ingest_tsv(w, tsv)
    rows = db.sql("SELECT * FROM sessions")
    assert len(rows) == 1
    s = rows[0]
    assert s["game"] == "demo" and s["uuid"].startswith("tsv:demo:")
    assert s["host"] == "hostA" and s["rc"] == 0 and s["kind"] == "golden"
    v = db.one("SELECT * FROM verdicts")
    assert v["run_id"] == rid and v["word"] == "pass"


def test_reconstructed_tsv(ws):
    d, db, cfg = ws
    tsv = os.path.join(store_dir(d), ingest_mod.TSV_RECON)
    with open(tsv, "w") as f:
        f.write("2026-10-20 10:00:00\tgolden\tmenu=20261020-100000:pass\trc=unknown\n")
    with db.write() as w:
        ingest_mod.ingest_tsv(w, tsv, reconstructed=True)
    s = db.one("SELECT * FROM sessions")
    assert s["kind"] == "reconstructed" and s["source"] == "reconstructed-tsv"
    assert s["rc"] is None


def test_session_record_and_tests_pass(ws):
    d, db, cfg = ws
    p = run_dir(store_dir(d), "20261025-072135", PROTON_INFO)
    rid, *_ = ingest(db, cfg, p)
    sdir = os.path.join(store_dir(d), record.SESSIONS)
    os.makedirs(sdir)
    sp = os.path.join(sdir, "s1.json")
    record.write(
        sp,
        {
            "record": "session",
            "uuid": "s1",
            "game": "demo",
            "kind": "golden",
            "started": "2026-10-25T14:30:00Z",
            "host": "hostA",
            "verdicts": {"menu": {"word": "pass", "run": "store/20261025-072135"}},
        },
    )
    with db.write() as w:
        assert ingest_mod.ingest_record(w, cfg, sp) == ("session", "added")
    v = db.one("SELECT * FROM verdicts")
    assert v["run_id"] == rid
    tp = os.path.join(store_dir(d), ingest_mod.TESTS_PASS)
    os.makedirs(tp)
    with open(os.path.join(tp, "k1.txt"), "w") as f:
        f.write("key: k1\npassed: 2026-10-14 17:26:15\nhost: hostA\n")
    with db.write() as w:
        assert ingest_mod.ingest_tests_pass(w, os.path.join(tp, "k1.txt")) == "added"
        assert ingest_mod.ingest_tests_pass(w, os.path.join(tp, "k1.txt")) == "updated"


@pytest.mark.parametrize(
    "only, want",
    [(["story"], "story"), (["story", "attract"], "story,attract"), ([], None)]
    + [("story,attract", "story,attract"), (None, None)],
)
def test_session_record_only_list_or_text(ws, only, want):
    """A session record from bench golden --only before 27e4685 holds `only`
    as a JSON list, which failed the insert ("type 'list' is not supported");
    it is stored joined, as the text form already was."""
    d, db, cfg = ws
    sdir = os.path.join(store_dir(d), record.SESSIONS)
    os.makedirs(sdir)
    sp = os.path.join(sdir, "s1.json")
    doc = {"record": "session", "uuid": "s1", "game": "demo", "kind": "golden", "rc": 0}
    doc.update(started="2026-10-26T21:17:45Z")
    record.write(sp, dict(doc, only=only, verdicts={"story": {"word": "pass", "run": None}}))
    with db.write() as w:
        assert ingest_mod.ingest_record(w, cfg, sp) == ("session", "added")
    s = db.one("SELECT * FROM sessions")
    assert s["only"] == want and s["rc"] == 0


def test_walk_all_order_and_topic_dirs(ws):
    d, db, cfg = ws
    s = store_dir(d)
    run_dir(s, "20261025-072135", PROTON_INFO)
    run_dir(os.path.join(s, "topic"), "20261024-100000", PROTON_INFO)
    run_dir(os.path.join(s, "topic"), "notes", "x")
    os.makedirs(os.path.join(s, "inbox", "hostA"))
    with open(os.path.join(s, "inbox", "hostA", "b.json"), "w") as f:
        json.dump({"record": "build", "uuid": "u", "game": "demo"}, f)
    with open(os.path.join(s, ingest_mod.TSV), "w") as f:
        f.write("")
    items = ingest_mod.walk_all(cfg)
    kinds = [k for k, _ in items]
    assert kinds[0] == "record"
    assert kinds.index("bundles") < kinds.index("run") < kinds.index("tsv")
    runs = [os.path.relpath(p, s) for k, p in items if k == "run"]
    assert runs == ["20261025-072135", os.path.join("topic", "20261024-100000")]


def test_bundles_and_patches(ws):
    d, db, cfg = ws
    g = os.path.join(d, "demo")
    idx = os.path.join(g, ".git", "xbr", "bundles", "windows")
    os.makedirs(idx)
    with open(os.path.join(idx, "index.json"), "w") as f:
        json.dump({"c" * 64: {"version": "v1", "made": "2026-10-20T10:00:00Z", "size": 9}}, f)
    pj = os.path.join(g, "dist", "demo-windows-v2", "patch.json")
    os.makedirs(os.path.dirname(pj))
    with open(pj, "w") as f:
        json.dump({"version": "v2", "base_version": "v1", "changed": [{"size": 4}]}, f)
    with db.write() as w:
        assert ingest_mod.ingest_bundles(w, cfg, "demo", g) == 2
    with db.write() as w:
        ingest_mod.ingest_bundles(w, cfg, "demo", g)
    rows = db.sql("SELECT kind, target, version, payload_bytes FROM builds ORDER BY kind")
    assert rows == [
        {"kind": "package", "target": "windows", "version": "v1", "payload_bytes": None},
        {"kind": "patch", "target": "windows", "version": "v2", "payload_bytes": 4},
    ]


def test_classify_and_expand(ws, tmp_path):
    d, db, cfg = ws
    s = store_dir(d)
    p = run_dir(s, "20261025-072135", PROTON_INFO)
    assert ingest_mod.classify(p) == "run"
    assert ingest_mod.classify(os.path.join(s, ingest_mod.TSV)) == "tsv"
    assert ingest_mod.classify(os.path.join(s, ingest_mod.TSV_RECON)) == "tsv-recon"
    assert ingest_mod.expand([s]) == [("run", p)]
    with pytest.raises(store.DbError):
        ingest_mod.expand([str(tmp_path / "nothing.bin")])


def test_normalise_env(ws):
    d, db, cfg = ws
    env = "A=%s/bench-logs/x B=%s/other C=/srv/u" % (d, d)
    assert ingest_mod.normalise_env(env, cfg) == "A=<store>/x B=<ws>/other C=/srv/u"
