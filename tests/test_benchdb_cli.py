"""benchdb's verbs (design D8): import (paths, --all, --dry-run,
--rebuild), query (named and --sql), show, export and releases, and the
`<game> bench db` alias."""

import json
import os

import pytest
from benchdb_util import (
    GAME_SHA,
    GAMES,
    GOLDEN_TXT,
    MAC_INFO,
    PROTON_INFO,
    SHA_A,
    flip_log,
    game_dir,
    run_dir,
    workspace,
)

from xboxrecomp_cli import bench, manifest
from xboxrecomp_cli.benchdb import cli, config, store


@pytest.fixture
def ws(d, monkeypatch):
    monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))
    game_dir(d)
    workspace(d, GAMES)
    s = os.path.join(d, "bench-logs")
    run_dir(s, "20261025-072135", PROTON_INFO, golden=GOLDEN_TXT, log=flip_log())
    run_dir(
        s,
        "20261025-074507-metal-menu",
        MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws=d),
        golden=GOLDEN_TXT,
        log=flip_log(),
    )
    run_dir(os.path.join(s, "topic"), "20261024-100000", extra={"game-stdio.log": "x\n"})
    with open(os.path.join(s, "golden-sessions.tsv"), "w") as f:
        f.write(
            "2026-10-25 07:34:10\tgolden\ttests=pass\tonly=menu\t"
            "menu=20261025-072135:pass\trc=0\thost=posix\n"
        )
    return d


def run(d, *args):
    return cli.main(["--config", os.path.join(d, config.NAME)] + list(args))


def test_import_all_then_again(ws, capsys):
    assert run(ws, "import", "--all") == 0
    out = capsys.readouterr().out
    assert "3 added" in out or "4 added" in out
    assert "unparsed" in out and "topic/20261024-100000" in out
    db = store.open(cfg=config.load(os.path.join(ws, config.NAME)), readonly=True)
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 3
    assert db.one("SELECT count(*) AS n FROM sessions")["n"] == 2
    assert run(ws, "import", "--all") == 0
    out = capsys.readouterr().out
    assert "0 added" in out
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 3


def test_import_dry_run_writes_nothing(ws, capsys):
    assert run(ws, "import", "--all", "--dry-run") == 0
    assert "(dry run)" in capsys.readouterr().out
    db = store.open(cfg=config.load(os.path.join(ws, config.NAME)), readonly=True)
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 0


def test_import_paths_and_errors(ws, capsys):
    p = os.path.join(ws, "bench-logs", "20261025-072135")
    assert run(ws, "import", p) == 0
    assert "1 added" in capsys.readouterr().out
    assert run(ws, "import") == 2
    assert run(ws, "import", os.path.join(ws, "nothing.bin")) == 1
    assert "not a run dir" in capsys.readouterr().err


def test_import_rebuild(ws, capsys):
    assert run(ws, "import", "--all") == 0
    cfg = config.load(os.path.join(ws, config.NAME))
    db = store.open(cfg=cfg)
    with db.write() as w:
        w.deletion("plan-1", "store", "20261001-000000", "old", "deleted", bytes=5)
    assert run(ws, "import", "--rebuild") == 0
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 3
    # The journal survives a rebuild: disk cannot recreate it.
    assert db.one("SELECT count(*) AS n FROM deletions")["n"] == 1


def test_import_rebuild_keeps_what_lands_during_the_fill(ws, monkeypatch, capsys):
    """A bench that ends mid-rebuild writes its run dir and its live row;
    a pins refresh writes the live pins. None of it is lost."""
    from xboxrecomp_cli.benchdb import releases

    assert run(ws, "import", "--all") == 0
    cfg = config.load(os.path.join(ws, config.NAME))
    db = store.open(cfg=cfg)
    real = releases.match
    done = []

    def match(tmp, c):
        if not done:
            done.append(1)
            s = os.path.join(ws, "bench-logs")
            run_dir(s, "20261025-090000", PROTON_INFO, golden=GOLDEN_TXT, log=flip_log())
            assert run(ws, "import", os.path.join(s, "20261025-090000")) == 0
            with db.write() as w:
                w.pin("20261025-090000", "notes/x.md:1", "2026-10-25T09:00:00Z")
        return real(tmp, c)

    monkeypatch.setattr(releases, "match", match)
    assert run(ws, "import", "--rebuild") == 0
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 4
    assert db.one("SELECT count(*) AS n FROM runs WHERE stamp = '20261025-090000'")["n"] == 1
    assert db.one("SELECT count(*) AS n FROM pins")["n"] == 1
    assert not os.path.exists(db.path + ".rebuild.lock")


def test_query_named_and_sql(ws, capsys):
    run(ws, "import", "--all")
    capsys.readouterr()
    assert run(ws, "query", "runs", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["stamp"] for r in rows} == {"20261025-072135", "20261025-074507", "20261024-100000"}
    assert run(ws, "query", "runs", "--backend", "metal", "--tsv") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("stamp\tgame") and len(lines) == 2
    assert run(ws, "query", "sessions") == 0
    assert "menu:pass" in capsys.readouterr().out
    assert run(ws, "query", "--sql", "SELECT count(*) AS n FROM runs", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{"n": 3}]
    # --sql is read-only.
    assert run(ws, "query", "--sql", "DELETE FROM runs") == 1
    assert run(ws, "query", "nosuch") == 1
    assert run(ws, "query", "pins", "--host", "x") == 1
    assert run(ws, "query") == 2


def test_show(ws, capsys):
    run(ws, "import", "--all")
    capsys.readouterr()
    assert run(ws, "show", "20261025-072135", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["scenario"] == "menu"
    assert {p["source"] for p in out["perf"]} == {"run-info", "flip-trace"}
    assert out["verdicts"][0]["word"] == "pass"
    assert run(ws, "show", "20261025-074507-metal-menu") == 0
    assert "== frames" in capsys.readouterr().out
    assert run(ws, "show", "nothing-here") == 1


def test_export(ws, capsys, tmp_path):
    run(ws, "import", "--all")
    capsys.readouterr()
    assert run(ws, "export", "--sessions-tsv", "-") == 0
    out = capsys.readouterr().out
    assert "menu=20261025-072135:pass" in out and "rc=0" in out
    dest = str(tmp_path / "out")
    assert run(ws, "export", "--jsonl", dest) == 0
    with open(os.path.join(dest, "runs.jsonl")) as f:
        assert len(f.readlines()) == 3


def test_export_refuses_a_pin_source(d, monkeypatch, capsys):
    monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))
    os.makedirs(os.path.join(d, "notes", "bench-db"))
    workspace(d, '\n[refs]\npaths = ["notes"]\n')
    assert run(d, "init") == 0
    assert run(d, "export", "--jsonl") == 1
    assert "[refs] exclude" in capsys.readouterr().err
    workspace(d, '\n[refs]\npaths = ["notes"]\nexclude = ["notes/bench-db"]\n')
    assert run(d, "export", "--jsonl") == 0


def test_releases_listing_empty(ws, capsys):
    run(ws, "init")
    capsys.readouterr()
    assert run(ws, "releases") == 0
    assert "(no rows)" in capsys.readouterr().out


def test_bench_db_alias(ws, monkeypatch, capsys):
    monkeypatch.setenv(config.ENV, os.path.join(ws, config.NAME))
    g = manifest.load(os.path.join(ws, "demo"))
    assert bench.main(["db", "import", "--all"], g) == 0
    capsys.readouterr()
    assert bench.main(["db", "query", "runs", "--json"], g) == 0
    rows = json.loads(capsys.readouterr().out)
    # --game demo: every run here is demo's but the unparsed one.
    assert {r["game"] for r in rows} == {"demo"}
    assert "db" in bench.render_help(g)
