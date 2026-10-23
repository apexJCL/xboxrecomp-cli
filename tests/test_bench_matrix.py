"""bench_matrix.py on synthetic scenes, logs, GPU samples and profiles."""

import csv
import datetime
import json
import os
import re
import struct
import time

import pytest

from xboxrecomp_cli import bench_matrix as B

SCENE = """
arm_id = "s{scale}-dsp{dsp}-{aspect}"

[scene]
name = "t"
run_s = 30
duration_s = 10
warmup_s = 2

[scene.start]
min_batches = 100
hold_flips = 5
fallback_s = 3

[profile]
hz = 250
delay_s = 1

[env]
RECOMP_TRACE = "flip"

[[axis]]
name = "scale"
values = [1, 2, 4]
config = "render.scale"
stock = 1

[[axis]]
name = "dsp"
values = [0, 1]
env = "RECOMP_APU_DSP"

[[axis]]
name = "aspect"
values = ["4:3", "16:9"]
labels = ["43", "169"]
config = "display.aspect"
stock = "4:3"

[hosts.mac]
kind = "local"
backend = "metal"

[hosts.deck]
kind = "proton"
backend = "d3d11"
exclude = ["scale=4"]
profile = { hz = 100 }

[verdict]
hosts = ["deck"]
warmup_fps_min = 50

[hosts.windows]
kind = "windows"
backend = "vulkan"
"""


@pytest.fixture
def scene(tmp_path):
    p = tmp_path / "scene.toml"
    p.write_text(SCENE)
    return B.load_scene(str(p))


def flip_log(intro=100, intro_ms=20.0, scene_ms=None, n=700, t0_us=1_000_000_000):
    """intro flips with 0 batches, then n 3D flips at scene_ms (a list
    cycles)."""
    scene_ms = scene_ms or [16.0]
    t = t0_us
    lines, k = [], 1
    for _ in range(intro):
        lines.append("[PACING] flip %d t_us %d batches 0" % (k, t))
        t += int(intro_ms * 1000)
        k += 1
    for i in range(n):
        lines.append("[PACING] flip %d t_us %d batches 500" % (k, t))
        t += int(scene_ms[i % len(scene_ms)] * 1000)
        k += 1
    return "\n".join(lines) + "\n"


def test_arms_order_ids_and_exclude(scene):
    ids = [a["id"] for a in B.arms(scene, "mac")]
    assert ids[:4] == ["s1-dsp0-43", "s1-dsp0-169", "s1-dsp1-43", "s1-dsp1-169"]
    assert len(ids) == 12
    deck = [a["id"] for a in B.arms(scene, "deck")]
    assert len(deck) == 8 and not any(i.startswith("s4") for i in deck)
    a = B.find_arm(scene, "mac", "s2-dsp1-169")
    assert a["config"] == {"render": {"scale": 2}, "display": {"aspect": "16:9"}}
    assert a["env"] == {"RECOMP_APU_DSP": "1"}
    # Stock values stay out of config.toml.
    assert B.find_arm(scene, "mac", "s1-dsp0-43")["config"] == {}
    with pytest.raises(B.MatrixError):
        B.find_arm(scene, "deck", "s4-dsp0-43")


def test_config_toml_and_env(scene):
    a = B.find_arm(scene, "mac", "s4-dsp1-169")
    text = B.config_toml(a["config"])
    assert '[display]\naspect = "16:9"' in text and "[render]\nscale = 4" in text
    env = B.run_env(scene, a, True, "/x/prof.txt")
    assert env["RECOMP_TRACE"] == "flip,pacing=all,prof=250,prof_delay=1,prof_out=/x/prof.txt"
    assert env["RECOMP_APU_DSP"] == "1" and env["RECOMP_WINDOW_QUIT_AFTER"] == "30"
    assert "prof" not in B.run_env(scene, a, False)["RECOMP_TRACE"]
    # A host's [profile] wins over the scene's (Proton samples slower).
    deck = dict(scene["hosts"]["deck"], _name="deck")
    a = B.find_arm(scene, "deck", "s2-dsp1-169")
    assert ",prof=100," in B.run_env(scene, a, True, "/x/p.txt", deck)["RECOMP_TRACE"]


def test_scene_errors(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(SCENE.replace('config = "render.scale"', 'config = "render.scale"\nenv = "X"'))
    with pytest.raises(B.MatrixError, match="one of config or env"):
        B.load_scene(str(p))
    p.write_text(SCENE.replace('arm_id = "s{scale}-dsp{dsp}-{aspect}"', ""))
    with pytest.raises(B.MatrixError, match="arm_id"):
        B.load_scene(str(p))


def test_start_by_batches_ignores_a_lone_busy_flip():
    flips = B.parse_flips(flip_log())
    flips[10] = (flips[10][0], flips[10][1], 900)  # one busy movie flip
    i, how = B.find_start(flips, {"min_batches": 100, "hold_flips": 5})
    assert (i, how) == (100, "batches")


def test_start_fallback():
    flips = B.parse_flips(flip_log(n=0, intro=400))
    i, how = B.find_start(flips, {"min_batches": 100, "fallback_s": 3})
    assert how == "fallback" and flips[i][1] - flips[0][1] >= 3e6
    with pytest.raises(B.MatrixError):
        B.find_start(flips, {"min_batches": 100})


def test_pct_nearest_rank():
    for n in (100, 300):
        v = list(range(1, n + 1))
        # ceil(p/100 * n): the 99th of 100 and the 297th of 300, the 1st
        # and the 3rd, whatever the parity (round() half-to-even was not).
        assert B.pct(v, 99) == 99 * n // 100
        assert B.pct(v, 1) == n // 100
    assert B.pct([5.0], 99) == 5.0 and B.pct([], 50) is None
    assert B.pct([1, 2, 3], 50) == 2 and B.pct([1, 2, 3, 4], 50) == 2


def test_frame_stats_steady_and_stutter():
    # 16 ms frames with one 50 ms hitch every 100 frames.
    ms = [16.0] * 99 + [50.0]
    flips = B.parse_flips(flip_log(scene_ms=ms, n=1000))
    i, _ = B.find_start(flips, {"min_batches": 100})
    st = B.frame_stats([f[1] for f in flips], flips[i][1], 10, 2)
    assert st["complete"]
    assert st["frame_ms"]["p50"] == 16.0 and st["frame_ms"]["max"] == 50.0
    # 50 ms is over both 1.5x and 2.5x the 16 ms median.
    assert st["stutters"] == st["hitches"] == sum(1 for f in st["frames"] if f[1] > 40)
    assert st["low1_fps"] == pytest.approx(20.0, abs=0.5)
    # (99 * 16 + 50) / 100 = 16.34 ms a frame.
    assert st["avg_fps"] == pytest.approx(61.2, abs=0.2) and len(st["fps_per_s"]) == 10
    # Frame times run between the window's flips; the interval into the
    # first one (from the intro's last flip, 20 ms) is the load gap.
    assert st["frames"][0][1] == 16.0 and st["load_gap_ms"] == 20.0
    assert len(st["frames"]) == st["flips"] - 1
    assert st["steady"]["low1_fps"] == pytest.approx(20.0, abs=0.5)
    assert st["warmup"]["frames"] + st["steady"]["frames"] == len(st["frames"])
    assert st["steady"]["slow_share"] == pytest.approx(0.01, abs=0.002)


def test_frame_stats_load_gap_is_not_a_frame():
    # A 1.5 s load between the intro and the scene: as a frame it would be
    # the max and most of the 1% low.
    flips = B.parse_flips(flip_log(intro_ms=1500.0, n=800))
    i, _ = B.find_start(flips, {"min_batches": 100})
    st = B.frame_stats([f[1] for f in flips], flips[i][1], 10, 2)
    assert st["load_gap_ms"] == 1500.0
    assert st["frame_ms"]["max"] == 16.0 and st["low1_fps"] == pytest.approx(62.5)


def test_frame_stats_hitch_and_stutter_lines():
    # 33.3 ms (a missed vblank at 60 Hz) is a hitch, not a stutter; 45 ms is both.
    ms = [16.7] * 48 + [33.3, 45.0]
    flips = B.parse_flips(flip_log(scene_ms=ms, n=800))
    st = B.frame_stats([f[1] for f in flips], flips[100][1], 10, 2, slow_fps=50)
    n = len(st["frames"]) // 50
    assert st["hitches"] in (2 * n, 2 * n + 1, 2 * n + 2)
    assert st["stutters"] in (n, n + 1)
    assert st["slow_share"] == pytest.approx(st["hitches"] / len(st["frames"]), abs=1e-3)


def test_frame_stats_incomplete_window():
    flips = B.parse_flips(flip_log(n=200))
    st = B.frame_stats([f[1] for f in flips], flips[100][1], 10, 2)
    assert not st["complete"] and st["window_s"] < 4
    # Only whole seconds the flips reach: no short last bucket.
    assert st["fps_per_s"] and min(st["fps_per_s"]) >= 60


def test_fallback_start_with_the_window_past_the_run():
    # No scene edge: the start falls back to 3 s, and the run stops 4 s on.
    flips = B.parse_flips(flip_log(n=0, intro=350, intro_ms=20.0))
    i, how = B.find_start(flips, {"min_batches": 100, "fallback_s": 3})
    st = B.frame_stats([f[1] for f in flips], flips[i][1], 10, 2)
    assert how == "fallback" and not st["complete"]
    assert st["window_s"] == pytest.approx(3.98, abs=0.03)
    assert st["steady_fps"] == pytest.approx(50.0, abs=1)


def test_gpu_clock_choice_and_stats():
    t0 = 1_000_000_000
    flips = [t0 + i * 16000 for i in range(700)]
    # MONOTONIC (col 0) holds the flips; RAW is 50 s off.
    s = [
        (t0 - 2_000_000 + k * 250_000, t0 + 50_000_000 + k * 250_000, 200 if k < 20 else 1600, 40)
        for k in range(80)
    ]
    assert B.gpu_clock_for(s, flips) == 0
    assert B.gpu_clock_for([(x[1], x[0], x[2], x[3]) for x in s], flips) == 1
    assert B.gpu_clock_for([(x[1], x[1], x[2], x[3]) for x in s], flips) is None
    rows, summ = B.gpu_stats(s, 0, t0, 10, 2)
    assert summ["warmup"]["mhz_mean"] == 200 < summ["steady"]["mhz_mean"]
    assert summ["steady"]["mhz_max"] == 1600 and summ["steady"]["busy_max"] == 40
    assert summ["first_above_idle_s"] == pytest.approx(3.0)
    assert rows[0][0] == pytest.approx(-2.0)


def test_cpu_stats_hot_threads_sharing_a_core():
    t0 = 1_000_000_000
    cpu = "".join(
        "%d %d 2800000 2800000 1600000 1600000\n" % (t0 + k * 1_000_000, t0 + k * 1_000_000)
        for k in range(-2, 12)
    )
    s = B.parse_cpu_tsv(cpu)
    assert s[0][2] == [2800, 2800, 1600, 1600]
    lines = []
    for k in range(0, 11):
        t = t0 + k * 1_000_000
        # Two spinners (100 ticks a second); the second shares core 0 with
        # the first every other second. An idle thread is not hot.
        lines.append("%d %d 11 0 %d guest-main" % (t, t, 100 * k))
        lines.append("%d %d 12 %d %d nv2a-ack" % (t, t, 0 if k % 2 else 1, 95 * k))
        lines.append("%d %d 13 3 %d audio" % (t, t, 5 * k))
    th = B.parse_threads_tsv("\n".join(lines) + "\nbad\n")
    out = B.cpu_stats(s, th, 0, t0, 10, 2)
    assert out["steady"]["mhz_mean"] == 2200 and out["steady"]["mhz_min_core"] == 1600
    hot = out["hot_threads"]
    assert [h["name"] for h in hot] == ["guest-main", "nv2a-ack"]
    assert hot[0]["load_pct"] == 100.0 and hot[1]["cores"] == {0: 4, 1: 4}
    assert hot[0]["shared_share"] == hot[1]["shared_share"] == 0.5


def test_parse_nvsmi():
    rows = B.parse_nvsmi("2026/10/22 18:00:00.250, 1905, 37\nbad line\n")
    assert len(rows) == 1 and rows[0][1:] == (1905, 37)


@pytest.fixture
def analyser_tz(monkeypatch):
    """Run in a zone that is not the PC's, so a stamp read in the analysing
    machine's zone gives a wrong answer on every machine."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


PC_INFO = {"started": "2026-10-22T23:06:32", "unix_us": 1792731992925000}


def test_nvsmi_stamps_use_the_pcs_zone(analyser_tz):
    # A PC at UTC-6: 23:06:32 local is 05:06:32 UTC the next day, whatever
    # zone the analysing machine is in.
    info = dict(PC_INFO)
    off = B.pc_utc_offset(info)
    assert off == -6 * 3600
    ((us, _, _),) = B.parse_nvsmi("2026/10/22 23:06:32.925, 300, 2", off)
    assert us == info["unix_us"]
    # A zone suffix is overridden: `started` is read as the PC's wall time.
    assert B.pc_utc_offset(dict(info, started="2026-10-22T23:06:32-06:00")) == off
    assert B.pc_utc_offset(dict(info, started="2026-10-22T23:06:32+02:00")) == off
    # No zone to go on: None, never a guess.
    assert B.pc_utc_offset({}) is None
    assert B.pc_utc_offset({"started": "2026-10-22T23:06:32"}) is None
    assert B.pc_utc_offset(dict(info, started="yesterday")) is None
    assert B.pc_utc_offset(dict(info, unix_us=None)) is None


def test_pc_cpu_stats():
    # Launch at QPC 1 s, the scene starts 2 s later; 1.5 cores in warm-up,
    # then 0.5 a second.
    rows = [(float(t), 1500 * t if t <= 4 else 6000 + 500 * (t - 4)) for t in range(0, 15)]
    text = "t,total_ms\n" + "\n".join("%g,%d" % r for r in rows) + "\nbad\n"
    s = B.parse_pc_cpu(text)
    assert len(s) == 15
    out = B.pc_cpu_stats(s, 1_000_000, 3_000_000, 10, 2)
    assert out["warmup"]["cores_mean"] == 1.5
    assert out["steady"]["cores_mean"] == 0.5 and out["cores_max"] == 0.5
    assert out["total_s"] == 11.0


def test_pc_cpu_stats_short_parts():
    # One sample in the warm-up and none in the steady window: no part
    # stats, but the run's total still counts.
    s = [(1.0, 1000.0), (3.5, 3000.0)]
    out = B.pc_cpu_stats(s, 1_000_000, 3_000_000, 10, 2)
    assert "warmup" not in out and "steady" not in out and "cores_max" not in out
    assert out["total_s"] == 3.0
    assert B.pc_cpu_stats([], 1_000_000, 3_000_000, 10, 2) == {}


PROF = """# recomp-prof 1 hz 250 rounds 100 samples 300 dropped 0 mode running
thread 0 11 150 guest-main
thread 1 12 0 ?
thread 2 13 150 nv2a-ack
module 0 0x100000 /x/game.exe
s 0 0 0x1010 100
s 2 0 0x2004 90
s 0 0 0x1020 50
module 1 0x7000000 C:\\windows\\system32\\ntdll.dll
s 2 1 0x44 60
"""


def test_parse_prof_truncated_or_empty():
    # A process killed mid-dump leaves a cut last line: it is skipped.
    cut = PROF.rsplit("\n", 3)[0] + "\ns 2 1 0x4"
    p = B.parse_prof(cut)
    assert len(p["rows"]) == 3 and p["header"]["mode"] == "running"
    assert B.parse_prof("") == {"header": {}, "threads": {}, "modules": {}, "rows": []}


def test_hot_paths(monkeypatch):
    p = B.parse_prof(PROF)
    assert p["header"]["mode"] == "running" and p["threads"][2]["name"] == "nv2a-ack"
    monkeypatch.setattr(B, "symbols", lambda exe, nm=None: ([0x1000, 0x2000], ["spin", "draw"]))
    h = B.hot_paths(p, "/elsewhere/GAME.EXE")
    assert h["functions"][0] == {"name": "spin", "samples": 150, "share": 50.0}
    assert {"name": "[ntdll.dll]", "samples": 60, "share": 20.0} in h["functions"]
    assert h["modules"][0]["name"] == "game.exe"
    assert h["threads"]["nv2a-ack"]["top"][0]["name"] == "draw"


def test_pe_image_base(tmp_path):
    d = bytearray(1024)
    d[:2] = b"MZ"
    struct.pack_into("<I", d, 0x3C, 0x80)
    d[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", d, 0x80 + 24, 0x20B)
    struct.pack_into("<Q", d, 0x80 + 24 + 24, 0x140000000)
    p = tmp_path / "a.exe"
    p.write_bytes(bytes(d))
    assert B.pe_image_base(str(p)) == 0x140000000
    p.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(100))
    assert B.pe_image_base(str(p)) is None


def make_run(root, scene, host, arm_id, ms, prof=True, suffix="", busy=None, warm_ms=None):
    """A run of the arm at ms a frame; warm_ms, when given, for the first
    2 s of the scene (the warm-up)."""
    d = os.path.join(root, host, arm_id + ("" if prof else "-noprof") + suffix)
    os.makedirs(d)
    n = 800
    if warm_ms:
        k = int(2000 / warm_ms[0])
        ms = [warm_ms[0]] * k + ms * ((n - k) // len(ms) + 1)
    with open(os.path.join(d, "game.log"), "w") as f:
        f.write(flip_log(scene_ms=ms, n=n))
    a = B.find_arm(scene, host, arm_id)
    info = B.base_info(scene, dict(scene["hosts"][host], _name=host), a, prof, "/none", {}, 0, [])
    if busy:
        info["busy_during"] = busy
    B.write_info(d, info)
    return B.collect(d)


def test_collect_and_analyse(scene, tmp_path):
    root = str(tmp_path / "out")
    make_run(root, scene, "mac", "s1-dsp0-43", [16.0])
    make_run(root, scene, "mac", "s1-dsp0-43", [15.0], prof=False)
    make_run(root, scene, "deck", "s2-dsp1-169", [25.0])
    rows, overhead, arms = B.analyse(root)
    assert len(rows) == 3 and len(arms) == 3
    assert overhead and overhead[0]["host"] == "mac" and overhead[0]["cost_pct"] > 0
    with open(os.path.join(root, "summary.csv")) as f:
        got = list(csv.DictReader(f))
    # The arm column is the arm; prof says whether the sampler ran.
    assert sorted((r["arm"], r["prof"]) for r in got) == [
        ("s1-dsp0-43", "0"),
        ("s1-dsp0-43", "250"),
        ("s2-dsp1-169", "100"),  # the deck host's own rate
    ]
    # The axes follow host, backend, arm and prof; the repeat comes after them.
    cols = list(got[0])
    assert cols[4].startswith("axis_") and "rep" in cols
    with open(os.path.join(root, "summary.json")) as f:
        js = json.load(f)
    assert js["runs"][0]["fps_per_s"] and js["verdict_rules"]
    assert "table" not in js and "steady_fps" in B.table(rows)
    assert os.path.isfile(os.path.join(root, "summary-arms.csv"))


def test_analyse_repeats_verdicts_and_disturbed(scene, tmp_path):
    root = str(tmp_path / "out")
    arm = "s1-dsp0-43"
    # Three repeats without the sampler: two good, one slow in its warm-up.
    make_run(root, scene, "deck", arm, [16.7], prof=False)
    make_run(root, scene, "deck", arm, [16.9], prof=False, suffix="-r2")
    make_run(root, scene, "deck", arm, [16.7], prof=False, suffix="-r3", warm_ms=[22.0])
    make_run(root, scene, "deck", arm, [17.0])
    # A Mac run another process disturbed stays out of the arm's figures.
    make_run(root, scene, "mac", arm, [16.7], busy=[[10, 99.0, "idea"]])
    make_run(root, scene, "mac", arm, [16.7], suffix="-r2")
    rows, overhead, arms = B.analyse(root)
    deck = [r for r in rows if r["host"] == "deck" and not r["prof"]]
    assert sorted(r["rep"] for r in deck) == [1, 2, 3]
    assert sorted(r["verdict"] for r in deck) == ["bad", "good", "good"]
    assert {r["verdict"] for r in rows if r["host"] == "mac"} == {""}
    a = next(x for x in arms if x["host"] == "deck" and not x["prof"])
    assert (a["n"], a["good"], a["bad"]) == (3, 2, 1)
    assert a["steady_fps_min"] <= a["steady_fps_median"] <= a["steady_fps_max"]
    assert a["steady_fps_mad"] >= 0 and a["axis_scale"] == 1
    m = next(x for x in arms if x["host"] == "mac")
    assert m["n"] == 1
    assert next(r for r in rows if r["host"] == "mac" and r["rep"] == 1)["disturbed"]
    _, _, arms_all = B.analyse(root, include_disturbed=True)
    assert next(x for x in arms_all if x["host"] == "mac")["n"] == 2
    o = next(x for x in overhead if x["host"] == "deck")
    assert (o["n_off"], o["n_on"]) == (3, 1) and "cost_pct_good" in o
    assert o["cost_pct_good"] > 0


def test_prof_rate_groups_runs():
    # The sampler's rate is the run's prof value: two rates, two groups.
    r = {"prof": True, "env": {"RECOMP_TRACE": "flip,pacing=all,prof=100,prof_delay=40"}}
    assert B.prof_hz(r) == 100
    assert B.prof_hz({"prof": True, "scene": {"profile": {"hz": 250}}}) == 250
    assert B.prof_hz({"prof": False, "env": r["env"]}) == 0
    # Without the env, the host's profile wins over the scene's.
    sc = {"profile": {"hz": 250}, "hosts": {"deck": {"profile": {"hz": 100}}}}
    assert B.prof_hz({"prof": True, "host": "deck", "scene": sc}) == 100


def test_write_windows_and_info(scene, tmp_path):
    out = str(tmp_path / "pc")
    with pytest.raises(B.MatrixError, match="exe_name"):
        B.write_windows(scene, {"dir": "D:\\b", "app": "Game"}, out)
    ids = B.write_windows(scene, {"dir": "D:\\b", "app": "Game"}, out, "game_recomp")
    assert len(ids) == 12
    with open(os.path.join(out, "run-arm.ps1"), newline="") as f:
        ps1 = f.read()
    # The game's exe and the host's backend, not constants of this CLI.
    assert "\\game_recomp.exe" in ps1 and "RECOMP_PB_BACKEND = 'vulkan'" in ps1
    assert "RECOMP_SAVE_DIR" in ps1 and "[int]$Rep = 1" in ps1
    # collect names the profile's exe module from it.
    assert "exe = (Split-Path -Leaf $exe);" in ps1
    assert "\r\n" in ps1 and "$S = 'D:\\b'" in ps1 and "Programs\\Game" in ps1
    # One mirrored hdd copy for every arm, not a full copy per run.
    assert "robocopy" in ps1 and "Copy-Item -Recurse" not in ps1
    # PowerShell variables are case-blind: two spellings of one name are one
    # variable ($a in the quiet loop once overwrote the arm dir $A).
    spell = {}
    for v in re.findall(r"\$([A-Za-z_]\w*)", ps1):
        spell.setdefault(v.lower(), set()).add(v)
    assert not [s for s in spell.values() if len(s) > 1]
    # Nor may the script assign to a PowerShell automatic variable: $input,
    # $host and the rest are the shell's, and a loop variable named so is
    # silently another value.
    auto = {
        "_",
        "args",
        "consolefilename",
        "error",
        "event",
        "eventargs",
        "eventsubscriber",
        "executioncontext",
        "foreach",
        "home",
        "host",
        "input",
        "lastexitcode",
        "matches",
        "myinvocation",
        "nestedpromptlevel",
        "ofs",
        "pid",
        "profile",
        "psboundparameters",
        "pscmdlet",
        "pscommandpath",
        "psculture",
        "psdebugcontext",
        "pshome",
        "psitem",
        "psscriptroot",
        "pssenderinfo",
        "psuiculture",
        "psversiontable",
        "pwd",
        "sender",
        "shellid",
        "stacktrace",
        "switch",
        "this",
    }
    assigned = {
        v.lower()
        for v in re.findall(r"\$([A-Za-z_]\w*)\s*(?:=[^=]|\+=|-=)", ps1)
        + re.findall(r"foreach\s*\(\s*\$([A-Za-z_]\w*)\s+in\b", ps1, re.I)
        + re.findall(r"\[[A-Za-z]+\]\$([A-Za-z_]\w*)", ps1)
        + re.findall(r"\$([A-Za-z_]\w*)\s*,(?=[^=\n]*=[^=])", ps1)
    }
    assert not assigned & auto, assigned & auto
    # The quiet wait compares cores in use (CPU seconds over a 5 s sleep)
    # with a share of one core, not with a percentage it can never reach.
    assert "/ 5) -gt %g " % (B.BUSY_PCT / 100) in ps1
    assert "-gt %g " % B.BUSY_PCT not in ps1
    # A run that did not get its arm stops before the game starts.
    assert "not applied" in ps1 and "exit 3" in ps1
    assert ps1.index("exit 3") < ps1.index("Start-Process $exe")
    with open(os.path.join(out, "arms", "s2-dsp1-169", "env.txt")) as f:
        env = f.read()
    assert "prof_out=@R\\prof.txt" in env and "RECOMP_APU_DSP=1" in env
    # What the PC brings back: a BOM'd run-info.json without the scene.
    d = tmp_path / "back"
    d.mkdir()
    (d / "run-info.json").write_text(
        "\ufeff" + json.dumps({"arm": "s2-dsp1-169-noprof", "host": "windows"})
    )
    B.windows_info(str(d), scene, "/x/game.exe")
    info = json.loads((d / "run-info.json").read_text())
    assert info["values"]["scale"] == 2 and info["symbols"] == "/x/game.exe" and info["scene"]


def test_busy_processes():
    ps = "%CPU COMM\n 99.0 node\n 60.0 game_recomp\n 10.0 sshd\n"
    assert B.busy_processes(ps, ("game_recomp",)) == [(99.0, "node")]


def test_quiet_ignore(scene):
    deck = dict(scene["hosts"]["deck"], quiet_ignore=["steamwebhelper"])
    got = B.quiet_ignore(scene, deck, "game_recomp.exe")
    assert got == ("game_recomp", "steamwebhelper", "wineserver", "umu-run")
    sc = dict(scene, scene=dict(scene["scene"], quiet_ignore=["a", "b"]))
    assert B.quiet_ignore(sc, {}, "game_recomp.exe")[:2] == ("a", "b")


def test_plan_cli(scene, tmp_path, capsys):
    p = tmp_path / "scene.toml"
    p.write_text(SCENE)
    assert B.main(["plan", str(p), "--host", "deck", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["deck"]) == 8
    assert B.main(["plan", str(p), "--host", "nope"]) == 2


def test_hot_paths_renamed_symbols_and_no_wait(monkeypatch):
    # Under Wine the profile samples every thread (mode all) and the symbol
    # copy has another name than the exe the game ran as.
    p = B.parse_prof(PROF.replace("mode running", "mode all"))
    monkeypatch.setattr(B, "symbols", lambda exe, nm=None: ([0x1000, 0x2000], ["spin", "draw"]))
    h = B.hot_paths(p, "/syms/game-t1234.exe", run_exe="C:\\x\\game.exe")
    assert h["functions"][0]["name"] == "spin"
    assert [f["name"] for f in h["functions_no_wait"]] == ["spin", "draw"]
    assert h["functions_no_wait"][0]["share"] == 62.5
    assert B.hot_paths(p, "/syms/game-t1234.exe")["functions"][0]["name"] == "[game.exe]"


def test_summary_top_function_mode_all(scene, tmp_path):
    r = make_run(str(tmp_path), scene, "deck", "s1-dsp0-43", [16.0])
    fns = [{"name": "[mmdevapi.dll]", "share": 30.0}, {"name": "sub_1", "share": 20.0}]
    r["profile"] = {"header": {"mode": "all"}, "functions_no_wait": fns, "functions": fns}
    assert B.summary_row(r)["top_function"] == "sub_1"
    r["profile"]["header"]["mode"] = "running"
    assert B.summary_row(r)["top_function"] == "[mmdevapi.dll]"
    # Windows: "running" still lands in ntdll's waits.
    r["profile"]["functions"] = [{"name": "[ntdll.dll]", "share": 95.0}] + fns
    assert B.summary_row(r)["top_function"] == "sub_1"
    r["profile"]["functions"] = [{"name": "[win32u.dll]", "share": 90.0}] + fns
    assert B.summary_row(r)["top_function"] == "sub_1"
    # All waits: the no-wait view is empty, so the wait module shows.
    r["profile"]["functions_no_wait"] = []
    assert B.summary_row(r)["top_function"] == "[win32u.dll]"


def make_pc_run(root, scene, gpu=True, cpu_tsv=False, **info_extra):
    """A PC run: QPC flips, nvidia-smi stamps in the PC's local time (UTC-6)
    and cpu.csv seconds from the launch."""
    d = os.path.join(root, "windows", "s1-dsp0-43")
    os.makedirs(d)
    qpc0 = 1_000_000_000  # launch; the flips start here, the scene 2 s later
    with open(os.path.join(d, "game.log"), "w") as f:
        f.write(flip_log(scene_ms=[16.0], n=800, t0_us=qpc0))
    d_us = qpc0 - PC_INFO["unix_us"]
    local = datetime.timezone(datetime.timedelta(hours=-6))
    lines = []
    for k in range(0, 40):
        q = qpc0 + k * 500_000
        dt = datetime.datetime.fromtimestamp((q - d_us) / 1e6, local)
        lines.append("%s, 1500, 30" % dt.strftime("%Y/%m/%d %H:%M:%S.%f")[:-3])
    if gpu:
        with open(os.path.join(d, "gpu.csv"), "w") as f:
            f.write("\n".join(lines) + "\n")
    # 1.25 cores the whole run.
    with open(os.path.join(d, "cpu.csv"), "w") as f:
        f.write("t,total_ms\n" + "".join("%d,%d\n" % (t, 1250 * t) for t in range(25)))
    if cpu_tsv:
        with open(os.path.join(d, "cpu.tsv"), "w") as f:
            f.write("not a sampler log\n")
    a = B.find_arm(scene, "windows", "s1-dsp0-43")
    info = B.base_info(
        scene, dict(scene["hosts"]["windows"], _name="windows"), a, False, "/none", {}, 0, []
    )
    info.update(qpc_us=qpc0, **PC_INFO)
    info.update(info_extra)
    B.write_info(d, info)
    return B.collect(d)


def test_collect_pc_run(scene, tmp_path, analyser_tz):
    r = make_pc_run(str(tmp_path), scene)
    assert r["gpu"]["source"] == "nvidia-smi" and r["gpu"]["utc_offset_s"] == -6 * 3600
    assert r["cpu"]["source"] == "process cpu time"
    row = B.summary_row(r)
    assert row["gpu_mhz_steady"] == 1500
    assert row["cpu_cores_steady"] == 1.25


def test_collect_pc_run_without_started(scene, tmp_path):
    r = make_pc_run(str(tmp_path), scene, started=None)
    assert r["gpu"] == {"source": "nvidia-smi", "error": "no started stamp for the PC's zone"}
    assert B.summary_row(r)["gpu_mhz_steady"] is None


def test_collect_pc_cpu_tsv_wins(scene, tmp_path):
    # cpu.csv is the PC's fallback; a sampler log, when there is one, is read
    # instead.
    r = make_pc_run(str(tmp_path), scene, gpu=False, cpu_tsv=True)
    assert (r.get("cpu") or {}).get("source") != "process cpu time"


def test_cpu_sampler_skips_itself():
    # Its own command line names the exe, so it once logged only itself.
    compile(B.CPU_SAMPLER, "cpu_sampler.py", "exec")
    assert "p == me" in B.CPU_SAMPLER and "os.getpid()" in B.CPU_SAMPLER
