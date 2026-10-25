"""The db's schema: the version-1 DDL (design D5), the append-only
MIGRATIONS, and migrate(), which backs the db up before it moves it on.

A migration only adds tables, columns or indices, or rewrites derived
rows. A destructive change is `benchdb import --rebuild` instead (D6)."""

import os
import sqlite3

V1 = """
CREATE TABLE roots (
  name TEXT PRIMARY KEY,           -- from benchdb.toml: 'store', 'runs', 'deck-bench'
  host TEXT NOT NULL,              -- 'local' or the ssh alias
  path TEXT NOT NULL,
  kind TEXT NOT NULL,              -- store | runs | worktrees | scratch | host-scratch |
                                   -- inbox | checkout | notes
  deletable INTEGER NOT NULL       -- 0 for checkout and notes: never planned (D11)
);

CREATE TABLE hosts (
  key TEXT PRIMARY KEY,            -- 'local' (this machine), or a [hosts] key from benchdb.toml
  os TEXT NOT NULL,                -- 'macos' | 'windows' | 'linux-proton' | 'linux-native'
  label TEXT
);
CREATE TABLE host_names (          -- what a run-info's host: line says -> key
  seen TEXT PRIMARY KEY,           -- 'Scylla', 'GUINDOS', 'azathoth'
  host TEXT NOT NULL REFERENCES hosts(key)
);

CREATE TABLE releases (            -- published versions: the work that is wrapped up
  game TEXT NOT NULL,
  tag TEXT NOT NULL,               -- 'v2026.10.3'
  published TEXT,                  -- tag date
  public_sha TEXT,                 -- the commit the public tag points at
  game_sha TEXT NOT NULL,          -- the private commit, from <slug>.map
  toolkit_pin TEXT,                -- [toolkit] commit in that game.toml (a public-fork sha)
  toolkit_sha TEXT,                -- the private sha, through [release] toolkit_map
  cli_sha TEXT,                    -- [cli] commit
  state TEXT NOT NULL,             -- published | unmapped | withdrawn (D10)
  PRIMARY KEY (game, tag)
);

CREATE TABLE builds (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,       -- from its record.json (D7); import upserts on it
  game TEXT NOT NULL,
  target TEXT NOT NULL,            -- windows | macos | steamos | linux
  host TEXT REFERENCES hosts(key), -- where it was built
  cross INTEGER,                   -- 1: target OS differs from the host's (llvm-mingw on the Mac)
  exe_sha256 TEXT,                 -- NULL for a failed build or a package that only restaged
  kind TEXT NOT NULL,              -- full | incremental | package | patch | unknown
  command TEXT,                    -- 'build', 'bench build', 'bench integrate', 'package'
  started TEXT, ended TEXT, secs REAL, ok INTEGER,
  jobs INTEGER,                    -- the -j given to cmake --build
  warm TEXT,                       -- 'cold' (new build dir) | 'warm' | 'reconfigured' | NULL
  gen_fresh INTEGER,               -- 1: recomp ran (gen key changed), 0: reused
  ccache TEXT,                     -- 'off' | 'on h/m' (hits/misses) | NULL when unknown
  edges INTEGER,                   -- ninja edges run, from .ninja_log
  cpu TEXT, ncpu INTEGER, mem_gb REAL,   -- the host's figures at build time
  load_start REAL,
  game_sha TEXT, game_branch TEXT, game_dirty INTEGER,
  toolkit_sha TEXT, toolkit_branch TEXT, toolkit_dirty INTEGER,
  cli_sha TEXT, gen_digest TEXT,
  toolchain TEXT,                  -- 'llvm-mingw 20250114, cmake 3.31.2, ninja 1.12.1' (D13)
  version TEXT,                    -- the bundle version string, when packaged
  base_version TEXT,               -- patch: what it applies to
  payload_bytes INTEGER, full_bytes INTEGER,
  release TEXT,                    -- the tag it belongs to, NULL: internal (D10)
  release_match TEXT,              -- 'match' | 'stand-in' | NULL
  after_release TEXT,              -- internal: the newest tag before it
  lineage TEXT NOT NULL,           -- 'integration' | 'branch:<name>' | 'unknown'
  record TEXT                      -- relpath of its record.json
);
CREATE TABLE build_steps (
  build_id INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  step TEXT NOT NULL,              -- 'recomp', 'configure', 'cmake --build', 'stage', 'wrap'
  secs REAL NOT NULL,
  detail TEXT,                     -- 'edges 94/2210', 'cached'
  PRIMARY KEY (build_id, seq)
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  game TEXT NOT NULL,
  root TEXT NOT NULL REFERENCES roots(name),
  relpath TEXT NOT NULL,
  stamp TEXT NOT NULL,             -- YYYYMMDD-HHMMSS, the citation key
  host TEXT REFERENCES hosts(key),
  backend TEXT,                    -- d3d11 | metal | cpu | vulkan
  build_id INTEGER REFERENCES builds(id),
  kind TEXT NOT NULL,              -- golden | integrate | golden-run | run | pacing | matrix | tests
  scenario TEXT,
  started TEXT, secs REAL, limit_secs REAL,
  exit_code INTEGER, stop TEXT,
  busy INTEGER, busy_why TEXT, cpu_stall_pct REAL,
  load_start REAL, load_end REAL, ncpu INTEGER, lock_wait_secs REAL,
  stock INTEGER,
  env TEXT,                        -- normalised, local paths replaced by <root>/... (D15)
  status TEXT NOT NULL,            -- ok | partial | unparsed
  problems TEXT,
  info_sha256 TEXT,
  UNIQUE (root, relpath)
);
CREATE TABLE perf (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  window TEXT NOT NULL,            -- 'run' | 'steady' | 'after:<anchor>'
  source TEXT NOT NULL,            -- run-info | flip-trace | pacing-stats | bench-matrix
  flips INTEGER, flips_exact INTEGER, secs REAL, flips_per_s REAL,
  ms_p50 REAL, ms_p95 REAL, ms_p99 REAL, ms_max REAL,
  hitches INTEGER, low1_fps REAL,
  PRIMARY KEY (run_id, window, source)
);
CREATE TABLE metrics (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name TEXT NOT NULL, value REAL, unit TEXT,
  PRIMARY KEY (run_id, name)
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,       -- live: written by golden; TSV: game + sha256(line)
  game TEXT NOT NULL,
  kind TEXT NOT NULL,              -- golden | integrate | golden-run | reconstructed
  host TEXT REFERENCES hosts(key), backend TEXT,
  build_id INTEGER REFERENCES builds(id),
  started TEXT NOT NULL, ended TEXT,
  tests TEXT, only TEXT, rc INTEGER,
  source TEXT                      -- live | tsv | reconstructed-tsv | golden.txt
);
CREATE TABLE verdicts (
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  scenario TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id),
  word TEXT NOT NULL,
  PRIMARY KEY (session_id, scenario)
);
CREATE TABLE frame_checks (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  frame TEXT NOT NULL, result TEXT NOT NULL, ref TEXT,
  dump INTEGER, present INTEGER,
  mae_r REAL, mae_g REAL, mae_b REAL, bad_pct REAL, tile_pct REAL,
  PRIMARY KEY (run_id, frame)
);
CREATE TABLE test_passes (
  key TEXT PRIMARY KEY, host TEXT, passed TEXT,
  cli TEXT, cmake TEXT, game_src TEXT, llvm_mingw TEXT, proton TEXT, prefix TEXT
);

CREATE TABLE artifacts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  build_id INTEGER REFERENCES builds(id) ON DELETE SET NULL,
  root TEXT NOT NULL REFERENCES roots(name),
  relpath TEXT NOT NULL,
  kind TEXT NOT NULL,              -- frames | hdd | stdio-log | kernel-log | proton-log |
                                   -- console-log | run-info | golden-txt | crash-symbols |
                                   -- dump | bundle | patch | record | host-run | worktree |
                                   -- scratch | link | other
  heavy INTEGER NOT NULL,
  bytes INTEGER, files INTEGER, sha256 TEXT,
  state TEXT NOT NULL,             -- present | deleted | missing
  seen TEXT NOT NULL,
  UNIQUE (root, relpath)
);
CREATE TABLE pins (
  stamp TEXT NOT NULL,             -- a stamp, HHMMSS token, or a path under a root
  source TEXT NOT NULL,            -- 'cat/TASKS.md:812'
  refreshed TEXT NOT NULL,
  PRIMARY KEY (stamp, source)
);
CREATE TABLE deletions (           -- the journal of every delete the tool made
  id INTEGER PRIMARY KEY,
  plan TEXT NOT NULL,              -- the plan id (D11)
  at TEXT NOT NULL,
  agent TEXT,                      -- $BENCHDB_AGENT, else $USER
  root TEXT NOT NULL, relpath TEXT NOT NULL,
  bytes INTEGER, reason TEXT NOT NULL,
  result TEXT NOT NULL             -- deleted | refused:<why> | failed:<err>
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);

CREATE INDEX runs_group   ON runs (game, host, backend, scenario, started);
CREATE INDEX runs_build   ON runs (build_id);
CREATE INDEX runs_stamp   ON runs (stamp);
CREATE INDEX builds_exe   ON builds (exe_sha256);
CREATE INDEX builds_rel   ON builds (game, target, release, after_release, ended);
CREATE INDEX builds_time  ON builds (game, host, target, kind, ended);
CREATE INDEX sess_game    ON sessions (game, started);
CREATE INDEX verd_run     ON verdicts (run_id);
CREATE INDEX art_run      ON artifacts (run_id);
CREATE INDEX art_state    ON artifacts (heavy, state);
CREATE INDEX pins_stamp   ON pins (stamp);
CREATE INDEX del_at       ON deletions (at);
"""

# (version, DDL): applied in order to a db whose user_version is below the
# version. Never edit a released step; append one.
MIGRATIONS = [(1, V1)]
LATEST = MIGRATIONS[-1][0]


class SchemaError(Exception):
    pass


def version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def backup_to(conn, path):
    """An online copy of conn's db at path (SQLite's backup API, so a
    reader or a WAL never sees half of it)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
    finally:
        dst.close()


def migrate(conn, backup_dir=None):
    """Brings conn's db to LATEST. A db at an older version is first copied
    to <backup_dir>/bench-pre-v<N>.db; each step is one BEGIN IMMEDIATE
    that ends by setting user_version, so a failure rolls it back. Returns
    the versions applied. A newer db is SchemaError: an older CLI never
    downgrades it (open() then opens it read-only)."""
    v = version(conn)
    if v > LATEST:
        raise SchemaError(
            "the db is at schema %d, newer than this CLI's %d: opened read-only" % (v, LATEST)
        )
    todo = [(n, ddl) for n, ddl in MIGRATIONS if n > v]
    if not todo:
        return []
    if v > 0 and backup_dir:
        backup_to(conn, os.path.join(backup_dir, "bench-pre-v%d.db" % todo[0][0]))
    if v == 0:
        # Sticky: set once, before the first table, outside a transaction.
        conn.execute("PRAGMA journal_mode = WAL")
    done = []
    for n, ddl in todo:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements(ddl):
                conn.execute(stmt)
            conn.execute("PRAGMA user_version = %d" % n)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # already rolled back: the error raised is the one to see
            raise
        done.append(n)
    return done


def statements(ddl):
    """The DDL's statements, without its PRAGMA lines (journal_mode and
    foreign_keys are set by the connection, user_version by migrate)."""
    lines = []
    for line in ddl.splitlines():
        code = line.split("--", 1)[0].rstrip()
        if code.lstrip().upper().startswith("PRAGMA"):
            continue
        lines.append(code)
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def master(conn):
    """The schema as sqlite_master lists it, for comparing two dbs."""
    return sorted(
        conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall(),
        key=lambda r: (r[0], r[1]),
    )
