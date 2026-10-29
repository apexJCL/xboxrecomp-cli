"""`benchdb VERB`: the bench results database's verbs (design D8). `<game>
bench db VERB` is the same command with --game set to the game's root."""

import argparse
import json
import os
import sqlite3
import sys

from . import config as config_mod
from . import ingest, queries, releases, schema, store

VERBS = {}
CONFIGURE = {}


def verb(name, help_, configure=None):
    def deco(fn):
        VERBS[name] = (help_, fn)
        if configure:
            CONFIGURE[name] = configure
        return fn

    return deco


# ---- output -------------------------------------------------------------------


def emit(rows, fmt="table", out=None):
    """Rows as an aligned table, --tsv or --json."""
    out = out or sys.stdout
    if fmt == "json":
        json.dump(rows, out, indent=1, sort_keys=False, default=str)
        out.write("\n")
        return
    if not rows:
        if fmt != "tsv":
            out.write("(no rows)\n")
        return
    cols = list(rows[0])
    cell = [[("" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows]
    if fmt == "tsv":
        out.write("\t".join(cols) + "\n")
        for c in cell:
            out.write("\t".join(x.replace("\t", " ") for x in c) + "\n")
        return
    w = [max(len(c), *(len(x[i]) for x in cell)) for i, c in enumerate(cols)]
    w = [min(x, 60) for x in w]
    out.write("  ".join(c.ljust(w[i]) for i, c in enumerate(cols)).rstrip() + "\n")
    for c in cell:
        out.write("  ".join(x[: w[i]].ljust(w[i]) for i, x in enumerate(c)).rstrip() + "\n")


def add_format(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", dest="fmt", action="store_const", const="json")
    g.add_argument("--tsv", dest="fmt", action="store_const", const="tsv")
    p.set_defaults(fmt="table")


# ---- init ---------------------------------------------------------------------


@verb("init", "create or migrate the db and load the roots and hosts from benchdb.toml")
def cmd_init(a, cfg):
    db = store.open(a.db, cfg)
    if db.readonly:
        print("benchdb: %s: %s; not migrated" % (db.path, db.note))
        return 1
    v = db.one("PRAGMA user_version")
    print("benchdb: %s at schema %d" % (db.path, list(v.values())[0] if v else schema.LATEST))
    if not cfg.present:
        print("benchdb: no %s: no roots or hosts loaded" % config_mod.NAME)
    else:
        n = db.one("SELECT (SELECT count(*) FROM roots) AS r, (SELECT count(*) FROM hosts) AS h")
        print("benchdb: %d roots, %d hosts from %s" % (n["r"], n["h"], cfg.path))
    return 0


# ---- import -------------------------------------------------------------------


class _Undo(Exception):
    pass


def do_import(db, cfg, items, dry_run=False, game=None):
    """Each item in its own short transaction (rolled back for a dry run).
    An item that fails is counted and reported; the others still land."""
    counts = ingest.new_counts()
    for kind, path in items:
        try:
            with db.write() as w:
                ingest.ingest_item(w, cfg, kind, path, counts)
                if dry_run:
                    raise _Undo()
        except _Undo:
            pass
        except (store.DbError, sqlite3.Error, OSError, ValueError, KeyError) as e:
            counts["failed"] += 1
            counts["notes"].append((path.replace("\0", " "), "failed", str(e)))
    return counts


def configure_import(p):
    p.add_argument("paths", nargs="*", metavar="PATH", help="run dirs, TSVs, records, a store")
    p.add_argument("--all", action="store_true", help="every root, record and inbox (D12)")
    p.add_argument("--rebuild", action="store_true", help="rebuild the db from disk (D9)")
    p.add_argument("--dry-run", action="store_true", help="count, write nothing")


@verb(
    "import",
    "import run dirs, session TSVs, tests-pass files, build records and inboxes",
    configure_import,
)
def cmd_import(a, cfg):
    if not (a.paths or a.all or a.rebuild):
        print("benchdb import: give PATHs, --all or --rebuild", file=sys.stderr)
        return 2
    db = store.open(a.db, cfg)
    if a.rebuild:
        if a.dry_run:
            print("benchdb import: --rebuild has no --dry-run", file=sys.stderr)
            return 2
        box = {}

        def fill(tmp):
            box["c"] = do_import(tmp, cfg, ingest.walk_all(cfg))
            releases.match(tmp, cfg)

        def refill(tmp, since):
            # Under the live lock: what landed on disk during the fill. The
            # bundles come from the game repos, which no bench writes.
            items = [
                (k, p)
                for k, p in ingest.walk_all(cfg)
                if k != "bundles" and store.touched_since(p, since)
            ]
            box["r"] = do_import(tmp, cfg, items)
            releases.match(tmp, cfg)

        db.rebuild(fill, refill)
        c = box["c"]
        c["failed"] += box["r"]["failed"]
        c["notes"] += [n for n in box["r"]["notes"] if n not in c["notes"]]
    else:
        items = ingest.walk_all(cfg) if a.all else []
        items += ingest.expand(a.paths)
        c = do_import(db, cfg, items, a.dry_run)
        if not a.dry_run:
            releases.match(db, cfg)
    print(
        "benchdb import%s: %d added, %d updated, %d partial, %d unparsed, %d failed"
        % (
            " (dry run)" if a.dry_run else "",
            c["added"],
            c["updated"],
            c["partial"],
            c["unparsed"],
            c["failed"],
        )
    )
    for path, status, why in c["notes"]:
        print("  %s %s: %s" % (status, path, why))
    return 1 if c["failed"] else 0


# ---- releases -----------------------------------------------------------------


def configure_releases(p):
    p.add_argument("--refresh", action="store_true", help="read the tags and export maps again")
    p.add_argument("--local", action="store_true", help="with --refresh: no ls-remote")
    add_format(p)


@verb("releases", "list the published releases per game and their builds", configure_releases)
def cmd_releases(a, cfg):
    if a.refresh:
        db = store.open(a.db, cfg)
        if not cfg.games:
            print("benchdb releases: benchdb.toml lists no [games]", file=sys.stderr)
            return 1
        # A failed ls-remote is a warning (local tags only). A game whose
        # refresh fails exits 1, after every other game has been refreshed.
        failed = []
        # Warnings go to stderr, so --format json stays parseable.
        releases.refresh(
            db,
            cfg,
            a.game,
            remote=not a.local,
            say=lambda m: print(m, file=sys.stderr),
            failed=failed,
        )
    else:
        failed = []
        db = store.open(a.db, cfg, readonly=True)
    rows = []
    for r in releases.listing(db, a.game):
        marks = ", ".join(
            "%s/%s#%d%s"
            % (
                b["target"],
                b["host"] or "?",
                b["id"],
                " (stand-in)" if b["release_match"] == "stand-in" else "",
            )
            for b in r["builds"]
        )
        rows.append(
            {
                "game": r["game"],
                "tag": r["tag"],
                "state": r["state"],
                "published": r["published"],
                "game_sha": (r["game_sha"] or "")[:12],
                "toolkit": (r["toolkit_sha"] or "")[:12],
                "cli": (r["cli_sha"] or "")[:12],
                "builds": marks or "-",
            }
        )
    emit(rows, a.fmt)
    return 1 if failed else 0


# ---- show ---------------------------------------------------------------------


def configure_show(p):
    p.add_argument("what", metavar="RUN|BUILD|SESSION|RELEASE", help="a stamp, path, uuid or tag")
    add_format(p)


def find(db, what, game=None):
    """(kind, row) of a stamp/run path, build id or uuid, session uuid or tag."""
    g = " AND game = ?" if game else ""
    ga = [game] if game else []
    r = db.sql("SELECT * FROM releases WHERE tag = ?" + g, [what] + ga)
    if r:
        return "release", r
    r = db.sql("SELECT * FROM builds WHERE uuid LIKE ?" + g, [what + "%"] + ga)
    if len(r) == 1:
        return "build", r
    r = db.sql("SELECT * FROM sessions WHERE uuid = ?" + g, [what] + ga)
    if r:
        return "session", r
    rel = what.split("/", 1)
    r = db.sql(
        "SELECT * FROM runs WHERE (stamp = ? OR relpath = ? OR relpath LIKE ? "
        "OR (root = ? AND relpath = ?))" + g,
        [what, what, "%/" + what, rel[0], rel[-1]] + ga,
    )
    if r:
        return "run", r
    if what.isdigit():
        r = db.sql("SELECT * FROM builds WHERE id = ?", (int(what),))
        if r:
            return "build", r
    return None, []


@verb("show", "one run, build, session or release with its child rows", configure_show)
def cmd_show(a, cfg):
    db = store.open(a.db, cfg, readonly=True)
    kind, rows = find(db, a.what, a.game)
    if not rows:
        print("benchdb show: nothing is %r" % a.what, file=sys.stderr)
        return 1
    if len(rows) > 1:
        print("benchdb show: %r names %d %ss:" % (a.what, len(rows), kind), file=sys.stderr)
        for r in rows:
            print(
                "  %s/%s" % (r.get("root", ""), r.get("relpath", r.get("uuid", ""))),
                file=sys.stderr,
            )
        return 1
    row = rows[0]
    out = {kind: row}
    if kind == "run":
        out["perf"] = db.sql("SELECT * FROM perf WHERE run_id = ?", (row["id"],))
        out["metrics"] = db.sql(
            "SELECT name, value, unit FROM metrics WHERE run_id = ?", (row["id"],)
        )
        out["frames"] = db.sql("SELECT * FROM frame_checks WHERE run_id = ?", (row["id"],))
        out["verdicts"] = db.sql(
            "SELECT s.uuid, s.kind, s.source, v.word FROM verdicts v "
            "JOIN sessions s ON s.id = v.session_id WHERE v.run_id = ?",
            (row["id"],),
        )
        out["artifacts"] = db.sql(
            "SELECT relpath, kind, heavy, bytes, state FROM artifacts WHERE run_id = ?",
            (row["id"],),
        )
        if row["build_id"]:
            out["build"] = db.one("SELECT * FROM builds WHERE id = ?", (row["build_id"],))
    elif kind == "build":
        out["steps"] = db.sql(
            "SELECT step, secs, detail FROM build_steps WHERE build_id = ? ORDER BY seq",
            (row["id"],),
        )
        out["runs"] = db.sql(
            "SELECT stamp, host, backend, scenario, kind, root, relpath FROM runs WHERE build_id = ?",
            (row["id"],),
        )
    elif kind == "session":
        out["verdicts"] = db.sql(
            "SELECT v.scenario, v.word, r.root, r.relpath FROM verdicts v "
            "LEFT JOIN runs r ON r.id = v.run_id WHERE v.session_id = ?",
            (row["id"],),
        )
    elif kind == "release":
        out["builds"] = db.sql(
            "SELECT id, uuid, target, host, release_match, exe_sha256 FROM builds "
            "WHERE game = ? AND release = ?",
            (row["game"], row["tag"]),
        )
    if a.fmt == "json":
        emit(out, "json")
        return 0
    for k, v in out.items():
        print("== %s" % k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                if vv is not None:
                    print("  %-16s %s" % (kk, vv))
        else:
            emit(v, a.fmt)
    return 0


# ---- query --------------------------------------------------------------------


def configure_query(p):
    p.add_argument(
        "name",
        nargs="?",
        help="; ".join("%s: %s" % (k, v[2]) for k, v in sorted(queries.QUERIES.items())),
    )
    p.add_argument("--sql", help="a read-only SELECT")
    for f in ("host", "backend", "scenario"):
        p.add_argument("--" + f)
    p.add_argument("--since", help="ISO date or time (UTC)")
    add_format(p)


@verb("query", "a named query, or --sql for a read-only SELECT", configure_query)
def cmd_query(a, cfg):
    if bool(a.name) == bool(a.sql):
        print("benchdb query: give a NAME or --sql", file=sys.stderr)
        return 2
    db = store.open(a.db, cfg, readonly=True)
    if a.sql:
        try:
            rows = db.sql(a.sql)
        except sqlite3.Error as e:
            print("benchdb query: %s" % e, file=sys.stderr)
            return 1
    else:
        rows = db.query(
            a.name,
            game=a.game,
            host=a.host,
            backend=a.backend,
            scenario=a.scenario,
            since=a.since,
        )
    emit(rows, a.fmt)
    return 0


# ---- export -------------------------------------------------------------------

TABLES = (
    "releases",
    "builds",
    "build_steps",
    "runs",
    "perf",
    "metrics",
    "sessions",
    "verdicts",
    "frame_checks",
    "test_passes",
    "artifacts",
    "pins",
    "deletions",
)


def configure_export(p):
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sessions-tsv", metavar="FILE", help="golden-sessions.tsv lines ('-': stdout)")
    g.add_argument(
        "--jsonl", metavar="DIR", nargs="?", const="", help="one <table>.jsonl per table"
    )


def under(p, base):
    p, base = os.path.realpath(p), os.path.realpath(base)
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


def pin_source(cfg, path):
    """Whether path is read as reference text (D11 guard 3): an export there
    would cite every stamp and pin the whole store."""
    if any(under(path, x) for x in cfg.refs_exclude):
        return False
    if any(under(path, x) for x in cfg.refs):
        return True
    for g in cfg.games.values():
        if under(path, g):
            return True
    return False


def session_lines(db, game=None):
    """sessions as golden-sessions.tsv lines (D17 Q4)."""
    out = []
    q = "SELECT * FROM sessions WHERE kind != 'reconstructed'"
    args = []
    if game:
        q += " AND game = ?"
        args.append(game)
    for s in db.sql(q + " ORDER BY started, id", args):
        f = [(s["started"] or "").replace("T", " ").rstrip("Z"), s["kind"]]
        f.append("tests=%s" % (s["tests"] or ""))
        f.append("only=%s" % (s["only"] or ""))
        for v in db.sql(
            "SELECT v.scenario, v.word, r.relpath FROM verdicts v "
            "LEFT JOIN runs r ON r.id = v.run_id WHERE v.session_id = ? ORDER BY v.scenario",
            (s["id"],),
        ):
            f.append("%s=%s:%s" % (v["scenario"], (v["relpath"] or "?").split("/")[-1], v["word"]))
        f.append("rc=%s" % ("unknown" if s["rc"] is None else s["rc"]))
        if s["host"]:
            f.append("host=%s" % s["host"])
        out.append("\t".join(f))
    return out


@verb("export", "the sessions as a TSV, or every table as JSONL", configure_export)
def cmd_export(a, cfg):
    db = store.open(a.db, cfg, readonly=True)
    if a.sessions_tsv is not None:
        lines = session_lines(db, a.game)
        if a.sessions_tsv == "-":
            sys.stdout.write("".join(x + "\n" for x in lines))
            return 0
        target = os.path.abspath(a.sessions_tsv)
    else:
        target = os.path.abspath(a.jsonl) if a.jsonl else os.path.join(cfg.dir, "notes", "bench-db")
    if cfg.present and pin_source(cfg, target):
        print(
            "benchdb export: %s is read for pins (D11 guard 3); list it in [refs] exclude first"
            % target,
            file=sys.stderr,
        )
        return 1
    if a.sessions_tsv is not None:
        with open(target, "w", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))
        print("benchdb export: %d sessions to %s" % (len(lines), target))
        return 0
    os.makedirs(target, exist_ok=True)
    for t in TABLES:
        rows = db.sql("SELECT * FROM %s" % t)
        with open(os.path.join(target, t + ".jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    print("benchdb export: %d tables to %s" % (len(TABLES), target))
    return 0


def _cfg_pull(p):
    p.add_argument("--host", help="only this [hosts] key (default: every host with an inbox)")


@verb("pull", "import the records in the hosts' inboxes, then delete them there", _cfg_pull)
def cmd_pull(a, cfg):
    from . import pull

    res = pull.pull(cfg, a.host, db=store.open(a.db, cfg))
    return 1 if any(f for _n, f in res.values()) else 0


# ---- cleanup (D11) ------------------------------------------------------------


def _cfg_pins(p):
    p.add_argument("--refresh", action="store_true", help="re-read the reference text first")
    add_format(p)


@verb("pins", "the stamps the reference text cites, and where (D11 guard 3)", _cfg_pins)
def cmd_pins(a, cfg):
    from . import clean

    if a.refresh:
        cfg.require("pins --refresh")
        n = clean.refresh_pins(cfg, store.open(a.db, cfg))
        print("benchdb pins: %d citations" % n, file=sys.stderr)
    db = store.open(a.db, cfg, readonly=True)
    emit(db.sql("SELECT stamp, source, refreshed FROM pins ORDER BY stamp, source"), a.fmt)
    return 0


def _cfg_plan(p):
    p.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="a root's name (a sweep, even when the cwd holds an entry so named), "
        "host:path, or a local path such as ./runs (default: the local store and runs roots)",
    )
    p.add_argument("--older-than", type=float, metavar="DAYS", help="guard 5's age for every root")
    p.add_argument(
        "--force-dirty",
        action="store_true",
        help="skip guard 4 (a worktree's uncommitted work), after looking",
    )
    p.add_argument(
        "--force-young",
        action="store_true",
        help="allow --older-than under 1 day (a run being written could go), after looking",
    )
    p.add_argument(
        "--states",
        metavar="FILE",
        help="judge each citation by the tracker states in FILE (as [pending] does): "
        "a citation of closed work no longer pins",
    )
    p.add_argument(
        "--guard-hours",
        type=float,
        metavar="H",
        help="the store and runs roots' age for an unpinned, not live run "
        "(as [clean] guard_hours; at least 1; needs tracker states)",
    )
    p.add_argument(
        "--frames",
        action="store_true",
        help="plan only the frame dumps of passing, unpinned runs in the db",
    )
    p.add_argument("--json", action="store_true", help="the plan as JSON")


@verb("plan", "what may be deleted and why, and what is refused; writes a plan", _cfg_plan)
def cmd_plan(a, cfg):
    from . import clean

    cfg.require("plan")
    db = store.open(a.db, cfg, readonly=True)
    plan = clean.make_plan(
        cfg,
        db,
        a.targets,
        a.older_than,
        a.force_dirty,
        force_young=a.force_young,
        states=os.path.abspath(a.states) if a.states else None,
        guard_hours=a.guard_hours,
        frames=a.frames,
    )
    pid, path = clean.save_plan(cfg, plan)
    if a.json:
        json.dump(plan, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        clean.table(plan)
        print("benchdb clean %s   # deletes it (within the hour)" % pid)
    return 0


def _cfg_clean(p):
    p.add_argument("plan", metavar="PLAN_ID", help="a plan id from benchdb plan")
    p.add_argument("--large", action="store_true", help="allow over 100 GB or 20%% of a disk")
    p.add_argument("--force-dirty", action="store_true", help="as plan's --force-dirty")
    p.add_argument("--json", action="store_true", help="the result as JSON")


@verb("clean", "delete what a plan and a fresh plan both hold (D11.5)", _cfg_clean)
def cmd_clean(a, cfg):
    from . import clean

    cfg.require("clean")
    db = store.open(a.db, cfg)
    lines = []
    out = clean.clean(
        cfg, db, a.plan, a.large, a.force_dirty, say=lines.append if a.json else print
    )
    if a.json:
        json.dump(dict(out, lines=lines), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(
            "benchdb clean %s: %d deleted (%s), %d skipped, %d failed"
            % (a.plan, out["deleted"], clean.human(out["bytes"]), out["skipped"], out["failed"])
        )
    return 1 if out["failed"] else 0


@verb("gc", "mark vanished artifacts missing, optimise, rotate backups, vacuum")
def cmd_gc(a, cfg):
    from . import clean

    clean.gc(cfg, store.open(a.db, cfg))
    return 0


# ---- regress (D13) ------------------------------------------------------------


def _cfg_regress(p):
    p.add_argument(
        "--build", help="build B: a uuid, uuid prefix or id (default: the newest integration build)"
    )
    p.add_argument("--against", help="build A for the runs (default: the previous release's)")
    p.add_argument("--release", help="compare against this release's builds")
    only = p.add_mutually_exclusive_group()
    only.add_argument("--builds-only", action="store_true", help="only the build times")
    only.add_argument("--runs-only", action="store_true", help="only the runs")
    p.add_argument("--strict", action="store_true", help="exit 1 when anything is flagged")
    p.add_argument("--json", action="store_true", help="the result as JSON")


@verb("regress", "a build's runs and build times against its baseline (D13)", _cfg_regress)
def cmd_regress(a, cfg):
    from . import regress

    db = store.open(a.db, cfg, readonly=True)
    res = regress.regress(
        db,
        cfg,
        a.game,
        a.build,
        a.against,
        a.release,
        runs=not a.builds_only,
        builds=not a.runs_only,
    )
    if a.json:
        json.dump(res, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for line in regress.lines(res):
            print(line)
    return 1 if a.strict and res["flags"] else 0


# ---- the parser ---------------------------------------------------------------


def make_parser(prog="benchdb"):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="the db file (default: benchdb.toml's db)")
    ap.add_argument(
        "--config", help="benchdb.toml (default: $BENCHDB_CONFIG, else found from the cwd)"
    )
    ap.add_argument("--game", help="limit to this game's slug, or its root dir")
    sub = ap.add_subparsers(dest="verb", metavar="verb")
    sub.required = True
    for name, (help_, fn) in VERBS.items():
        p = sub.add_parser(name, help=help_, description=help_)
        CONFIGURE.get(name, lambda p: None)(p)
        p.set_defaults(func=fn)
    return ap


def game_slug(g):
    """--game takes a slug or a game root (the `<game> bench db` alias
    passes the root); the slug is the root's game.toml slug."""
    if not g or not os.path.isdir(g):
        return g
    from .. import manifest

    try:
        return manifest.load(g).slug
    except manifest.ManifestError:
        return os.path.basename(os.path.abspath(g))


def main(argv=None, prog="benchdb"):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        a = make_parser(prog).parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    try:
        cfg = config_mod.load(a.config)
        a.game = game_slug(a.game)
        return a.func(a, cfg) or 0
    except (config_mod.ConfigError, store.DbError, schema.SchemaError) as e:
        print("%s: %s" % (prog, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
