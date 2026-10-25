"""The named queries of `benchdb query NAME` (D8). Each is a SELECT with
the common filters (--game, --host, --backend, --scenario, --since)
applied to the columns its table has."""

from .store import DbError

# name -> (SQL with a {where} slot, {filter: column}, help)
QUERIES = {
    "runs": (
        """SELECT r.stamp, r.game, r.host, r.backend, r.scenario, r.kind, r.status,
                  r.exit_code, p.flips, round(p.flips_per_s, 2) AS flips_per_s,
                  b.uuid AS build, r.root || '/' || r.relpath AS path, r.problems
           FROM runs r LEFT JOIN builds b ON b.id = r.build_id
           LEFT JOIN perf p ON p.run_id = r.id AND p.window = 'run' AND p.source = 'run-info'
           {where} ORDER BY r.started, r.id""",
        {
            "game": "r.game",
            "host": "r.host",
            "backend": "r.backend",
            "scenario": "r.scenario",
            "since": "r.started",
        },
        "every run, newest last",
    ),
    "sessions": (
        """SELECT s.started, s.game, s.kind, s.host, s.backend, s.source, s.rc,
                  group_concat(v.scenario || ':' || v.word, ' ') AS verdicts
           FROM sessions s LEFT JOIN verdicts v ON v.session_id = s.id
           {where} GROUP BY s.id ORDER BY s.started, s.id""",
        {
            "game": "s.game",
            "host": "s.host",
            "backend": "s.backend",
            "since": "s.started",
            "scenario": "v.scenario",
        },
        "golden sessions and their verdicts",
    ),
    "flakes": (
        """SELECT b.uuid AS build, s.game, s.host, v.scenario,
                  group_concat(DISTINCT v.word) AS words, count(*) AS n
           FROM verdicts v JOIN sessions s ON s.id = v.session_id
           JOIN builds b ON b.id = s.build_id
           {where} AND s.kind != 'reconstructed' AND s.source NOT IN ('reconstructed-tsv')
           GROUP BY b.id, s.host, v.scenario HAVING count(DISTINCT v.word) > 1
           ORDER BY s.game, v.scenario""",
        {
            "game": "s.game",
            "host": "s.host",
            "backend": "s.backend",
            "scenario": "v.scenario",
            "since": "s.started",
        },
        "scenarios with different verdicts on one build (reconstructed sessions left out)",
    ),
    "builds": (
        """SELECT b.id, b.uuid, b.game, b.target, b.host, b.kind, b.command, b.ended,
                  round(b.secs, 1) AS secs, b.ok, b.jobs, b.warm, b.ccache, b.edges,
                  b.release, b.release_match, b.after_release, b.lineage,
                  (SELECT group_concat(step || '=' || round(secs, 1), ' ') FROM build_steps
                   WHERE build_id = b.id) AS steps
           FROM builds b {where} ORDER BY coalesce(b.ended, b.started), b.id""",
        {"game": "b.game", "host": "b.host", "since": "coalesce(b.ended, b.started)"},
        "builds with their steps, jobs, warm and ccache",
    ),
    "perf": (
        """SELECT r.stamp, r.game, r.host, r.backend, r.scenario, p.window, p.source,
                  p.flips, round(p.flips_per_s, 2) AS flips_per_s, p.ms_p50, p.ms_p95,
                  p.ms_p99, p.ms_max, p.hitches, round(p.low1_fps, 1) AS low1_fps
           FROM perf p JOIN runs r ON r.id = p.run_id
           {where} ORDER BY r.started, r.id, p.window, p.source""",
        {
            "game": "r.game",
            "host": "r.host",
            "backend": "r.backend",
            "scenario": "r.scenario",
            "since": "r.started",
        },
        "every perf row",
    ),
    "series": (
        """SELECT r.game, r.host, r.backend, r.scenario, b.uuid AS build,
                  min(r.started) AS first, count(*) AS runs,
                  round(avg(p.flips_per_s), 2) AS flips_per_s,
                  round(avg(p.ms_p50), 2) AS ms_p50, round(avg(p.ms_p95), 2) AS ms_p95
           FROM runs r JOIN perf p ON p.run_id = r.id AND p.window = 'run'
                AND p.source = 'flip-trace'
           LEFT JOIN builds b ON b.id = r.build_id
           {where} GROUP BY r.game, r.host, r.backend, r.scenario, r.build_id
           ORDER BY r.game, r.host, r.backend, r.scenario, first""",
        {
            "game": "r.game",
            "host": "r.host",
            "backend": "r.backend",
            "scenario": "r.scenario",
            "since": "r.started",
        },
        "flips/s and p50/p95 per build over time, per group",
    ),
    "boot": (
        """SELECT r.game, r.host, r.backend, b.uuid AS build, min(r.started) AS first,
                  count(*) AS runs, round(avg(m.value), 2) AS first_flip_s
           FROM metrics m JOIN runs r ON r.id = m.run_id
           LEFT JOIN builds b ON b.id = r.build_id
           {where} AND m.name = 'first_flip_s'
           GROUP BY r.game, r.host, r.backend, r.build_id ORDER BY first""",
        {
            "game": "r.game",
            "host": "r.host",
            "backend": "r.backend",
            "scenario": "r.scenario",
            "since": "r.started",
        },
        "time to the first flip per build",
    ),
    "artifacts": (
        """SELECT a.root, a.kind, a.state, a.heavy, count(*) AS n,
                  sum(a.bytes) AS bytes
           FROM artifacts a LEFT JOIN runs r ON r.id = a.run_id
           {where} GROUP BY a.root, a.kind, a.state, a.heavy ORDER BY bytes DESC""",
        {
            "game": "r.game",
            "host": "r.host",
            "backend": "r.backend",
            "scenario": "r.scenario",
            "since": "r.started",
        },
        "size by root, kind and state",
    ),
    "pins": (
        "SELECT stamp, source, refreshed FROM pins {where} ORDER BY stamp, source",
        {},
        "the stamps and paths the reference text cites",
    ),
    "orphans": (
        """SELECT a.root, a.relpath, a.kind, a.bytes, a.state FROM artifacts a
           {where} AND a.run_id IS NULL AND a.build_id IS NULL ORDER BY a.root, a.relpath""",
        {},
        "artifacts no run or build owns",
    ),
    "deletions": (
        """SELECT at, plan, agent, root, relpath, bytes, reason, result FROM deletions
           {where} ORDER BY at, id""",
        {"since": "at"},
        "the journal of every delete",
    ),
}

FILTERS = ("game", "host", "backend", "scenario", "since")


def build(name, **filters):
    """(sql, params) of a named query with its filters."""
    if name not in QUERIES:
        raise DbError("no query %r (one of: %s)" % (name, ", ".join(sorted(QUERIES))))
    sql, cols, _help = QUERIES[name]
    where, params = ["1 = 1"], []
    for k in FILTERS:
        v = filters.get(k)
        if v is None:
            continue
        if k not in cols:
            raise DbError("query %s takes no --%s" % (name, k))
        if k == "since":
            where.append("%s >= ?" % cols[k])
        else:
            where.append("%s = ?" % cols[k])
        params.append(v)
    return sql.format(where="WHERE " + " AND ".join(where)), params


def run(db, name, **filters):
    sql, params = build(name, **filters)
    return db.sql(sql, params)
