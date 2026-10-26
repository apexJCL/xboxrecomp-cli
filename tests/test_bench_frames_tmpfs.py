"""run_game.sh's D3D11 dumps: a golden run's go to a tmpfs first and land
in the run's frames/ when the script ends (on a slow disk, writing them in
present stalled the game's flips), and a later run finishes a move an
earlier one could not."""

import os
import re
import subprocess

from xboxrecomp_cli.bench import golden as bg

HOST = os.path.join(os.path.dirname(os.path.abspath(bg.__file__)), "host")


def blocks():
    """The frames functions, trap and sweep, and the FRAMES=1 block, to run
    on their own."""
    with open(os.path.join(HOST, "run_game.sh")) as f:
        text = f.read()
    head = re.search(r"^FRAMES_SHM=.*?^done\n", text, re.S | re.M)
    block = re.search(r'^if \[ "\$FRAMES" = 1 \]; then\n.*?^fi\n', text, re.S | re.M)
    assert head and block, "no frames blocks in run_game.sh"
    return head.group(0), block.group(0)


def run(tmp_path, shm, game, debug="fb_dump_at=1-5,61", stamp="s1", pre=""):
    head, block = blocks()
    script = (
        "set -euo pipefail\nSTAMP=%s\nLOG=bench-logs/%s\nFRAMES=1\nGAME_ENV=()\n"
        "export FRAMES_TMPFS=%s RECOMP_DEBUG=%s\n%s%s%s"
        'echo "DBG=$RECOMP_DEBUG"\necho "TMP=$FRAMES_TMP"\n'
        "dir=${RECOMP_DEBUG##*d3d11_dump=Z:}\n%s"
    ) % (stamp, stamp, shm, debug, pre, head, block, game)
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)


def frames(tmp_path, stamp="s1"):
    return sorted(os.listdir(tmp_path / "bench-logs" / stamp / "frames"))


def test_golden_dumps_go_to_tmpfs_then_frames(tmp_path):
    shm = tmp_path / "shm"
    shm.mkdir()
    r = run(tmp_path, shm, 'printf x > "$dir/f1.bmp"; printf y > "$dir/f2.bmp"\n')
    assert r.returncode == 0, r.stderr
    tmp = r.stdout.split("TMP=")[1].split()[0]
    assert tmp.startswith(str(shm) + "/xbr-frames-s1.")
    assert "d3d11_dump=Z:%s/" % tmp in r.stdout
    assert frames(tmp_path) == ["f1.bmp", "f2.bmp"]
    assert os.listdir(shm) == []


def test_unbounded_dumps_stay_on_disk(tmp_path):
    """No fb_dump_at (every 60th present of a long run): not tmpfs."""
    shm = tmp_path / "shm"
    shm.mkdir()
    r = run(tmp_path, shm, 'printf x > "$dir/f1.bmp"\n', debug="")
    assert r.returncode == 0, r.stderr
    assert "TMP=\n" in r.stdout
    assert "d3d11_dump=Z:%s/bench-logs/s1/frames/" % os.path.realpath(tmp_path) in r.stdout
    assert frames(tmp_path) == ["f1.bmp"] and os.listdir(shm) == []


def test_no_tmpfs_writes_frames_directly(tmp_path):
    r = run(tmp_path, tmp_path / "gone", 'printf x > "$dir/f1.bmp"\n')
    assert r.returncode == 0, r.stderr
    assert "TMP=\n" in r.stdout
    assert frames(tmp_path) == ["f1.bmp"]


def test_too_many_dumps_for_the_tmpfs_stay_on_disk(tmp_path):
    """8 MiB a dump: ten million of them fit no tmpfs."""
    shm = tmp_path / "shm"
    shm.mkdir()
    r = run(tmp_path, shm, 'printf x > "$dir/f1.bmp"\n', debug="fb_dump_at=1-10000000")
    assert r.returncode == 0, r.stderr
    assert "TMP=\n" in r.stdout and os.listdir(shm) == []


def test_failed_move_is_kept_and_retried(tmp_path):
    """A move that fails keeps the dir and warns; the EXIT trap tries again."""
    shm = tmp_path / "shm"
    shm.mkdir()
    game = (
        'printf x > "$dir/f1.bmp"\nchmod a-w "$LOG/frames"\nframes_home\n'
        'echo "KEPT=$FRAMES_TMP"\nchmod u+w "$LOG/frames"\n'
    )
    r = run(tmp_path, shm, game)
    assert r.returncode == 0, r.stderr
    assert "WARN some frames stayed in" in r.stderr
    assert "KEPT=%s/xbr-frames-s1." % shm in r.stdout
    assert frames(tmp_path) == ["f1.bmp"] and os.listdir(shm) == []


def test_sweep_moves_or_drops_earlier_runs(tmp_path):
    shm = tmp_path / "shm"
    shm.mkdir()
    home = tmp_path / "bench-logs" / "s0" / "frames"
    home.mkdir(parents=True)
    left = shm / "xbr-frames-s0.aaaaaa"
    left.mkdir()
    (left / ".home").write_text("%s\n" % home)
    (left / "f9.bmp").write_text("z")
    orphan = shm / "xbr-frames-s9.bbbbbb"
    orphan.mkdir()
    (orphan / ".home").write_text("%s\n" % (tmp_path / "bench-logs" / "s9" / "frames"))
    (orphan / "f1.bmp").write_text("z")
    foreign = shm / "xbr-frames-x.cccccc"
    foreign.mkdir()
    r = run(tmp_path, shm, ":\n")
    assert r.returncode == 0, r.stderr
    assert os.listdir(home) == ["f9.bmp"]
    assert not left.exists() and not orphan.exists()
    assert foreign.exists() and "has no .home; left alone" in r.stderr
    assert sorted(os.listdir(shm)) == ["xbr-frames-x.cccccc"]


def test_trap_removes_seed_and_moves_frames(tmp_path):
    """The one EXIT trap does both: the seed copy goes, the frames move."""
    shm = tmp_path / "shm"
    shm.mkdir()
    game = 'mkdir -p "$LOG/hdd/UDATA"; SEED_HDD="$LOG/hdd"\nprintf x > "$dir/f1.bmp"\nexit 3\n'
    r = run(tmp_path, shm, game)
    assert r.returncode == 3, r.stderr
    assert not (tmp_path / "bench-logs" / "s1" / "hdd").exists()
    assert frames(tmp_path) == ["f1.bmp"] and os.listdir(shm) == []
