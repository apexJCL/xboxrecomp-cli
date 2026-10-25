"""Releases (design D10) on a fixture git repo: tags under
refs/public-tags/, the export map and the toolkit map, the matching rules,
stand-ins, after_release, unmapped and withdrawn tags."""

import os
import subprocess
import time

import pytest
from benchdb_util import CLI_SHA, game_dir, open_db

from xboxrecomp_cli.benchdb import releases

PRIV_TK = "4" * 40
PUB_TK = "5" * 40
ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.invalid",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.invalid",
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
)


def git(repo, *args, date=None):
    env = dict(ENV)
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    return subprocess.run(
        ["git", "-C", repo, *args], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo, files, msg, date="2026-10-19T10:00:00Z"):
    for rel, text in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg, date=date)
    return git(repo, "rev-parse", "HEAD")


def public_tag(repo, tag, date, annotated=True):
    """refs/public-tags/<tag> on an orphan 'public' commit; its sha."""
    tree = git(repo, "mktree", "--missing")  # the empty tree, from no input
    pub = git(repo, "commit-tree", tree, "-m", "public %s" % tag, date=date)
    if annotated:
        git(repo, "tag", "-a", "-m", tag, tag, pub, date=date)
        obj = git(repo, "rev-parse", "refs/tags/" + tag)
        git(repo, "update-ref", "refs/public-tags/" + tag, obj)
        git(repo, "tag", "-d", tag)
    else:
        git(repo, "update-ref", "refs/public-tags/" + tag, pub)
    return pub


@pytest.fixture
def rel(d, monkeypatch):
    g = game_dir(d)
    with open(os.path.join(g, "game.toml")) as f:
        toml = f.read()
    toml = toml.replace(
        'commit = "6d51fa2d2b9eae4a31b22022925c6e72ad14dd04"', 'commit = "%s"' % PUB_TK
    ).replace('commit = "0000000000000000000000000000000000000000"', 'commit = "%s"' % CLI_SHA)
    with open(os.path.join(g, "game.toml"), "w") as f:
        f.write(toml)
    git(g, "init", "-q", "-b", "main")
    c0 = commit(g, {"src/a.c": "int a;\n"}, "c0", "2026-10-18T10:00:00Z")
    c1 = commit(g, {"src/a.c": "int a = 1;\n"}, "c1")
    c2 = commit(g, {"README.md": "docs\n"}, "c2", "2026-10-19T12:00:00Z")
    maps = os.path.join(d, "maps")
    os.makedirs(maps)
    pub = public_tag(g, "v1", "2026-10-20T10:00:00Z")
    with open(os.path.join(maps, "demo.map"), "w") as f:
        f.write("%s %s 2026-10-20T10:00:00Z\n" % (pub, c2))
    with open(os.path.join(d, "map.txt"), "w") as f:
        f.write("%s %s\n" % (PRIV_TK, PUB_TK))
    extra = '\n[games]\ndemo = "demo"\n\n[release]\nexport_maps = "maps"\ntoolkit_map = "map.txt"\n'
    db = open_db(d, extra, monkeypatch)
    return db, db.cfg, g, {"c0": c0, "c1": c1, "c2": c2, "pub": pub}


def build(db, uuid, target, game_sha, started, dirty=0, lineage="integration", kind="full"):
    with db.write() as w:
        return w.build(
            uuid,
            "demo",
            target,
            kind,
            lineage=lineage,
            game_sha=game_sha,
            game_dirty=dirty,
            toolkit_sha=PRIV_TK,
            toolkit_dirty=0,
            cli_sha=CLI_SHA,
            started=started,
        )


def by_uuid(db, uuid):
    return db.one("SELECT * FROM builds WHERE uuid = ?", (uuid,))


def test_refresh_reads_tags_and_maps(rel):
    db, cfg, g, c = rel
    seen = releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    assert seen == {"demo": ["v1"]}
    r = db.one("SELECT * FROM releases")
    assert r["state"] == "published" and r["game_sha"] == c["c2"]
    assert r["public_sha"] == c["pub"]
    assert r["published"] == "2026-10-20T10:00:00Z"
    assert r["toolkit_pin"] == PUB_TK and r["toolkit_sha"] == PRIV_TK
    assert r["cli_sha"] == CLI_SHA


def test_match_standin_and_after(rel):
    db, cfg, g, c = rel
    # Windows: c1 differs from the release commit only in docs: a match.
    build(db, "win-ok", "windows", c["c1"], "2026-10-19T11:00:00Z")
    # c0's src differs: not a match.
    build(db, "win-old", "windows", c["c0"], "2026-10-18T11:00:00Z")
    # macOS: only a dirty build before the release: the stand-in.
    build(db, "mac-dirty", "macos", c["c1"], "2026-10-19T11:30:00Z", dirty=1)
    # A branch build is never a stand-in (a clean one could still match).
    build(db, "mac-branch", "macos", c["c1"], "2026-10-19T11:40:00Z", dirty=1, lineage="branch:x")
    # After the release.
    build(db, "win-new", "windows", c["c2"], "2026-10-21T10:00:00Z", dirty=1)
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    assert (by_uuid(db, "win-ok")["release"], by_uuid(db, "win-ok")["release_match"]) == (
        "v1",
        "match",
    )
    assert by_uuid(db, "win-old")["release"] is None
    m = by_uuid(db, "mac-dirty")
    assert (m["release"], m["release_match"]) == ("v1", "stand-in")
    assert by_uuid(db, "mac-branch")["release"] is None
    assert by_uuid(db, "win-new")["after_release"] == "v1"
    rows = releases.listing(db, "demo")
    assert {b["target"]: b["release_match"] for b in rows[0]["builds"]} == {
        "windows": "match",
        "macos": "stand-in",
    }


def test_toolkit_dirty_or_other_sha_does_not_match(rel):
    db, cfg, g, c = rel
    with db.write() as w:
        w.build(
            "tk-dirty",
            "demo",
            "windows",
            "full",
            lineage="integration",
            game_sha=c["c2"],
            game_dirty=0,
            toolkit_sha=PRIV_TK,
            toolkit_dirty=1,
            cli_sha=CLI_SHA,
            started="2026-10-19T13:00:00Z",
        )
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    # Not a match; the newest integration build before the release stands in.
    assert by_uuid(db, "tk-dirty")["release_match"] == "stand-in"


def test_unmapped_and_withdrawn(rel):
    db, cfg, g, c = rel
    public_tag(g, "v2", "2026-10-22T10:00:00Z", annotated=False)
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    r = db.one("SELECT * FROM releases WHERE tag = 'v2'")
    assert r["state"] == "unmapped" and r["game_sha"] == ""
    git(g, "update-ref", "-d", "refs/public-tags/v2")
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    assert db.one("SELECT state FROM releases WHERE tag = 'v2'")["state"] == "withdrawn"


def test_failed_ls_remote_withdraws_nothing(rel):
    db, cfg, g, c = rel
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    git(g, "update-ref", "-d", "refs/public-tags/v1")
    said = []
    releases.refresh(db, cfg, "demo", remote=True, say=said.append)
    assert said and "ls-remote" in said[0]
    assert db.one("SELECT state FROM releases WHERE tag = 'v1'")["state"] == "published"


def test_by_prefix():
    m = {"abcdef1234": "x", "abd0000000": "y"}
    assert releases.by_prefix(m, "abcdef") == "x"
    assert releases.by_prefix(m, "ab") is None
    assert releases.by_prefix(m, None) is None


def test_after_tag_hook(rel, d, tmp_path):
    db, cfg, g, c = rel
    out = []
    assert releases.after_tag(g, say=out.append) == {"demo": ["v1"]}
    assert db.one("SELECT state FROM releases")["state"] == "published"
    # No benchdb.toml above the game: nothing done, nothing said.
    other = tmp_path / "lone"
    other.mkdir()
    assert releases.after_tag(str(other), say=out.append) is None
    # A broken config warns and never raises.
    with open(cfg.path, "a") as f:
        f.write("\n[[[ not toml\n")
    assert releases.after_tag(g, say=out.append) is None
    assert out and "refresh after the tag failed" in out[-1]


def test_a_missing_repo_keeps_the_matches(rel, capsys):
    """H3: matching with the game repo gone (a removed worktree, another
    machine) keeps the release columns as they were, and says so."""
    db, cfg, g, c = rel
    build(db, "win-ok", "windows", c["c1"], "2026-10-19T11:00:00Z")
    build(db, "mac-dirty", "macos", c["c1"], "2026-10-19T11:30:00Z", dirty=1)
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    cols = "SELECT uuid, release, release_match, after_release FROM builds ORDER BY uuid"
    before = [dict(x) for x in db.sql(cols)]
    assert before[1]["release_match"] == "match"
    os.rename(g, g + "-gone")
    releases.match(db, cfg, "demo")
    assert [dict(x) for x in db.sql(cols)] == before
    assert "cannot read the game repo" in capsys.readouterr().err
    # A repo that holds none of the builds' commits (another clone) is the same.
    os.rename(g + "-gone", g)
    with db.write() as w:
        w.sql("UPDATE builds SET game_sha = ?", ("f" * 40,))
    releases.match(db, cfg, "demo")
    assert [dict(x) for x in db.sql(cols)] == before


def test_times_are_compared_parsed(rel):
    """L13: a build at 11:00+01:00 is before a 10:30Z release, though its
    text sorts after."""
    db, cfg, g, c = rel
    build(db, "mac-dirty", "macos", c["c1"], "2026-10-20T11:00:00+01:00", dirty=1)
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    with db.write() as w:
        w.sql("UPDATE releases SET published = '2026-10-20T10:30:00Z'")
    releases.match(db, cfg, "demo")
    assert by_uuid(db, "mac-dirty")["release_match"] == "stand-in"


def test_a_build_without_times_takes_its_first_runs(rel):
    """A build whose run-info had no shipped line has no times: its first
    run's start places it, so a release still gets its stand-in and a later
    build its after_release."""
    db, cfg, g, c = rel
    old = build(db, "mac-old", "macos", c["c1"], None, dirty=1)
    new = build(db, "mac-new", "macos", c["c2"], None, dirty=1)
    with db.write() as w:
        for i, (b, t) in enumerate(
            [
                (old, "2026-10-19T11:30:00Z"),
                (old, "2026-10-21T09:00:00Z"),
                (new, "2026-10-21T10:00:00Z"),
            ]
        ):
            stamp = t[:10].replace("-", "") + "-" + t[11:19].replace(":", "")
            w.run("demo", "store", "r%d" % i, stamp, "golden", build=b, started=t)
    releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
    assert by_uuid(db, "mac-old")["release_match"] == "stand-in"
    assert by_uuid(db, "mac-new")["after_release"] == "v1"


def test_a_first_run_is_placed_by_its_stamp(rel, monkeypatch):
    """A Windows run-info time is naive local time: read as UTC it moves the
    run by the zone's offset. The stamp is read as local time and wins."""
    monkeypatch.setenv("TZ", "America/Mexico_City")
    time.tzset()
    try:
        db, cfg, g, c = rel
        new = build(db, "win-new", "windows", c["c2"], None, dirty=1)
        with db.write() as w:
            # 08:30 at UTC-6 is 14:30Z, after v1 (10:00Z); as UTC, before.
            w.run(
                "demo",
                "store",
                "r0",
                "20261020-083000",
                "golden",
                build=new,
                started="2026-10-20T08:30:00",
            )
        releases.refresh(db, cfg, "demo", remote=False, say=lambda m: None)
        assert by_uuid(db, "win-new")["after_release"] == "v1"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_one_failing_game_does_not_stop_the_others(rel, d):
    db, cfg, g, c = rel
    build(db, "win-ok", "windows", c["c1"], "2026-10-19T11:00:00Z")
    # A game whose export map is not text (a half-copied file) fails its
    # refresh; the next game is still read.
    with open(os.path.join(d, "maps", "aaa.map"), "wb") as f:
        f.write(b"\xff\xfe\x00garbage\n")
    cfg.games = dict(cfg.games, aaa=g)
    said, failed = [], []
    seen = releases.refresh(db, cfg, remote=False, say=said.append, failed=failed)
    assert seen == {"demo": ["v1"]} and failed == ["aaa"]
    assert any("aaa: refresh failed" in m for m in said)
    assert by_uuid(db, "win-ok")["release_match"] == "match"
