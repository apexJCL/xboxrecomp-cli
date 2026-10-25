"""Retention (design D10): which runs keep their large files.

Rows are kept forever; only a run's heavy artifacts (frames, disk images,
big logs, dumps) ever go. Per group (game, host, backend, scenario), the
first reason that holds keeps them:

  1. incomplete: the run has no exit code (being written, or a broken pull);
  2. young: it started less than [bench.gc] days ago;
  3. pinned: the reference text cites it (D11 guard 3);
  4. no build: it is linked to no build, so no rule can supersede it;
  5. release <tag>: its build is a match or the stand-in of one of the
     newest [bench.db] keep_releases published releases;
  6. internal: its build is one of the newest internal_keep internal builds
     since the last release (the newest integration build always among
     them, so branch builds never push main's baseline out);
  7. baseline <tag>: the group has no run on the newest release, so the
     two newest releases it did run on keep theirs.

Anything else is superseded, and its heavy artifacts may go."""

import datetime

from . import ingest


def parse_time(s):
    """A UTC timestamp (seconds) of an ISO time or a YYYYMMDD-HHMMSS stamp
    (taken as local time, as the stamp is); None when neither parses."""
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.UTC)
        return d.timestamp()
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(s[:15], "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


def build_time(b):
    return b["started"] or b["ended"] or ""


def internal_kept(builds, keep):
    """The ids of the newest `keep` builds (newest first), with the newest
    integration build among them even when newer branch builds exist."""
    if keep <= 0:
        return set()
    order = sorted(builds, key=lambda b: (build_time(b), b["id"]), reverse=True)
    top = order[:keep]
    integ = next((b for b in order if b["lineage"] == "integration"), None)
    if integ is not None and integ not in top:
        top = top[: keep - 1] + [integ]
    return {b["id"] for b in top}


def decide(db, cfg, pinned=None, now=None, game=None):
    """{run id: (keep, reason)} for every run in the db (of game, if set).
    pinned is a function of a run row that returns the citing source or
    None (default: never pinned)."""
    now = now if now is not None else datetime.datetime.now(datetime.UTC).timestamp()
    pinned = pinned or (lambda r: None)
    out = {}
    q = "SELECT * FROM runs"
    args = []
    if game:
        q += " WHERE game = ?"
        args.append(game)
    runs = db.sql(q, args)
    games = sorted({r["game"] for r in runs})
    for slug in games:
        gm = ingest.settings(cfg, slug)
        dbs = gm["bench"]["db"]
        days = float(gm["bench"]["gc"]["days"])
        rels = db.sql(
            "SELECT tag, published FROM releases WHERE game = ? AND state = 'published' "
            "ORDER BY coalesce(published, '') DESC, tag DESC",
            (slug,),
        )
        tags = [r["tag"] for r in rels]
        keep_tags = set(tags[: max(0, int(dbs["keep_releases"]))])
        newest = tags[0] if tags else None
        builds = {b["id"]: b for b in db.sql("SELECT * FROM builds WHERE game = ?", (slug,))}
        mine = [r for r in runs if r["game"] == slug]
        groups = {}
        for r in mine:
            groups.setdefault((r["host"], r["backend"], r["scenario"]), []).append(r)
        for rs in groups.values():
            # Internal builds since the last release (ever, with no release),
            # among those this group ran.
            internal = {}
            for r in rs:
                b = builds.get(r["build_id"])
                if b is None or b["release"]:
                    continue
                if newest is None or b["after_release"] == newest:
                    internal[b["id"]] = b
            kept_internal = internal_kept(list(internal.values()), int(dbs["internal_keep"]))
            ran = []
            for t in tags:
                if any(builds.get(r["build_id"], {}).get("release") == t for r in rs):
                    ran.append(t)
            baseline = set()
            if newest is not None and newest not in ran:
                baseline = set(ran[:2])
            for r in rs:
                out[r["id"]] = reason(
                    r,
                    builds.get(r["build_id"]),
                    now,
                    days,
                    pinned,
                    keep_tags,
                    kept_internal,
                    baseline,
                )
    return out


def reason(r, b, now, days, pinned, keep_tags, kept_internal, baseline):
    if r["exit_code"] is None:
        return True, "incomplete (no exit code)"
    t = parse_time(r["started"]) or parse_time(r["stamp"])
    if t is None or t >= now - days * 86400:
        return True, "younger than %gd" % days
    src = pinned(r)
    if src:
        return True, "pinned (%s)" % src
    if b is None:
        return True, "no build"
    if b["release"] in keep_tags:
        return True, "release %s%s" % (
            b["release"],
            " (stand-in)" if b["release_match"] == "stand-in" else "",
        )
    if b["id"] in kept_internal:
        return True, "internal" + (" (integration)" if b["lineage"] == "integration" else "")
    if b["release"] and b["release"] in baseline:
        return True, "baseline %s" % b["release"]
    return False, "superseded"
