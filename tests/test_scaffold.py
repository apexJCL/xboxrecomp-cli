"""`xbr new`: the starter game it writes, against a fake toolkit template
and a synthetic XBE, offline (no clone, no pins, no lock), and with the
host mocked to Windows for the wrapper and the doctor line. One test reads
the real toolkit template when a checkout is beside this one, so a template
change shows up here before a developer meets it.

  uv run pytest tests/test_scaffold.py
"""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys

import pytest
from test_xbe import XDK, fence_wait, header, image

from xboxrecomp_cli import (
    cli_dir,
    config_defaults,
    doctor,
    env,
    fetch,
    host,
    main,
    manifest,
    pins,
    pipeline,
    scaffold,
    toolkit,
    wrapper,
    xbe,
)
from xboxrecomp_cli.host import CliError

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = "1" * 40
TK = "2" * 40

# The marks the template carries, as the toolkit's templates/new-game has
# them on 2026-10-07, with the two-host template's host section
# (test_real_template_patches_match reads the real one).
FAKE_CMAKE = """\
cmake_minimum_required(VERSION 3.20)
# YOUR_GAME_NAME - Static Recompilation
project(your_game_recomp C)
add_executable(${PROJECT_NAME} WIN32 src/main.c src/recomp_manual.c)
"""
FAKE_MAIN = """\
/* YOUR_GAME_NAME - Recompiled Game Entry Point
 *   Title:       YOUR_GAME_NAME
 *   Title ID:    0x00000000
 *   Entry point: 0x00000000
 */
#define YOUR_GAME_ENTRY_POINT   0x00000000  /* XBE entry point VA */
#define YOUR_GAME_XBE_PATH      "game\\\\Your Game Title\\\\default.xbe"
#define YOUR_GAME_DIR            "game\\\\Your Game Title"
extern void xbe_entry_point(void);
#if defined(_WIN32)
#include <windows.h>
#endif
int main(void) {
    xbox_MemoryLayoutInit();
    /* xbox_Nv2aMirrorFence(0x00000000, 0x2C, 0x30); */
    puts("=== YOUR_GAME_NAME ==="); recomp_dispatch_init(); xbe_entry_point(); return 0;
}
"""
# The template before it carried the fence example line.
FAKE_MAIN_NO_FENCE = FAKE_MAIN.replace("    %s\n" % scaffold.FENCE_EXAMPLE, "")
# The template up to toolkit blinx2-v0.3.0: Win32 only.
FAKE_MAIN_WIN32_ONLY = FAKE_MAIN.replace("#if defined(_WIN32)\n", "").replace("#endif\n", "")
FAKE_MANUAL = "/* overrides */\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@pytest.fixture
def tk(d, monkeypatch):
    """A fake toolkit with the template, as XBOXRECOMP_DIR."""
    t = os.path.join(d, "tk")
    os.makedirs(os.path.join(t, "tools"))
    write(os.path.join(t, "templates", "new-game", "CMakeLists.txt"), FAKE_CMAKE)
    write(os.path.join(t, "templates", "new-game", "src", "main.c"), FAKE_MAIN)
    write(os.path.join(t, "templates", "new-game", "src", "recomp_manual.c"), FAKE_MANUAL)
    monkeypatch.setenv("XBOXRECOMP_DIR", t)
    return t


@pytest.fixture
def game(monkeypatch):
    """The current game is restored after each test (new makes the
    scaffold current)."""
    saved = manifest.current()
    yield
    manifest.use(saved)


def new(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main.main(["new", *args])
    return rc, out.getvalue(), err.getvalue()


def read(path, newline=None):
    with open(path, encoding="utf-8", newline=newline) as f:
        return f.read()


def test_offline_scaffold_loads_and_names_what_is_left(d, tk, game):
    root = os.path.join(d, "my-game")
    rc, out, err = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0, err
    G = manifest.load(root)
    assert (G.slug, G.name, G.exe_name) == ("my-game", "my-game", "my_game_recomp")
    assert G.m["cli"]["commit"] == CLI and G.m["toolkit"]["commit"] == TK
    assert G.m["build"]["targets"] == ["windows", "macos"]
    assert G.m["pipeline"]["icall_seeds"] is False
    assert G.m["xbe"]["title_id"] == 0 and "package" not in G.m
    assert wrapper.check(G) == (True, [])
    # Every code section is swept: .text alone stubs the XDK libraries.
    assert G.m["pipeline"]["disasm"] == {"text_only": False, "extra_sections": []}
    manifest.use(G)
    (dis,) = [c for c in pipeline.disasm_cmds() if c[1] == "disasm"]
    assert "--text-only" not in dis[2] and "--extra-sections" not in dis[2]
    for f in (
        "my-game",
        "my-game.cmd",
        "pyproject.toml",
        ".gitignore",
        ".gitattributes",
        "README.md",
        "CHANGELOG.md",
        "config/seed_functions.json",
        "CMakeLists.txt",
        "src/main.c",
        "src/recomp_manual.c",
        ".github/workflows/release-wizard.yml",
    ):
        assert os.path.isfile(os.path.join(root, f)), f
    wf = read(os.path.join(root, ".github", "workflows", "release-wizard.yml"))
    assert "@SLUG@" not in wf and "./my-game wizard build" in wf
    assert "# [wizard]" in read(os.path.join(root, "game.toml"))
    assert os.path.isdir(G.game_files) and os.path.isdir(G.gen)
    assert os.stat(os.path.join(root, "my-game")).st_mode & stat.S_IXUSR
    # The template, patched where it marks the edits, plus the declaration
    # Clang needs.
    cmake = read(os.path.join(root, "CMakeLists.txt"))
    assert "project(my_game_recomp C)" in cmake and "# my-game - Static Recompilation" in cmake
    c = read(os.path.join(root, "src", "main.c"))
    assert '#define YOUR_GAME_XBE_PATH      "game_files/default.xbe"' in c
    assert '#define YOUR_GAME_DIR            "game_files"' in c
    assert "#define YOUR_GAME_ENTRY_POINT   0x00000000" in c  # no XBE: left at 0
    assert "extern int recomp_dispatch_init(void);" in c
    assert "YOUR_GAME_NAME" not in c and c.count("my-game") == 3  # every occurrence
    assert read(os.path.join(root, "src", "recomp_manual.c")) == FAKE_MANUAL
    # The option defaults are the toolkit's, under the game's name, and doctor
    # says so.
    defaults = read(os.path.join(root, "packaging", "config.default.toml"))
    assert defaults == config_defaults.template_text("my-game")
    assert defaults.startswith("# my-game options:") and 'pacing = "sleep"' in defaults
    # Offline: the toolkit beside is used, the pins and the lock are named.
    assert "setup' clones" not in out
    assert "pins refresh' writes config/setup-pins.json and uv.lock" in out
    assert "./my-game setup" in out and "./my-game all" in out and "title_id is 0" in out
    assert "(the template changed)" not in out


def test_cli_tag_pins(d, tk, game):
    root = os.path.join(d, "g1")
    args = ("--offline", "--cli-tag", "v0.2.0", "--toolkit-commit", TK)
    rc, out, err = new(root, *args, "--cli-commit", CLI)
    assert rc == 0, err
    G = manifest.load(root)
    assert (G.m["cli"]["tag"], G.m["cli"]["commit"]) == ("v0.2.0", CLI)
    assert "cli v0.2.0 (%s)" % CLI[:12] in out
    assert wrapper.check(G) == (True, [])
    # A tag alone, offline: no commit locks it, and it still loads.
    root = os.path.join(d, "g2")
    rc, out, err = new(root, *args)
    assert rc == 0, err
    G = manifest.load(root)
    assert (G.m["cli"]["tag"], G.m["cli"]["commit"]) == ("v0.2.0", "")
    assert 'tag = "v0.2.0"\nurl = ' in read(os.path.join(root, "game.toml"))


def test_the_xbe_fills_the_constants_and_is_not_copied(d, tk, game):
    root = os.path.join(d, "g")
    dump = os.path.join(d, "dump", "default.xbe")
    write(dump, "")
    with open(dump, "wb") as f:
        f.write(header(title='Title: "The" Game', title_id=0x4B4C0001, entry=0x00123456))
    rc, out, _ = new(root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    G = manifest.load(root)
    assert G.name == "Title:  The  Game" and G.m["xbe"]["title_id"] == 0x4B4C0001
    assert G.m["pipeline"]["game_name"] == G.name
    c = read(os.path.join(root, "src", "main.c"))
    assert "#define YOUR_GAME_ENTRY_POINT   0x00123456" in c
    assert " *   Title ID:    0x4B4C0001" in c and " *   Entry point: 0x00123456" in c
    assert os.listdir(G.game_files) == []  # never copied
    assert "entry point 0x00123456 (retail)" in out
    # The dump already in game_files/ is found without --xbe.
    root2 = os.path.join(d, "g2")
    os.makedirs(os.path.join(root2, "game_files"))
    with open(os.path.join(root2, "game_files", "default.xbe"), "wb") as f:
        f.write(header(title="Second", title_id=7))
    rc, out, err = new(root2, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0, err
    assert manifest.load(root2).m["xbe"]["title_id"] == 7


def test_the_xbe_is_read_from_a_disc_image_or_folder(d, tk, game):
    """A porter holding only the .iso starts the game from it: without the
    title ID, import would refuse that same image afterwards."""
    from test_dump import build_image, write_folder

    xbe_bytes = header(title="Imaged", title_id=0x4B4C0002, entry=0x00012340)
    tree = [("default.xbe", xbe_bytes), ("media", [("a.bik", b"BIK1")])]
    iso = build_image(os.path.join(d, "Game.xiso"), tree, base=0x18300000)
    rc, out, err = new(
        os.path.join(d, "g"), "--offline", "--xbe", iso, "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    G = manifest.load(os.path.join(d, "g"))
    assert G.name == "Imaged" and G.m["xbe"]["title_id"] == 0x4B4C0002
    assert "entry point 0x00012340 (retail)" in out
    assert os.listdir(G.game_files) == []  # read in place, never extracted
    folder = write_folder(os.path.join(d, "dump"), tree)
    rc, out, err = new(
        os.path.join(d, "g2"),
        "--offline",
        "--xbe",
        folder,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert manifest.load(os.path.join(d, "g2")).m["xbe"]["title_id"] == 0x4B4C0002
    # A --xbe that cannot be read stops new before anything is written: a
    # title ID of 0 would only move the refusal to import.
    junk = os.path.join(d, "notes.txt")
    write(junk, "hello")
    empty = os.path.join(d, "empty.iso")
    write(empty, "")
    no_xbe_iso = build_image(os.path.join(d, "noxbe.iso"), [("readme.txt", b"hi")])
    no_xbe_dir = write_folder(os.path.join(d, "noxbe"), [("readme.txt", b"hi")])
    for bad, words in (
        (junk, "not an Xbox disc image"),
        (empty, "not an Xbox disc image"),
        (no_xbe_iso, "no default.xbe on this disc image"),
        (no_xbe_dir, "no default.xbe in"),
        (os.path.join(d, "gmae.iso"), "no such file or folder"),
    ):
        root = os.path.join(d, "bad")
        rc, out, err = new(
            root, "--offline", "--xbe", bad, "--cli-commit", CLI, "--toolkit-commit", TK
        )
        assert rc == 1 and "--xbe " in err and words in err, (bad, err)
        assert not os.path.exists(os.path.join(root, "game.toml")), bad
    # No --xbe and no dump in game_files/: a note, not an error.
    rc, out, err = new(
        os.path.join(d, "g3"), "--offline", "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    assert "import refuses every dump until then" in out
    assert manifest.load(os.path.join(d, "g3")).m["xbe"]["title_id"] == 0


def test_a_changed_template_is_reported_not_fatal(d, tk, game):
    main_c = os.path.join(tk, "templates", "new-game", "src", "main.c")
    write(main_c, FAKE_MAIN.replace("0x00000000  /* XBE", "0x00000001  /* XBE"))
    root = os.path.join(d, "g")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    assert "src/main.c: set YOUR_GAME_ENTRY_POINT to 0x00000000" in out
    assert "(the template changed)" in out
    assert os.path.isfile(os.path.join(root, "src", "main.c"))
    # A template that already declares recomp_dispatch_init is left alone.
    write(
        main_c,
        FAKE_MAIN.replace(
            "extern void xbe", "extern int recomp_dispatch_init(void);\nextern void xbe"
        ),
    )
    rc, out, _ = new(
        os.path.join(d, "g2"), "--offline", "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert read(os.path.join(d, "g2", "src", "main.c")).count("recomp_dispatch_init(void)") == 1


def test_refusals(d, tk, game):
    root = os.path.join(d, "full")
    write(os.path.join(root, "x"), "")
    rc, _, err = new(root, "--offline")
    assert rc == 1 and "not empty" in err and not os.path.exists(os.path.join(root, "game.toml"))
    rc, _, err = new(os.path.join(d, "g"), "--offline", "--slug", "My Game")
    assert rc == 1 and "slug" in err and not os.path.exists(os.path.join(d, "g"))
    write(os.path.join(d, "file"), "")
    rc, _, err = new(os.path.join(d, "file"), "--offline")
    assert rc == 1 and "not a directory" in err


def test_no_toolkit_names_the_copy(d, game, monkeypatch):
    monkeypatch.setenv("XBOXRECOMP_DIR", os.path.join(d, "none"))
    root = os.path.join(d, "g")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    assert "./g setup' clones the toolkit (offline now)" in out
    assert "copy external/xboxrecomp/templates/new-game/" in out
    assert not os.path.exists(os.path.join(root, "src", "main.c"))


def test_gitignore_and_attributes(d, tk, game):
    root = os.path.join(d, "g")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0
    ign = read(os.path.join(root, ".gitignore")).split("\n")
    for want in (
        "/game_files",
        "/game_files.partial/",
        "/game_files.old/",
        "/src/recomp/gen/",
        "/src/recomp/gen.key.json",
        "/analysis/",
        "/build-win/",
        "/build/",
        "/build-*/",
        "/dist/",
        "/external",
        "/third_party",
        "/.venv*",
        "/.xbr-pin",
        "bench-logs",
        "/.xbr/",
        "*.xbe",
        "*.iso",
        "*.png",
        "*.wav",
        "*.sav",
        "*.exe",
    ):
        assert want in ign, want
    attrs = read(os.path.join(root, ".gitattributes"))
    assert "*.cmd     text eol=crlf" in attrs and "g    text eol=lf" in attrs
    assert "*.py      text eol=lf" in attrs and "*.toml    text eol=lf" in attrs
    # The game's pyproject is the tools environment, nothing installed.
    py = read(os.path.join(root, "pyproject.toml"))
    assert "capstone==5.0.9" in py and "package = false" in py and "no-build = true" in py


def test_windows_wrapper_and_messages(d, tk, game, monkeypatch):
    monkeypatch.setattr(host, "host_os", lambda system=None: "windows")
    root = os.path.join(d, "g")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    cmd = read(os.path.join(root, "g.cmd"), newline="")
    assert cmd.count("\r\n") == cmd.count("\n") > 0, "cmd.exe wants CRLF"
    lines = cmd.replace("\r\n", "\n").split("\n")
    assert lines[0] == "@echo off"
    assert '    py -3 "%~dp0g.py" %*' in lines and '    python "%~dp0g.py" %*' in lines
    assert "where py >nul 2>nul" in lines and lines[-2] == "exit /b %errorlevel%"
    # Every other file is LF.
    for f in ("g", "g.py", "game.toml", "pyproject.toml", "src/main.c"):
        assert "\r" not in read(os.path.join(root, f), newline=""), f
    assert "cd %s && g import PATH with your disc image or folder" % root in out
    assert "  2. g setup" in out and "  3. g all        -> build-win/g_recomp.exe" in out
    # fopen takes / on Windows: the patched paths never need escaping.
    assert '"game_files/default.xbe"' in read(os.path.join(root, "src", "main.c"))


def test_next_build_is_this_hosts_own(d, tk, game, monkeypatch):
    """Step 3 names the build `all` makes here (build.default_build_target):
    build/<exe> on a Mac, build-win/<exe>.exe elsewhere."""
    for os_name, want in (
        ("macos", "./g all        -> build/g_recomp\n"),
        ("linux", "./g all        -> build-win/g_recomp.exe\n"),
    ):
        monkeypatch.setattr(host, "host_os", lambda system=None, o=os_name: o)
        root = os.path.join(d, os_name, "g")
        rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
        assert rc == 0 and want in out, out
    readme = read(os.path.join(root, "README.md"))
    assert "`build-win/g_recomp.exe`" in readme and "`build/g_recomp`" in readme


def test_a_win32_only_template_is_noted_not_fatal(d, tk, game):
    """An old toolkit beside the game (blinx2-v0.3.0 and older) has the
    Win32-only template: both targets are still written, and the summary
    says what `build macos` needs."""
    note = "has the Win32-only template: `build macos` will not compile"
    root = os.path.join(d, "two")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0 and note not in out
    write(os.path.join(tk, "templates", "new-game", "src", "main.c"), FAKE_MAIN_WIN32_ONLY)
    root = os.path.join(d, "old")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0 and note in out, out
    assert "the toolkit at %s %s" % (TK[:12], note) in out
    assert "drop macos from build.targets" in out
    assert manifest.load(root).m["build"]["targets"] == ["windows", "macos"]


def test_a_win32_only_template_on_a_mac_points_at_windows(d, tk, game, monkeypatch):
    """On a Mac, `all` builds macos, which the Win32-only template cannot:
    step 3 names the windows build instead."""
    monkeypatch.setattr(host, "host_os", lambda system=None: "macos")
    write(os.path.join(tk, "templates", "new-game", "src", "main.c"), FAKE_MAIN_WIN32_ONLY)
    root = os.path.join(d, "old")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0, out
    assert "./old build windows -> build-win/old_recomp.exe\n" in out, out
    assert "./old all" not in out


def test_doctor_next_line(d, tk, game, monkeypatch):
    root = os.path.join(d, "g")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0
    # A fresh scaffold on a Windows host without uv: the first fix is uv,
    # with the Windows hint.
    monkeypatch.setattr(host, "host_os", lambda system=None: "windows")
    monkeypatch.setattr(env, "find_uv", lambda: (_ for _ in ()).throw(CliError("no uv")))
    monkeypatch.setattr(doctor, "windows_long_paths_enabled", lambda: None)
    monkeypatch.setattr(cli_dir, "doctor_line", lambda: "cli:        test")
    lines, targets, blocked = doctor.doctor_report()
    assert lines[-1].startswith("next:       install uv: winget install --id=astral-sh.uv")
    assert targets == [] and blocked == {}  # no [package]: nothing to package
    assert any("game.toml has no [package]" in x for x in lines)
    assert "defaults:   config.default.toml: the toolkit's defaults" in lines
    # The order after uv: setup, the dump, the pipeline, the build.
    nxt = doctor.next_step
    assert nxt(
        ["no cmake in .venv (g setup)", "no g/game_files/default.xbe"], False, "game_files"
    ) == ("'g setup' (no cmake in .venv)")
    assert nxt(["no game_files/default.xbe"], False, "game_files").startswith("run '")
    assert nxt([], False, "game_files") == "'g analyze', then 'g recomp' ('g all' builds too)"
    assert nxt([], True, "game_files") == "'g build', or 'g package <target>'"


def test_missing_pins_is_a_message(d, tk, game):
    root = os.path.join(d, "g")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0
    with pytest.raises(CliError, match="run 'g pins refresh'"):
        fetch.load_pins()


def test_pin_resolution(monkeypatch):
    # The installed distribution's record, as uv writes it for git+URL.
    import importlib.metadata as md

    class Dist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return '{"url": "https://x", "vcs_info": {"vcs": "git", "commit_id": "%s"}}' % (
                "a" * 40
            )

    monkeypatch.setattr(md, "distribution", lambda name: Dist())
    assert scaffold.installed_commit() == "a" * 40
    monkeypatch.setattr(cli_dir, "tree_state", lambda: ("", 0))
    assert scaffold.resolve_cli_commit() == ("a" * 40, "the installed distribution")
    monkeypatch.setattr(scaffold, "installed_commit", lambda: "")
    assert scaffold.resolve_cli_commit(offline=True) == (
        scaffold.ZERO,
        "unresolved: fill in [cli] commit",
    )
    monkeypatch.setattr(cli_dir, "tree_state", lambda: ("e" * 40, 2))
    assert scaffold.resolve_cli_commit() == ("e" * 40, "this checkout (uncommitted changes)")
    assert scaffold.resolve_cli_commit("f" * 40) == ("f" * 40, "--cli-commit")


def test_help_lists_new(game):
    ap, _ = main.make_parser("blinx2")
    text = ap.format_help()
    assert "blinx2 new DIR" in text.split("Developer commands:")[1]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as e:
        main.main(["new", "--help"])
    assert e.value.code == 0 and "--offline" in out.getvalue()


def test_slug_from():
    assert scaffold.slug_from("/x/My Game 2!") == "my-game-2"
    assert scaffold.slug_from("/x/---") == "game"


REAL_TOOLKIT = next(
    (
        p
        for p in (
            os.environ.get("XBOXRECOMP_DIR", ""),
            os.path.join(HERE, "..", "..", "xboxrecomp"),
        )
        if p and os.path.isfile(os.path.join(p, "templates", "new-game", "src", "main.c"))
    ),
    "",
)


@pytest.mark.skipif(not REAL_TOOLKIT, reason="no toolkit checkout beside this one")
def test_real_template_patches_match(d, game, monkeypatch):
    """Every patch finds its line in the toolkit's own template: a template
    change shows up here, not in a developer's first `new`."""
    monkeypatch.setenv("XBOXRECOMP_DIR", os.path.abspath(REAL_TOOLKIT))
    root = os.path.join(d, "g")
    rc, out, _ = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    assert "(the template changed)" not in out, out
    assert "D3D fence: not found (no XBE read)" in out
    c = read(os.path.join(root, "src", "main.c"))
    assert c.count("recomp_dispatch_init(void)") == 1
    assert "YOUR_GAME_NAME" not in c and "YOUR_GAME_NAME" not in read(
        os.path.join(root, "CMakeLists.txt")
    )
    # The two-host template: host sections, the POSIX window, and
    # <windows.h> only inside a _WIN32 section.
    assert "Win32-only template" not in out, out
    assert "#if defined(_WIN32)" in c and "xbox_HostWindowMain" in c
    # The window's title, in both entry points, once the template names it.
    template = read(os.path.join(REAL_TOOLKIT, "templates", "new-game", "src", "main.c"))
    if "xbox_HostWindowSetTitle(" in template:
        assert template.count('xbox_HostWindowSetTitle("YOUR_GAME_NAME");') == 2
        assert c.count('xbox_HostWindowSetTitle("g");') == 2
    depth, win32 = [], []
    for line in c.splitlines():
        s = line.strip()
        if s.startswith("#if"):
            depth.append("_WIN32" in s and "!" not in s)
        elif s.startswith("#elif"):
            depth[-1] = "_WIN32" in s and "!" not in s
        elif s.startswith("#else"):
            depth[-1] = False
        elif s.startswith("#endif"):
            depth.pop()
        elif s == "#include <windows.h>":
            win32.append(any(depth))
    assert win32 and all(win32), "#include <windows.h> outside a _WIN32 section"
    for p in (os.path.join(root, "CMakeLists.txt"), os.path.join(root, "src", "recomp_manual.c")):
        assert os.path.getsize(p) > 0
    # A dump whose fence was found: the live line where the template has the
    # example, the hand-edit hint (never a failure) where it predates it.
    dump = os.path.join(d, "fence.xbe")
    with open(dump, "wb") as f:
        f.write(image([("D3D", 0x6, fence_wait(0x00235528))], XDK))
    root = os.path.join(d, "g2")
    rc, out, err = new(
        root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    c = read(os.path.join(root, "src", "main.c"))
    template = read(os.path.join(REAL_TOOLKIT, "templates", "new-game", "src", "main.c"))
    # A reworded example would fall to the old-template branch below and
    # pass: any example in the template must be the exact text new patches.
    if "xbox_Nv2aMirrorFence(0x00000000" in template:
        assert scaffold.FENCE_EXAMPLE in template
    if scaffold.FENCE_EXAMPLE in template:
        assert "(the template changed)" not in out, out
        assert "    xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30);" in c
        assert "main.c carries the mirror line" in out
    else:
        assert "xbox_Nv2aMirrorFence" not in template
        assert "add `xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30)` after" in out, out
        assert "xbox_Nv2aMirrorFence" not in c


def test_bootstrap_runs_in_the_scaffold(d, tk, game):
    """The copied <slug>.py prints the help with the system Python, from the
    scaffold's game.toml (its [game] and [build] lines parse by line)."""
    root = os.path.join(d, "g")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0
    env_ = dict(
        os.environ, PATH=os.path.dirname(sys.executable), XBOXRECOMP_CLI_DIR=os.path.dirname(HERE)
    )
    env_.pop("VIRTUAL_ENV", None)
    r = subprocess.run(
        [sys.executable, os.path.join(root, "g.py"), "--help"],
        capture_output=True,
        text=True,
        env=env_,
    )
    assert "g: set up, generate, build and package g on this host." in r.stdout + r.stderr


def test_missing_dump_is_said_plainly(d, tk, game):
    from xboxrecomp_cli import pipeline

    root = os.path.join(d, "g")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0
    with pytest.raises(
        CliError,
        match="no game_files/default.xbe: run '.*g import PATH' with your disc image or "
        "folder, or put the files in game_files/",
    ):
        pipeline.require_dump()


def test_title_is_made_a_plain_toml_string(d, tk, game):
    """A certificate title with a backslash, a quote or a control character
    would be an invalid basic string for the bootstrap's line parser: it is
    cleaned, and the manifest is validated before anything is written."""
    for title, want in (("a\\b", "a b"), ('Say "Hi"\x01', "Say  Hi"), ("\\\\", "g")):
        root = os.path.join(d, "g")
        dump = os.path.join(d, "default.xbe")
        with open(dump, "wb") as f:
            f.write(header(title=title))
        rc, _, err = new(
            root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
        )
        assert rc == 0, err
        assert manifest.load(root).name == want, title
        shutil.rmtree(root)
    assert scaffold.clean_name("", "slug") == "slug"


def test_the_title_reaches_the_window(d, tk, game):
    """The template names its window with xbox_HostWindowSetTitle
    ("YOUR_GAME_NAME") in both entry points (toolkit fix/fb-window-guard);
    the every-occurrence YOUR_GAME_NAME patch hands it the game's name.
    clean_name keeps quotes and backslashes out of the name, so it is a
    valid C string too."""
    main_c = os.path.join(tk, "templates", "new-game", "src", "main.c")
    titled = FAKE_MAIN.replace(
        "int main(void) {\n",
        'int main(void) {\n    xbox_HostWindowSetTitle("YOUR_GAME_NAME");\n',
    )
    write(main_c, titled)
    dump = os.path.join(d, "default.xbe")
    with open(dump, "wb") as f:
        f.write(header(title='Say "Hi"'))
    root = os.path.join(d, "g")
    rc, out, err = new(
        root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    assert "(the template changed)" not in out, out
    c = read(os.path.join(root, "src", "main.c"))
    assert '    xbox_HostWindowSetTitle("Say  Hi");\n' in c
    assert "YOUR_GAME_NAME" not in c


def test_ds_store_does_not_make_the_dir_full(d, tk, game):
    root = os.path.join(d, "g")
    os.makedirs(os.path.join(root, "game_files"))
    write(os.path.join(root, ".DS_Store"), "")
    assert new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)[0] == 0


def test_tree_state_ignores_an_enclosing_repository(monkeypatch):
    """Under uvx the package runs from uv's cache; a $HOME that is a git
    repository must not lend its HEAD to the CLI (the scaffold would pin a
    commit the CLI's remote never had)."""
    d = cli_dir.cli_dir()
    monkeypatch.setattr(host, "git_head", lambda path: "b" * 40)
    monkeypatch.setattr(cli_dir, "toplevel", lambda path: os.path.dirname(os.path.dirname(path)))
    assert cli_dir.tree_state() == ("", 0)
    assert cli_dir.doctor_line().endswith("(not a git checkout)")
    monkeypatch.setattr(cli_dir, "toplevel", lambda path: path)
    assert cli_dir.tree_state()[0] == "b" * 40
    assert cli_dir.is_checkout(d) == (cli_dir.toplevel(d) == d)


def test_online_failures_become_later_steps(d, tk, game, monkeypatch):
    monkeypatch.setenv("XBOXRECOMP_DIR", os.path.join(d, "none"))
    monkeypatch.setattr(
        toolkit, "clone_toolkit", lambda: (_ for _ in ()).throw(CliError("no network"))
    )
    monkeypatch.setattr(pins, "pins_refresh", lambda: (_ for _ in ()).throw(CliError("no uv")))
    rc, out, _ = new(os.path.join(d, "g"), "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0
    assert "./g setup' clones the toolkit (no network)" in out
    assert "./g pins refresh' writes config/setup-pins.json and uv.lock (no uv)" in out


def test_toolkit_checkout_on_another_branch_is_said(d, monkeypatch):
    t = os.path.join(d, "xboxrecomp")
    os.makedirs(os.path.join(t, "tools"))
    g = ["git", "-C", t, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(g + ["init", "-q", "-b", "other"], check=True)
    subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    monkeypatch.setenv("XBOXRECOMP_DIR", t)
    sha, how = scaffold.resolve_toolkit_commit(d, "u", "wanted", offline=True)
    assert len(sha) == 40 and how == t + " (not on wanted)"
    assert scaffold.resolve_toolkit_commit(d, "u", "other", offline=True)[1] == t


def fence_dump(path, sections, libraries=XDK):
    write(path, "")
    with open(path, "wb") as f:
        f.write(image(sections, libraries, title="Fenced", title_id=0x4B4C0003))
    return path


def test_new_fills_the_fence_line_from_the_dump(d, tk, game):
    """D3D's fence wait in the dump's D3D section: main.c gets the live
    mirror line in place of the example, and the summary says where it came
    from and which libraries the title links."""
    code = b"\xcc" * 64 + fence_wait(0x00235528) + b"\xcc" * 64
    dump = fence_dump(
        os.path.join(d, "dump", "default.xbe"),
        [(".text", 0x6, b"\x90" * 64), ("D3D", 0x6, code)],
    )
    root = os.path.join(d, "g")
    rc, out, err = new(
        root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    c = read(os.path.join(root, "src", "main.c"))
    assert scaffold.FENCE_EXAMPLE not in c
    with open(dump, "rb") as f:
        raw = f.read()
    wait = xbe.sections(raw)[1].va + 64
    assert (
        "    xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30);   "
        "/* from D3D's fence wait at 0x%08X */\n" % wait
    ) in c
    assert (
        "D3D fence: device pointer 0x00235528 (+0x2C submitted, +0x30 completed), from the "
        "fence wait at 0x%08X in D3D8 1.0.5849; main.c carries the mirror line" % wait
    ) in out
    assert "XDK libraries: XAPILIB 1.0.5849, D3D8 1.0.5849, DSOUND 1.0.5849" in out
    assert "(the template changed)" not in out
    # The same dump on a disc image: the section is read in place.
    from test_dump import build_image

    iso = build_image(os.path.join(d, "F.xiso"), [("default.xbe", raw)])
    rc, out, err = new(
        os.path.join(d, "g2"),
        "--offline",
        "--xbe",
        iso,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert "device pointer 0x00235528" in out
    assert "xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30);" in read(
        os.path.join(d, "g2", "src", "main.c")
    )


def test_new_without_a_fence_keeps_the_example(d, tk, game):
    """Zero or several matches, or no D3D section: the example line stays and
    the note says why; a title with no D3D section is scanned in its
    executable sections."""
    cases = (
        ([("D3D", 0x6, b"\xcc" * 64)], "no match in D3D"),
        ([("D3D", 0x6, fence_wait(0x1000) + fence_wait(0x2000))], "2 matches in D3D"),
        ([(".text", 0x6, b"\x90" * 64)], "no D3D section"),
        (
            [("D3D", 0x6, b"\xcc" * 22 + fence_wait(0x00235528))],
            "D3D section truncated (10 of 41 bytes)",
        ),
        # Not executable: never scanned.
        ([(".text", 0x6, b"\x90" * 8), (".data", 0x1, fence_wait(0x3000))], "no D3D section"),
    )
    for i, (sections, why) in enumerate(cases):
        dump = fence_dump(os.path.join(d, "dump%d" % i, "default.xbe"), sections)
        if why.startswith("D3D section truncated"):
            # The dump ends inside the D3D section, fence wait and all.
            with open(dump, "rb") as f:
                raw = f.read()
            with open(dump, "wb") as f:
                f.write(raw[: -(len(raw) - xbe.sections(raw)[0].raw_off - 10)])
        root = os.path.join(d, "g%d" % i)
        rc, out, err = new(
            root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
        )
        assert rc == 0, err
        assert scaffold.FENCE_EXAMPLE in read(os.path.join(root, "src", "main.c"))
        assert (
            'D3D fence: not found (%s); main.c keeps the commented example, see docs/porting.md "The D3D fence"'
            % why
        ) in out, out
        assert "(the template changed)" not in out
    # One match in an executable section of a title with no D3D section.
    dump = fence_dump(os.path.join(d, "dumpx", "default.xbe"), [(".text", 0x6, fence_wait(0x4000))])
    rc, out, err = new(
        os.path.join(d, "gx"),
        "--offline",
        "--xbe",
        dump,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert "device pointer 0x00004000" in out and "in D3D8 1.0.5849" in out
    # No dump at all.
    rc, out, err = new(
        os.path.join(d, "gn"), "--offline", "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    assert "D3D fence: not found (no XBE read)" in out
    assert "XDK libraries" not in out


def test_a_found_fence_on_an_older_template_is_a_hint(d, tk, game):
    write(os.path.join(tk, "templates", "new-game", "src", "main.c"), FAKE_MAIN_NO_FENCE)
    dump = fence_dump(
        os.path.join(d, "dump", "default.xbe"), [("D3D", 0x6, fence_wait(0x00235528))]
    )
    root = os.path.join(d, "g")
    rc, out, err = new(
        root, "--offline", "--xbe", dump, "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    assert "the template has no example line to replace" in out
    assert (
        "src/main.c: add `xbox_Nv2aMirrorFence(0x00235528, 0x2C, 0x30)` after "
        "xbox_MemoryLayoutInit yourself (the template changed)"
    ) in out
    # Not found on the older template: nothing to say about the template.
    dump = fence_dump(os.path.join(d, "dump2", "default.xbe"), [("D3D", 0x6, b"\xcc" * 8)])
    rc, out, err = new(
        os.path.join(d, "g2"),
        "--offline",
        "--xbe",
        dump,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert "(the template changed)" not in out


def test_fence_past_64k_and_in_a_dump_folder(d, tk, game):
    """A header larger than the 64 KiB read first is read again whole, and
    a dump folder is read at the section's offset, not only the header."""
    from test_dump import write_folder

    big = image([("D3D", 0x6, b"\xcc" * 32 + fence_wait(0x00235528))], XDK, tables_at=0x10000)
    assert xbe.header_size(big) > 0x10000 and xbe.sections(big)[0].raw_off > 0x10000
    dump = os.path.join(d, "big", "default.xbe")
    write(dump, "")
    with open(dump, "wb") as f:
        f.write(big)
    rc, out, err = new(
        os.path.join(d, "g"),
        "--offline",
        "--xbe",
        dump,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert "device pointer 0x00235528" in out and "XDK libraries: XAPILIB 1.0.5849" in out
    raw = image([(".text", 0x6, b"\x90" * 0x10000), ("D3D", 0x6, fence_wait(0x002F1FB8))], XDK)
    assert xbe.sections(raw)[1].raw_off > 0x10000
    folder = write_folder(os.path.join(d, "dumpdir"), [("default.xbe", raw)])
    rc, out, err = new(
        os.path.join(d, "g2"),
        "--offline",
        "--xbe",
        folder,
        "--cli-commit",
        CLI,
        "--toolkit-commit",
        TK,
    )
    assert rc == 0, err
    assert "device pointer 0x002F1FB8" in out
    assert "xbox_Nv2aMirrorFence(0x002F1FB8, 0x2C, 0x30);" in read(
        os.path.join(d, "g2", "src", "main.c")
    )


def test_license_and_openspec_are_choices(d, tk, game):
    root = os.path.join(d, "g")
    assert (
        new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK, "--no-openspec")[0] == 0
    )
    assert not os.path.exists(os.path.join(root, "LICENSE"))
    assert not os.path.exists(os.path.join(root, "openspec"))
    root2 = os.path.join(d, "g2")
    assert (
        new(root2, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK, "--license", "mit")[0]
        == 0
    )
    lic = read(os.path.join(root2, "LICENSE"))
    assert lic.startswith("MIT License") and "g2 static recompilation contributors" in lic
    spec = read(os.path.join(root2, "openspec", "config.yaml"))
    assert spec.startswith("schema: spec-driven") and "@" not in spec
    ign = read(os.path.join(root2, ".gitignore")).split("\n")
    for want in ("/wt/", "_local/", "runs/", "*.bak", "*.bak-*", "*.pack", "CACHE/"):
        assert want in ign, want
