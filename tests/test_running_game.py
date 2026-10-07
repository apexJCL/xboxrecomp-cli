"""running_game.py on synthetic process listings: BLiNX 2's match rule
(moved here with the script from its scripts/), and the same rule for
another exe name given by --exe or baked in as DEFAULT_EXE.

  uv run pytest tests/test_running_game.py
"""

import os
import subprocess
import sys

from xboxrecomp_cli import running_game as rg


def test_match_rule():
    yes = [
        ["/srv/u/Games/BLiNX2/versions/v1/cat_recomp.exe"],
        ["wine64", "Z:\\home\\u\\Games\\BLiNX2\\versions\\v1\\cat_recomp.exe"],
        ["C:\\Program Files\\x\\CAT_RECOMP.EXE"],
        ["/Applications/BLiNX2.app/Contents/MacOS/cat_recomp"],
        ["./build/cat_recomp", "--flag"],
        ["cat_recomp.exe"],
    ]
    no = [
        [],
        ["cat_recomp.exe.log"],
        ["/usr/bin/tail", "-f", "logs/cat_recomp.log"],
        ["/x/cat_recomp_test"],
        ["vim", "src/cat_recomp.c"],
        ["/x/not_cat_recomp"],
        ["grep", "cat_recomp"],
    ]
    for a in yes:
        assert rg.is_game(a, "cat_recomp"), a
    for a in no:
        assert not rg.is_game(a, "cat_recomp"), a


def test_proc_reader(d):
    proc = os.path.join(d, "proc")

    def mk(pid, ppid, argv, comm="x y"):
        p = os.path.join(proc, str(pid))
        os.makedirs(p)
        with open(os.path.join(p, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(p, "stat"), "w") as f:
            f.write("%d (%s) S %d 1 1 0" % (pid, comm, ppid))

    mk(10, 1, ["wine64", "Z:\\g\\cat_recomp.exe"], comm="a) b")
    mk(11, 10, [])
    os.makedirs(os.path.join(proc, "self"))
    got = sorted(rg.processes_linux(proc))
    assert got == [(10, 1, ["wine64", "Z:\\g\\cat_recomp.exe"]), (11, 10, [])], got


def test_parse_ps():
    text = (
        "    1     0 /sbin/launchd\n"
        "  500     1 /Applications/My Games/BLiNX2.app/Contents/MacOS/cat_recomp -x\n"
        "  501   500 /bin/zsh -l\n"
        "garbage line\n"
    )
    got = rg.parse_ps(text, "cat_recomp")
    assert got[1] == (
        500,
        1,
        ["/Applications/My Games/BLiNX2.app/Contents/MacOS/cat_recomp", "-x"],
    ), got
    assert len(got) == 3
    assert rg.is_game(got[1][2], "cat_recomp") and not rg.is_game(got[2][2], "cat_recomp")


def test_own_tree_excluded():
    # 1 init; 100 shell; 200 bench (me) -> 201 umu -> 202 game (ours);
    # 100 -> 300 foreign game started from the same shell.
    procs = [
        (1, 0, ["init"]),
        (100, 1, ["bash"]),
        (200, 100, ["bench.sh"]),
        (201, 200, ["umu-run", "/r/cat_recomp.exe"]),
        (202, 201, ["wine", "Z:\\r\\cat_recomp.exe"]),
        (300, 100, ["/i/versions/v/cat_recomp.exe"]),
    ]
    assert rg.own_tree(procs, 200) == {1, 100, 200, 201, 202}
    assert [p for p, _ in rg.find_games(procs, 200, "cat_recomp")] == [300]


def test_cycle_safe():
    procs = [(5, 6, ["a"]), (6, 5, ["b"]), (7, 5, ["cat_recomp.exe"])]
    assert rg.own_tree(procs, 5) == {5, 6, 7}


def test_other_exe():
    assert rg.is_game(["wine64", "Z:\\g\\Burnout3.EXE"], "burnout3")
    assert rg.is_game(["/i/burnout3.exe"], "burnout3.exe")
    assert not rg.is_game(["/i/cat_recomp.exe"], "burnout3")
    assert not rg.is_game(["/i/burnout3.exe"], "cat_recomp")
    # A name with regex characters matches only itself.
    assert not rg.is_game(["/i/gameXexe"], "game.")
    got = rg.parse_ps("  9 1 /Apps/A b.app/Contents/MacOS/burnout3 -x\n", "burnout3")
    assert got == [(9, 1, ["/Apps/A b.app/Contents/MacOS/burnout3", "-x"])], got


def test_exe_required(d):
    script = os.path.join(os.path.dirname(rg.__file__), "running_game.py")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert r.returncode == 2 and "--exe NAME" in r.stderr, r
    # A bundle's copy: DEFAULT_EXE baked in, no argument needed.
    baked = os.path.join(d, "running_game.py")
    with open(script) as f:
        text = f.read().replace("DEFAULT_EXE = None", 'DEFAULT_EXE = "no_such_game_xyz"', 1)
    with open(baked, "w") as f:
        f.write(text)
    r = subprocess.run([sys.executable, baked], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "", r
