"""ingest_run, ingest_build and the other importers: the one path from
files on disk to rows, shared by the live writers and `benchdb import`
(D7, D12). Each takes a Writer (one open transaction) and upserts on the
natural keys, so ingesting the same thing twice changes nothing.

A run that cannot be read whole is kept: `partial` with its problems, or
`unparsed` when it has no run-info.txt (D17 Q7)."""

import glob
import hashlib
import json
import os
import platform
import re

from . import parse, record
from .store import DbError

STAMP_RE = re.compile(r"^(\d{8}-\d{6})(?:-([A-Za-z0-9_.-]+))?$")
MAC_SUFFIX_RE = re.compile(r"^(metal|d3d11|cpu|vulkan)-(.+)$")
BACKENDS = ("d3d11", "metal", "cpu", "vulkan")
TSV = "golden-sessions.tsv"
TSV_RECON = "golden-sessions.reconstructed.tsv"
TESTS_PASS = "tests-pass"
INBOX = "inbox"


class Games:
    """The [games] of the config, loaded once: exe stem -> slug, and each
    slug's manifest settings."""

    def __init__(self, cfg):
        from .. import manifest

        self.by_exe = {}
        self.games = {}
        for slug, d in cfg.games.items():
            try:
                g = manifest.load(d)
            except manifest.ManifestError:
                continue
            self.games[g.slug] = g
            self.by_exe[g.exe_name.lower()] = g.slug
            if slug != g.slug:
                self.games.setdefault(slug, g)

    def from_exe(self, exe):
        if not exe:
            return None
        stem = re.split(r"[\\/]", exe)[-1]
        if stem.lower().endswith(".exe"):
            stem = stem[:-4]
        return self.by_exe.get(stem.lower())

    def get(self, slug):
        return self.games.get(slug)


def games_of(cfg):
    """The Games of cfg, loaded once per Config object."""
    g = getattr(cfg, "_benchdb_games", None)
    if g is None:
        g = Games(cfg)
        cfg._benchdb_games = g
    return g


def settings(cfg, slug):
    """A game's manifest settings (bench.db, release, bench, input), or the
    SPEC defaults for a game the config does not list."""
    from .. import manifest

    g = games_of(cfg).get(slug)
    if g is not None:
        return g.m
    m = {}
    for table in (
        "bench",
        "bench.gc",
        "bench.db",
        "bench.db.regress",
        "release",
        "input",
        "golden",
    ):
        node = m
        parts = table.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node.setdefault(parts[-1], {})
        for k, (_kind, default) in manifest.SPEC[table].items():
            node[parts[-1]].setdefault(k, default)
    return m


# ---- helpers ------------------------------------------------------------------


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalise_env(env, cfg):
    """A run's env line with local paths replaced by <root>/... (D15), so
    it can be compared across agents and never carries a private path."""
    if not env:
        return env
    subs = [(r.path, "<%s>" % r.name) for r in cfg.roots.values()]
    if cfg.present:
        subs.append((cfg.dir, "<ws>"))
    for p, name in sorted(subs, key=lambda s: -len(s[0])):
        if p and p != "/":
            env = env.replace(p, name)
    return env


def env_value(env, key):
    m = re.search(r"(?:^|\s)%s=(\S*)" % re.escape(key), env or "")
    return m.group(1) if m else None


ART_KINDS = (
    ("frames", "frames", True),
    ("hdd", "hdd", True),
    ("game-stdio.log", "stdio-log", None),
    ("xbox_kernel.log", "kernel-log", None),
    ("console.log", "console-log", None),
    ("run-info.txt", "run-info", False),
    ("golden.txt", "golden-txt", False),
    (record.RUN, "record", False),
    ("crash-symbols", "crash-symbols", False),
)


def art_kind(name, is_dir):
    for n, kind, heavy in ART_KINDS:
        if name == n:
            return kind, heavy
    if name.startswith("steam-") or name.startswith("proton") and name.endswith(".log"):
        return "proton-log", None
    if name.endswith(".dmp") or name == "dump" or name.endswith(".core"):
        return "dump", True
    return "other", None if not is_dir else True


def run_artifacts(w, run_id, root, relpath, run_dir, gm):
    """One artifact per entry of the run dir; rows of entries now gone
    become `missing` (a `deleted` row stays as the journal left it)."""
    heavy_log = gm["bench"]["db"]["heavy_log"]
    hash_max = gm["bench"]["db"]["hash_max"]
    seen = set()
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(run_dir, name)
        is_link = os.path.islink(p)
        is_dir = os.path.isdir(p) and not is_link
        kind, heavy = art_kind(name, is_dir)
        if is_link:
            kind, heavy = "link", False
        size, files = parse.size_of(p)
        if heavy is None:
            heavy = size > heavy_log
        sha = None
        if not is_dir and not is_link and size <= hash_max:
            try:
                sha = sha256_file(p)
            except OSError:
                pass
        rel = "%s/%s" % (relpath, name)
        seen.add(rel)
        w.artifact(run_id, root, rel, kind, heavy, bytes=size, files=files, sha256=sha)
    for r in w.sql(
        "SELECT id, relpath FROM artifacts WHERE run_id = ? AND state = 'present'", (run_id,)
    ):
        if r["relpath"] not in seen:
            w.sql("UPDATE artifacts SET state = 'missing' WHERE id = ?", (r["id"],))


def lineage_of(trees, gm):
    g = trees.get("game")
    if not g or not g.get("branch"):
        return "unknown"
    if g["branch"] == gm["bench"]["main_branch"]:
        return "integration"
    return "branch:%s" % g["branch"]


def build_fields(trees):
    out = {}
    for name, prefix in (("game", "game"), ("toolkit", "toolkit"), ("cli", "cli")):
        t = trees.get(name)
        if not t:
            continue
        out["%s_sha" % prefix] = t.get("sha")
        if prefix != "cli":
            out["%s_branch" % prefix] = t.get("branch")
            out["%s_dirty" % prefix] = t.get("dirty")
    return out


def find_build(w, game, uuid=None, exe_sha=None):
    if uuid:
        r = w.one("SELECT id FROM builds WHERE uuid = ?", (uuid,))
        if r is not None:
            return r
    if exe_sha:
        return w.one(
            "SELECT id FROM builds WHERE exe_sha256 = ? AND game = ? "
            "ORDER BY coalesce(ended, started) DESC, id DESC LIMIT 1",
            (exe_sha, game),
        )
    return None


def built_on(w, info, host):
    """The host key of the machine that built a run's exe: the built: line's
    'on <name>' (this machine is 'local'), else for a Mac run the run's own
    host, which runs the exe it built in place, as does a host that compiled
    a synced tree (a `synced:` line, BENCH_BUILD=host); None when unknown."""
    on = (info.get("shipped") or {}).get("on")
    if on == "the Mac":
        # The hand-written lines' name for the one macOS host.
        macs = w.conn.execute("SELECT key FROM hosts WHERE os = 'macos'").fetchall()
        return macs[0][0] if len(macs) == 1 else None
    if on:
        key = w.one("SELECT host FROM host_names WHERE seen = ?", (on,))
        if key is not None:
            return key
        if on.split(".")[0] == platform.node().split(".")[0]:
            return "local"
        return None
    return host if info.get("format") == "mac" or info.get("synced") else None


def run_build(w, game, info, target, host, gm):
    """The build a run ran: the uuid ship wrote, else the newest build with
    its exe sha, else a `kind = unknown` build from its built: or tree:
    lines (D5). A stand-in has no kind or secs (nothing timed that build:
    its run predates the build records), but it knows where it was built,
    and a re-import fills that into one made without it."""
    uuid, exe = info.get("build_uuid"), info.get("exe_sha256")
    b = find_build(w, game, uuid, exe)
    bhost = built_on(w, info, host) if (uuid or exe) else None
    hos = w.one("SELECT os FROM hosts WHERE key = ?", (bhost,)) if bhost else None
    cross = int(target != hos) if hos is not None else None
    if b is not None:
        if hos is not None:
            w.conn.execute(
                "UPDATE builds SET host = ?, cross = coalesce(cross, ?) "
                "WHERE id = ? AND kind = 'unknown' AND host IS NULL",
                (bhost, cross, b),
            )
        return b
    if not uuid and not exe:
        return None
    trees = info.get("trees", {})
    shipped = info.get("shipped") or {}
    f = dict(build_fields(trees))
    f["exe_sha256"] = exe
    f["gen_digest"] = info.get("gen")
    f["ended"] = shipped.get("at")
    if hos is not None:
        f["host"], f["cross"] = bhost, cross
    return w.build(
        uuid or record.derived_uuid("exe", game, target, exe),
        game,
        target,
        "unknown",
        lineage=lineage_of(trees, gm),
        **f,
    )


def run_scenario(info, golden, suffix, gm, kind):
    if golden and golden.get("verdict"):
        return golden["verdict"][0]
    if suffix:
        m = MAC_SUFFIX_RE.match(suffix)
        if m:
            return m.group(2)
        if suffix.startswith("play-"):
            return suffix[5:]
    script = env_value(info.get("env"), gm["input"]["script_env"])
    if script:
        base = re.split(r"[\\/]", script)[-1]
        return re.sub(r"\.(json|txt|script)$", "", base) or None
    return None


def run_backend(info, suffix):
    if info.get("backend"):
        return info["backend"]
    b = env_value(info.get("env"), "RECOMP_PB_BACKEND")
    if b:
        return b
    if suffix:
        m = MAC_SUFFIX_RE.match(suffix)
        if m:
            return m.group(1)
    if info.get("format") in ("proton", "windows"):
        return "d3d11"
    return None


def run_kind(golden, suffix, fmt):
    if suffix and MAC_SUFFIX_RE.match(suffix):
        return "golden-run"
    if golden is not None:
        return "golden-run" if fmt == "mac" else "golden"
    return "run"


def local_host_key(cfg, uname):
    node = platform.node().split(".")[0]
    if uname and uname.split(".")[0] == node:
        return "local"
    return None


def stock_of(log, gm):
    """golden's rule: no non-stock [ENHANCE] value in the log (D5)."""
    if not log or not os.path.exists(log):
        return None
    from .. import golden as golden_mod

    saved = golden_mod.GAME_ENHANCE
    golden_mod.GAME_ENHANCE = dict(gm.get("golden", {}).get("enhance_stock", {}))
    try:
        return int(golden_mod.enhance_nonstock(log) is None)
    finally:
        golden_mod.GAME_ENHANCE = saved


# ---- runs ---------------------------------------------------------------------


def ingest_run(w, cfg, run_dir, game=None, host=None, kind=None):
    """Upserts one run dir's rows. host is BENCH_HOST's key when a writer
    knows it (never the uname); game and kind override what the dir says.
    Returns (run id, 'added'|'updated', status, problems)."""
    run_dir = os.path.abspath(run_dir)
    loc = cfg.locate(run_dir)
    if loc is None:
        raise DbError("%s is outside every root in benchdb.toml" % run_dir)
    root, relpath = loc
    name = os.path.basename(run_dir)
    m = STAMP_RE.match(name)
    rec = record.read(os.path.join(run_dir, record.RUN)) or {}
    if m:
        stamp, suffix = m.group(1), m.group(2)
    elif rec.get("stamp") and STAMP_RE.match(rec["stamp"]):
        # A bench-matrix run dir is named for its arm (<arm>[-rN]): its
        # record carries the stamp.
        stamp, suffix = STAMP_RE.match(rec["stamp"]).group(1), ""
    else:
        raise DbError("%s: not a run dir (no YYYYMMDD-HHMMSS name)" % run_dir)
    text = read_text(os.path.join(run_dir, "run-info.txt"))
    problems = []
    if text is None:
        info = {"trees": {}, "problems": []}
        if rec.get("kind") == "matrix" and (kind or "matrix") == "matrix":
            # bench-matrix keeps run-info.json; its record holds what it says.
            status = "ok"
        else:
            status = "unparsed"
            problems.append("no run-info.txt")
    else:
        info = parse.run_info(text)
        problems += info["problems"]
        status = "ok"
    gtext = read_text(os.path.join(run_dir, "golden.txt"))
    golden = parse.golden_txt(gtext) if gtext is not None else None
    games = games_of(cfg)
    game = game or rec.get("game") or games.from_exe(info.get("exe") or rec.get("exe"))
    if not game:
        game = "unknown"
        problems.append("game unknown")
    gm = settings(cfg, game)
    if host is None:
        host = rec.get("host")
    if host is None and text is not None:
        host, _why = parse.host_key(info, text, cfg)
        if host is None:
            host = local_host_key(cfg, info.get("uname")) if info["format"] == "mac" else None
        if host is None:
            problems.append("host unknown")
    if host is not None and w.one("SELECT key FROM hosts WHERE key = ?", (host,)) is None:
        problems.append("host %s not in the config" % host)
        host = None
    fmt = info.get("format")
    target = "macos" if fmt == "mac" else "windows"
    offset = None
    if fmt == "windows":
        from .. import bench_matrix

        launch = parse.load_json(os.path.join(run_dir, "launch.json"))
        offset = bench_matrix.pc_utc_offset(launch) if isinstance(launch, dict) else None
    started = (info.get("time_raw") and parse.iso_utc(info["time_raw"], offset)) or (
        parse.stamp_iso(stamp)
    )
    exit_code = info.get("exit_code")
    ec = read_text(os.path.join(run_dir, "exit-code"))
    if ec is not None and ec.strip().lstrip("-").isdigit():
        exit_code = int(ec.strip())
    build = run_build(w, game, info, target, host, gm) if text is not None else None
    if build is None and (rec.get("build") or rec.get("exe_sha256")):
        build = find_build(w, game, rec.get("build"), rec.get("exe_sha256"))
    if text is not None and build is None:
        problems.append("build unknown")
    log = os.path.join(run_dir, "game-stdio.log")
    fields = dict(
        host=host,
        backend=rec.get("backend") or run_backend(info, suffix),
        scenario=rec.get("scenario") or run_scenario(info, golden, suffix, gm, kind),
        started=started,
        secs=info.get("secs"),
        limit_secs=info.get("limit_secs"),
        exit_code=exit_code,
        stop=info.get("stop"),
        busy=info.get("busy"),
        busy_why=info.get("busy_why"),
        cpu_stall_pct=info.get("cpu_stall_pct"),
        load_start=info.get("load_start"),
        load_end=info.get("load_end"),
        ncpu=info.get("ncpu"),
        lock_wait_secs=info.get("lock_wait_secs"),
        env=normalise_env(info.get("env"), cfg),
        info_sha256=hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
    )
    st = stock_of(log, gm)
    fields["stock"] = st if st is not None else rec.get("stock")
    for k in ("secs", "exit_code", "stock"):
        if fields.get(k) is None and rec.get(k) is not None:
            fields[k] = rec[k]
    if status == "ok" and problems:
        status = "partial"
    kind = kind or rec.get("kind") or run_kind(golden, suffix, fmt)
    old = w.one("SELECT id FROM runs WHERE root = ? AND relpath = ?", (root, relpath))
    run_id = w.run(
        game,
        root,
        relpath,
        stamp,
        kind,
        status=status,
        build=build,
        problems="; ".join(problems) or None,
        **fields,
    )
    w.clear_run(run_id)
    if info.get("flips") is not None:
        w.perf(
            run_id,
            "run",
            "run-info",
            flips=info["flips"],
            flips_exact=info.get("flips_exact"),
            secs=info.get("secs"),
            flips_per_s=info["flips"] / info["secs"] if info.get("secs") else None,
        )
    stats = parse.flip_stats(log) if os.path.exists(log) else None
    if stats:
        w.perf(
            run_id,
            "run",
            "flip-trace",
            **{k: v for k, v in stats.items() if k not in ("coarse", "first_s")},
        )
        if stats.get("first_s") is not None:
            w.metric(run_id, "first_flip_s", stats["first_s"], "s")
    for p in rec.get("perf", []):
        p = dict(p)
        w.perf(run_id, p.pop("window", "run"), p.pop("source", "record"), **p)
    for k, v in (rec.get("metrics") or {}).items():
        if isinstance(v, dict):
            w.metric(run_id, k, v.get("value"), v.get("unit"))
        else:
            w.metric(run_id, k, v)
    if golden:
        for fc in golden["frames"]:
            fc = dict(fc)
            w.frame_check(run_id, fc.pop("frame"), fc.pop("result"), **fc)
        if fmt == "mac" and golden.get("verdict") and not rec.get("session"):
            sid = w.session(
                "golden.txt:%s/%s" % (root, relpath),
                game,
                "golden-run",
                started,
                source="golden.txt",
                build=build,
                host=host,
                backend=fields["backend"],
            )
            w.verdict(sid, golden["verdict"][0], golden["verdict"][1], run=run_id)
    run_artifacts(w, run_id, root, relpath, run_dir, gm)
    return run_id, ("updated" if old is not None else "added"), status, problems


# ---- builds -------------------------------------------------------------------

BUILD_KEYS = (
    "host",
    "cross",
    "exe_sha256",
    "command",
    "started",
    "ended",
    "secs",
    "ok",
    "jobs",
    "warm",
    "gen_fresh",
    "ccache",
    "edges",
    "cpu",
    "ncpu",
    "mem_gb",
    "load_start",
    "gen_digest",
    "toolchain",
    "version",
    "base_version",
    "payload_bytes",
    "full_bytes",
)


def ingest_build(w, cfg, doc, source=None):
    """Upserts one build record (a build-record.json, a package record or
    an inbox copy): the build row and its steps. Returns (id, added|updated)."""
    if not isinstance(doc, dict) or not doc.get("uuid") or not doc.get("game"):
        raise DbError("%s: not a build record (no uuid or game)" % (source or "record"))
    gm = settings(cfg, doc["game"])
    trees = doc.get("trees") or {}
    f = {k: doc[k] for k in BUILD_KEYS if k in doc}
    f.update(build_fields(trees))
    if (
        f.get("host") is not None
        and w.one("SELECT key FROM hosts WHERE key = ?", (f["host"],)) is None
    ):
        f["host"] = None
    if source:
        loc = cfg.locate(source)
        f["record"] = "%s/%s" % loc if loc else None
    old = w.one("SELECT id FROM builds WHERE uuid = ?", (doc["uuid"],))
    steps = doc.get("steps")
    bid = w.build(
        doc["uuid"],
        doc["game"],
        doc.get("target") or "unknown",
        doc.get("kind") or "unknown",
        lineage=doc.get("lineage") or lineage_of(trees, gm),
        steps=[
            tuple(s) if isinstance(s, (list, tuple)) else (s["step"], s["secs"], s.get("detail"))
            for s in steps
        ]
        if steps is not None
        else None,
        **f,
    )
    for a in doc.get("artifacts") or []:
        loc = cfg.locate(a["path"]) if a.get("path") else None
        if loc:
            w.artifact(
                None,
                loc[0],
                loc[1],
                a.get("kind", "other"),
                a.get("heavy", True),
                build=bid,
                bytes=a.get("bytes"),
                files=a.get("files"),
                sha256=a.get("sha256"),
            )
    return bid, ("updated" if old is not None else "added")


def ingest_bundles(w, cfg, slug, game_root):
    """Bundle index entries and patch.json files of a game checkout as
    package/patch builds; their uuids come from the manifest sha and the
    source path, so re-import is a no-op (D12.4)."""
    n = 0
    for idx in sorted(
        glob.glob(os.path.join(game_root, ".git", "xbr", "bundles", "*", "index.json"))
    ):
        target = os.path.basename(os.path.dirname(idx))
        for e in parse.bundle_index(parse.load_json(idx)):
            ingest_build(
                w,
                cfg,
                {
                    "uuid": record.derived_uuid("bundle", slug, target, e["manifest_sha256"]),
                    "game": slug,
                    "target": target,
                    "kind": "package",
                    "command": "package",
                    "ended": e.get("made"),
                    "version": e.get("version"),
                    "full_bytes": e.get("size"),
                },
            )
            n += 1
    for pj in sorted(
        glob.glob(os.path.join(game_root, "dist", "**", "patch.json"), recursive=True)
    ):
        p = parse.patch_json(parse.load_json(pj))
        if not p:
            continue
        ingest_build(
            w,
            cfg,
            {
                "uuid": record.derived_uuid("patch", slug, os.path.relpath(pj, game_root)),
                "game": slug,
                "target": target_of_path(pj),
                "kind": "patch",
                "command": "package",
                "version": p["version"],
                "base_version": p["base_version"],
                "payload_bytes": p["payload_bytes"],
                "full_bytes": p["full_bytes"],
            },
        )
        n += 1
    # package's own records last: they share the uuids above and add the
    # steps, times and artifacts the index and patch.json lack.
    for rp in sorted(
        glob.glob(os.path.join(game_root, "dist", "**", "*" + PACKAGE_RECORD), recursive=True)
    ):
        doc = record.read(rp)
        if doc and doc.get("uuid"):
            ingest_build(w, cfg, doc, rp)
            n += 1
    return n


PACKAGE_RECORD = ".package-record.json"


def target_of_path(p):
    for t in ("windows", "steamos", "macos", "linux"):
        if t in p.replace("\\", "/").split("/") or ("-%s" % t) in p:
            return t
    return "unknown"


# ---- sessions -----------------------------------------------------------------


def run_of_token(w, token, game=None):
    """The run a TSV's stamp token names: by dir name first, then stamp."""
    base = token.split("/")[-1]
    q = "SELECT id, game, host, backend, build_id FROM runs WHERE (relpath = ? OR relpath LIKE ?)"
    args = [base, "%/" + base]
    if game:
        q += " AND game = ?"
        args.append(game)
    r = w.sql(q + " ORDER BY id", args)
    if r:
        return r[0]
    m = STAMP_RE.match(base)
    if not m:
        return None
    q = "SELECT id, game, host, backend, build_id FROM runs WHERE stamp = ?"
    args = [m.group(1)]
    if game:
        q += " AND game = ?"
        args.append(game)
    r = w.sql(q + " ORDER BY id", args)
    return r[0] if len(r) == 1 else None


def ingest_tsv(w, path, reconstructed=False, game=None):
    """golden-sessions.tsv (source tsv) or its reconstructed twin (kind
    reconstructed). The game and host come from the runs each line names
    (`posix` is not a host); a line whose runs are not in the db yet
    keeps game 'unknown' until the next import. Returns (added, updated)."""
    text = read_text(path) or ""
    added = updated = 0
    for s in parse.sessions_tsv(text):
        linked = {sc: run_of_token(w, st, game) for sc, (st, _word) in s["scen"].items()}
        runs = [r for r in linked.values() if r]
        g = game or (runs[0]["game"] if runs else "unknown")
        uuid = parse.line_uuid(g if runs or game else "unknown", s["line"])
        if g != "unknown":
            # A line first seen before its runs were imported keeps one row.
            stale = parse.line_uuid("unknown", s["line"])
            w.sql("UPDATE sessions SET uuid = ?, game = ? WHERE uuid = ?", (uuid, g, stale))
        old = w.one("SELECT id FROM sessions WHERE uuid = ?", (uuid,))
        host = next((r["host"] for r in runs if r["host"]), None)
        backend = next((r["backend"] for r in runs if r["backend"]), None)
        build = next((r["build_id"] for r in runs if r["build_id"]), None)
        sid = w.session(
            uuid,
            g,
            "reconstructed" if reconstructed else (s["kind"] or "golden"),
            parse.tsv_time(s["time"]) or s["time"],
            source="reconstructed-tsv" if reconstructed else "tsv",
            build=build,
            host=host,
            backend=backend,
            tests=s["tests"],
            only=s["only"],
            rc=s["rc"],
        )
        for sc, (_st, word) in s["scen"].items():
            r = linked[sc]
            w.verdict(sid, sc, word, run=r["id"] if r else None)
        if old is None:
            added += 1
        else:
            updated += 1
    return added, updated


def ingest_session(w, cfg, doc):
    """A live session record (benchdb-sessions/<uuid>.json)."""
    if not doc.get("uuid") or not doc.get("game"):
        raise DbError("not a session record (no uuid or game)")
    build = find_build(w, doc["game"], doc.get("build")) if doc.get("build") else None
    old = w.one("SELECT id FROM sessions WHERE uuid = ?", (doc["uuid"],))
    host = doc.get("host")
    if host and w.one("SELECT key FROM hosts WHERE key = ?", (host,)) is None:
        host = None
    sid = w.session(
        doc["uuid"],
        doc["game"],
        doc.get("kind", "golden"),
        doc.get("started"),
        source="live",
        build=build,
        host=host,
        backend=doc.get("backend"),
        ended=doc.get("ended"),
        tests=doc.get("tests"),
        only=doc.get("only"),
        rc=doc.get("rc"),
    )
    first = None
    for sc, v in (doc.get("verdicts") or {}).items():
        run = None
        if v.get("run"):
            loc = cfg.locate(v["run"]) if os.path.isabs(v["run"]) else tuple(v["run"].split("/", 1))
            if loc and len(loc) == 2:
                run = w.one("SELECT id FROM runs WHERE root = ? AND relpath = ?", loc)
        if first is None and run is not None:
            first = run
        w.verdict(sid, sc, v["word"], run=run)
    if build is None and first is not None:
        # A golden session runs one exe: its build is its runs' build.
        w.conn.execute(
            "UPDATE sessions SET build_id = (SELECT build_id FROM runs WHERE id = ?) WHERE id = ?",
            (first, sid),
        )
    return sid, ("updated" if old is not None else "added")


def ingest_tests_pass(w, path):
    t = parse.tests_pass(read_text(path) or "")
    if not t["key"]:
        t["key"] = os.path.splitext(os.path.basename(path))[0]
    old = w.one("SELECT key FROM test_passes WHERE key = ?", (t["key"],))
    w.test_pass(**t)
    return "updated" if old is not None else "added"


def ingest_record(w, cfg, path):
    """Any record file by its 'record' field: build, session or run."""
    doc = record.read(path)
    if doc is None:
        raise DbError("%s: not a JSON record" % path)
    what = doc.get("record") or ("build" if os.path.basename(path) == record.BUILD else "")
    if what == "build":
        return "build", ingest_build(w, cfg, doc, path)[1]
    if what == "session":
        return "session", ingest_session(w, cfg, doc)[1]
    if what == "run" and doc.get("dir"):
        return "run", ingest_run(w, cfg, doc["dir"])[1]
    raise DbError("%s: unknown record kind %r" % (path, what))


# ---- what import --all walks ---------------------------------------------------


def is_run_dir(path):
    return (
        os.path.isdir(path)
        and not os.path.islink(path)
        and bool(STAMP_RE.match(os.path.basename(path)))
    )


def store_runs(root_path):
    """Run dirs of a store root: its stamp dirs, and the stamp dirs one
    level down in its topic dirs (jsrf-golden/<stamp>, D12.7)."""
    out = []
    try:
        names = sorted(os.listdir(root_path))
    except OSError:
        return out
    for n in names:
        p = os.path.join(root_path, n)
        if is_run_dir(p):
            out.append(p)
        elif (
            os.path.isdir(p)
            and not os.path.islink(p)
            and n
            not in (
                INBOX,
                TESTS_PASS,
                record.SESSIONS,
                "db-backup",
            )
        ):
            try:
                sub = sorted(os.listdir(p))
            except OSError:
                continue
            out += [os.path.join(p, s) for s in sub if is_run_dir(os.path.join(p, s))]
    return out


def gen_records(game_dir):
    """The analyze and recomp verbs' records in a game's analysis dir."""
    try:
        from .. import manifest

        out = manifest.load(game_dir).out
    except Exception:  # noqa: BLE001 (no readable game.toml: no records)
        return []
    return [
        p
        for p in (os.path.join(out, "%s-record.json" % v) for v in ("analyze", "recomp"))
        if os.path.isfile(p)
    ]


def walk_all(cfg):
    """Everything `import --all` reads, as (kind, path): the store roots'
    runs, then build and session records and inboxes (so runs can link
    to builds by uuid), TSVs last (they link to runs by stamp)."""
    items = []
    stores = [r for r in cfg.roots.values() if r.local and r.kind == "store"]
    for r in stores:
        for d in sorted(glob.glob(os.path.join(r.path, INBOX, "*", "*.json"))):
            items.append(("record", d))
    for slug, d in sorted(cfg.games.items()):
        for p in sorted(glob.glob(os.path.join(d, "build*", record.BUILD))):
            items.append(("record", p))
        for p in gen_records(d):
            items.append(("record", p))
        items.append(("bundles", "%s\0%s" % (slug, d)))
    for r in stores:
        for p in store_runs(r.path):
            items.append(("run", p))
        for p in sorted(glob.glob(os.path.join(r.path, record.SESSIONS, "*.json"))):
            items.append(("record", p))
        for p in sorted(glob.glob(os.path.join(r.path, TESTS_PASS, "*.txt"))):
            items.append(("tests-pass", p))
        for name, recon in ((TSV, False), (TSV_RECON, True)):
            p = os.path.join(r.path, name)
            if os.path.isfile(p):
                items.append(("tsv-recon" if recon else "tsv", p))
    return items


def classify(path):
    """The import kind of a PATH given on the command line."""
    b = os.path.basename(path.rstrip("/"))
    if b == TSV_RECON:
        return "tsv-recon"
    if b.endswith(".tsv"):
        return "tsv"
    if is_run_dir(path):
        return "run"
    if path.endswith(".json"):
        return "record"
    if os.path.basename(os.path.dirname(path)) == TESTS_PASS and path.endswith(".txt"):
        return "tests-pass"
    if os.path.isdir(path) and os.path.basename(path) == TESTS_PASS:
        return "tests-pass-dir"
    if os.path.isdir(path):
        return "store-dir"
    return None


def ingest_item(w, cfg, kind, path, counts):
    """One import item; counts gains added/updated/partial/unparsed."""
    if kind == "run":
        _rid, how, status, problems = ingest_run(w, cfg, path)
        counts[how] += 1
        if status != "ok":
            counts[status] += 1
            counts["notes"].append((path, status, "; ".join(problems)))
    elif kind in ("tsv", "tsv-recon"):
        a, u = ingest_tsv(w, path, reconstructed=kind == "tsv-recon")
        counts["added"] += a
        counts["updated"] += u
    elif kind == "tests-pass":
        counts[ingest_tests_pass(w, path)] += 1
    elif kind == "record":
        _what, how = ingest_record(w, cfg, path)
        counts[how] += 1
    elif kind == "bundles":
        slug, d = path.split("\0")
        counts["added"] += ingest_bundles(w, cfg, slug, d)
    else:
        raise DbError("%s: nothing to import" % path)


def new_counts():
    return {"added": 0, "updated": 0, "partial": 0, "unparsed": 0, "failed": 0, "notes": []}


def expand(paths):
    out = []
    for p in paths:
        p = os.path.abspath(p)
        k = classify(p)
        if k == "tests-pass-dir":
            out += [("tests-pass", x) for x in sorted(glob.glob(os.path.join(p, "*.txt")))]
        elif k == "store-dir":
            subs = store_runs(p)
            if not subs:
                raise DbError("%s: no run dirs in it" % p)
            out += [("run", x) for x in subs]
        elif k is None:
            raise DbError("%s: not a run dir, TSV, record or tests-pass file" % p)
        else:
            out.append((k, p))
    return out


def dumps(obj):
    return json.dumps(obj, sort_keys=True)
