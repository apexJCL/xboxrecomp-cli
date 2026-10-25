"""retain.decide (design D10): which runs keep their large files. The game
is not listed in the config, so the manifest defaults apply: 7 days,
keep_releases 2, internal_keep 3."""

import itertools

import pytest
from benchdb_util import open_db

from xboxrecomp_cli.benchdb import retain

NOW = retain.parse_time("2026-10-25T12:00:00Z")
OLD = "2026-10-01T10:00:00Z"
_seq = itertools.count()


@pytest.fixture
def db(tmp_path, monkeypatch):
    return open_db(str(tmp_path), monkeypatch=monkeypatch)


def release(db, tag, published):
    with db.write() as w:
        w.release("demo", tag, "1" * 40, "published", published=published)


def build(db, started, release=None, after=None, lineage="integration", match="match"):
    with db.write() as w:
        return w.build(
            "b%d" % next(_seq),
            "demo",
            "windows",
            "full",
            lineage=lineage,
            started=started,
            release=release,
            release_match=match if release else None,
            after_release=after,
        )


def run(db, b=None, started=OLD, exit_code=0, scenario="menu", host=None, backend="d3d11"):
    n = next(_seq)
    with db.write() as w:
        return w.run(
            "demo",
            "store",
            "20261001-1000%02d-r%d" % (n % 60, n),
            "20261001-100000",
            "golden",
            build=b,
            started=started,
            exit_code=exit_code,
            scenario=scenario,
            host=host,
            backend=backend,
        )


def test_first_rules_keep(db):
    b = build(db, OLD)
    incomplete = run(db, b, exit_code=None)
    young = run(db, b, started="2026-10-24T12:00:00Z")
    pinned = run(db, b)
    nobuild = run(db, None)
    out = retain.decide(
        db, db.cfg, pinned=lambda r: "TASKS.md" if r["id"] == pinned else None, now=NOW
    )
    assert out[incomplete] == (True, "incomplete (no exit code)")
    assert out[young] == (True, "younger than 7d")
    assert out[pinned] == (True, "pinned (TASKS.md)")
    assert out[nobuild] == (True, "no build")


def test_stamp_stands_in_for_a_missing_start(db):
    b = build(db, OLD)
    r = run(db, b, started=None)
    assert retain.decide(db, db.cfg, now=NOW)[r][1] != "younger than 7d"


def test_newest_two_releases_keep_theirs(db):
    for i, tag in enumerate(("v1", "v2", "v3")):
        release(db, tag, "2026-09-0%dT10:00:00Z" % (i + 1))
    r1 = run(db, build(db, OLD, release="v1"))
    r2 = run(db, build(db, OLD, release="v2", match="stand-in"))
    r3 = run(db, build(db, OLD, release="v3"))
    out = retain.decide(db, db.cfg, now=NOW)
    assert out[r3] == (True, "release v3")
    assert out[r2] == (True, "release v2 (stand-in)")
    assert out[r1] == (False, "superseded")


def test_internal_keeps_the_newest_three_since_the_release(db):
    release(db, "v1", "2026-09-01T10:00:00Z")
    rs = [run(db, build(db, "2026-10-0%dT10:00:00Z" % (i + 1), after="v1")) for i in range(5)]
    before = run(db, build(db, OLD, after="v0"))  # internal, but from before v1
    out = retain.decide(db, db.cfg, now=NOW)
    assert [out[r][0] for r in rs] == [False, False, True, True, True]
    assert out[rs[-1]] == (True, "internal (integration)")
    assert out[before] == (False, "superseded")


def test_branch_builds_never_push_out_the_newest_integration_build(db):
    integ = run(db, build(db, "2026-10-01T10:00:00Z"))
    older = run(db, build(db, "2026-09-30T10:00:00Z"))
    branch = [
        run(db, build(db, "2026-10-0%dT10:00:00Z" % (i + 2), lineage="branch:x")) for i in range(3)
    ]
    out = retain.decide(db, db.cfg, now=NOW)
    assert out[integ] == (True, "internal (integration)")
    assert [out[r] for r in branch] == [
        (False, "superseded"),
        (True, "internal"),
        (True, "internal"),
    ]
    assert out[older] == (False, "superseded")


def test_baseline_keeps_the_two_newest_releases_a_group_ran(db):
    for i, tag in enumerate(("v1", "v2", "v3", "v4")):
        release(db, tag, "2026-09-0%dT10:00:00Z" % (i + 1))
    # This group never ran v3 or v4, so its v2 and v1 runs are its baseline.
    old = [run(db, build(db, OLD, release=t), scenario="race") for t in ("v1", "v2")]
    run(db, build(db, OLD, release="v0"), scenario="race")
    # Another group that ran v4: its v1 run has no such excuse.
    other = run(db, build(db, OLD, release="v1"), scenario="menu")
    run(db, build(db, OLD, release="v4"), scenario="menu")
    out = retain.decide(db, db.cfg, now=NOW)
    assert out[old[0]] == (True, "baseline v1")
    assert out[old[1]] == (True, "baseline v2")
    assert out[other] == (False, "superseded")


def test_groups_are_per_host_backend_and_scenario(db):
    """Each group keeps its own newest internal builds."""
    a = [run(db, build(db, "2026-10-0%dT10:00:00Z" % (i + 1)), backend="metal") for i in range(4)]
    b = run(db, build(db, OLD), backend="d3d11")
    out = retain.decide(db, db.cfg, now=NOW)
    assert [out[r][0] for r in a] == [False, True, True, True]
    assert out[b][0]


def test_game_filter(db):
    r = run(db, build(db, OLD))
    assert retain.decide(db, db.cfg, now=NOW, game="other") == {}
    assert r in retain.decide(db, db.cfg, now=NOW, game="demo")


def test_internal_kept_edges():
    bs = [
        {"id": 1, "started": "2026-10-01", "ended": None, "lineage": "integration"},
        {"id": 2, "started": "2026-10-02", "ended": None, "lineage": "branch:a"},
    ]
    assert retain.internal_kept(bs, 0) == set()
    assert retain.internal_kept(bs, 1) == {1}
    assert retain.internal_kept(bs, 5) == {1, 2}


def test_parse_time():
    assert retain.parse_time("") is None
    assert retain.parse_time("nonsense") is None
    assert retain.parse_time("2026-10-25T12:00:00+00:00") == NOW
    assert retain.parse_time("20261025-120000") is not None
