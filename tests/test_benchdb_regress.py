"""benchdb regress (design D13): the run whitelist, one verdict per run, the
CLAUDE.md flips and frame-ms rules, thin data, backends, and the build-time
rule over comparable builds. The game is not listed in the config, so the
manifest's [bench.db.regress] defaults apply."""

import itertools

import pytest
from benchdb_util import open_db

from xboxrecomp_cli.benchdb import regress
from xboxrecomp_cli.benchdb.store import DbError

_seq = itertools.count()


@pytest.fixture
def db(tmp_path, monkeypatch):
    return open_db(str(tmp_path), monkeypatch=monkeypatch)


def build(db, started, lineage="integration", release=None, match="match", **f):
    n = next(_seq)
    f.setdefault("ok", 1)
    with db.write() as w:
        return w.build(
            "%08d-0000-4000-8000-%012d" % (n, n),
            "demo",
            f.pop("target", "windows"),
            f.pop("kind", "full"),
            lineage=lineage,
            started=started,
            release=release,
            release_match=match if release else None,
            steps=f.pop("steps", None),
            **f,
        )


def uuid(db, bid):
    return db.one("SELECT uuid FROM builds WHERE id = ?", (bid,))["uuid"]


def run(
    db,
    b,
    flips=60.0,
    p50=16.7,
    p95=17.0,
    scenario="menu",
    backend="d3d11",
    host="hosta",
    exit_code=0,
    stock=1,
    busy=0,
    exact=1,
    window="run",
    source="run-info",
):
    n = next(_seq)
    with db.write() as w:
        if host:
            w.host(host, "linux-proton")
        rid = w.run(
            "demo",
            "store",
            "20261001-1%05d" % n,
            "20261001-100000",
            "golden",
            build=b,
            host=host,
            backend=backend,
            scenario=scenario,
            exit_code=exit_code,
            stock=stock,
            busy=busy,
            started="2026-10-01T10:00:00Z",
        )
        w.perf(rid, window, source, flips_exact=exact, flips_per_s=flips, ms_p50=p50, ms_p95=p95)
    return rid


def judge(db, rid, word, kind="golden", source="live", started="2026-10-02T10:00:00Z"):
    n = next(_seq)
    with db.write() as w:
        s = w.session("s%d" % n, "demo", kind, started, source=source)
        w.verdict(s, "menu", word, run=rid)


@pytest.fixture
def ab(db):
    a = build(db, "2026-10-01T10:00:00Z", lineage="integration", release="v1")
    with db.write() as w:
        w.release("demo", "v1", "1" * 40, "published", published="2026-10-01T12:00:00Z")
    b = build(db, "2026-10-05T10:00:00Z")
    return a, b


def only(res, **k):
    rows = [r for r in res["runs"] if all(r[x] == v for x, v in k.items())]
    assert len(rows) == 1, rows
    return rows[0]


def test_flips_drop_and_frame_ms_rise(db, ab):
    a, b = ab
    for _ in range(3):
        run(db, a, flips=60.0, scenario="menu")
        run(db, a, p95=17.0, scenario="race")
        run(db, a, flips=60.0, scenario="ok")
    for _ in range(3):
        run(db, b, flips=45.0, scenario="menu")  # 0.75 x: flagged
        run(db, b, p95=25.5, scenario="race")  # 1.5 x: flagged
        run(db, b, flips=46.0, scenario="ok")
    res = regress.regress(db, db.cfg, game="demo")
    assert res["a"] == "release v1" and res["b"] == uuid(db, b)
    assert only(res, scenario="menu")["flag"] == "flips"
    assert only(res, scenario="race")["flag"] == "frame-ms"
    assert only(res, scenario="ok")["flag"] == ""
    assert res["flags"] == 2
    text = "\n".join(regress.lines(res))
    assert "FLAG flips" in text and text.count("p95 is the sensitive figure") == 1


def test_the_whitelist_and_one_verdict_per_run(db, ab):
    a, b = ab
    run(db, a, flips=60.0)
    good = run(db, b, flips=60.0)
    judge(db, good, "pass-with-known")
    regression = run(db, b, flips=60.0)
    judge(db, regression, "REGRESSION")
    for word in ("FAIL-RUN", "FAIL-PRESENT", "MISSING", "NEWVIEW", "INCONCLUSIVE", "INCOMPLETE"):
        judge(db, run(db, b, flips=1.0), word)
    # Unjudged: counts on exit code 0 only.
    run(db, b, flips=60.0)
    run(db, b, flips=1.0, exit_code=1)
    # Not stock, busy, inexact flips: never.
    run(db, b, flips=1.0, stock=0)
    run(db, b, flips=1.0, busy=1)
    run(db, b, flips=1.0, exact=0)
    # Its newest real session decides: a later FAIL-RUN beats an earlier
    # pass; a reconstructed one or golden.txt's never does.
    twice = run(db, b, flips=1.0)
    judge(db, twice, "pass", started="2026-10-02T10:00:00Z")
    judge(db, twice, "FAIL-RUN", started="2026-10-03T10:00:00Z")
    late = run(db, b, flips=60.0)
    judge(db, late, "pass", started="2026-10-02T10:00:00Z")
    judge(db, late, "FAIL-RUN", kind="reconstructed", started="2026-10-04T10:00:00Z")
    judge(db, late, "FAIL-RUN", source="golden.txt", started="2026-10-04T11:00:00Z")
    row = only(regress.regress(db, db.cfg, game="demo"), scenario="menu")
    assert row["b_n"] == 4 and row["b_flips"] == 60.0 and row["flag"] == ""


def test_one_perf_row_per_run_and_the_steady_window(db, ab):
    a, b = ab
    ra = run(db, a, flips=60.0)
    rb = run(db, b, flips=60.0)
    with db.write() as w:
        # flip-trace is the more exact source of the same run.
        w.perf(rb, "run", "flip-trace", flips_exact=1, flips_per_s=30.0, ms_p50=16.7, ms_p95=17)
        w.perf(ra, "steady", "bench-matrix", flips_exact=1, flips_per_s=60, ms_p50=16, ms_p95=17)
        w.perf(rb, "steady", "bench-matrix", flips_exact=1, flips_per_s=60, ms_p50=16, ms_p95=30)
    res = regress.regress(db, db.cfg, game="demo")
    r = only(res, window="run")
    assert (r["b_n"], r["b_flips"], r["flag"]) == (1, 30.0, "flips")
    assert "(1 run)" in r["note"]
    assert only(res, window="steady")["flag"] == "frame-ms"


def test_thin_data_backends_and_groups(db, ab):
    a, b = ab
    run(db, a, scenario="gone")
    run(db, b, scenario="new")
    run(db, a, backend="cpu", flips=60.0)
    run(db, b, backend="cpu", flips=10.0)
    run(db, a, backend="metal", host="local", flips=60.0)
    run(db, b, backend="metal", host="local", flips=10.0)
    res = regress.regress(db, db.cfg, game="demo")
    assert only(res, scenario="gone")["note"] == "not run"
    assert only(res, scenario="new")["note"] == "no baseline"
    cpu = only(res, backend="cpu")
    assert cpu["flag"] == "" and cpu["note"].startswith("not checked (cpu)")
    assert only(res, backend="metal")["flag"] == "flips"


def test_baselines_against_a_build_a_release_or_a_stand_in(db):
    v1 = build(db, "2026-09-01T10:00:00Z", release="v1", match="stand-in")
    v2 = build(db, "2026-09-10T10:00:00Z", release="v2")
    with db.write() as w:
        w.release("demo", "v1", "1" * 40, "published", published="2026-09-01T12:00:00Z")
        w.release("demo", "v2", "2" * 40, "published", published="2026-09-10T12:00:00Z")
        w.release("demo", "v3", "3" * 40, "published", published="2026-12-01T12:00:00Z")
    b = build(db, "2026-10-05T10:00:00Z")
    branch = build(db, "2026-10-06T10:00:00Z", lineage="branch:x")
    for x, f in ((v1, 60.0), (v2, 30.0), (b, 30.0), (branch, 30.0)):
        run(db, x, flips=f)
    # Default: B is the newest integration build, A the release before it.
    res = regress.regress(db, db.cfg, game="demo")
    assert res["b"] == uuid(db, b) and res["a"] == "release v2"
    assert only(res)["flag"] == ""
    res = regress.regress(db, db.cfg, game="demo", release="v1")
    assert only(res)["flag"] == "flips" and "stand-in baseline" in only(res)["note"]
    res = regress.regress(db, db.cfg, build=uuid(db, branch)[:8], against=str(v1))
    assert res["a"] == uuid(db, v1) and only(res)["flag"] == "flips"
    with pytest.raises(DbError, match="not found"):
        regress.regress(db, db.cfg, build="nosuchbuild")


def test_previous_release_is_never_the_builds_own(db):
    with db.write() as w:
        w.release("demo", "v1", "1" * 40, "published", published="2026-09-01T12:00:00Z")
        w.release("demo", "v2", "2" * 40, "published", published="2026-09-10T12:00:00Z")
    b = db.one(
        "SELECT * FROM builds WHERE id = ?", (build(db, "2026-09-10T10:00:00Z", release="v2"),)
    )
    assert regress.previous_release(db, "demo", b) == "v1"


def test_no_integration_build(db):
    build(db, "2026-10-01T10:00:00Z", lineage="branch:x")
    with pytest.raises(DbError, match="no integration build"):
        regress.regress(db, db.cfg, game="demo")


# ---- build times -------------------------------------------------------------


def cold(db, day, secs, steps=None, **f):
    f.setdefault("warm", "cold")
    f.setdefault("host", None)
    return build(
        db,
        "2026-10-%02dT10:00:00Z" % day,
        secs=secs,
        jobs=8,
        gen_fresh=0,
        ccache=f.pop("ccache", "on 10/2"),
        toolchain=f.pop("toolchain", "tc1"),
        steps=steps,
        **f,
    )


def brow(res, measure):
    rows = [r for r in res["builds"] if r["measure"] == measure]
    assert len(rows) == 1, res["builds"]
    return rows[0]


def test_build_time_rise_with_the_floor_and_the_window(db):
    for d, s in ((1, 1000.0), (2, 100.0), (3, 100.0), (4, 100.0), (5, 100.0), (6, 100.0)):
        cold(db, d, s, steps=[("configure", 2.0), ("cmake --build", s - 2)])
    # The ccache counts differ, and still compare (on or off).
    b = cold(db, 7, 160.0, steps=[("configure", 4.0), ("cmake --build", 156.0)], ccache="on 0/99")
    res = regress.regress(db, db.cfg, build=uuid(db, b), runs=False)
    t = brow(res, "total")
    # The newest 5 only: day 1's 1000 s is out of the window.
    assert (t["n"], t["base"], t["flag"]) == (5, 100.0, "build-time")
    # configure doubled, but by 2 s: under the 30 s floor.
    assert brow(res, "step configure")["flag"] == ""
    assert brow(res, "step cmake --build")["flag"] == "build-time"
    assert res["runs"] == [] and res["flags"] == 2


def test_comparable_builds_only(db):
    for d in range(1, 4):
        cold(db, d, 100.0)
    cold(db, 4, 100.0, toolchain="tc0")  # another toolchain
    b = cold(db, 6, 400.0, toolchain="tc2")  # a toolchain bump: a new baseline
    res = regress.regress(db, db.cfg, build=uuid(db, b), runs=False)
    assert brow(res, "total")["note"] == "no baseline"
    b = cold(db, 7, 400.0, ccache="off")
    assert brow(regress.regress(db, db.cfg, build=uuid(db, b), runs=False), "total")["note"] == (
        "no baseline"
    )


def test_few_baselines_busy_and_failed(db):
    cold(db, 1, 100.0)
    cold(db, 2, 100.0)
    cold(db, 3, 100.0, load_start=20.0, ncpu=8)  # busy: out of the baseline
    b = cold(db, 4, 400.0)
    t = brow(regress.regress(db, db.cfg, build=uuid(db, b), runs=False), "total")
    assert (t["n"], t["flag"], t["note"]) == (2, "", "(2 builds)")
    bad = cold(db, 5, 400.0, ok=0)
    t = brow(regress.regress(db, db.cfg, build=uuid(db, bad), runs=False), "total")
    assert t["note"] == "failed build, not compared"


def test_incremental_builds_by_secs_per_edge(db):
    def inc(day, secs, edges, steps=None):
        return cold(db, day, secs, warm="warm", kind="incremental", edges=edges, steps=steps)

    for d in range(1, 4):
        inc(d, 100.0, 100)
    # Few edges: secs/edge is not measured; the total is printed only.
    few = inc(4, 900.0, 10)
    res = regress.regress(db, db.cfg, build=uuid(db, few), runs=False)
    assert [r["measure"] for r in res["builds"]] == ["total"]
    assert brow(res, "total")["note"] == "incremental: printed, not flagged"
    slow = inc(5, 400.0, 100, steps=[("recomp", 50.0), ("cmake --build", 350.0)])
    res = regress.regress(db, db.cfg, build=uuid(db, slow), runs=False)
    assert brow(res, "secs/edge")["flag"] == "build-time"
    assert brow(res, "total")["flag"] == ""
    # recomp reused (gen_fresh 0): not a measure.
    assert all(r["measure"] != "step recomp" for r in res["builds"])


def test_against_a_release_uses_its_builds(db):
    for d in range(1, 4):
        cold(db, d, 100.0, release="v1")
    for d in range(4, 8):
        cold(db, d, 300.0)
    b = cold(db, 8, 310.0)
    res = regress.regress(db, db.cfg, build=uuid(db, b), runs=False)
    assert brow(res, "total")["flag"] == ""
    res = regress.regress(db, db.cfg, build=uuid(db, b), runs=False, release="v1")
    t = brow(res, "total")
    assert (t["n"], t["base"], t["flag"]) == (3, 100.0, "build-time")


def test_the_verb_and_strict(db, ab, monkeypatch, capsys):
    from xboxrecomp_cli.benchdb import cli

    a, b = ab
    run(db, a, flips=60.0)
    run(db, b, flips=10.0)
    monkeypatch.setattr(cli.store, "open", lambda *x, **k: db)
    assert cli.main(["--game", "demo", "regress"]) == 0
    out = capsys.readouterr().out
    assert "FLAG flips" in out
    assert cli.main(["--game", "demo", "regress", "--strict"]) == 1
    capsys.readouterr()
    assert cli.main(["--game", "demo", "regress", "--json", "--runs-only"]) == 0
    assert '"flags": 1' in capsys.readouterr().out


def test_live_lines_never_raise(db, ab, monkeypatch, capsys):
    from xboxrecomp_cli.benchdb import live

    a, b = ab
    run(db, a, flips=60.0)
    run(db, b, flips=10.0)
    monkeypatch.setattr(live, "here", lambda cwd: (db.cfg, db))
    got = []
    assert live.regress_lines(".", "demo", uuid(db, b), say=got.append) == 1
    assert any("FLAG flips" in x for x in got)
    got = []
    live.regress_lines(".", "demo", uuid(db, b), runs=False, say=got.append)
    assert not any("flips/s" in x for x in got)
    # No record, no db, or a bad uuid: nothing printed, nothing raised.
    assert live.regress_lines(".", "demo", None) == 0
    assert live.regress_lines(".", "demo", "ffffffffffff", say=got.append) == 0
    assert "regress skipped" in capsys.readouterr().err
    monkeypatch.setattr(live, "here", lambda cwd: None)
    assert live.regress_lines(".", "demo", uuid(db, b)) == 0
