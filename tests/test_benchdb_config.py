"""benchdb.toml (design D3): discovery by the cwd walk and the env, the keys
and their errors, the clean verbs refused without it, locate(), and the
game.toml [bench.db] defaults."""

import os

import pytest
from benchdb_util import workspace

from xboxrecomp_cli import manifest
from xboxrecomp_cli.benchdb import cli, config, store


def test_env_then_cwd_walk(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    workspace(ws)
    deep = os.path.join(ws, "wt", "a", "b")
    os.makedirs(deep)
    lone = str(tmp_path / "lone")
    os.makedirs(lone)
    alt = str(tmp_path / "cfg.toml")
    with open(alt, "w") as f:
        f.write('db = "x.db"\n')
    monkeypatch.delenv(config.ENV, raising=False)
    assert config.find(deep) == os.path.join(ws, config.NAME)
    assert config.find(lone) is None
    monkeypatch.setenv(config.ENV, alt)
    # An explicit env wins over a file above the cwd.
    assert config.find(deep) == alt
    assert config.find(lone) == alt
    assert config.load(cwd=lone).db == str(tmp_path / "x.db")
    monkeypatch.setenv(config.ENV, str(tmp_path / "missing.toml"))
    with pytest.raises(config.ConfigError, match="no such file"):
        config.find(lone)


def test_paths_relative_to_the_config(d):
    cfg = workspace(d)
    assert cfg.db == os.path.join(d, "bench-logs", "bench.db")
    assert cfg.roots["store"].path == os.path.join(d, "bench-logs")
    assert cfg.store().name == "store"
    assert cfg.roots["store"].matches("20261020-101010-boot")
    assert not cfg.roots["store"].matches("golden-sessions.tsv")
    assert cfg.host_of_alias("hosta") == "hostA"


@pytest.mark.parametrize(
    "extra, msg",
    [
        ('\n[roots.x]\npath = "x"\nkind = "bogus"\n', "is not one of"),
        ('\n[roots.x]\npath = "x"\nkind = "notes"\ndeletable = true\n', "never deletable"),
        ('\n[roots.x]\npath = "x"\nkind = "scratch"\n', "needs one"),
        ('\n[roots.x]\npath = "x"\nkind = "runs"\nhost = "nowhere"\npattern = "a"\n', "not local"),
        ('\n[roots.x]\npath = "x"\nkind = "runs"\npattern = "("\n', "pattern"),
        ('\n[roots.x]\nkind = "runs"\npattern = "a"\n', "required"),
        ('\n[hosts.b]\nos = "amiga"\n', "hosts.b.os"),
        ("\n[bogus]\n", "unknown key"),
    ],
)
def test_errors_name_the_key(d, extra, msg):
    with pytest.raises(config.ConfigError, match=msg):
        workspace(d, extra)


def test_reference_roots_are_not_deletable(d):
    cfg = workspace(d, '\n[roots.co]\npath = "cat"\nkind = "checkout"\n')
    assert not cfg.roots["co"].deletable
    assert cfg.roots["store"].deletable


def test_no_config_refuses_the_clean_verbs(d, monkeypatch):
    monkeypatch.delenv(config.ENV, raising=False)
    monkeypatch.chdir(d)
    cfg = config.load(cwd=d)
    assert not cfg.present
    for v in ("plan", "clean"):
        with pytest.raises(config.ConfigError, match="the clean verbs need one"):
            cfg.require(v)
    workspace(d).require("plan")


def test_locate_deepest_root(d):
    cfg = workspace(d, '\n[roots.inbox]\npath = "bench-logs/inbox"\nkind = "inbox"\n')
    assert cfg.locate(os.path.join(d, "bench-logs", "20261020-101010")) == (
        "store",
        "20261020-101010",
    )
    assert cfg.locate(os.path.join(d, "bench-logs", "inbox", "hostA", "u.json")) == (
        "inbox",
        "hostA/u.json",
    )
    assert cfg.locate(os.path.join(d, "outside")) is None


def test_locate_through_a_link(d):
    cfg = workspace(d)
    os.makedirs(os.path.join(d, "game"))
    os.symlink(os.path.join(d, "bench-logs"), os.path.join(d, "game", "bench-logs"))
    real = os.path.realpath(os.path.join(d, "game", "bench-logs", "20261020-101010"))
    assert cfg.locate(real) == ("store", "20261020-101010")


def test_game_toml_defaults():
    """D17's defaults, from the manifest SPEC."""
    m = manifest.current().m
    db = m["bench"]["db"]
    assert (db["keep_releases"], db["internal_keep"]) == (2, 3)
    r = m["bench"]["db"]["regress"]
    assert (r["build_rise"], r["build_min_secs"], r["build_window"]) == (1.5, 30, 5)
    assert r["build_min_baselines"] == 3
    assert m["release"]["tag_glob"] == "v*"
    assert config.game_settings(manifest.current())["db"]["internal_keep"] == 3


def test_init_verb(d, monkeypatch, capsys):
    workspace(d)
    monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))
    assert cli.main(["--config", os.path.join(d, config.NAME), "init"]) == 0
    out = capsys.readouterr().out
    assert "schema 1" in out and "2 roots, 2 hosts" in out
