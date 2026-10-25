"""benchdb regress (design D13): a build's runs against a baseline's by the
CLAUDE.md perf rules, and its build times against the comparable builds
before it. A flag is printed, never an exit code, unless --strict.

- Runs: B is the given build (default: the game's newest integration
  build); A is --against's build, --release's builds, or by default the
  builds of the release before B (its match or its stand-in, D10). Only
  clean runs count: stock, not busy, exact flips, and judged pass,
  pass-with-known or REGRESSION by their newest real session (an unjudged
  run counts on exit code 0). D3D11 and Metal are checked; any other
  backend is listed, never flagged.
- Builds: each measure of B against the median of the newest comparable
  builds before it (or --release's), flagged at build_rise x and
  build_min_secs longer. Only work that can be normalised is flagged:
  cold and full builds by total and step, a fresh recomp or analyze, the
  package and patch steps, and incremental builds by seconds per edge."""

import statistics

from . import ingest, retain
from .store import DbError

GPU = ("d3d11", "metal")
COUNTED = ("pass", "pass-with-known", "REGRESSION")
WINDOWS = ("run", "steady")
# One perf row per run and window, from its most exact source.
SOURCES = ("flip-trace", "pacing-stats", "bench-matrix", "run-info")
FRESH_STEPS = ("recomp", "analyze")
PACKAGE_STEPS = ("package", "patch")
P95_NOTE = "p95 is the sensitive figure: a paced stock run's flips/s shows stalls, not speedups"

RUNS_SQL = """
WITH judged AS (
  SELECT v.run_id, v.word,
         ROW_NUMBER() OVER (PARTITION BY v.run_id ORDER BY s.started DESC, s.id DESC) AS k
  FROM verdicts v JOIN sessions s ON s.id = v.session_id
  WHERE v.run_id IS NOT NULL AND s.kind <> 'reconstructed'
    AND COALESCE(s.source, '') NOT IN ('golden.txt', 'reconstructed-tsv')
)
SELECT r.id, r.host, r.backend, r.scenario, r.build_id, p.window, p.source,
       p.flips_per_s, p.ms_p50, p.ms_p95
FROM runs r
JOIN perf p ON p.run_id = r.id AND p.window IN ('run', 'steady')
LEFT JOIN judged j ON j.run_id = r.id AND j.k = 1
WHERE r.game = ? AND r.build_id IN (%s)
  AND r.stock = 1 AND COALESCE(r.busy, 0) = 0 AND p.flips_exact = 1
  AND ((j.word IS NULL AND r.exit_code = 0) OR j.word IN (%s))
"""


def rules(cfg, game):
    return ingest.settings(cfg, game)["bench"]["db"]["regress"]


def when(b):
    return b.get("started") or b.get("ended") or ""


# ---- which builds ------------------------------------------------------------


def find_build(db, ref, game=None):
    """A build by uuid, a uuid prefix of 8 or more, or its id."""
    ref = str(ref)
    rows = db.sql("SELECT * FROM builds WHERE uuid = ?", (ref,))
    if not rows and len(ref) >= 8:
        rows = db.sql("SELECT * FROM builds WHERE uuid LIKE ?", (ref + "%",))
    if not rows and ref.isdigit():
        rows = db.sql("SELECT * FROM builds WHERE id = ?", (int(ref),))
    if game:
        rows = [r for r in rows if r["game"] == game]
    if len(rows) != 1:
        raise DbError("build %s: %s" % (ref, "not found" if not rows else "ambiguous"))
    return rows[0]


def newest_integration(db, game):
    b = db.one(
        "SELECT * FROM builds WHERE game = ? AND lineage = 'integration' AND kind <> 'package'"
        " AND kind <> 'patch' AND COALESCE(ok, 1) = 1"
        " ORDER BY COALESCE(started, ended) DESC, id DESC LIMIT 1",
        (game,),
    )
    if b is None:
        raise DbError("%s has no integration build: pass --build" % game)
    return b


def release_builds(db, game, tag):
    return db.sql(
        "SELECT * FROM builds WHERE game = ? AND release = ? AND COALESCE(ok, 1) = 1",
        (game, tag),
    )


def previous_release(db, game, b):
    """The newest published release before B (never B's own)."""
    t = retain.parse_time(when(b))
    for r in db.sql(
        "SELECT tag, published FROM releases WHERE game = ? AND state = 'published'"
        " ORDER BY published DESC",
        (game,),
    ):
        if r["tag"] == b.get("release"):
            continue
        p = retain.parse_time(r["published"])
        if t is None or p is None or p < t:
            return r["tag"]
    return None


def game_of(db, game, build):
    if build:
        return find_build(db, build, game)["game"]
    if game:
        return game
    gs = [r["game"] for r in db.sql("SELECT DISTINCT game FROM builds ORDER BY game")]
    if len(gs) != 1:
        raise DbError("pass --game (the db has %s)" % (", ".join(gs) or "no builds"))
    return gs[0]


# ---- runs --------------------------------------------------------------------


def clean_perf(db, game, build_ids):
    """{(host, backend, scenario, window): {build_id: [(flips/s, p50, p95)]}}
    from the clean runs of build_ids, one perf row per run and window."""
    if not build_ids:
        return {}
    rows = db.sql(
        RUNS_SQL % (",".join("?" * len(build_ids)), ",".join("?" * len(COUNTED))),
        [game] + list(build_ids) + list(COUNTED),
    )
    best = {}
    for r in rows:
        k = (r["id"], r["window"])
        rank = SOURCES.index(r["source"]) if r["source"] in SOURCES else len(SOURCES)
        if k not in best or rank < best[k][0]:
            best[k] = (rank, r)
    out = {}
    for _rank, r in best.values():
        g = (r["host"], r["backend"], r["scenario"], r["window"])
        out.setdefault(g, {}).setdefault(r["build_id"], []).append(
            (r["flips_per_s"], r["ms_p50"], r["ms_p95"])
        )
    return out


def med(vals, i):
    xs = [v[i] for v in vals if v[i] is not None]
    return statistics.median(xs) if xs else None


def ratio(a, b):
    return round(b / a, 3) if a and b is not None else None


def compare_runs(db, cfg, game, b, a_builds, stand_in=False):
    rl = rules(cfg, game)
    a_ids = [x["id"] for x in a_builds]
    perf = clean_perf(db, game, [b["id"]] + a_ids)
    rows = []
    for g in sorted(perf, key=lambda g: tuple("" if x is None else str(x) for x in g)):
        host, backend, scenario, window = g
        by = perf[g]
        bv = by.get(b["id"], [])
        av = [v for i in a_ids for v in by.get(i, [])]
        if not bv and not av:
            continue
        row = dict(
            host=host,
            backend=backend,
            scenario=scenario,
            window=window,
            a_n=len(av),
            b_n=len(bv),
            a_flips=med(av, 0),
            b_flips=med(bv, 0),
            flips_ratio=None,
            a_p50=med(av, 1),
            b_p50=med(bv, 1),
            a_p95=med(av, 2),
            b_p95=med(bv, 2),
            flag="",
            note="",
        )
        notes = []
        if not av:
            notes.append("no baseline")
        elif not bv:
            notes.append("not run")
        else:
            row["flips_ratio"] = ratio(row["a_flips"], row["b_flips"])
            flags = []
            if row["a_flips"] and row["b_flips"] is not None:
                if row["b_flips"] <= (1 - rl["flips_drop"]) * row["a_flips"]:
                    flags.append("flips")
            for k in ("p50", "p95"):
                a, v = row["a_" + k], row["b_" + k]
                if a and v is not None and v >= rl["ms_rise"] * a:
                    flags.append("frame-ms")
                    break
            if backend not in GPU:
                if flags:
                    notes.append("not checked (%s)" % (backend or "no backend"))
            else:
                row["flag"] = ",".join(flags)
            if len(av) == 1 or len(bv) == 1:
                notes.append("(1 run)")
            if stand_in:
                notes.append("stand-in baseline")
        row["note"] = " ".join(notes)
        rows.append(row)
    return rows


# ---- builds ------------------------------------------------------------------


def ccache_on(c):
    if c is None:
        return None
    return "off" if c == "off" else "on"


def comparable(b):
    """D13's comparable builds: the same game, host, target, kind, warm,
    fresh gen, jobs, ccache on or off and toolchain. A standalone verb
    (target gen) is compared with the same verb only."""
    return (
        b["game"],
        b["host"],
        b["target"],
        b["kind"],
        b["warm"],
        b["gen_fresh"],
        b["jobs"],
        ccache_on(b["ccache"]),
        b["toolchain"],
        b["command"] if b["target"] == "gen" else None,
    )


def busy(b):
    return b["load_start"] is not None and b["ncpu"] and b["load_start"] > b["ncpu"]


def steps_of(db, bid):
    out = {}
    for s in db.sql(
        "SELECT step, secs, detail FROM build_steps WHERE build_id = ? ORDER BY seq", (bid,)
    ):
        if s["detail"] == "failed":
            continue
        out[s["step"]] = out.get(s["step"], 0.0) + (s["secs"] or 0.0)
    return out


def measures(db, b, min_edges):
    """[(measure, value, scale, flaggable)]: scale turns a difference into
    seconds of this build, for the build_min_secs floor."""
    steps = steps_of(db, b["id"])
    out = []
    whole = b["warm"] == "cold" or b["kind"] in ("full",) + PACKAGE_STEPS
    if b["secs"] is not None:
        out.append(("total", b["secs"], 1, whole))
    for name, secs in steps.items():
        if whole:
            out.append(("step " + name, secs, 1, True))
        elif name in PACKAGE_STEPS or (name in FRESH_STEPS and b["gen_fresh"] == 1):
            out.append(("step " + name, secs, 1, True))
    if not whole and b["edges"] and b["edges"] >= min_edges and b["secs"]:
        out.append(("secs/edge", b["secs"] / b["edges"], b["edges"], True))
    return out


def compare_build(db, cfg, b, release=None):
    rl = rules(cfg, b["game"])
    if b["ok"] == 0:
        return [
            dict(
                build=b["uuid"][:8],
                measure="total",
                base=None,
                n=0,
                value=b["secs"],
                ratio=None,
                flag="",
                note="failed build, not compared",
            )
        ]
    if release:
        pool = [x for x in release_builds(db, b["game"], release) if x["id"] != b["id"]]
    else:
        pool = db.sql(
            "SELECT * FROM builds WHERE game = ? AND id <> ? AND COALESCE(ok, 1) = 1"
            " ORDER BY COALESCE(started, ended) DESC, id DESC",
            (b["game"], b["id"]),
        )
        t = retain.parse_time(when(b))
        pool = [
            x
            for x in pool
            if t is not None
            and retain.parse_time(when(x)) is not None
            and retain.parse_time(when(x)) < t
        ]
    key = comparable(b)
    pool = [x for x in pool if comparable(x) == key and not busy(x)]
    base = {}
    for x in pool:
        for name, v, _s, _f in measures(db, x, rl["build_min_edges"]):
            base.setdefault(name, []).append(v)
    rows = []
    for name, v, scale, flaggable in measures(db, b, rl["build_min_edges"]):
        vals = base.get(name, [])
        if not release:
            vals = vals[: rl["build_window"]]
        m = statistics.median(vals) if vals else None
        notes, flag = [], ""
        if not flaggable:
            notes.append("incremental: printed, not flagged")
        elif not vals:
            notes.append("no baseline")
        elif len(vals) < rl["build_min_baselines"]:
            notes.append("(%d builds)" % len(vals))
        elif v >= rl["build_rise"] * m and (v - m) * scale >= rl["build_min_secs"]:
            flag = "build-time"
        if busy(b):
            notes.append("busy")
        rows.append(
            dict(
                build=b["uuid"][:8],
                measure=name,
                base=None if m is None else round(m, 4 if scale > 1 else 1),
                n=len(vals),
                value=round(v, 4 if scale > 1 else 1),
                ratio=ratio(m, v),
                flag=flag,
                note=" ".join(notes),
            )
        )
    return rows


# ---- the verb ----------------------------------------------------------------


def regress(db, cfg, game=None, build=None, against=None, release=None, runs=True, builds=True):
    """{"b", "a", "runs", "builds", "flags"}: B's run and build lines."""
    game = game_of(db, game, build)
    b = find_build(db, build, game) if build else newest_integration(db, game)
    out = dict(game=game, b=b["uuid"], a=None, runs=[], builds=[], flags=0)
    if runs:
        stand_in = False
        if against:
            a_builds = [find_build(db, against, game)]
            out["a"] = a_builds[0]["uuid"]
        else:
            tag = release or previous_release(db, game, b)
            a_builds = release_builds(db, game, tag) if tag else []
            stand_in = any(x["release_match"] == "stand-in" for x in a_builds)
            out["a"] = "release %s" % tag if tag else None
        out["runs"] = compare_runs(
            db, cfg, game, b, [x for x in a_builds if x["id"] != b["id"]], stand_in
        )
    if builds:
        out["builds"] = compare_build(db, cfg, b, release)
    out["flags"] = sum(1 for r in out["runs"] + out["builds"] if r["flag"])
    return out


def fmt(v, nd=1):
    if v is None:
        return "-"
    return ("%%.%df" % nd) % v if isinstance(v, float) else str(v)


RUN_LINE = (
    "  %-10s %-6s %-14s %-6s flips/s %s -> %s (%s)  p50 %s -> %s  p95 %s -> %s  n %d/%d  %s%s"
)
BUILD_LINE = "  %-22s %s (median of %d) -> %s (%s)  %s%s"


def flag(r):
    return ("FLAG " + r["flag"] + " ") if r["flag"] else ""


def lines(res):
    """The text of a regress result, one line per group or measure."""
    out = []
    if res["runs"] or res.get("a") is not None:
        out.append(
            "regress %s: build %s against %s"
            % (res["game"], res["b"][:8], res["a"] or "nothing (no previous release)")
        )
        for r in res["runs"]:
            text = RUN_LINE % (
                r["host"] or "-",
                r["backend"] or "-",
                r["scenario"] or "-",
                r["window"],
                fmt(r["a_flips"], 2),
                fmt(r["b_flips"], 2),
                fmt(r["flips_ratio"], 2),
                fmt(r["a_p50"]),
                fmt(r["b_p50"]),
                fmt(r["a_p95"]),
                fmt(r["b_p95"]),
                r["a_n"],
                r["b_n"],
                flag(r),
                r["note"],
            )
            out.append(text.rstrip())
        if res["runs"]:
            out.append("  (%s)" % P95_NOTE)
    if res["builds"]:
        out.append("build times %s:" % res["b"][:8])
        for r in res["builds"]:
            nd = 4 if r["measure"] == "secs/edge" else 1
            text = BUILD_LINE % (
                r["measure"],
                fmt(r["base"], nd),
                r["n"],
                fmt(r["value"], nd),
                fmt(r["ratio"], 2),
                flag(r),
                r["note"],
            )
            out.append(text.rstrip())
    return out
