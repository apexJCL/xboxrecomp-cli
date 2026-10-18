"""The game every test runs against unless it builds its own: testdata/game,
BLiNX 2's game.toml with its .gitignore and golden thresholds (every frame
hash in golden.json zeroed: the references are the game's output, and no
test reads a hash). Loading it here configures the modules that carry a
game's values, as the CLI does on every command."""

import os

import pytest

from xboxrecomp_cli import golden, manifest, toolkit
from xboxrecomp_cli.package import plib

GAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "game")

manifest.use(manifest.load(GAME))
plib()
golden.configure(
    os.path.join(GAME, "analysis", "golden", "golden.json"),
    os.path.join(GAME, "analysis", "golden", "frames"),
    GAME,
    manifest.current().m["golden"]["enhance_stock"],
)


@pytest.fixture
def d(tmp_path):
    """A scratch directory, as a str (the tests join paths onto it)."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def pin_stub():
    """toolkit.pin_has_enhance answers True with no git and no network:
    package and the wizard ask it of the test games' real GitHub pins. Its
    own MonkeyPatch, so a test's monkeypatch.undo() keeps it; a test of the
    real lookup calls pin_stub.undo()."""
    mp = pytest.MonkeyPatch()
    mp.setattr(toolkit, "pin_has_enhance", lambda pin: True)
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def content_memo():
    """A fresh per-run memo of the no-commit trees' digests for each test
    (a CLI run is one process; the tests edit trees between calls)."""
    from xboxrecomp_cli.package import lib

    lib.CONTENT_MEMO.clear()
    yield
    lib.CONTENT_MEMO.clear()
