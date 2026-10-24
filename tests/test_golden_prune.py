"""Pruning a passing golden run's frames (golden check --used, golden prune,
bench golden's prune): only the images the check read stay, and a pruned
run checks, records and takes a reference exactly as before. Synthetic
images and logs in temp dirs.

  uv run pytest tests/test_golden_prune.py
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys

from test_golden import H, W, flat, make_golden, metal_log, ref_png, setup_run, with_rect, write_bmp

from xboxrecomp_cli import golden as G
from xboxrecomp_cli.bench import golden as bg

HOST = os.path.join(os.path.dirname(os.path.abspath(bg.__file__)), "host")
ANCHOR = {"e": {"after_flip": 1, "min_batches": 5}}


def frame_def(sha, **extra):
    return dict(
        {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]},
        anchor={"event": "e", "ref_flip": 10},
        **extra,
    )


def scenario_run(tmp, target):
    """An anchored frame: anchor at flip 30, so its target is flip 81 and
    the pulled window 79-83; flip 80 is the reference exactly (the window's
    best), 81 is `target`. The plain dump frame_0001.bmp and flip 61."""
    b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
    flips = {61: flat(0), 79: flat(50), 80: flat(100), 81: target, 82: flat(50), 83: flat(50)}
    d = setup_run(tmp, metal_log(b), flips)
    write_bmp(os.path.join(d, "frame_0001.bmp"), flat(90))
    return d


def check(tmp, gj, d, *extra):
    G.GOLDEN_JSON = gj
    G.FRAMES_DIR = os.path.join(tmp, "frames")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = G.cmd_check(list(extra) + ["s=" + d])
    return rc, out.getvalue()


def names(d):
    return sorted(os.listdir(d))


CLOSE = with_rect(flat(100), [0, 0, 8, 8], 104)  # within tol and limits: CLOSE


def test_used_and_prune_keep_what_the_check_read(tmp_path):
    tmp = str(tmp_path)
    gj = make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100))), ANCHOR)
    d = scenario_run(tmp, CLOSE)
    used = os.path.join(tmp, "used.txt")
    rc, before = check(tmp, gj, d, "--used", used)
    assert rc == 0 and "CLOSE" in before and "best flip offset -1" in before, before
    res = G.read_used(used)
    assert res["s"][0] == 0
    assert {os.path.basename(p) for p in res["s"][1]} == {
        "frame_0001.bmp",
        "flip_00081.bmp",
        "flip_00080.bmp",
    }
    n, size, kept = G.prune_frames(d, res["s"][1])
    assert (n, kept) == (4, 2) and size > 0
    assert names(d) == ["flip_00080.bmp", "flip_00081.bmp", "frame_0001.bmp"]
    # The re-check invariant: the same verdict, numbers and window line.
    rc2, after = check(tmp, gj, d)
    assert rc2 == 0 and after == before, (before, after)


def test_pruned_run_records_and_references_the_same(tmp_path):
    tmp = str(tmp_path)
    gj = make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100))), ANCHOR)
    d = scenario_run(tmp, CLOSE)
    full = os.path.join(tmp, "full")
    shutil.copytree(os.path.dirname(d), full)
    used = os.path.join(tmp, "used.txt")
    check(tmp, gj, d, "--used", used)
    G.prune_frames(d, G.read_used(used)["s"][1])

    def record(run_frames, tag):
        g2 = os.path.join(tmp, tag + ".json")
        shutil.copy(gj, g2)
        G.GOLDEN_JSON, G.FRAMES_DIR = g2, os.path.join(tmp, tag + "-frames")
        with contextlib.redirect_stdout(io.StringIO()):
            assert G.cmd_record(["--only", "f", "s=" + run_frames]) == 0
        with open(g2) as f:
            fr = json.load(f)["scenarios"]["s"]["frames"][0]
        # As a reference too, on a frame that holds a references list.
        with open(g2) as f:
            g = json.load(f)
        g["scenarios"]["s"]["frames"][0] = dict(
            {k: v for k, v in fr.items() if k != "sha256"}, references=[]
        )
        with open(g2, "w") as f:
            json.dump(g, f)
        with contextlib.redirect_stdout(io.StringIO()):
            assert G.cmd_reference(["s", "f", "a", run_frames]) == 0
        with open(g2) as f:
            ref = json.load(f)["scenarios"]["s"]["frames"][0]["references"][0]
        return fr["sha256"], fr.get("ref_flip"), ref["sha256"], ref.get("anchor_flip")

    assert record(d, "pruned") == record(os.path.join(full, "frames"), "full")


def test_no_prune_short_of_a_pass(tmp_path):
    tmp = str(tmp_path)
    gj = make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100))), ANCHOR)
    # FAIL: the target is a wrong screen.
    d = scenario_run(tmp, flat(200))
    used = os.path.join(tmp, "used.txt")
    rc, out = check(tmp, gj, d, "--used", used)
    assert rc == 1, out
    assert G.read_used(used)["s"][0] == 1
    before = names(d)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert G.cmd_prune(["s=" + d]) == 0
    assert "nothing pruned" in out.getvalue() and names(d) == before
    # INCOMPLETE: the target flip was not dumped.
    os.remove(os.path.join(d, "flip_00081.bmp"))
    rc, out = check(tmp, gj, d, "--used", used)
    assert rc == 2 and G.read_used(used)["s"][0] == 2, out


def test_prune_command_dry_run_then_prune(tmp_path):
    tmp = str(tmp_path)
    gj = make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100))), ANCHOR)
    d = scenario_run(tmp, CLOSE)
    G.GOLDEN_JSON, G.FRAMES_DIR = gj, os.path.join(tmp, "frames")
    before = names(d)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        G.cmd_prune(["--dry-run", "s=" + d])
    assert "would prune 4 flip dumps" in out.getvalue() and names(d) == before
    with contextlib.redirect_stdout(io.StringIO()) as out:
        G.cmd_prune(["s=" + d])
    assert "pruned 4 flip dumps" in out.getvalue(), out.getvalue()
    assert names(d) == ["flip_00080.bmp", "flip_00081.bmp", "frame_0001.bmp"]


def test_prune_skips_option_values(tmp_path):
    """The value after --log is a SCEN=FILE pair, not a run to prune."""
    tmp = str(tmp_path)
    gj = make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100))), ANCHOR)
    d = scenario_run(tmp, CLOSE)
    G.GOLDEN_JSON, G.FRAMES_DIR = gj, os.path.join(tmp, "frames")
    log = os.path.join(os.path.dirname(d), "game-stdio.log")
    assert os.path.isfile(log)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        G.cmd_prune(["s=" + d, "--log", "s=" + log])
    assert "pruned 4 flip dumps" in out.getvalue(), out.getvalue()
    assert os.path.isfile(log)
    assert G.parse_check_args(["--window", "3", "--log", "s=" + log, "s=" + d])[5] == {"s": d}


class FakeRemote:
    def __init__(self):
        self.sent = []

    def ship(self, name, assigns="", prologue=True):
        return name + "\n" + assigns

    def remote(self, script, capture=False):
        self.sent.append(script)
        return 0, ""


class FakeBench:
    def __init__(self, env):
        self.cfg = env
        self.r = FakeRemote()
        self.said = []

    def say(self, msg=""):
        self.said.append(msg)

    @contextlib.contextmanager
    def no_errexit(self):
        yield


def test_bench_prunes_only_passing_clean_runs(tmp_path):
    tmp = str(tmp_path)
    runs, lines = {}, []
    for scen, rc, clean in (("a", 0, True), ("b", 0, False), ("c", 1, True), ("d", 2, True)):
        frames = os.path.join(tmp, scen, "frames")
        os.makedirs(frames)
        for n in ("frame_0001.bmp", "flip_00061.bmp", "flip_00062.bmp", "pulls.txt"):
            open(os.path.join(frames, n), "w").close()
        runs[scen] = ("20261007-00000%d" % len(runs), frames, clean)
        lines += ["%s\t%s" % (scen, os.path.join(frames, "flip_00061.bmp"))]
        lines += ["%s\t#verdict\t%d" % (scen, rc)]
    used = os.path.join(tmp, "used.txt")
    with open(used, "w") as f:
        f.write("\n".join(lines) + "\n")
    b = FakeBench({})
    bg.prune(b, runs, G.read_used(used))
    assert names(os.path.join(tmp, "a", "frames")) == [
        "flip_00061.bmp",
        "frame_0001.bmp",
        "pulls.txt",
    ]
    for scen in "bcd":
        assert len(names(os.path.join(tmp, scen, "frames"))) == 4, scen
    assert b.r.sent == ["prune_frames.sh\nSTAMPS=(20261007-000000 )\n"], b.r.sent
    # Kept on request: nothing removed, nothing sent.
    b = FakeBench({"BENCH_KEEP_FRAMES": "1"})
    bg.prune(b, {"b": runs["b"][:2] + (True,)}, used)
    assert len(names(os.path.join(tmp, "b", "frames"))) == 4 and not b.r.sent


def test_host_prune_script(tmp_path):
    """prune_frames.sh removes only bench-logs/<stamp>/frames and refuses a
    name that is not a stamp."""
    game = tmp_path / "game"
    run = game / "bench-logs" / "20261007-010203"
    (run / "frames").mkdir(parents=True)
    (run / "frames" / "flip_00001.bmp").write_text("x")
    (run / "run-info.txt").write_text("x")
    with open(os.path.join(HOST, "prune_frames.sh")) as f:
        body = f.read()

    def go(stamps):
        script = "REMOTE_GAME=%s\nSTAMPS=(%s)\n%s" % (game, stamps, body)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    r = go("../../x")
    assert r.returncode == 1 and "not a run stamp" in r.stderr
    assert (run / "frames").is_dir()
    r = go("20261007-010203")
    assert r.returncode == 0, r.stderr
    assert not (run / "frames").exists() and (run / "run-info.txt").is_file()


def test_golden_ship_seeds(tmp_path, monkeypatch):
    # bench golden sends each scenario's hdd seed once to the base's
    # golden-seeds/ and points the run at it there; other rows pass as they are.
    from types import SimpleNamespace

    (tmp_path / "fx" / "hdd" / "UDATA").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(bg, "rsync", lambda *a: calls.append(a) or 0)
    cfg = SimpleNamespace(
        game=SimpleNamespace(root=str(tmp_path), game_files=str(tmp_path / "game_files")),
        base="~/g-bench/skates",
        host="deck",
    )
    b = SimpleNamespace(cfg=cfg, step=lambda m: None)
    rows = [
        "a\t60\t100\tRECOMP_TRACE=flip RECOMP_HDD_DIR=@seed:fx/hdd",
        "b\t60\t0\tRECOMP_HDD_DIR=@seed:fx/hdd/",
        "c\t30\t0\tRECOMP_TRACE=flip",
    ]
    out = bg.ship_seeds(b, rows)
    dst = "~/g-bench/skates/golden-seeds/fx/hdd"
    assert out == [
        "a\t60\t100\tRECOMP_TRACE=flip RECOMP_HDD_DIR=@seed:" + dst,
        "b\t60\t0\tRECOMP_HDD_DIR=@seed:" + dst,
        rows[2],
    ]
    assert len(calls) == 1 and calls[0][-2:] == (
        str(tmp_path / "fx" / "hdd") + "/",
        "deck:" + dst + "/",
    )
    assert "--rsync-path=mkdir -p ~/g-bench/skates/golden-seeds/fx && rsync" in calls[0]
    # The fixture is read-only; the host's copy must stay deletable.
    assert "--chmod=Du+w,Fu+w" in calls[0]
    # A seed that is missing or not a safe path is refused before any rsync:
    # with --delete, "" or ".." would empty the base or what is above it.
    (tmp_path / "game_files" / "UDATA").mkdir(parents=True)
    for seed in ("missing", "", "/", "/etc", "..", "fx/../..", ".", "game_files"):
        try:
            bg.ship_seeds(b, ["d\t1\t0\tRECOMP_HDD_DIR=@seed:" + seed])
        except bg.BenchError:
            pass
        else:
            raise AssertionError("seed %r was not refused" % seed)
    assert len(calls) == 1


def test_golden_ship_pad_scripts(tmp_path, monkeypatch):
    # A pad script file goes to the base's golden-seeds/ once and the row
    # names it @seed:<host path>; an absolute @file is left as it is.
    from types import SimpleNamespace

    (tmp_path / "fx").mkdir()
    (tmp_path / "fx" / "route.pad").write_text("f10:a\n")
    calls = []
    monkeypatch.setattr(bg, "rsync", lambda *a: calls.append(a) or 0)
    cfg = SimpleNamespace(
        game=SimpleNamespace(root=str(tmp_path), game_files=str(tmp_path / "game_files")),
        base="~/g-bench/skates",
        host="deck",
    )
    b = SimpleNamespace(cfg=cfg, step=lambda m: None)
    rows = [
        "a\t60\t0\tRECOMP_TRACE=flip RECOMP_PAD_SCRIPT=@fx/route.pad",
        "b\t60\t0\tRECOMP_PAD_SCRIPT=@fx/route.pad",
        "c\t30\t0\tRECOMP_PAD_SCRIPT=@/abs/route.pad",
        "e\t60\t0\tRECOMP_DEBUG=fb_dump_at=60,pad_script=@fx/route.pad,pad_record=r.pad",
    ]
    out = bg.ship_seeds(b, rows)
    dst = "~/g-bench/skates/golden-seeds/fx/route.pad"
    assert out == [
        "a\t60\t0\tRECOMP_TRACE=flip RECOMP_PAD_SCRIPT=@seed:" + dst,
        "b\t60\t0\tRECOMP_PAD_SCRIPT=@seed:" + dst,
        rows[2],
        "e\t60\t0\tRECOMP_DEBUG=fb_dump_at=60,pad_script=@seed:%s,pad_record=r.pad" % dst,
    ]
    assert len(calls) == 1 and calls[0][-2:] == (str(tmp_path / "fx" / "route.pad"), "deck:" + dst)
    assert "--rsync-path=mkdir -p ~/g-bench/skates/golden-seeds/fx && rsync" in calls[0]
    for pad in ("fx/gone.pad", "../x.pad", "fx"):
        try:
            bg.ship_seeds(b, ["d\t1\t0\tRECOMP_PAD_SCRIPT=@" + pad])
        except bg.BenchError:
            pass
        else:
            raise AssertionError("pad %r was not refused" % pad)
    assert len(calls) == 1


def pad_block():
    """run_game.sh's RECOMP_PAD_SCRIPT=@seed: block, to run on its own."""
    import re

    with open(os.path.join(HOST, "run_game.sh")) as f:
        m = re.search(r"^case \"\$\{RECOMP_PAD_SCRIPT:-\}\" in\n.*?^esac\n", f.read(), re.S | re.M)
    assert m, "no pad block in run_game.sh"
    return m.group(0)


def test_host_pad_script(tmp_path):
    """run_game.sh hands Wine a shipped pad script as a Z: path, in both the
    exported variable and GAME_ENV, and refuses one that is not there."""
    (tmp_path / "seeds").mkdir()
    pad = tmp_path / "seeds" / "route.pad"
    pad.write_text("f10:a\n")

    def go(path):
        script = (
            "set -u\nGAME_ENV=(X=1 RECOMP_PAD_SCRIPT=@seed:%s)\nexport RECOMP_PAD_SCRIPT=@seed:%s\n%s"
            'printf "%%s\\n" "${GAME_ENV[@]}"\necho "PAD=$RECOMP_PAD_SCRIPT"\n'
        ) % (path, path, pad_block())
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)

    r = go(pad)
    assert r.returncode == 0, r.stderr
    z = "@Z:%s" % pad
    assert r.stdout.split() == ["X=1", "RECOMP_PAD_SCRIPT=" + z, "PAD=" + z]
    r = go("seeds/route.pad")
    assert (
        r.returncode == 0 and "PAD=@Z:%s/seeds/route.pad" % os.path.realpath(tmp_path) in r.stdout
    )
    r = go(tmp_path / "gone.pad")
    assert r.returncode == 1 and "no pad script" in r.stderr


def debug_pad_block():
    """run_game.sh's RECOMP_DEBUG pad_script=@seed: block, to run on its own."""
    import re

    with open(os.path.join(HOST, "run_game.sh")) as f:
        m = re.search(r'^case ",\$\{RECOMP_DEBUG:-\}" in\n.*?^esac\n', f.read(), re.S | re.M)
    assert m, "no RECOMP_DEBUG pad block in run_game.sh"
    return m.group(0)


def test_host_debug_pad_script(tmp_path):
    """The same for RECOMP_DEBUG's pad_script=@seed:PATH, in the merged list
    and in GAME_ENV, leaving the list's other keys alone."""
    (tmp_path / "seeds").mkdir()
    pad = tmp_path / "seeds" / "route.pad"
    pad.write_text("f10:a\n")

    def go(path):
        dbg = "fb_dump_at=60,pad_script=@seed:%s" % path
        script = (
            "set -u\nGAME_ENV=(X=1 RECOMP_DEBUG=%s)\n"
            "export RECOMP_DEBUG=%s,d3d11_dump=Z:/f/\n%s"
            'printf "%%s\\n" "${GAME_ENV[@]}"\necho "DBG=$RECOMP_DEBUG"\n'
        ) % (dbg, dbg, debug_pad_block())
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)

    r = go(pad)
    assert r.returncode == 0, r.stderr
    z = "fb_dump_at=60,pad_script=@Z:%s" % pad
    assert r.stdout.split() == ["X=1", "RECOMP_DEBUG=" + z, "DBG=%s,d3d11_dump=Z:/f/" % z]
    r = go(tmp_path / "gone.pad")
    assert r.returncode == 1 and "no pad script" in r.stderr
    # A path with glob characters and an & is spliced in as it is (bash 5.2
    # reads & in a ${v/pat/rep} replacement as the match), at the list's start.
    odd = tmp_path / "seeds" / "r[1]&*.pad"
    odd.write_text("f10:a\n")
    script = (
        "set -u\nshopt -s patsub_replacement 2>/dev/null\n"
        "GAME_ENV=(RECOMP_DEBUG='pad_script=@seed:%s,peek=1')\n"
        "export RECOMP_DEBUG='pad_script=@seed:%s,peek=1'\n%s"
        'printf "%%s\\n" "${GAME_ENV[@]}"\necho "DBG=$RECOMP_DEBUG"\n'
    ) % (odd, odd, debug_pad_block())
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    z = "pad_script=@Z:%s,peek=1" % odd
    assert r.returncode == 0 and r.stdout.splitlines() == ["RECOMP_DEBUG=" + z, "DBG=" + z], (
        r.stdout + r.stderr
    )
    script = 'set -u\nGAME_ENV=()\nexport RECOMP_DEBUG=pad_script=f1:a\n%secho "$RECOMP_DEBUG"\n'
    r = subprocess.run(["bash", "-c", script % debug_pad_block()], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "pad_script=f1:a"


def seed_block():
    """run_game.sh's RECOMP_HDD_DIR=@seed: block, to run on its own."""
    import re

    with open(os.path.join(HOST, "run_game.sh")) as f:
        m = re.search(r"^SEED_HDD=\n.*?^esac\n", f.read(), re.S | re.M)
    assert m, "no seed block in run_game.sh"
    return m.group(0)


def test_host_seed_copy(tmp_path):
    """run_game.sh boots the run on a writable copy of a read-only seed, its
    save dir the copy too (over BENCH_ENV's @run), requires the seed's
    UDATA/, and removes the copy when the script exits."""
    seed = tmp_path / "seeds" / "hdd"
    (seed / "UDATA" / "t").mkdir(parents=True)
    (seed / "UDATA" / "t" / "SAVE").write_text("s")
    (tmp_path / "bare").mkdir()
    # cp -a --sparse=always is GNU cp (the Deck's); macOS's cp lacks --sparse.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "cp").write_text(
        '#!/bin/sh\nfor a; do [ "$a" = --sparse=always ] || set -- "$@" "$a"; shift; done\n'
        'exec /bin/cp "$@"\n'
    )
    os.chmod(shim / "cp", 0o755)
    subprocess.run(["chmod", "-R", "a-w", str(seed)], check=True)
    work = tmp_path / "work"
    (work / "log").mkdir(parents=True)

    def go(path, game_env):
        script = (
            "set -u\nLOG=log\nGAME_ENV=(%s)\nexport RECOMP_HDD_DIR=@seed:%s\n%s"
            'printf "%%s\\n" "${GAME_ENV[@]}"\necho "SAVE=$RECOMP_SAVE_DIR"\n'
            "echo changed > log/hdd/UDATA/t/SAVE && echo WROTE\n"
        ) % (game_env, path, seed_block())
        env = dict(os.environ, PATH="%s:%s" % (shim, os.environ["PATH"]))
        return subprocess.run(
            ["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True
        )

    try:
        r = go(seed, "RECOMP_HDD_DIR=@seed:%s RECOMP_SAVE_DIR=Z:/elsewhere X=1" % seed)
        assert r.returncode == 0, r.stderr
        z = "Z:%s/log/hdd" % os.path.realpath(work)
        lines = r.stdout.split()
        assert lines[:3] == ["RECOMP_HDD_DIR=" + z, "RECOMP_SAVE_DIR=" + z, "X=1"], lines
        assert "SAVE=" + z in lines and "WROTE" in lines
        assert not (work / "log" / "hdd").exists()  # the EXIT trap
        assert (seed / "UDATA" / "t" / "SAVE").read_text() == "s"
        r = go(seed, "RECOMP_HDD_DIR=@seed:%s" % seed)
        assert r.returncode == 0, r.stderr
        assert "RECOMP_SAVE_DIR=" + z in r.stdout.split()
        r = go(tmp_path / "bare", "")
        assert r.returncode == 1 and "has no UDATA/" in r.stderr
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(seed)], check=True)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
