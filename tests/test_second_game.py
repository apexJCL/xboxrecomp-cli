"""A second game (testdata/game2, in the shape of Burnout 3's manifest):
the keys added for it, each against BLiNX 2's behaviour, which must not
move. Synthetic tree, fake toolkit, no network, no build.

  uv run pytest tests/test_second_game.py
"""

import contextlib
import json
import os
import shutil
import subprocess

from xboxrecomp_cli import build, host, main, manifest, pipeline
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.bench import sync
from xboxrecomp_cli.package import lib as pl
from xboxrecomp_cli.package import plib

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
GAME2 = os.path.join(HERE, "testdata", "game2")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@contextlib.contextmanager
def game2_tree(d):
    """game2 copied to d/game2 with what its pipeline reads, its dump folder
    made read-only as the player's own copy is; the current game while it
    runs, with the package library configured for it."""
    root = os.path.join(d, "game2")
    tk = os.path.join(d, "tk")
    os.makedirs(root)
    shutil.copy(os.path.join(GAME2, "game.toml"), root)
    G = manifest.load(root)
    os.makedirs(G.gen)
    write(G.xbe, "xbe")
    write(G.seeds[0], "[]")
    write(G.recomp_manual, "/* */")
    open(os.path.join(G.gen, "recomp_funcs.h"), "w").close()
    os.makedirs(os.path.join(tk, "tools", "recomp", "output"))
    os.chmod(G.game_files, 0o555)
    saved_env = os.environ.get("XBOXRECOMP_DIR")
    saved = manifest.current()
    os.environ["XBOXRECOMP_DIR"] = tk
    manifest.use(G)
    try:
        yield G, tk
    finally:
        os.chmod(G.game_files, 0o755)
        manifest.use(saved)
        plib()
        if saved_env is None:
            os.environ.pop("XBOXRECOMP_DIR", None)
        else:
            os.environ["XBOXRECOMP_DIR"] = saved_env


def test_manifest_paths(d):
    with game2_tree(d) as (G, _):
        assert G.analysis_json == os.path.join(G.root, "build", "xr", "analysis.json")
        assert G.game_files == os.path.join(G.root, "Second Game Files")
        assert G.m["bench"]["sync_excludes"] == ["/bin/", "/runs/"]
    cat = manifest.load(GAME)
    # cat's default: beside the dump, as before the key existed.
    assert cat.analysis_json == os.path.join(GAME, "game_files", "default_analysis.json")
    assert cat.m["pipeline"]["analysis_json"] == ""
    assert cat.m["bench"]["sync_excludes"] == []


def test_analysis_json_must_stay_inside():
    text = open(os.path.join(GAME2, "game.toml")).read()
    bad = text.replace(
        'analysis_json = "build/xr/analysis.json"', 'analysis_json = "../dump/a.json"'
    )
    try:
        manifest.parse_text(bad)
    except manifest.ManifestError as e:
        assert "pipeline.analysis_json" in str(e)
    else:
        raise AssertionError("an analysis_json outside the game was accepted")


def test_stage_argv(d):
    """parse and disasm read and write the analysis under build/xr; the
    spaced dump folder is one argument everywhere; regen.sh's shape:
    every section (no --text-only), split 1000, the manifest's game name."""
    with game2_tree(d) as (G, _):
        argv = json.loads(pipeline.stage_argv())
    xbe = "$ROOT/Second Game Files/default.xbe"
    analysis = "$ROOT/build/xr/analysis.json"
    parse, disasm, funcid, abi, recomp = argv
    assert parse == ["tool", "xbe_parser", [xbe, "--json", analysis]]
    assert disasm[2][0] == xbe and analysis in disasm[2]
    assert "--text-only" not in disasm[2] and "--extra-sections" not in disasm[2]
    assert ["--seed-functions", "$ROOT/config/seed_functions.json"] == disasm[2][
        disasm[2].index("--seed-functions") : disasm[2].index("--seed-functions") + 2
    ]
    r = recomp[2]
    assert r[r.index("--split") + 1] == "1000"
    assert r[r.index("--game-name") + 1] == "Second Game"
    assert r[r.index("--exclude-manual") + 1] == "$ROOT/src/game/recomp/recomp_manual.c"
    assert "--spin-waits" not in r
    for cmd in argv:
        for a in cmd[2]:
            assert a.count("Second Game Files") <= 1 and (
                "Second Game Files" not in a or a.startswith("$ROOT/")
            ), a


def test_parse_makes_the_analysis_dir_and_leaves_the_dump(d, monkeypatch):
    calls = []
    with game2_tree(d) as (G, _):
        monkeypatch.setenv("XBOXRECOMP_PYTHON", "python3")
        monkeypatch.setattr(host, "run", lambda cmd, **kw: calls.append(cmd))
        monkeypatch.setattr(pipeline, "begin_stage", lambda *a: None)
        before = sorted(os.listdir(G.game_files))
        pipeline.stage_parse()
        assert os.path.isdir(os.path.dirname(G.analysis_json))
        assert sorted(os.listdir(G.game_files)) == before
    assert calls and calls[0][-2:] == ["--json", G.analysis_json], calls


def test_default_targets(d):
    with game2_tree(d):
        for o in ("macos", "linux", "windows"):
            assert build.default_build_target(o) == "windows"
        # No [package]: the host's own bundle, which package then refuses.
        assert main.native_target("macos") == "macos"
        host.g().m["package"] = {"targets": ["windows", "steamos"]}
        assert main.native_target("macos") == "windows"
        assert main.native_target("linux") == "steamos"
        assert main.native_target("windows") == "windows"
        host.g().m["package"] = {"targets": ["macos", "steamos"]}
        assert main.native_target("windows") == "steamos"
        assert main._build_target_help() == "default: windows (game.toml's build.targets)"
    # BLiNX 2 (windows and macos everywhere): the host's own target.
    assert build.default_build_target("macos") == "macos"
    assert build.default_build_target("linux") == "windows"
    assert [main.native_target(o) for o in ("macos", "linux", "windows")] == [
        "macos",
        "steamos",
        "windows",
    ]
    assert main._build_target_help() == "default: macos on a macOS host, windows elsewhere"


def test_stock_rule_from_the_manifest(d):
    off = {"CMAKE_BUILD_TYPE": "Release", "XBOXRECOMP_ENHANCE": "OFF"}
    # BLiNX 2: the layer is stock, OFF is refused with the text it always had.
    assert pl.cache_problems(off) == ([], ["XBOXRECOMP_ENHANCE=OFF (want ON)"])
    assert pl.cache_problems({**off, "XBOXRECOMP_ENHANCE": "ON"}) == ([], [])
    assert pl.nonstock_help(["CAT_GEN_OPT"], ["-DXBOXRECOMP_ENHANCE=ON", "-UCAT_GEN_OPT"]) == [
        "CAT_GEN_OPT",
        "XBOXRECOMP_ENHANCE=OFF",
    ]
    with game2_tree(d):
        host.g().m["package"] = {"app": "Game2", "product": "game2-recomp"}
        plib()
        # No enhancements layer: a cache without it is the stock build.
        assert pl.cache_problems(off) == ([], [])
        dbg, ns = pl.cache_problems({**off, "CMAKE_BUILD_TYPE": "Debug"})
        assert dbg == ["CMAKE_BUILD_TYPE=Debug (want Release)"] and ns == []
    # Other values: booleans by CMake's truth, anything else as text.
    pl.configure("p", "n", 1, "game", "App", stock_cmake=["-DFOO:BOOL=OFF", "-DMODE=fast"])
    try:
        assert pl.cache_problems({"CMAKE_BUILD_TYPE": "Release", "FOO": "0", "MODE": "fast"}) == (
            [],
            [],
        )
        assert pl.cache_problems({"CMAKE_BUILD_TYPE": "Release", "FOO": "YES", "MODE": "x"})[1] == [
            "FOO=YES (want OFF)",
            "MODE=x (want fast)",
        ]
        assert pl.nonstock_help([], ["-DFOO:BOOL=OFF", "-DMODE=fast"]) == ["FOO=ON", "MODE!=fast"]
    finally:
        plib()


def test_sync_excludes_last(d):
    cat = manifest.load(GAME)
    with game2_tree(d) as (G, _):
        ex = sync.game_excludes(G)
    assert ex[-2:] == ["/bin/", "/runs/"]
    assert "/Second Game Files" in ex and "/build/xr" in ex
    # BLiNX 2's list is exactly the one every recorded sync sent.
    assert sync.game_excludes(cat)[-1] == "*.tar"


def test_notice_optional(d, monkeypatch):
    with game2_tree(d) as (G, _):
        host.g().m["package"] = {"content": "packaging"}
        G.content = G.path("packaging")
        write(os.path.join(G.content, "launch.env.default.windows"), "LOG_KEEP=10\n")
        write(os.path.join(G.content, "enhance.toml.default"), "# none\n")
        write(G.path("LICENSE"), "MIT\n")
        monkeypatch.setattr(pkg, "readme", lambda *a: None)
        monkeypatch.setattr(pl, "stage_game_files", lambda src, dst: 0)
        payload = os.path.join(d, "payload")
        os.makedirs(payload)
        pkg.write_common(payload, "windows", "v", pl)
        assert sorted(os.listdir(payload)) == [
            "LICENSE",
            "enhance.toml.default",
            "launch.env.default",
        ]
        write(G.path("NOTICE"), "third parties\n")
        pkg.write_common(payload, "windows", "v", pl)
        assert "NOTICE" in os.listdir(payload)


def test_host_path():
    from xboxrecomp_cli.bench.remote import host_path

    assert (
        host_path("~/xbox-recomp/b3/Second Game Files") == "~/xbox-recomp/b3/Second\\ Game\\ Files"
    )
    assert host_path("~/xbox-recomp/cat/game_files") == "~/xbox-recomp/cat/game_files"
    assert host_path("~") == "~" and host_path("~/") == "~/"
    assert host_path("/srv/a b") == "/srv/a\\ b"
    # Only a leading ~ is left to the host's shell.
    assert host_path("/srv/~x") == "/srv/~x" and host_path("~x") == "\\~x"


def test_prologue_values(d):
    """The game's values reach the host from its manifest; a game's own
    toolchain file wins over the CLI's."""
    from xboxrecomp_cli.bench.config import Config
    from xboxrecomp_cli.bench.remote import Remote

    with game2_tree(d) as (G, tk):
        env = {"XBOXRECOMP_DIR": tk, "HOME": d}
        cfg = Config(G, env)
        assert cfg["BENCH_GAME_FILES"] == "~/xbox-recomp/game2/Second Game Files"
        p = Remote(cfg).prologue()
        assert 'TOOLCHAIN="$CLI_DIR"/src/xboxrecomp_cli/cmake/llvm-mingw-x86_64.cmake\n' in p
        assert "CRASH_TAG=\\[FAULT\\]\n" in p and "EXE_REL=build-win/game2.exe\n" in p
        G.m["build"]["toolchain_file"] = "cmake/my toolchain.cmake"
        G.m["build"]["exe_dir"] = "bin"
        p = Remote(cfg).prologue()
        assert 'TOOLCHAIN="$REMOTE_GAME"/cmake/my\\ toolchain.cmake\n' in p
        assert "EXE_REL=build-win/bin/game2.exe\n" in p
    assert os.path.isfile(manifest.CLI_TOOLCHAIN)
    assert manifest.load(GAME).toolchain_file == manifest.CLI_TOOLCHAIN


def test_stale_toolchain_reset(d):
    bdir = os.path.join(d, "build-win")
    sysf = os.path.join(bdir, "CMakeFiles", "3.31.0", "CMakeSystem.cmake")
    write(os.path.join(bdir, "CMakeCache.txt"), "x\n")
    write(sysf, 'set(A 1)\ninclude("%s")\n' % manifest.CLI_TOOLCHAIN)
    assert not build.reset_stale_toolchain(bdir)
    assert os.path.isfile(os.path.join(bdir, "CMakeCache.txt"))
    write(sysf, 'include("%s")\n' % os.path.join(d, "gone", "llvm-mingw-x86_64.cmake"))
    assert build.reset_stale_toolchain(bdir)
    assert not os.path.exists(os.path.join(bdir, "CMakeCache.txt"))
    assert not os.path.exists(os.path.join(bdir, "CMakeFiles"))


def test_other_toolchain_reset(d):
    """A cache naming another toolchain file, which still exists: CMake would
    keep it whatever -DCMAKE_TOOLCHAIN_FILE says, so the tree starts afresh."""
    bdir = os.path.join(d, "build-win")
    other = os.path.join(d, "old", "llvm-mingw-x86_64.cmake")
    write(other, "# old\n")
    cache = os.path.join(bdir, "CMakeCache.txt")
    write(cache, "CMAKE_TOOLCHAIN_FILE:FILEPATH=%s\n" % manifest.CLI_TOOLCHAIN)
    assert not build.reset_stale_toolchain(bdir, manifest.CLI_TOOLCHAIN)
    write(cache, "CMAKE_TOOLCHAIN_FILE:UNINITIALIZED=%s\n" % other)
    write(os.path.join(bdir, "CMakeFiles", "x"), "")
    assert not build.reset_stale_toolchain(bdir)  # no current file to compare
    assert build.reset_stale_toolchain(bdir, manifest.CLI_TOOLCHAIN)
    assert not os.path.exists(cache)
    assert not os.path.exists(os.path.join(bdir, "CMakeFiles"))


def host_fresh_if_stale(script):
    """fresh_if_stale from a host script, as bash source."""
    with open(os.path.join(os.path.dirname(build.__file__), "bench", "host", script)) as f:
        text = f.read()
    start = text.index("fresh_if_stale() {")
    return text[start : text.index("\n}\n", start) + 3]


def test_host_fresh_if_stale(d):
    """build.sh and tests.sh start a tree afresh when its toolchain file is
    gone or is another file (the toolkit's standalone test trees on the bench
    host kept the game's old copy), and leave a current tree alone."""
    current = os.path.join(d, "cli", "llvm-mingw-x86_64.cmake")
    other = os.path.join(d, "game", "llvm-mingw-x86_64.cmake")
    write(current, "")
    write(other, "")
    for script in ("build.sh", "tests.sh"):
        fn = host_fresh_if_stale(script)
        for name, sys_inc, cached, reset in (
            ("current", current, current, False),
            ("gone", os.path.join(d, "gone.cmake"), os.path.join(d, "gone.cmake"), True),
            ("other", other, other, True),
            ("fresh", None, None, False),
        ):
            t = os.path.join(d, script, name)
            os.makedirs(os.path.join(t, "CMakeFiles", "3.31.0"))
            if sys_inc:
                write(
                    os.path.join(t, "CMakeFiles", "3.31.0", "CMakeSystem.cmake"),
                    'include("%s")\n' % sys_inc,
                )
            if cached:
                write(
                    os.path.join(t, "CMakeCache.txt"), "CMAKE_TOOLCHAIN_FILE:FILEPATH=%s\n" % cached
                )
            r = subprocess.run(
                ["bash", "-c", 'set -euo pipefail\n%sfresh_if_stale "$1"' % fn, "x", t],
                env=dict(os.environ, TOOLCHAIN=current),
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0, (script, name, r.stderr)
            assert os.path.isdir(os.path.join(t, "CMakeFiles")) != reset, (script, name)
            assert ("configuring afresh" in r.stdout) == reset, (script, name, r.stdout)
