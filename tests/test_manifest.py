"""game.toml: the schema, its defaults and errors, and BLiNX 2's manifest
(testdata/game) giving exactly the constants its single-file CLI carried
(blinx2-recomp 70bf5c8: blinx2.py and scripts/package_lib.py).

  uv run pytest tests/test_manifest.py
"""

import os

import pytest

from xboxrecomp_cli import build, manifest, pipeline
from xboxrecomp_cli import doctor as doctor_mod
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.package import lib as pl

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
SHA = "0123456789abcdef0123456789abcdef01234567"

MINIMAL = """schema = 1
[game]
name = "Some Game"
slug = "some-game"
[xbe]
title_id = 0x12345678
[cli]
commit = "%s"
[toolkit]
url = "https://example.invalid/tk.git"
branch = "main"
commit = "%s"
[pipeline]
game_name = "some"
gen = "src/gen"
[build]
exe = "some_recomp"
""" % (SHA, SHA)


def parse(text):
    return manifest.parse_text(text)


def test_minimal_defaults():
    m = parse(MINIMAL)
    assert m["xbe"]["path"] == "game_files/default.xbe"
    assert m["xbe"]["sha256"] == []
    assert m["pipeline"]["out"] == "analysis" and m["pipeline"]["split"] == 250
    assert m["pipeline"]["disasm"] == {"text_only": True, "extra_sections": []}
    assert m["build"]["targets"] == ["windows"]
    assert m["build"]["stock_cmake"] == ["-DXBOXRECOMP_ENHANCE=ON"]
    assert m["data"]["dir_env"] == "SOME_GAME_DATA_DIR"
    assert m["data"]["steamos"] == "~/Games/some-game"
    assert m["golden"] == {"json": "", "frames": "", "audio": "", "enhance_stock": {}}
    assert m["bench"]["gc"] == {"refs": [], "refs_exclude": [], "keep": 3, "days": 7}
    assert m["bench"]["main_branch"] == "main" and m["bench"]["toolkit_tests"] is True
    assert "package" not in m  # optional: package says so when asked


def test_package_defaults():
    m = parse(MINIMAL + '[package]\napp = "SomeGame"\n')
    p = m["package"]
    assert p["product"] == "some-game-recomp" and p["targets"] == ["windows", "steamos"]
    assert m["data"]["windows"] == "%LOCALAPPDATA%\\SomeGame"
    assert m["data"]["dir_env"] == "SOMEGAME_DATA_DIR"


@pytest.mark.parametrize(
    "edit, key",
    [
        (lambda t: t.replace('name = "Some Game"\n', ""), "game.name: required"),
        (lambda t: t + "[bogus]\nx = 1\n", "bogus: unknown table"),
        (lambda t: t.replace("[build]\n", '[build]\nexe_name = "x"\n'), "build.exe_name: unknown"),
        (lambda t: t.replace("schema = 1", "schema = 2"), "schema: this CLI reads schema 1"),
        (lambda t: t.replace('slug = "some-game"', 'slug = "Some Game"'), "game.slug:"),
        (lambda t: t.replace("title_id = 0x12345678", 'title_id = "x"'), "xbe.title_id: must be"),
        (lambda t: t.replace(SHA, "abc", 1), "cli.commit: a full 40-character"),
        (lambda t: t.replace('gen = "src/gen"', 'gen = "/abs/gen"'), "pipeline.gen: a path"),
        (lambda t: t.replace('gen = "src/gen"', 'gen = "../gen"'), "pipeline.gen: must stay"),
        (
            lambda t: t.replace("[build]\n", '[build]\ntargets = ["linux"]\n'),
            "build.targets: 'linux'",
        ),
        (
            lambda t: t + '[package]\napp = "A"\ntargets = ["macos"]\n',
            "package.targets: macos needs build.targets",
        ),
        (
            lambda t: t + '[package]\napp = "Some_Recomp"\n',
            "package.app: 'Some_Recomp' names the same file as build.exe",
        ),
        (lambda t: t + '[xbe.extra]\nx = "y"\n', "xbe.extra: unknown table"),
        (
            lambda t: t + '[golden]\nenhance_stock = { "render.scale" = "1" }\n',
            "golden.enhance_stock: 'render.scale' is a toolkit key",
        ),
        (
            lambda t: t + '[golden]\nenhance_stock = { "fx.glow" = 1 }\n',
            "golden.enhance_stock: must be a table of strings",
        ),
        (lambda t: t + '[bench.gc]\nkeep = "3"\n', "bench.gc.keep: must be a integer"),
        (lambda t: t + '[bench.gc]\nrefs = ["/abs"]\n', "bench.gc.refs: '/abs' must be relative"),
        (lambda t: t + "[bench.gc]\nother = 1\n", "bench.gc.other: unknown key"),
    ],
)
def test_errors_name_the_key(edit, key):
    with pytest.raises(manifest.ManifestError, match=key.replace("(", r"\(")):
        parse(edit(MINIMAL))


def test_game_tables():
    """enhance_stock is one key holding a table; bench.gc's refs may leave
    the game root (the workspace's notes beside it), unlike other paths."""
    m = parse(
        MINIMAL
        + '[golden]\nenhance_stock = { "fps.mode" = "lock30", "fx.glow" = "on" }\n'
        + '[bench.gc]\nrefs = ["timeline", "../notes"]\nrefs_exclude = ["../notes/x/*"]\ndays = 3\n'
    )
    assert m["golden"]["enhance_stock"] == {"fps.mode": "lock30", "fx.glow": "on"}
    assert m["bench"]["gc"] == {
        "refs": ["timeline", "../notes"],
        "refs_exclude": ["../notes/x/*"],
        "keep": 3,
        "days": 3,
    }


def test_path_with_a_space(d):
    root = os.path.join(d, "my games", "some game")
    os.makedirs(root)
    with open(os.path.join(root, "game.toml"), "w") as f:
        f.write(MINIMAL.replace('gen = "src/gen"', 'gen = "src/gen files"'))
    g = manifest.load(root)
    assert g.gen == os.path.join(root, "src", "gen files")
    assert g.rel(g.gen) == "src/gen files"


def test_no_manifest(d):
    with pytest.raises(manifest.ManifestError, match="game.toml"):
        manifest.load(d)


def test_blinx2_constants():
    """The values blinx2.py and package_lib.py held at 70bf5c8."""
    g = manifest.use(manifest.load(GAME))
    m = g.m
    assert m["pipeline"]["game_name"] == "cat"  # GAME_NAME
    assert (
        ",".join(m["pipeline"]["disasm"]["extra_sections"])
        == "D3D,D3DX,XGRPH,DSOUND,PSFD_I,PSFD_B,PSFD_P,PSFD00,SRCADV,SRCED,SRCAC,XPP"
    )  # DISASM_EXTRA_SECTIONS
    assert build.stock_cmake_args() == (
        "-DCMAKE_BUILD_TYPE=Release",
        "-DXBOXRECOMP_ENHANCE=ON",
        "-UCAT_GEN_OPT",
    )  # STOCK_CMAKE_ARGS
    assert {t: pkg.data_paths(t) for t in ("windows", "steamos", "macos")} == {
        "windows": "  %LOCALAPPDATA%\\BLiNX2\\   (hdd, config, logs)",
        "steamos": "  ~/Games/BLiNX2/   (hdd, config, logs; the program is in versions/)",
        "macos": "  ~/Library/Application Support/BLiNX2/   (hdd, config, logs)",
    }  # DATA_PATHS
    assert doctor_mod.brew_formulae() == ["sdl2", "sdl3", "openssl", "libepoxy"]  # brew_missing
    pkg.plib()
    assert pl.TITLE_ID == 0x4D530065
    assert pl.PRODUCT == "blinx2-recomp" and pl.PRODUCT_NAME == "BLiNX 2"
    assert pl.GAME_FILES_EXCLUDE == ("default_analysis.json", "UDATA", "TDATA", ".DS_Store")
    assert m["data"]["dir_env"] == "BLINX2_DATA_DIR"
    assert m["build"]["exe"] == "cat_recomp" and g.exe_name == "cat_recomp"
    assert pipeline.split_size() == "250" or os.environ.get("SPLIT")
    assert g.analysis_json == os.path.join(GAME, "game_files", "default_analysis.json")
    assert g.regen_marker == os.path.join(GAME, "src", "recomp", ".gen-regenerating")
    assert g.gen_key == os.path.join(GAME, "src", "recomp", "gen.key.json")
