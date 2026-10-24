"""The scripted pad's grammar (pad_script.py, the runtime's
src/usb/pad_script.c mirrored), golden pad (a pad_record file as a
scenario's pad_script), golden.json's check of scenario scripts, and golden
play's environment and run with a fake game.

  uv run pytest tests/test_pad_script.py
"""

import json
import os
import shutil
import sys

import pytest

from xboxrecomp_cli import golden as G
from xboxrecomp_cli import golden_run as R
from xboxrecomp_cli import pad_script as P

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")

RECORD = """# pad_record: pad_script steps timed by flip (f<flip>:<buttons>:<hold flips>)
f5120:start:12
f5300:ldown:6
f5390:a+lup:40
f6001:p2-b:3
"""


def test_parse_both_forms():
    s = P.parse(
        "8000:start:300, 9000:a\n# a comment, a:b\nf120:lup+a:30 f200:x f300:p2-b:4f 16:down"
    )
    assert [P.format_step(x) for x in s] == [
        "8000:start:300",
        "9000:a:150",
        "f120:lup+a:30",
        "f200:x:9",
        "f300:p2-b:4",
        "16:down:150",
    ]
    assert s[4].pad == 1 and s[2].by_flip and not s[0].by_flip


@pytest.mark.parametrize(
    "text, why",
    [
        ("f10:jump", "unknown button jump"),
        ("start", "bad entry"),
        ("f10:a:5x", "bad entry"),
        ("100:a:5f", "hold in flips"),
        ("f10:p5-a", "unknown button p5-a"),
        # Decimal, as the runtime reads it: a leading zero is a typo here
        # (base 0 would have read it as octal), and hex is no time.
        ("f010:a", "leading zero"),
        ("f10:a:08", "leading zero"),
        ("0x10:down", "bad entry"),
        ("f10:lx=40000", "unknown button lx=40000"),
        ("f10:lx=-012", "unknown button lx=-012"),
        ("f10:lz=5", "unknown button lz=5"),
    ],
)
def test_parse_refuses(text, why):
    with pytest.raises(P.PadScriptError, match=why):
        P.parse(text)


def test_stick_values():
    # The recorder writes sticks at their values; a step may mix them with
    # buttons, and the values pass through golden pad unchanged.
    s = P.parse("f5:lx=-12000+ry=300+a:2, f7:ly=0:1, f9:LX=32767")
    assert [P.format_step(x) for x in s] == [
        "f5:lx=-12000+ry=300+a:2",
        "f7:ly=0:1",
        "f9:lx=32767:9",
    ]
    rec = "# pad_record\nf100:lx=13434:1\nf101:lx=19000:1\nf102:lx=32767:30\nf100:a:40\n"
    assert P.to_script(P.from_record(rec, shift=-50)) == (
        "f50:lx=13434:1,f50:a:40,f51:lx=19000:1,f52:lx=32767:30"  # by start flip
    )
    assert P.script_of("pad_script=f1:lx=-5:2,f2:a,peek=1") == "f1:lx=-5:2,f2:a"


def test_from_record_cuts_and_shifts():
    steps = P.from_record(RECORD)
    assert P.to_script(steps) == "f5120:start:12,f5300:ldown:6,f5390:a+lup:40,f6001:p2-b:3"
    cut = P.from_record(RECORD, start=5300, end=6000, shift=-300)
    assert P.to_script(cut) == "f5000:ldown:6,f5090:a+lup:40"
    with pytest.raises(P.PadScriptError, match="before flip 0"):
        P.from_record(RECORD, shift=-6000)
    with pytest.raises(P.PadScriptError, match="timed in ms"):
        P.from_record("100:a")
    # What it prints parses back to the same steps.
    assert P.parse(P.to_script(steps)) == steps


def test_script_of_a_debug_list():
    d = "fb_dump=x/,pad_script=90000:start:200,95000:a,f10:b,pad_live=/tmp/l,peek=1"
    assert P.script_of(d) == "90000:start:200,95000:a,f10:b"
    assert P.script_of("pad_script=f1:a") == "f1:a"
    assert P.script_of("fb_dump=x/") is None and P.script_of(None) is None
    assert P.scenario_errors({"RECOMP_DEBUG": "pad_script=f1:jump"})
    assert P.scenario_errors({"RECOMP_DEBUG": d}) == []
    # Another key ends the script whatever its value holds; a step never
    # reads as a key, and the runtime trims each fragment.
    assert P.script_of("pad_script=f1:a,f2:b,pad_record=Z:/x/y.pad") == "f1:a,f2:b"
    assert P.script_of("pad_script=f1:a, fb_dump=Z:/x/") == "f1:a"
    assert P.script_of("pad_script=f1:a,f2:lx=0.5,500:b,f9") == "f1:a,f2:lx=0.5,500:b,f9"


def test_host_paths_refused(tmp_path, monkeypatch):
    # An absolute or ~ @path is neither checked nor shipped to a bench
    # host, and would put a private path in golden.json: refused at load.
    for v in ("@/some/file", "@~/x.pad", "@C:/x.pad", "@Z:\\x.pad", "@\\\\srv\\x.pad"):
        for env in ({"RECOMP_PAD_SCRIPT": v}, {"RECOMP_DEBUG": "pad_script=" + v}):
            errs = P.scenario_errors(env, str(tmp_path))
            assert len(errs) == 1 and "under the game root" in errs[0], (env, errs)
    assert P.scenario_errors({"RECOMP_DEBUG": "pad_script=@seed:fx"}, str(tmp_path)) == []
    g = json.load(open(os.path.join(GAME, "analysis", "golden", "golden.json")))
    scen = next(iter(g["scenarios"]))
    g["scenarios"][scen].setdefault("env", {})["RECOMP_DEBUG"] = "pad_script=@/abs/x.pad"
    p = tmp_path / "golden.json"
    p.write_text(json.dumps(g))
    monkeypatch.setattr(G, "GOLDEN_JSON", str(p))
    with pytest.raises(SystemExit, match="under the game root"):
        G.load_golden()


def test_from_record_sorts_by_start():
    # A press inside a long stick hold is written first (steps are written
    # when they end); from_record returns them by start flip.
    rec = "f20:a:3\nf10:lx=1:50\nf20:b:2\n"
    assert [(s.at, P.format_step(s)) for s in P.from_record(rec)] == [
        (10, "f10:lx=1:50"),
        (20, "f20:a:3"),
        (20, "f20:b:2"),
    ]


def test_load_golden_refuses_a_bad_script(tmp_path, monkeypatch):
    g = json.load(open(os.path.join(GAME, "analysis", "golden", "golden.json")))
    scen = next(iter(g["scenarios"]))
    g["scenarios"][scen].setdefault("env", {})["RECOMP_DEBUG"] = "pad_script=f100:strat"
    p = tmp_path / "golden.json"
    p.write_text(json.dumps(g))
    monkeypatch.setattr(G, "GOLDEN_JSON", str(p))
    with pytest.raises(SystemExit, match="unknown button strat"):
        G.load_golden()


def test_pad_file_scripts(tmp_path, monkeypatch):
    # RECOMP_PAD_SCRIPT=@PATH under the game root: read and checked at load,
    # named by seeds_missing when absent, refused when unsafe.
    root = tmp_path / "game"
    (root / "fx").mkdir(parents=True)
    (root / "fx" / "route.pad").write_text(RECORD)
    (root / "fx" / "bad.pad").write_text("f10:jump\n")
    assert P.scenario_errors({"RECOMP_PAD_SCRIPT": "@fx/route.pad"}, str(root)) == []
    errs = P.scenario_errors({"RECOMP_PAD_SCRIPT": "@fx/bad.pad"}, str(root))
    assert len(errs) == 1 and errs[0].startswith("fx/bad.pad: ")
    # Missing: seeds_missing's to name, not a parse error.
    assert P.scenario_errors({"RECOMP_PAD_SCRIPT": "@fx/gone.pad"}, str(root)) == []
    assert G.pad_seed({"RECOMP_PAD_SCRIPT": "@fx/route.pad"}) == "fx/route.pad"
    for v in ("@/abs/x.pad", "@~/x.pad", "@seed:fx", "f1:a", ""):
        assert G.pad_seed({"RECOMP_PAD_SCRIPT": v}) is None, v
    assert G.pad_file("fx/route.pad", str(root)) == os.path.realpath(root / "fx" / "route.pad")
    os.symlink(root / "fx" / "route.pad", root / "fx" / "link.pad")
    (root / "game_files").mkdir()
    (root / "game_files" / "x.pad").write_text(RECORD)
    for bad in ("", "../x.pad", "fx/../../x.pad", "fx/link.pad", "fx", "game_files/x.pad"):
        with pytest.raises(G.SeedError):
            G.pad_file(bad, str(root))
    with pytest.raises(G.SeedMissing):
        G.pad_file("fx/gone.pad", str(root))
    # The RECOMP_DEBUG form, the path ending at the list's next key.
    dbg = {"RECOMP_DEBUG": "fb_dump_at=60,pad_script=@fx/route.pad,d3d11_dump=Z:/x/"}
    assert G.pad_seed(dbg) == "fx/route.pad"
    assert P.scenario_errors(dbg, str(root)) == []
    errs = P.scenario_errors({"RECOMP_DEBUG": "pad_script=@fx/bad.pad"}, str(root))
    assert len(errs) == 1 and errs[0].startswith("fx/bad.pad: ")
    for v in ("pad_script=@/abs/x.pad", "pad_script=f1:a", "fb_dump_at=60"):
        assert G.pad_seed({"RECOMP_DEBUG": v}) is None, v
    monkeypatch.setattr(G, "REPO", str(root))
    g = {"scenarios": {"a": {"env": {"RECOMP_DEBUG": "pad_script=@fx/gone.pad"}}}}
    assert G.seeds_missing(g)
    g = {"scenarios": {"a": {"env": {"RECOMP_PAD_SCRIPT": "@fx/route.pad"}}}}
    assert not G.seeds_missing(g)
    g["scenarios"]["a"]["env"]["RECOMP_PAD_SCRIPT"] = "@fx/gone.pad"
    assert G.seeds_missing(g)


def test_cmd_pad(tmp_path, capsys):
    rec = tmp_path / "rec.pad"
    rec.write_text(RECORD)
    assert G.cmd_pad([str(rec), "--from", "5300", "--to", "6000"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "f5300:ldown:6,f5390:a+lup:40" and "2 steps" in err
    assert G.cmd_pad([str(rec), "--shift", "x"]) == 2
    assert G.cmd_pad([str(tmp_path / "none")]) == 1
    assert G.cmd_pad([]) == 2


def test_play_env():
    base = {"PATH": "/bin", "RECOMP_TRACE": "inherited", "SDL_AUDIO_DRIVER": "coreaudio"}
    scen = {
        "RECOMP_TRACE": "flip",
        "RECOMP_KEYBOARD": "0",
        "RECOMP_HOST_PAD": "0",
        "RECOMP_DEBUG": "pad_script=90000:start:200,95000:a,peek=1",
    }
    env = R.play_env(base, scen, "metal", "/run", "/r/rec.pad", unset=["RECOMP_HOST_PAD"])
    assert env["RECOMP_TRACE"] == "flip" and env["SDL_AUDIO_DRIVER"] == "coreaudio"
    assert env["RECOMP_KEYBOARD"] == "1" and "RECOMP_HOST_PAD" not in env
    assert "RECOMP_HEADLESS" not in env and "SDL_AUDIODRIVER" not in env
    assert env["RECOMP_DEBUG"] == "pad_script=90000:start:200,95000:a,peek=1,pad_record=/r/rec.pad"
    env = R.play_env(base, scen, "metal", "/run", "/r/rec.pad", script=False)
    assert env["RECOMP_DEBUG"] == "peek=1,pad_record=/r/rec.pad"
    env = R.play_env(base, {}, "metal", "/run", "/r/rec.pad", True, [("RECOMP_DEBUG", "x=1")])
    assert env["RECOMP_DEBUG"] == "pad_record=/r/rec.pad,x=1"
    assert env["RECOMP_SAVE_DIR"] == "/run/save"
    # The flip trace is on, once, for the stop line and the recording.
    assert env["RECOMP_TRACE"] == "flip"
    assert R.play_env(base, scen, "metal", "/r", "/p")["RECOMP_TRACE"] == "flip"
    # A game's own input pins come from game.toml, not the CLI.
    assert R.play_env(base, scen, "metal", "/r", "/p", unset=())["RECOMP_HOST_PAD"] == "0"
    pins = {"GAME_PAD_PIN": "0", "RECOMP_INPUT_SCRIPT": "@s", "RECOMP_PAD_SCRIPT": "f1:a"}
    env = R.play_env(base, dict(scen, **pins), "metal", "/r", "/p", unset=["GAME_PAD_PIN"])
    assert "GAME_PAD_PIN" not in env
    # The script kept, every one of them stays ...
    assert env["RECOMP_INPUT_SCRIPT"] == "@s" and env["RECOMP_PAD_SCRIPT"] == "f1:a"
    # ... and --no-script drops them all, the game's own input script too.
    env = R.play_env(
        base, dict(scen, **pins), "metal", "/r", "/p", False, (), (), "RECOMP_INPUT_SCRIPT"
    )
    assert "RECOMP_INPUT_SCRIPT" not in env and "RECOMP_PAD_SCRIPT" not in env
    assert "pad_script" not in env["RECOMP_DEBUG"]


def test_play_record_path_without_commas():
    with pytest.raises(R.RunError, match="cannot hold a ','"):
        R.parse_play(["garage", "--record", "/tmp/a,b.pad"])
    assert R.parse_play(["garage", "--record", "/tmp/ab.pad"])["record"] == "/tmp/ab.pad"


FAKE_GAME = r"""#!/usr/bin/env python3
# Writes a pad_record file where RECOMP_DEBUG's pad_record= names, flips a
# little, and exits as a closed window does.
import os
debug = os.environ.get("RECOMP_DEBUG", "")
path = [x.split("=", 1)[1] for x in debug.split(",") if x.startswith("pad_record=")][0]
with open(path, "w") as f:
    f.write("# pad_record\nf10:a:3\nf20:start:9\n")
for n in range(1, 41):
    print("[GPU] flip %d %d ms batches 0" % (n, n * 16), flush=True)
print("keyboard=%s" % os.environ.get("RECOMP_KEYBOARD"))
hdd = os.environ.get("RECOMP_HDD_DIR", "")
print("hdd=%s isdir=%s save=%s" % (hdd, os.path.isdir(hdd), os.environ.get("RECOMP_SAVE_DIR")))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_play_records_into_a_new_file(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "game")
    shutil.copytree(GAME, root)
    exe = str(tmp_path / "fake_game")
    with open(exe, "w") as f:
        f.write(FAKE_GAME)
    os.chmod(exe, 0o755)
    monkeypatch.setattr(R, "LOCK", str(tmp_path / "lock"))
    monkeypatch.setattr(R, "POLL", 0.02)
    monkeypatch.setattr(sys, "platform", "darwin")
    out = str(tmp_path / "out")
    rec = str(tmp_path / "rec" / "garage.pad")
    cfg = ["--golden-json", os.path.join(root, "analysis", "golden", "golden.json")]
    cfg += ["--game-root", root]
    args = ["play", "attract", "--record", rec, "--exe", exe, "--out", out, "--secs", "30"]
    assert G.main(cfg + args) == 0
    text = capsys.readouterr().out
    assert "closed at flip 40" in text and "2 steps in " + rec in text, text
    (d,) = [os.path.join(out, n) for n in os.listdir(out)]
    assert d.endswith("-play-attract")
    info = open(os.path.join(d, "run-info.txt")).read()
    assert "pad_record=" + rec in info and "RECOMP_HEADLESS" not in info, info
    log = open(os.path.join(d, "game-stdio.log")).read()
    assert "keyboard=1" in log
    # No seed: the game still booted on an empty hdd of the run's own (the
    # default hdd root is the player's disk), and the copy is gone after.
    hdd = os.path.join(d, "hdd")
    assert "hdd=%s isdir=True save=%s" % (hdd, hdd) in log, log
    assert not os.path.exists(hdd) and "seed:   none (an empty " in info
    assert not os.path.exists(str(tmp_path / "lock"))
    # Never over an earlier recording.
    assert G.main(cfg + args) == 1
    assert "exists" in capsys.readouterr().err
    assert G.main(cfg + ["play", "attract"]) == 2
    assert G.main(cfg + ["play", "nope", "--record", str(tmp_path / "x"), "--exe", exe]) == 1
