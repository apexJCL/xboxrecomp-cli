"""A second game (testdata/game2, in the shape of Burnout 3's manifest):
the keys added for it, each against BLiNX 2's behaviour, which must not
move. Synthetic tree, fake toolkit, no network, no build.

  uv run pytest tests/test_second_game.py
"""

import contextlib
import json
import ntpath
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
    # An import's staging and swapped-out trees hold the dump too.
    assert "/Second Game Files.partial" in ex and "/Second Game Files.old" in ex
    # BLiNX 2's list is exactly the one every recorded sync sent, plus those.
    assert sync.game_excludes(cat)[-1] == "*.tar"
    assert {"/game_files.partial", "/game_files.old"} <= set(sync.game_excludes(cat))


def test_notice_optional(d, monkeypatch):
    with game2_tree(d) as (G, _):
        host.g().m["package"] = {"content": "packaging"}
        G.content = G.path("packaging")
        write(os.path.join(G.content, "launch.env.default.windows"), "LOG_KEEP=10\n")
        write(os.path.join(G.content, "config.default.toml"), "# none\n")
        write(G.path("LICENSE"), "MIT\n")
        monkeypatch.setattr(pkg, "readme", lambda *a: None)
        monkeypatch.setattr(pl, "stage_game_files", lambda src, dst: 0)
        payload = os.path.join(d, "payload")
        os.makedirs(payload)
        pkg.write_common(payload, "windows", "v", pl)
        assert sorted(os.listdir(payload)) == [
            "LICENSE",
            "config.default.toml",
            "launch.env.default",
        ]
        write(G.path("NOTICE"), "third parties\n")
        pkg.write_common(payload, "windows", "v", pl)
        assert "NOTICE" in os.listdir(payload)


# The package table the two tests below need (game2 has none).
PKG2 = {
    "content": "packaging",
    "targets": ["windows"],
    "dylib_companions": [],
    "app": "Game2",
    "product": "game2-recomp",
}


def test_old_defaults_file_refused(d, monkeypatch):
    """A game whose packaging content still has enhance.toml.default and no
    config.default.toml is refused before anything is built, the message
    naming the rename; with config.default.toml it goes on."""
    with game2_tree(d) as (G, _):
        host.g().m["package"] = dict(PKG2)
        G.content = G.path("packaging")
        write(os.path.join(G.content, "enhance.toml.default"), "# old\n")
        try:
            pkg.check_content()
        except host.CliError as e:
            msg = str(e)
        else:
            raise AssertionError("not refused")
        assert "config.default.toml" in msg and "git mv" in msg, msg
        assert "packaging/enhance.toml.default" in msg, msg
        # package() refuses before its plan (the build) runs.
        monkeypatch.setattr(pkg.setup, "host_blockers", lambda t, importing=False: [])
        monkeypatch.setattr(pkg.toolkit, "exclude_pin_mark", lambda t: None)

        def no_plan(a):
            raise AssertionError("the plan ran")

        monkeypatch.setattr(pkg, "package_plan", no_plan)
        a = type("A", (), {"target": "windows", "out": d, "hud_pack": None})()
        try:
            pkg.package(a)
        except host.CliError as e:
            assert "config.default.toml" in str(e)
        else:
            raise AssertionError("not refused")
        # Both present (a game mid-rename): the new file wins, no refusal.
        write(os.path.join(G.content, "config.default.toml"), "# new\n")
        pkg.check_content()
        os.remove(os.path.join(G.content, "enhance.toml.default"))
        pkg.check_content()


def test_hud_pack_textures_table_refused(d):
    """--hud-pack appends a [textures] table to the defaults; one already
    there (not commented) would be a duplicate header that makes the
    runtime reject the whole file, so package refuses before building."""
    with game2_tree(d) as (G, _):
        host.g().m["package"] = dict(PKG2)
        G.content = G.path("packaging")
        new = os.path.join(G.content, "config.default.toml")
        write(new, '[render]\nscale = 1\n\n[textures]\npack = "none"\n')
        info = {"path": "x", "entries": 1, "scale": 4}
        try:
            pkg.check_content(info)
        except host.CliError as e:
            msg = str(e)
        else:
            raise AssertionError("not refused")
        assert "[textures]" in msg and "packaging/config.default.toml" in msg, msg
        pkg.check_content()  # without --hud-pack the table is fine
        write(new, '[render]\nscale = 1\n\n# [textures]\n# pack = "x"\n')
        pkg.check_content(info)


def test_macos_defaults_in_resources(d, monkeypatch):
    """On macOS write_common's payload, config.default.toml with the hud
    pack's key included, lands in Contents/Resources: the runtime's Apple
    fallback (<exe dir>/../Resources), beside the read-only content."""
    from xboxrecomp_cli.package import macos

    with game2_tree(d) as (G, _):
        host.g().m["package"] = dict(PKG2)
        G.content = G.path("packaging")
        write(os.path.join(G.content, "launch.env.default.macos"), "LOG_KEEP=10\n")
        write(os.path.join(G.content, "config.default.toml"), "[render]\nscale = 1\n")
        write(G.path("LICENSE"), "MIT\n")
        monkeypatch.setattr(pkg, "readme", lambda *a: None)
        monkeypatch.setattr(pl, "stage_game_files", lambda src, dst: 0)
        stage = os.path.join(d, "stage")
        payload = os.path.join(stage, "payload")
        os.makedirs(payload)
        pk = os.path.join(d, "hud.pack")
        write(pk, "pack")
        info = {"path": pk, "entries": 3, "scale": 4}
        pkg.write_common(payload, "macos", "v", pl, info)
        exe = os.path.join(d, "exe")
        write(exe, "exe")
        # Everything after the move (relink, sign, dmg) is the host's tools.
        monkeypatch.setattr(macos, "templates", lambda: d)
        monkeypatch.setattr(macos, "render", lambda src, vals, dst, *m: write(dst, ""))
        monkeypatch.setattr(macos, "write_manifest", lambda *a, **k: None)
        monkeypatch.setattr(macos, "_otool", lambda f: "")
        monkeypatch.setattr(host, "run", lambda *a, **k: None)
        monkeypatch.setattr(pl, "dylib_plan", lambda exe, comp: {"libs": [], "changes": []})
        icns = os.path.join(d, "a.icns")
        write(icns, "icns")
        macos.wrap_macos(stage, payload, "v", exe, d, [], pl, icns)
        res = os.path.join(stage, "dmg", pkg.app() + ".app", "Contents", "Resources")
        with open(os.path.join(res, "config.default.toml")) as f:
            text = f.read()
        assert text.startswith("[render]\nscale = 1\n"), text
        assert 'pack = "../Resources/enhance/hud.pack"' in text
        assert os.path.isfile(os.path.join(res, "enhance", "hud.pack"))
        assert not os.path.exists(os.path.join(res, "enhance.toml.default"))


def test_macos_dmg_rerun(d, monkeypatch):
    """hdiutil create refuses an existing file, so a same-version re-run
    (the wizard's Retry, or a second build into one output folder) needs
    the old .dmg removed first."""
    from xboxrecomp_cli.package import macos

    with game2_tree(d):
        host.g().m["package"] = dict(PKG2)
        monkeypatch.setattr(macos, "templates", lambda: d)
        monkeypatch.setattr(macos, "render", lambda src, vals, dst, *m: write(dst, ""))
        monkeypatch.setattr(macos, "write_manifest", lambda *a, **k: None)
        monkeypatch.setattr(macos, "_otool", lambda f: "")
        monkeypatch.setattr(pl, "dylib_plan", lambda exe, comp: {"libs": [], "changes": []})
        made = []

        def run(argv, *a, **k):
            if argv[:2] == ["hdiutil", "create"]:
                if os.path.exists(argv[-1]):
                    raise host.CliError("hdiutil: create failed - File exists")
                write(argv[-1], "dmg %d" % len(made))
                made.append(argv[-1])

        monkeypatch.setattr(host, "run", run)
        exe = os.path.join(d, "exe")
        write(exe, "exe")
        icns = os.path.join(d, "a.icns")
        write(icns, "icns")
        out = os.path.join(d, "out")
        os.makedirs(out)
        for i in range(2):
            stage = os.path.join(d, "stage%d" % i)
            payload = os.path.join(stage, "payload")
            os.makedirs(payload)
            dmg = macos.wrap_macos(stage, payload, "v", exe, out, [], pl, icns)
        assert made == [dmg, dmg]
        with open(dmg) as f:
            assert f.read() == "dmg 1"


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


def test_other_toolkit_reset(d, monkeypatch, capsys):
    """A tree configured against another toolkit (a game moved between the
    upstream and fork pins) starts afresh: the cached XBOXRECOMP_DIR linked
    fork gen/ against upstream libraries. The same toolkit by another
    spelling (a symlink) keeps its tree."""
    with game2_tree(d) as (G, tk):
        write(os.path.join(tk, "CMakeLists.txt"), "")
        other = os.path.join(d, "upstream")
        os.makedirs(other)
        link = os.path.join(d, "tk-link")
        os.symlink(tk, link)
        bdir = os.path.join(d, "build")
        cache = os.path.join(bdir, "CMakeCache.txt")
        assert not build.reset_stale_toolkit(bdir, tk)  # no tree yet
        write(cache, "XBOXRECOMP_DIR:PATH=%s\n" % link)
        assert not build.reset_stale_toolkit(bdir, tk)
        write(cache, "CMAKE_BUILD_TYPE:STRING=Release\n")
        assert not build.reset_stale_toolkit(bdir, tk)  # nothing to compare
        assert capsys.readouterr().err == ""
        # Native Windows: CMake's spelling and toolkit_dir()'s differ in case
        # and slashes for one directory.
        assert build.same_dir("d:/xbr/tk", "D:\\xbr\\tk", ntpath)
        assert not build.same_dir("d:/xbr/tk", "D:\\xbr\\upstream", ntpath)

        monkeypatch.setattr(pipeline, "refuse_stale_gen", lambda *a: None)
        monkeypatch.setattr(host, "refuse_while_regenerating", lambda: None)
        monkeypatch.setattr(build, "build_tools", lambda st: ("cmake", None))
        monkeypatch.setattr(build, "check_build_target", lambda t: None)
        monkeypatch.setattr(G, "exe", lambda b, t: G.xbe)
        runs = []
        monkeypatch.setattr(host, "run", lambda argv, **k: runs.append(argv))
        write(cache, "XBOXRECOMP_DIR:PATH=%s\n" % other)
        write(os.path.join(bdir, "CMakeFiles", "x"), "")
        build.build("macos", bdir=bdir)
        err = capsys.readouterr().err
        assert "configured with the toolkit %s, not %s; configuring afresh" % (other, tk) in err
        assert not os.path.exists(os.path.join(bdir, "CMakeFiles"))
        assert runs[0][:5] == ["cmake", "-S", G.root, "-B", bdir]
        assert "-DXBOXRECOMP_DIR=" + tk in runs[0]
        assert runs[1][:3] == ["cmake", "--build", bdir]


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


def test_manifest_enhance(d):
    """manifest.json's build.enhance names the enhancements layer only when
    the build has it: BLiNX 2 (its stock_cmake turns it on) yes, game2 (no
    such option in stock_cmake or the cache, as Burnout 3) no."""
    release = {"CMAKE_BUILD_TYPE": "Release"}
    assert pl.enhance_on(release)
    assert pl.enhance_on({**release, "XBOXRECOMP_ENHANCE": "ON"})
    assert not pl.enhance_on({**release, "XBOXRECOMP_ENHANCE": "OFF"})
    with game2_tree(d):
        host.g().m["package"] = {"app": "Game2", "product": "game2-recomp"}
        plib()
        assert not pl.enhance_on(release)
        payload = os.path.join(d, "payload")
        write(os.path.join(payload, "game2.exe"), "MZ")
        state = {"commit": "a" * 40, "branch": "main", "dirty": False, "dirty_paths": 0}
        m = pl.build_manifest(
            payload, "windows-x86_64", "v", state, state, "c" * 64, 1, release, "clang"
        )
        assert m["build"]["enhance"] is False
        assert pl.enhance_on({**release, "XBOXRECOMP_ENHANCE": "1"})


def test_rsync_escapes_args():
    from xboxrecomp_cli import bench

    assert bench.rsync_escapes_args("rsync  version 3.5.1  protocol version 32\n")
    assert bench.rsync_escapes_args("rsync  version v3.2.4  protocol version 31\n")
    assert not bench.rsync_escapes_args("rsync  version 3.2.3  protocol version 31\n")
    assert not bench.rsync_escapes_args(
        "openrsync: protocol version 29\nrsync version 2.6.9 compatible\n"
    )


def test_package_pacing_note(d, monkeypatch, capsys):
    """package() prints the frame pacing note once, after the bundle: line
    and before the PRIVATE notice, only when the README got the paragraph
    (a staged config.default.toml at "sleep"); never at "spin"."""
    from xboxrecomp_cli.package import windows

    with game2_tree(d) as (G, _):
        host.g().m["package"] = dict(PKG2)
        G.content = G.path("packaging")
        write(os.path.join(G.content, "launch.env.default.windows"), "LOG_KEEP=10\n")
        write(G.path("LICENSE"), "MIT\n")
        stage = os.path.join(d, "stage")
        payload = os.path.join(stage, "payload")
        exe = os.path.join(d, "build", G.exe_name + ".exe")
        write(exe, "exe")
        monkeypatch.setattr(pkg.setup, "host_blockers", lambda t, importing=False: [])
        monkeypatch.setattr(pkg.toolkit, "exclude_pin_mark", lambda t: None)
        monkeypatch.setattr(pkg, "package_plan", lambda a, imp=None: [])
        monkeypatch.setattr(pkg, "run_plan_prefix", lambda a, plan: None)
        monkeypatch.setattr(pkg, "check_staged", lambda root, lib: None)
        monkeypatch.setattr(pkg, "write_manifest", lambda *a, **k: None)
        monkeypatch.setattr(pkg, "stage_payload", lambda t, a, lib: (stage, payload, "v1", [], exe))
        monkeypatch.setattr(pl, "stage_game_files", lambda src, dst: 0)
        monkeypatch.setattr(pl, "tree_state", lambda root: {"commit": "abcdef0123"})
        monkeypatch.setattr(windows, "build_launcher_exe", lambda *a: None)
        monkeypatch.setattr(windows, "wrap_windows", lambda *a: os.path.join(d, "setup.exe"))

        class View:  # package()'s progress view, plain lines
            plan = []

            def say(self, msg):
                print(msg)

            def begin(self, msg):
                pass

            def end(self, ok):
                pass

        monkeypatch.setattr(host, "VIEW", View())
        write(os.path.join(G.content, "windows", "README.part"), "\nInstall\n")
        a = type(
            "A",
            (),
            {
                "target": "windows",
                "out": d,
                "hud_pack": None,
                "keep_stage": True,
                "archive": False,
                "icon_dir": d,
            },
        )()
        for pacing, note in (("sleep", True), ("spin", False)):
            write(
                os.path.join(G.content, "config.default.toml"),
                '[present]\npacing = "%s"\n' % pacing,
            )
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(payload)
            capsys.readouterr()
            pkg.package(a)
            out = capsys.readouterr().out
            readme = open(os.path.join(payload, "README.txt")).read()
            assert ("console-original" in readme) == note
            assert out.count("note: frame pacing") == (1 if note else 0), out
            assert out.index("bundle: ") < out.index("PRIVATE: ")
            if note:
                assert (
                    out.index("bundle: ") < out.index("note: frame pacing") < out.index("PRIVATE: ")
                )
                assert 'pacing = "spin" under [present]' in out
