"""Tests for the windows templates and the README (templates/windows/,
templates/README.txt.in, templates/README.intro): rendered with the test
game's values as `package windows` renders them, and with a second game's
(a ':' in its name, its own data folder) to show nothing of BLiNX 2 is left
in them. No build, no compiler, no game data.
"""

import os
import re
import shutil

import pytest

from xboxrecomp_cli import host, manifest, toolkit
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.host import CliError
from xboxrecomp_cli.package import lib as pl
from xboxrecomp_cli.package import plib, windows

HERE = os.path.dirname(os.path.abspath(__file__))
GAME2 = os.path.join(HERE, "testdata", "game2")
WIN = ("launcher.c", "installer.nsi.in", "app.rc.in")
PLACEHOLDER = re.compile(r"@[A-Z_]+@")


def render_launcher(dst):
    pkg.render(pkg.template("windows", "launcher.c"), windows.launcher_values(), dst)
    with open(dst) as f:
        return f.read()


def render_installer(dst):
    values = windows.installer_values(
        pl, "v1", "C:/out/setup.exe", "C:/stage", "C:/icon/app.ico", "  File a\n", "  Delete a\n"
    )
    pkg.render(pkg.template("windows", "installer.nsi.in"), values, dst)
    with open(dst) as f:
        return f.read()


@pytest.fixture
def game2(d):
    """game2 with a [package] table, the current game while the test runs."""
    root = os.path.join(d, "game2")
    os.makedirs(root)
    with open(os.path.join(GAME2, "game.toml")) as f:
        text = f.read()
    with open(os.path.join(root, "game.toml"), "w") as f:
        # A data folder one level deeper than the app's own default.
        text = text.replace("[data]\n", '[data]\nwindows = "%LOCALAPPDATA%\\\\Second\\\\Game"\n')
        f.write(text + '\n[package]\napp = "Second"\ncontent = "packaging"\n')
    saved = manifest.current()
    manifest.use(manifest.load(root))
    plib()
    try:
        yield host.g()
    finally:
        manifest.use(saved)
        plib()


def test_templates_game_agnostic():
    """The CLI's windows and README templates name no game."""
    names = [os.path.join("windows", n) for n in WIN] + ["README.txt.in", "README.intro"]
    for name in names:
        with open(os.path.join(pkg.DEFAULTS, name)) as f:
            text = f.read()
        for word in ("BLiNX", "blinx", "BLINX", "cat_recomp", "Burnout"):
            assert word not in text, (name, word)


def test_launcher_cat_values(d):
    """BLiNX 2's launcher strings, as its own launcher.c spelt them."""
    text = render_launcher(os.path.join(d, "launcher.c"))
    assert not PLACEHOLDER.findall(text)
    assert 'GetEnvironmentVariableW(L"BLINX2_DATA_DIR"' in text
    assert 'L"%ls\\\\BLiNX2", appdata' in text
    assert 'L"%ls\\\\cat_recomp.exe", g_exe_dir' in text
    assert "beside BLiNX2.exe)" in text
    assert "#ifndef APP_NAME" in text


def test_installer_cat_values(d):
    text = render_installer(os.path.join(d, "installer.nsi"))
    assert not PLACEHOLDER.findall(text)
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\BLiNX2"' in text
    assert "kept in $LOCALAPPDATA\\BLiNX2 and" in text
    assert '"$SMPROGRAMS\\BLiNX2\\BLiNX 2.lnk" "$INSTDIR\\BLiNX2.exe"' in text


def test_second_game_values(d, game2):
    text = render_launcher(os.path.join(d, "launcher.c"))
    assert not PLACEHOLDER.findall(text)
    # The nested data folder is C-escaped; the env name is derived from app.
    assert 'L"%ls\\\\Second\\\\Game", appdata' in text
    assert 'L"SECOND_DATA_DIR"' in text
    assert 'L"%ls\\\\game2.exe"' in text
    text = render_installer(os.path.join(d, "installer.nsi"))
    assert not PLACEHOLDER.findall(text)
    # ':' cannot be in a file name: the shortcut is named without it.
    assert '"$SMPROGRAMS\\Second\\Second Game - Test.lnk"' in text
    assert 'Name "Second Game: Test"' in text
    assert "kept in $LOCALAPPDATA\\Second\\Game and" in text


def test_link_name():
    assert windows.link_name("BLiNX 2") == "BLiNX 2"
    assert windows.link_name("Burnout 3: Takedown") == "Burnout 3 - Takedown"
    assert windows.link_name('A <b> "c"/d|e?*.') == "A b cde"


def test_data_dir_must_be_below_localappdata(monkeypatch):
    data = host.g().m["data"]
    assert windows.data_dir() == "BLiNX2"
    for bad in ("C:\\Games\\X", "%LOCALAPPDATA%\\", "%APPDATA%\\X"):
        monkeypatch.setitem(data, "windows", bad)
        with pytest.raises(CliError):
            windows.data_dir()


def test_c_escape():
    assert windows.c_escape('a\\b"c') == 'a\\\\b\\"c'


def test_game_override_wins(d, monkeypatch):
    G = host.g()
    tdir = os.path.join(d, "tpl")
    os.makedirs(os.path.join(tdir, "windows"))
    own = os.path.join(tdir, "windows", "launcher.c")
    open(own, "w").close()
    monkeypatch.setattr(G, "templates", tdir)
    assert pkg.template("windows", "launcher.c") == own
    assert pkg.template("windows", "app.rc.in") == os.path.join(
        pkg.DEFAULTS, "windows", "app.rc.in"
    )
    monkeypatch.setattr(G, "templates", "")
    assert pkg.template("windows", "launcher.c") == os.path.join(
        pkg.DEFAULTS, "windows", "launcher.c"
    )


def readme_text(d, monkeypatch, target="windows"):
    G = host.g()
    monkeypatch.setattr(pl, "tree_state", lambda root: {"commit": "abcdef0123"})
    monkeypatch.setattr(toolkit, "toolkit_dir", lambda: d)
    os.makedirs(os.path.join(G.content, target), exist_ok=True)
    with open(os.path.join(G.content, target, "README.part"), "w") as f:
        f.write("\nInstall @VERSION@\n")
    dst = os.path.join(d, "README.txt")
    pkg.readme(target, "v1", pl, dst)
    with open(dst) as f:
        return f.read()


def test_readme_default_intro(d, monkeypatch, game2):
    monkeypatch.setattr(game2, "content", os.path.join(d, "content"))
    monkeypatch.setattr(game2, "templates", "")
    text = readme_text(d, monkeypatch)
    assert not PLACEHOLDER.findall(text)
    assert text.startswith("Second Game: Test v1 - windows-x86_64\n")
    assert "your own copy of Second Game: Test and code" in text
    assert "by the game2-recomp project" in text
    assert "Built from: Second Game abcdef0, toolkit abcdef0.\n\nSaves and" in text
    assert "%LOCALAPPDATA%\\Second\\Game\\" in text
    assert text.endswith("\nInstall v1\n")


def test_readme_game_intro(d, monkeypatch, game2):
    content = os.path.join(d, "content")
    monkeypatch.setattr(game2, "content", content)
    monkeypatch.setattr(game2, "templates", "")
    os.makedirs(content)
    with open(os.path.join(content, "README.intro"), "w") as f:
        f.write("Our own words about @NAME@ (@SOURCES@).\n\n")
    text = readme_text(d, monkeypatch)
    assert "\n\nOur own words about Second Game: Test (Second Game abcdef0" in text
    assert "PRIVATE" not in text
    shutil.rmtree(content)
