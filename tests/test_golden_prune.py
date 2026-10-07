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
    bg.prune(b, runs, used)
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


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
