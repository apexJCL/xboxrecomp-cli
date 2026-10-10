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
from test_xbe import header

from xboxrecomp_cli import (
    cli_dir,
    doctor,
    env,
    fetch,
    host,
    main,
    manifest,
    pins,
    scaffold,
    toolkit,
    wrapper,
)
from xboxrecomp_cli.host import CliError

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = "1" * 40
TK = "2" * 40

# The marks the template carries, as the toolkit's templates/new-game has
# them on 2026-10-07 (test_real_template_patches_match reads the real one).
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
int main(void) { puts("=== YOUR_GAME_NAME ==="); recomp_dispatch_init(); xbe_entry_point(); return 0; }
"""
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
    assert G.m["build"]["targets"] == ["windows"] and G.m["pipeline"]["icall_seeds"] is False
    assert G.m["xbe"]["title_id"] == 0 and "package" not in G.m
    assert wrapper.check(G) == (True, [])
    for f in (
        "my-game",
        "my-game.cmd",
        "pyproject.toml",
        ".gitignore",
        ".gitattributes",
        "README.md",
        "config/seed_functions.json",
        "CMakeLists.txt",
        "src/main.c",
        "src/recomp_manual.c",
    ):
        assert os.path.isfile(os.path.join(root, f)), f
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
        "game_files/",
        "src/recomp/gen/",
        "src/recomp/gen.key.json",
        "analysis/",
        "build-win/",
        "build/",
        "dist/",
        "external/",
        "third_party/",
        ".venv/",
        ".xbr-pin",
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
    assert "cd %s && g setup" % root in out and "  3. g all" in out
    # fopen takes / on Windows: the patched paths never need escaping.
    assert '"game_files/default.xbe"' in read(os.path.join(root, "src", "main.c"))


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
    # The order after uv: setup, the dump, the pipeline, the build.
    nxt = doctor.next_step
    assert nxt(
        ["no cmake in .venv (g setup)", "no g/game_files/default.xbe"], False, "game_files"
    ) == ("'g setup' (no cmake in .venv)")
    assert nxt(["no game_files/default.xbe"], False, "game_files").startswith("put your dump")
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
    c = read(os.path.join(root, "src", "main.c"))
    assert c.count("recomp_dispatch_init(void)") == 1
    assert "YOUR_GAME_NAME" not in c and "YOUR_GAME_NAME" not in read(
        os.path.join(root, "CMakeLists.txt")
    )
    for p in (os.path.join(root, "CMakeLists.txt"), os.path.join(root, "src", "recomp_manual.c")):
        assert os.path.getsize(p) > 0


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
    with pytest.raises(CliError, match="put your dump .* in game_files/"):
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
