"""The live writers' entry points (D7): what `bench`, `golden`, `build`
and `package` call once their own files are on disk.

Each call writes its record file first (beside the run, the build or the
bundle), then the rows when this machine has the db, else a copy to the
host's inbox when BENCHDB_INBOX and BENCHDB_HOST are set. Nothing here
ever raises or changes a command's rc: a failed write warns once, and
`benchdb import` reads the record later (the rule the sessions TSV
follows)."""

import datetime
import os
import sys

from . import config, ingest, parse, record, store

_warned = set()


def warn(what, e):
    if what in _warned:
        return
    _warned.add(what)
    print(
        "benchdb: warning: %s not recorded in the db (%s); `benchdb import` catches up" % (what, e),
        file=sys.stderr,
    )


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def here(cwd):
    """(cfg, db) when a benchdb.toml with a db is found from cwd, else
    None: a machine without the workspace config writes records only."""
    cfg = config.load(cwd=cwd)
    if not cfg.present or not cfg.db:
        return None
    return cfg, store.open(cfg=cfg)


def _rows(cwd, what, fn):
    try:
        h = here(cwd)
        if h is None:
            return None
        cfg, db = h
        with db.write() as w:
            return fn(w, cfg)
    except Exception as e:  # noqa: BLE001 (warn-only: never fails the command)
        warn(what, e)
        return None


def run(run_dir, game, host=None, kind=None, cwd=None, host_alias=None, **fields):
    """A run dir's record.json, then its rows. host is a [hosts] key;
    host_alias an ssh target (BENCH_HOST) resolved through the config.
    fields are the record's extras the run-info lacks (backend, scenario,
    build uuid, perf, metrics, stock, secs, exit_code). Returns the run id
    or None."""
    if host is None and host_alias:
        try:
            host = host_of(config.load(cwd=cwd or run_dir), host_alias)
        except Exception as e:  # noqa: BLE001
            warn("the host of %s" % host_alias, e)
    try:
        doc = record.read(os.path.join(run_dir, record.RUN)) or {}
        doc.update({k: v for k, v in fields.items() if v is not None})
        doc.update(record="run", dir=os.path.abspath(run_dir))
        if game:
            doc["game"] = game
        if host:
            doc["host"] = host
        if kind:
            doc["kind"] = kind
        record.write(os.path.join(run_dir, record.RUN), doc)
    except Exception as e:  # noqa: BLE001 (a field json cannot write is a TypeError)
        warn("the run record of %s" % run_dir, e)
        return None
    return _rows(
        cwd or run_dir,
        "run %s" % os.path.basename(run_dir),
        lambda w, cfg: ingest.ingest_run(w, cfg, run_dir, game=game, host=host, kind=kind)[0],
    )


# bench-matrix's summary columns as perf rows: (window, perf column, column).
MATRIX_PERF = (
    ("steady", "flips", "flips"),
    ("steady", "low1_fps", "steady_low1_fps"),
    ("steady", "ms_p99", "steady_p99_ms"),
    ("steady", "hitches", "steady_hitches"),
    ("run", "ms_p50", "p50_ms"),
    ("run", "ms_p99", "p99_ms"),
    ("run", "ms_max", "max_ms"),
    ("run", "low1_fps", "low1_fps"),
    ("run", "hitches", "hitches"),
)


def matrix(run_dir, row, info):
    """A bench-matrix collect (its summary row and run-info.json) as a run
    with perf rows (source bench-matrix) and every numeric column as a
    metric. The run dir is named for its arm, so the stamp comes from the
    run's start."""
    import re
    import time

    started = info.get("started") or ""
    digits = re.sub(r"[^0-9]", "", started)[:14]
    if len(digits) == 14:
        stamp = "%s-%s" % (digits[:8], digits[8:])
    else:
        t = os.path.getmtime(os.path.join(run_dir, "run.json"))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
    perf = {}
    for window, col, key in MATRIX_PERF:
        v = row.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            perf.setdefault(window, {"window": window, "source": "bench-matrix"})[col] = v
    metrics = {
        k: v
        for k, v in row.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("rep",)
    }
    return run(
        run_dir,
        None,
        kind="matrix",
        stamp=stamp,
        backend=row.get("backend") or info.get("backend"),
        scenario=info.get("scene_name"),
        exe=info.get("exe"),
        exe_sha256=info.get("exe_sha256"),
        arm=row.get("arm"),
        perf=list(perf.values()),
        metrics=metrics,
    )


def tests_pass(path, cwd=None):
    """A tests-pass record (bench tests wrote it) as its test_passes row."""
    return _rows(
        cwd or os.path.dirname(path),
        "the tests pass %s" % os.path.basename(path),
        lambda w, cfg: ingest.ingest_tests_pass(w, path),
    )


def session(store_dir, game, kind, host=None, backend=None, build=None, started=None, **fields):
    """A session record in <store>/benchdb-sessions/<uuid>.json, then its
    rows. fields: tests, only, rc, ended, and verdicts as
    {scenario: (run dir or None, word)}. Returns the session uuid."""
    verdicts = {}
    for sc, (rdir, word) in (fields.pop("verdicts", None) or {}).items():
        verdicts[sc] = {"word": word, "run": os.path.abspath(rdir) if rdir else None}
    doc = dict(
        record="session",
        uuid=record.new_uuid(),
        game=game,
        kind=kind,
        host=host,
        backend=backend,
        build=build,
        started=started or now(),
        ended=fields.pop("ended", None) or now(),
        verdicts=verdicts,
        **fields,
    )
    path = os.path.join(store_dir, record.SESSIONS, "%s.json" % doc["uuid"])
    try:
        record.write(path, doc)
    except OSError as e:
        warn("the session record", e)
        return None
    _rows(store_dir, "the %s session" % kind, lambda w, cfg: ingest.ingest_session(w, cfg, doc))
    return doc["uuid"]


def build(build_dir, doc, cwd=None, inbox=None, name=None):
    """<build>/build-record.json (merged into one already there, so ship's
    provenance and the build's own fields share a record), then the rows
    here or a copy in the inbox. name replaces the file name (the analyze
    and recomp verbs' records in the analysis dir). Returns the uuid."""
    path = os.path.join(build_dir, name or record.BUILD)
    try:
        old = record.read(path) or {}
        if old.get("uuid") and doc.get("uuid") and old["uuid"] != doc["uuid"]:
            old = {}
        old.update({k: v for k, v in doc.items() if v is not None})
        old["record"] = "build"
        record.write(path, old)
        doc = record.read(path)
    except OSError as e:
        warn("the build record in %s" % build_dir, e)
        return None
    return _land(doc, path, cwd or build_dir, inbox)


def package(path, doc, cwd=None, inbox=None):
    """A package or patch record at path (beside the bundle), then rows."""
    try:
        doc = dict(doc, record="build")
        record.write(path, doc)
        doc = record.read(path)
    except OSError as e:
        warn("the package record %s" % path, e)
        return None
    return _land(doc, path, cwd or os.path.dirname(path), inbox)


def _land(doc, path, cwd, inbox):
    h, broken = None, False
    try:
        h = here(cwd)
    except Exception as e:  # noqa: BLE001
        warn("build %s" % doc.get("uuid"), e)
        broken = True
    if h is not None:
        cfg, db = h
        try:
            with db.write() as w:
                ingest.ingest_build(w, cfg, doc, path)
        except Exception as e:  # noqa: BLE001
            warn("build %s" % doc.get("uuid"), e)
        return doc["uuid"]
    # A broken config was warned about above: to_inbox's env inbox is
    # all this_host would find, without a second warning.
    if inbox is None and not broken:
        key, d = this_host(cwd)
        inbox = (key, d) if d else None
    try:
        record.to_inbox(doc, env=inbox)
    except OSError as e:
        warn("the inbox copy of build %s" % doc.get("uuid"), e)
    return doc["uuid"]


def this_host(cwd=None):
    """(host key, inbox dir or None) of this machine: BENCHDB_HOST and
    BENCHDB_INBOX when both are set, else the host and its inbox that the
    benchdb.toml found from cwd names, else ('local', None). A build host
    without the db (the PC) names itself there, so its records carry its
    key and reach its inbox without an environment to set first."""
    ib = record.inbox()
    if ib:
        return ib
    try:
        cfg = config.load(cwd=cwd)
    except Exception as e:  # noqa: BLE001 (a bad config never fails a build)
        warn("this host's benchdb.toml", e)
        return "local", None
    key = cfg.this_host
    h = cfg.hosts.get(key)
    # A relative inbox is relative to the config's directory, not the cwd.
    return key, (cfg.local_path(h.inbox) if h and h.inbox else None)


def regress_lines(cwd, game, uuid, runs=True, say=print):
    """Print `benchdb regress --build uuid` (D13) at the end of integrate
    or package: build-time lines only when runs is False. It reads the db
    and prints, never raises and never changes the command's rc; with no
    db or no record it prints nothing."""
    if not uuid:
        return 0
    try:
        h = here(cwd)
        if h is None:
            return 0
        cfg, db = h
        from . import regress

        res = regress.regress(db, cfg, game, uuid, runs=runs)
        for line in regress.lines(res):
            say(line)
        return res["flags"]
    except Exception as e:  # noqa: BLE001 (warn-only: never fails the command)
        print("benchdb: regress skipped (%s)" % e, file=sys.stderr)
        return 0


def deletions(cwd, plan, items):
    """Journals deletes another tool made (package's prune_bundles):
    items are (path, bytes, reason, result)."""

    def fn(w, cfg):
        agent = os.environ.get("BENCHDB_AGENT")
        for path, size, reason, result in items:
            loc = cfg.locate(path) or ("", os.path.abspath(path))
            w.deletion(plan, loc[0], loc[1], reason, result, bytes=size, agent=agent)

    return _rows(cwd, "the %s deletions" % plan, fn)


def gen_verb(g, verb, stages, toolkit_dir=None):
    """The standalone analyze or recomp verb, timed as a build (D13):
    stages are (name, fn) run in order, each a step. The record goes to
    <out>/<verb>-record.json, so the next run of the verb replaces it.
    A raise is recorded (ok = 0) and re-raised."""
    from ..cli_dir import cli_dir

    t = Timer(
        g.slug,
        "gen",
        verb,
        kind="full",
        cwd=g.root,
        trees=trees(g.root, toolkit_dir, cli_dir()),
    )
    ok = False
    try:
        for name, fn in stages:
            t.timed(name, fn)
        ok = True
    finally:
        t.finish(g.out, ok=ok, cwd=g.root, name=GEN_RECORD % verb)


GEN_RECORD = "%s-record.json"


# ---- what the writers pass in -------------------------------------------------


def host_of(cfg, alias):
    """The [hosts] key of an ssh target ('user@name' or 'name'), or None."""
    if not alias:
        return None
    return cfg.host_of_alias(alias.rsplit("@", 1)[-1])


def tree(name, d):
    """{name, sha, branch, dirty, paths} of a checkout, or None outside git."""
    from ..bench import checks

    if not d or not os.path.isdir(d):
        return None
    return parse.tree_line(checks.tree_state(name, d))


def trees(game_dir, toolkit_dir, cli_dir):
    out = {}
    for name, d in (("game", game_dir), ("toolkit", toolkit_dir), ("cli", cli_dir)):
        t = tree(name, d)
        if t:
            out[name] = t
    return out


def _exe_records(exe):
    d = os.path.dirname(os.path.abspath(exe))
    return [os.path.join(c, record.BUILD) for c in (d, os.path.dirname(d))]


def record_for_exe(exe, sha=None):
    """The build-record.json of the build dir an exe sits in (its dir or
    the one above), when its exe sha is sha; else None."""
    for p in _exe_records(exe):
        doc = record.read(p)
        if doc and doc.get("uuid") and (sha is None or doc.get("exe_sha256") == sha):
            return doc
    return None


def record_path_of(exe, doc):
    """Where record_for_exe found doc."""
    for p in _exe_records(exe):
        r = record.read(p)
        if r and r.get("uuid") == doc.get("uuid"):
            return p
    return _exe_records(exe)[0]


def host_figures():
    """(cpu model, cores, memory GB, load) of this machine; None where unknown."""
    import platform
    import subprocess

    cpu = mem = load = None
    try:
        load = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        if sys.platform == "darwin":

            def sysctl(k):
                return subprocess.run(
                    ["sysctl", "-n", k], capture_output=True, text=True, timeout=5
                ).stdout.strip()

            cpu = sysctl("machdep.cpu.brand_string") or None
            m = sysctl("hw.memsize")
            mem = round(int(m) / 2**30, 1) if m.isdigit() else None
        elif sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("model name"):
                        cpu = line.split(":", 1)[1].strip()
                        break
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem = round(int(line.split()[1]) / 2**20, 1)
                        break
        else:
            cpu = platform.processor() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return cpu, os.cpu_count(), mem, load


class Timer:
    """A build's record as it runs: steps timed in order, then finish()
    writes it (a failed build too, with ok = 0 and the step it broke in)."""

    def __init__(self, game, target, command, kind="incremental", cwd=None, **fields):
        cpu, ncpu, mem, load = host_figures()
        self.doc = dict(
            record="build",
            uuid=record.new_uuid(),
            game=game,
            target=target,
            command=command,
            kind=kind,
            host=this_host(cwd)[0],
            started=now(),
            cpu=cpu,
            ncpu=ncpu,
            mem_gb=mem,
            load_start=load,
            steps=[],
        )
        self.doc.update(fields)
        self.t0 = self._clock()

    @staticmethod
    def _clock():
        import time

        return time.monotonic()

    def step(self, name, secs, detail=None):
        self.doc["steps"].append([name, round(secs, 3), detail])

    def timed(self, name, fn, detail=None):
        """fn(), timed as step name; a raise is timed too, marked failed."""
        t = self._clock()
        try:
            out = fn()
        except BaseException:
            self.step(name, self._clock() - t, "failed")
            self.doc["failed_step"] = name
            raise
        self.step(name, self._clock() - t, detail)
        return out

    def finish(self, build_dir, ok, exe=None, cwd=None, name=None):
        self.doc["ended"] = now()
        self.doc["secs"] = round(self._clock() - self.t0, 3)
        self.doc["ok"] = int(bool(ok))
        if exe and ok and os.path.isfile(exe):
            self.doc["exe_sha256"] = ingest.sha256_file(exe)
        try:
            return build(build_dir, self.doc, cwd=cwd, name=name)
        except Exception as e:  # noqa: BLE001
            warn("the build record", e)
            return None
