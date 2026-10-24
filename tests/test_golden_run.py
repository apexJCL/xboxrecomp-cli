"""golden run (golden_run.py): what a scenario needs before its run can
stop, the flip log read as it grows, the Mac run lock, the game's
environment, and one run of a fake game end to end.

  uv run pytest tests/test_golden_run.py
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from xboxrecomp_cli import golden as G
from xboxrecomp_cli import golden_run as R

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")

ANCHORED = {
    "dump_slack": 40,
    "anchors": {
        "a": {"after_flip": 100, "min_batches": 5},
        "b": {"min_batches": 1000},
    },
    "frames": [
        {"name": "plain", "dump": 2},
        {"name": "one", "dump": 5, "anchor": {"event": "a", "ref_flip": 228}},
        {
            "name": "multi",
            "dump": 19,
            "anchor": {"event": "b", "ref_flip": 897},
            "references": [
                {"label": "x", "anchor_flip": 897, "sha256": "0", "size": [1, 1]},
                {"label": "y", "anchor_flip": 890, "sha256": "0", "size": [1, 1]},
            ],
        },
    ],
}


def batches(upto, a=None, b=None):
    """A flip log to `upto`: 1 batch, 10 from a, 6000 from b."""
    out = {}
    for f in range(1, upto + 1):
        out[f] = 6000 if b and f >= b else 10 if a and f >= a else 1
    return out


def needs(log):
    return {n: (need, settled) for n, need, settled in R.frame_needs(ANCHORED, log)}


def test_needs_before_any_anchor_are_the_range_ends():
    n = needs(batches(150))
    assert n["plain"] == (121 + 2, True)
    # 60*5+1 + window 2 + slack 40; 60*19+1 + 42.
    assert n["one"] == (343, False) and n["multi"] == (1183, False)
    assert R.stop_flip(ANCHORED, batches(150)) == 1183 + R.STOP_AFTER


def test_needs_follow_the_anchor_per_reference():
    # a at 230: 230 + 301 - 228 = 303; b at 900: the later reference (890)
    # needs 900 + 1141 - 890 = 1151.
    n = needs(batches(1000, a=230, b=900))
    assert n["one"] == (303 + 2, True)
    assert n["multi"] == (1151 + 2, True)
    assert R.stop_flip(ANCHORED, batches(1000, a=230, b=900)) == 1153 + R.STOP_AFTER
    # An early anchor: earlier stop. A late one: never past the dumped range.
    assert needs(batches(1000, a=230, b=850))["multi"] == (1103, True)
    assert needs(batches(1000, a=230, b=980))["multi"] == (1183, True)
    # The anchor needs the flip after it in the log before it counts.
    assert needs(batches(900, a=230, b=900))["multi"] == (1183, False)


def test_flip_log_reads_as_it_grows(tmp_path):
    p = tmp_path / "log"
    p.write_bytes(b"[GPU] flip 1 16 ms batches 0\n[METAL] flip 1 batches 7\n[METAL] fl")
    f = R.FlipLog(str(p))
    f.poll()
    assert f.batches == {1: 7} and f.last == 1
    with open(p, "ab") as w:
        w.write(b"ip 2 batches 9\n[GPU] flip 3 50 ms\nnoise\n[GPU] flip 2 33 ms batches 0\n")
    f.poll()
    # The backend's line wins over the walker's, whichever comes first; a
    # flip line without batches still counts as progress.
    assert f.batches == {1: 7, 2: 9} and f.last == 3


def test_run_env():
    base = {"PATH": "/bin", "RECOMP_KEYBOARD": "1", "RECOMP_DEBUG": "x", "HOME": "/h"}
    env = R.run_env(
        base,
        {"RECOMP_TRACE": "flip", "RECOMP_PB_BACKEND": "d3d11", "RECOMP_SAVE_DIR": "@run"},
        "metal",
        "/r/d",
        "61-65,121",
        [("RECOMP_TRACE", "pacing"), ("RECOMP_GLOW", "off")],
    )
    assert "RECOMP_KEYBOARD" not in env and env["PATH"] == "/bin"
    assert env["RECOMP_PB_BACKEND"] == "metal" and env["RECOMP_HEADLESS"] == "1"
    assert env["SDL_AUDIO_DRIVER"] == env["SDL_AUDIODRIVER"] == "dummy"
    assert env["RECOMP_SAVE_DIR"] == "/r/d/save"
    assert env["RECOMP_DEBUG"] == "fb_dump=/r/d/frames/,fb_dump_at=61-65,121"
    assert env["RECOMP_TRACE"] == "flip,pacing" and env["RECOMP_GLOW"] == "off"


def test_mac_lock(tmp_path):
    lock = str(tmp_path / "lock")
    said = []
    with R.mac_lock("golden run x", lock=lock, say=said.append, parents=[]) as waited:
        assert waited == 0 and R.read_holder(lock) == os.getpid()
        assert open(os.path.join(lock, "cmd")).read() == "golden run x\n"
        # Busy: a live holder that is not an ancestor; waits, then 75.
        with pytest.raises(R.RunError) as e:
            with R.mac_lock("other", wait=0, lock=lock, say=said.append, parents=[]):
                pass
        assert e.value.rc == 75 and "held for 0 s" in str(e.value)
        # Held by an ancestor (the wrapping script): run under it, leave it.
        with R.mac_lock("inner", lock=lock, say=said.append, parents=[os.getpid()]):
            pass
        assert R.read_holder(lock) == os.getpid()
    assert not os.path.exists(lock)
    # A dead holder's lock is taken over.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    os.mkdir(lock)
    with open(os.path.join(lock, "pid"), "w") as f:
        f.write("%d\n" % p.pid)
    with R.mac_lock("again", lock=lock, say=said.append, parents=[]):
        assert R.read_holder(lock) == os.getpid()
    assert any("stale" in s for s in said), said
    assert not os.path.exists(lock)


FAKE_GAME = r"""#!/usr/bin/env python3
# Flips at about 2000/s: title-3d crosses at 230, first-3d at 900; then
# idles until SIGINT, which it logs with the last flip.
import signal, sys, time
last = 0
def stop(*_):
    print("fake: SIGINT after flip %d" % last, flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
for f in range(1, 5001):
    b = 6000 if f >= 900 else 10 if f >= 230 else 1
    print("[GPU] flip %d %d ms batches 0" % (f, f * 33))
    print("[METAL] flip %d batches %d" % (f, b), flush=True)
    last = f
    time.sleep(0.0005)
time.sleep(60)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_run_stops_after_the_last_needed_flip(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "game")
    shutil.copytree(GAME, root)
    os.makedirs(os.path.join(root, "game_files"))
    g = json.load(open(os.path.join(root, "analysis", "golden", "golden.json")))
    frames = os.path.join(root, "analysis", "golden", "frames")
    os.makedirs(frames, exist_ok=True)
    for sc in g["scenarios"].values():
        for fr in sc["frames"]:
            labels = [r["label"] for r in fr.get("references", [])]
            for n in ["%s.%s.png" % (fr["name"], x) for x in labels] or [fr["name"] + ".png"]:
                open(os.path.join(frames, n), "wb").close()
    exe = str(tmp_path / "fake_game")
    with open(exe, "w") as f:
        f.write(FAKE_GAME)
    os.chmod(exe, 0o755)
    monkeypatch.setattr(R, "LOCK", str(tmp_path / "lock"))
    monkeypatch.setattr(R, "POLL", 0.02)
    monkeypatch.setattr(sys, "platform", "darwin")
    out = str(tmp_path / "out")
    cfg = ["--golden-json", os.path.join(root, "analysis", "golden", "golden.json")]
    cfg += ["--golden-frames", frames, "--game-root", root]
    rc = G.main(cfg + ["run", "attract", "--exe", exe, "--out", out, "--secs", "30"])
    text = capsys.readouterr().out
    (d,) = [os.path.join(out, n) for n in os.listdir(out)]
    assert d.endswith("-metal-attract"), d
    # attract: title-3d at 230 -> 230 + 301 - 228 = 303; first-3d at 900 ->
    # 900 + 1141 - 897 = 1144; + window 2 + 3: stop from flip 1149.
    info = open(os.path.join(d, "run-info.txt")).read()
    stop = int(info.split("stop:   done at flip ")[1].split()[0])
    assert 1149 <= stop < 1600, info
    log = open(os.path.join(d, "game-stdio.log")).read()
    assert "fake: SIGINT after flip" in log, log[-300:]
    assert "RECOMP_PB_BACKEND=metal" in info and "RECOMP_HEADLESS=1" in info, info
    assert open(os.path.join(d, "exit-code")).read().strip() == "0"
    verdict = open(os.path.join(d, "golden.txt")).read().splitlines()[-1]
    # No frames were dumped (the fake writes none): not a pass.
    assert verdict.startswith("verdict: attract ") and verdict != "verdict: attract pass"
    assert rc in (1, 2) and "golden run: verdicts: attract " in text, text
    assert not os.path.exists(str(tmp_path / "lock"))


def test_run_refusals(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = ["--golden-json", os.path.join(GAME, "analysis", "golden", "golden.json")]
    cfg += ["--game-root", GAME]
    assert G.main(cfg + ["run", "--backend", "d3d11"]) == 1
    assert G.main(cfg + ["run", "--bogus"]) == 1
    assert G.main(cfg + ["run", "--exe", str(tmp_path / "none")]) == 1
    err = capsys.readouterr().err
    assert "--backend is one of metal, cpu" in err and "unknown option --bogus" in err
    assert "--exe PATH" in err, err
    monkeypatch.setattr(sys, "platform", "linux")
    assert G.main(cfg + ["run"]) == 1
    assert "macOS only" in capsys.readouterr().err


def test_hdd_seed(tmp_path, monkeypatch):
    # @seed:PATH, relative to the game root: the run boots on its own copy,
    # its save dir that copy too (any save dir set turns off the runtime's
    # fallback to the game dir's UDATA), and the seed, kept read-only, is
    # left as it was; a scenario without one gets an empty hdd/ of its own.
    assert G.hdd_seed({"RECOMP_HDD_DIR": "@seed:fixtures/hdd"}) == "fixtures/hdd"
    assert G.hdd_seed({"RECOMP_HDD_DIR": "/x/hdd"}) is None and G.hdd_seed({}) is None
    root = tmp_path / "root"
    seed = root / "fixtures" / "hdd" / "UDATA" / "t"
    seed.mkdir(parents=True)
    (seed / "SAVE").write_bytes(b"s")
    subprocess.run(["chmod", "-R", "a-w", str(root / "fixtures")], check=True)
    try:
        run = tmp_path / "run"
        run.mkdir()
        env = R.run_env({}, {"RECOMP_HDD_DIR": "@seed:fixtures/hdd"}, "metal", str(run), "61", [])
        assert env["RECOMP_SAVE_DIR"] == str(run / "save")
        got = R.seed_hdd(env, str(root), str(run))
        assert got == env["RECOMP_HDD_DIR"] == env["RECOMP_SAVE_DIR"] == str(run / "hdd")
        assert (run / "hdd" / "UDATA" / "t" / "SAVE").read_bytes() == b"s"
        (run / "hdd" / "UDATA" / "t" / "SAVE").write_bytes(b"changed")
        (run / "hdd" / "UDATA" / "t" / "NEW").write_bytes(b"n")
        assert (seed / "SAVE").read_bytes() == b"s"
        shutil.rmtree(run / "hdd")
        # No seed: an empty hdd/ of the run's own, never the runtime's
        # default root (the player's disk).
        env = R.run_env({}, {}, "metal", str(run), "61", [])
        assert R.seed_hdd(env, str(root), str(run)) == str(run / "hdd")
        assert env["RECOMP_HDD_DIR"] == env["RECOMP_SAVE_DIR"] == str(run / "hdd")
        assert not os.listdir(run / "hdd")
        shutil.rmtree(run / "hdd")
        # A plain RECOMP_HDD_DIR the env names is its own choice.
        env = R.run_env({}, {}, "metal", str(run), "61", [("RECOMP_HDD_DIR", "/x/hdd")])
        assert R.seed_hdd(env, str(root), str(run)) is None
        assert env["RECOMP_HDD_DIR"] == "/x/hdd"
        assert env["RECOMP_SAVE_DIR"] == str(run / "save")
        with pytest.raises(R.RunError):
            R.seed_hdd({"RECOMP_HDD_DIR": "@seed:nope"}, str(root), str(run))
        # seeds_missing names a scenario whose seed is not on disk.
        monkeypatch.setattr(G, "REPO", str(root))
        g = {"scenarios": {"a": {"env": {"RECOMP_HDD_DIR": "@seed:fixtures/hdd"}}, "b": {}}}
        assert not G.seeds_missing(g)
        g["scenarios"]["b"]["env"] = {"RECOMP_HDD_DIR": "@seed:gone"}
        assert G.seeds_missing(g)
        g["scenarios"]["b"]["env"] = {"RECOMP_HDD_DIR": "@seed:../root/fixtures/hdd"}
        assert G.seeds_missing(g)
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(root)], check=True)


def test_seed_dir_refuses_unsafe_seeds(tmp_path):
    # Every copy of a seed starts at seed_dir: a seed of "" or ".." would have
    # bench golden's rsync --delete empty the wrong host directory, and a
    # symlink in it would let a run save into whatever it points at.
    root = tmp_path / "game"
    (root / "fx" / "hdd" / "UDATA").mkdir(parents=True)
    (root / "fx" / "bare").mkdir()
    (root / "game_files" / "UDATA").mkdir(parents=True)
    (tmp_path / "outside" / "UDATA").mkdir(parents=True)
    os.symlink(str(tmp_path / "outside"), str(root / "fx" / "out"))
    (root / "fx" / "linked" / "UDATA").mkdir(parents=True)
    os.symlink(str(tmp_path / "outside" / "UDATA"), str(root / "fx" / "linked" / "UDATA" / "t"))
    (root / "fx" / "udlink").mkdir()
    os.symlink(str(tmp_path / "outside" / "UDATA"), str(root / "fx" / "udlink" / "UDATA"))
    r = str(root)
    assert G.seed_dir("fx/hdd", r) == os.path.realpath(str(root / "fx" / "hdd"))
    assert G.seed_dir("./fx/hdd", r) == G.seed_dir("fx/hdd", r)
    bad = {
        "": "relative to the game root",
        "/etc": "relative to the game root",
        "~/x": "relative to the game root",
        "..": "no '..'",
        "fx/../../outside": "no '..'",
        "fx\\..\\..\\outside": "no '..'",
        ".": "not inside the game root",
        "fx/out": "not inside the game root",
        "game_files": "inside the game files",
        "game_files/UDATA": "inside the game files",
        "fx/bare": "no UDATA/",
        "fx/linked": "is a symlink",
        "fx/udlink": "is a symlink",
    }
    for seed, why in bad.items():
        with pytest.raises(G.SeedError) as e:
            G.seed_dir(seed, r)
        assert why in str(e.value), (seed, str(e.value))
        assert not isinstance(e.value, G.SeedMissing), seed
    with pytest.raises(G.SeedMissing):
        G.seed_dir("fx/gone", r)
    # game_files elsewhere (game.toml's) is the one refused.
    assert G.seed_dir("game_files", r, str(tmp_path / "gf")).endswith("game_files")
    with pytest.raises(G.SeedError):
        G.seed_dir("fx/hdd", r, str(root / "fx"))


def test_run_one_removes_the_hdd_copy(tmp_path, monkeypatch):
    # The run's hdd copy (save and partition images) goes however the run
    # ends, a crash in the runner included; --keep-frames keeps it.
    root = tmp_path / "root"
    (root / "fx" / "UDATA").mkdir(parents=True)

    def boom(o, sc, d, env, secs, say):
        assert os.path.isdir(env["RECOMP_HDD_DIR"])
        raise KeyboardInterrupt

    monkeypatch.setattr(R, "run_game", boom)
    monkeypatch.setattr(R, "run_env", lambda base, env, *a: dict(env))
    out = tmp_path / "out"
    o = {"root": str(root), "out": str(out), "keep": False, "game_files": None}
    o.update(backend="metal", extra=[])
    sc = {"frames": []}
    env = {"RECOMP_HDD_DIR": "@seed:fx"}
    with pytest.raises(KeyboardInterrupt):
        R.run_one(o, "s", sc, env, 10, lambda m: None)
    (d,) = out.iterdir()
    assert not (d / "hdd").exists()
    shutil.rmtree(d)
    monkeypatch.setattr(R, "run_game", lambda o, sc, d, env, secs, say: (0, "end", 1.0, 9))
    o["keep"] = True
    d, rc, *_ = R.run_one(o, "s", sc, env, 10, lambda m: None)
    assert rc == 0 and os.path.isdir(os.path.join(d, "hdd", "UDATA"))
    # No seed: the run still boots on an empty hdd/ of its own, removed after.
    seen = {}

    def look(o, sc, d, env, secs, say):
        seen.update(env)
        return (0, "end", 1.0, 9)

    monkeypatch.setattr(R, "run_game", look)
    o["keep"] = False
    d, rc, *_ = R.run_one(o, "t", sc, {}, 10, lambda m: None)
    assert seen["RECOMP_HDD_DIR"] == seen["RECOMP_SAVE_DIR"] == os.path.join(d, "hdd")
    assert not os.path.exists(os.path.join(d, "hdd"))


def test_stop_flip_ignores_frames_skipped_on_the_backend():
    # A D3D11 run of a scenario whose late frames skip d3d11 stops after
    # the last frame it checks, not at the time limit waiting for the rest.
    sc = {
        "frames": [
            {"name": "early", "dump": 2},
            {"name": "late", "dump": 50, "skip_backends": ["d3d11"]},
        ]
    }
    assert R.stop_flip(sc, {}) == 60 * 50 + 1 + R.WINDOW + R.STOP_AFTER
    assert R.stop_flip(sc, {}, backend="metal") == 60 * 50 + 1 + R.WINDOW + R.STOP_AFTER
    assert R.stop_flip(sc, {}, backend="d3d11") == 121 + R.WINDOW + R.STOP_AFTER
    assert [n for n, _, _ in R.frame_needs(sc, {}, backend="d3d11")] == ["early"]
    # Every frame skipped: nothing to wait for.
    sc["frames"][0]["skip_backends"] = ["d3d11"]
    assert R.stop_flip(sc, {}, backend="d3d11") == R.STOP_AFTER
