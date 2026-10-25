"""bench gc: replaced by benchdb plan and clean; for one release round it
prints the matching benchdb plan call and exits 2, deleting nothing.

  uv run pytest tests/test_bench_gc.py
"""

import os
import shutil

from xboxrecomp_cli import manifest
from xboxrecomp_cli.bench import gc
from xboxrecomp_cli.bench.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")


class FakeBench:
    def __init__(self, root, env):
        self.cfg = Config(manifest.load(root), env)


def test_gc_prints_the_plan_call_and_exits_2(tmp_path, capsys):
    root = tmp_path / "game"
    root.mkdir()
    shutil.copy(os.path.join(GAME, "game.toml"), str(root / "game.toml"))
    (tmp_path / "xboxrecomp").mkdir()
    b = FakeBench(str(root), {"BENCH_HOST": "me@hostA"})
    assert gc.cmd_gc(b, []) == 2
    err = capsys.readouterr().err
    assert "  benchdb plan\n" in err and "benchdb clean PLAN_ID" in err
    assert gc.cmd_gc(b, ["--host", "--apply", "--days", "1.5"]) == 2
    err = capsys.readouterr().err
    assert "benchdb plan --older-than 1.5 hostA:%s/bench-logs" % b.cfg.remote_game in err
