"""bench_hw.py: the hw, run and build blocks from recorded tool output,
each fixture carrying the identifiers that must never reach a block."""

import argparse
import json
import os
import shutil
import subprocess

import pytest

from xboxrecomp_cli import bench_hw as H
from xboxrecomp_cli import bench_matrix as B

# A host name, serials, a UUID, a MAC, an IP and paths.
PLANTS = [
    "myhost-7",
    "C02XK1ABCDEF",
    "FVFG1234ABCD",
    "8D2E6F0A-1B2C-4D5E-9F00-123456789ABC",
    "a4:83:e7:12:34:56",
    "192.0.2.17",
    "/Us" + "ers/someone",
    "/ho" + "me/someone",
]


def no_plants(hw):
    text = json.dumps(hw)
    for p in PLANTS:
        assert p not in text, p


MAC_OUT = {
    "machdep.cpu.brand_string": "Apple M2 Pro",
    "hw.physicalcpu": "12",
    "hw.logicalcpu": "12",
    "hw.perflevel0.physicalcpu": "8",
    "hw.perflevel1.physicalcpu": "4",
    "hw.cpufrequency_max": "",
    "hw.model": "Mac14,9",
    "hw.memsize": "34359738368",
    "kern.hostname": "myhost-7",
}
MAC_DISPLAYS = {
    "SPDisplaysDataType": [
        {
            "_name": "myhost-7 GPU",
            "sppci_model": "Apple M2 Pro",
            "sppci_cores": "19",
            "spdisplays_vendor": "sppci_vendor_Apple",
            "spdisplays_mtlgpufamilysupport": "spdisplays_metal3",
            "spdisplays_ndrvs": [
                {
                    "_name": "Studio Display",
                    "_spdisplays_display-serial-number": "FVFG1234ABCD",
                    "_spdisplays_displayID": "8D2E6F0A-1B2C-4D5E-9F00-123456789ABC",
                    "_spdisplays_resolution": "2560 x 1440 @ 60.00Hz",
                    "spdisplays_main": "spdisplays_yes",
                }
            ],
        }
    ]
}


def mac_run(cmd):
    if cmd[0] == "sysctl":
        return MAC_OUT.get(cmd[-1], "") + "\n"
    if cmd[0] == "sw_vers":
        return {"-productName": "macOS", "-productVersion": "26.1", "-buildVersion": "25B78"}[
            cmd[1]
        ]
    if cmd[0] == "uname":
        return "25.1.0\n"
    if cmd[0] == "system_profiler":
        assert cmd[1] == "SPDisplaysDataType"
        return json.dumps(MAC_DISPLAYS)
    if cmd[:2] == ["pmset", "-g"] and len(cmd) == 2:
        return " lowpowermode         0\n hibernatefile        /Us" + "ers/someone/sleepimage\n"
    if cmd[:3] == ["pmset", "-g", "batt"]:
        return "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\n"
    raise AssertionError(cmd)


def test_capture_macos():
    hw = H.capture_macos(mac_run, backend="metal")
    no_plants(hw)
    assert hw["cpu"]["model"] == "Apple M2 Pro" and hw["cpu"]["p_cores"] == 8
    assert hw["ram_gb"] == 32 and hw["device"]["model"] == "Mac14,9"
    assert hw["gpu"][0]["driver"] == "Metal 3" and hw["gpu"][0]["vendor"] == "Apple"
    assert hw["display"] == {"refresh_hz": 60, "width": 2560, "height": 1440}
    assert hw["power"]["source"] == "AC" and hw["power"]["profile"] == "normal"
    assert hw["os"]["name"] == "macOS" and hw["backend"]["name"] == "metal"


DECK_OUT = """==xbr== os-release
NAME="SteamOS"
VERSION_ID=3.7.13
BUILD_ID=20250630.1
HOME_URL="https://myhost-7.local/"
==xbr== kernel
6.11.11-valve19-1-neptune-611
==xbr== product
Jupiter
==xbr== cpuinfo
model name	: AMD Custom APU 0405
==xbr== lscpu
Core(s) per socket:              4
Socket(s):                       1
Thread(s) per core:              2
CPU(s):                          8
CPU max MHz:                     3500.0000
==xbr== cpu-max
3500000
==xbr== cpu-hw-max
3500000
==xbr== governor
schedutil
==xbr== meminfo
MemTotal:       16252928 kB
==xbr== vulkan
	deviceName         = AMD Custom GPU 0405 (RADV VANGOGH)
	driverName         = radv
	driverInfo         = Mesa 24.3.0
	vendorID           = 0x1002
	deviceUUID         = 8d2e6f0a-1b2c-4d5e-9f00-123456789abc
==xbr== lspci
VGA compatible controller [0300]: AMD [1002:163f] VanGogh [AMD Custom GPU 0405] (rev ae)
==xbr== vram
1073741824
==xbr== sclk
0: 200Mhz
1: 1600Mhz *
==xbr== od
OD_SCLK:
0: 200Mhz
1: 1040Mhz
OD_RANGE:
SCLK:     200Mhz       1600Mhz
==xbr== perf
manual
==xbr== tdp
15000000
==xbr== xrandr
   800x1280      60.00*+  myhost-7 a4:83:e7:12:34:56 192.0.2.17
==xbr== power
Mains 1 Not Charging
Battery 1 Full
"""


def test_capture_linux_deck():
    seen = []

    def run(script):
        seen.append(script)
        return DECK_OUT

    hw = H.capture_linux(run, "d3d11", proton="/ho" + "me/someone/compat/GE-Proton10-25/")
    no_plants(hw)
    # The script reads none of the identifying sources.
    for bad in ("hostname", "product_serial", "product_uuid", "board_serial", "ip ", "ifconfig"):
        assert bad not in seen[0]
    assert hw["os"] == {
        "name": "SteamOS",
        "version": "3.7.13",
        "build": "20250630.1",
        "kernel": "6.11.11-valve19-1-neptune-611",
    }
    assert hw["cpu"]["cores"] == 4 and hw["cpu"]["threads"] == 8
    assert hw["gpu"][0]["model"] == "AMD Custom GPU 0405 (RADV VANGOGH)"
    assert hw["gpu"][0]["driver"] == "radv Mesa 24.3.0" and hw["gpu"][0]["max_mhz"] == 1600
    assert hw["power"]["gpu_mhz_cap"] == 1040 and hw["power"]["tdp_w"] == 15.0
    assert hw["power"]["source"] == "AC" and hw["power"]["perf_level"] == "manual"
    assert hw["display"]["refresh_hz"] == 60 and hw["ram_gb"] == 15.5
    assert hw["backend"]["translation"] == "GE-Proton10-25"


def test_capture_survives_no_output():
    hw = H.capture_linux(lambda s: "", None)
    assert hw["schema"] == 1 and hw["gpu"] == [] and hw["cpu"]["model"] is None


def test_sanitize_drops_unknown_keys_and_cleans_text():
    hw = H.sanitize_hw(
        {
            "os": {"name": "Windows 11 <script>", "hostname": "myhost-7"},
            "device": {"model": "x" * 200, "serial": "C02XK1ABCDEF"},
            "gpu": {"model": "RTX 4070", "uuid": "GPU-8D2E6F0A", "max_mhz": "3105"},
            "ram_gb": "31.9",
            "net": {"ip": "192.0.2.17"},
        }
    )
    no_plants(hw)
    assert hw["os"]["name"] == "Windows 11 ?script?"
    assert len(hw["device"]["model"]) == H.TEXT_MAX
    assert hw["gpu"] == [
        {"model": "RTX 4070", "vendor": None, "driver": None, "vram_mb": None, "max_mhz": 3105}
    ]
    assert hw["ram_gb"] == 31.9 and "net" not in hw


# What run-arm.ps1's ConvertTo-Json writes for a one-GPU PC: a one-element
# @() array of hashtables, numbers as numbers, a missing power source null.
PS1_RUN_INFO = """{
  "hw": {
    "schema": 1,
    "os": {"name": "Microsoft Windows 11 Pro", "version": "10.0.26100", "build": "26100"},
    "device": {"model": "Micro-Star International Co., Ltd. MS-7D25"},
    "cpu": {"model": "13th Gen Intel(R) Core(TM) i7-13700K", "cores": 16, "threads": 24,
            "max_mhz": 3400},
    "gpu": [{"model": "NVIDIA GeForce RTX 4070", "vendor": "NVIDIA", "driver": "576.88",
             "max_mhz": "3105"}],
    "ram_gb": 31.8,
    "display": {"refresh_hz": 144, "width": 2560, "height": 1440},
    "power": {"source": "ac", "profile": "Balanced"},
    "backend": {"name": "d3d11"}
  },
  "build": {"version": "20261023.0049-c5d8e7b0-t4ec1a73", "game_sha": "c5d8e7b0",
            "toolkit_sha": "4ec1a73"},
  "present_fullscreen": "1",
  "refresh_hz": 144
}"""


def test_ps1_hw_shape():
    info = json.loads(PS1_RUN_INFO)
    hw = H.sanitize_hw(info["hw"])
    assert hw["cpu"]["threads"] == 24 and hw["gpu"][0]["max_mhz"] == 3105
    assert hw["os"]["build"] == "26100" and hw["display"]["refresh_hz"] == 144
    assert set(hw) == set(H.empty_hw())
    b = H.build_block(version=info["build"]["version"], game_sha="c5d8e7b0", toolkit_sha="4ec1a73")
    assert b["version_public"] == "20261023.0049"


@pytest.mark.parametrize(
    "version, public",
    [
        ("20261023.0049-c5d8e7b0-t4ec1a73", "20261023.0049"),
        ("20261023.0049", "20261023.0049"),
        ("c5d8e7b0", None),
        ("dev", None),
        ("2026.10.23", None),
        (None, None),
    ],
)
def test_version_public_takes_only_the_date_and_sequence(version, public):
    assert H.version_public(version) == public


@pytest.fixture
def ps1(tmp_path):
    scene = B.load_scene(_scene_file(tmp_path))
    out = str(tmp_path / "pc")
    B.write_windows(scene, {"dir": "D:\\b", "app": "Game"}, out, "game_recomp")
    with open(os.path.join(out, "run-arm.ps1"), newline="") as f:
        return os.path.join(out, "run-arm.ps1"), f.read()


def _scene_file(tmp_path):
    p = tmp_path / "scene.toml"
    p.write_text(
        'arm_id = "s{scale}"\n[scene]\nname = "t"\nrun_s = 30\nduration_s = 10\n'
        "[scene.start]\nmin_batches = 1\nhold_flips = 1\nfallback_s = 3\n"
        '[[axis]]\nname = "scale"\nvalues = [1]\nconfig = "render.scale"\n'
        '[hosts.pc]\nkind = "windows"\nbackend = "d3d11"\n'
    )
    return str(p)


def test_ps1_writes_hw_and_build(ps1):
    _, text = ps1
    assert "hw = $hwInfo; build = $build;" in text
    assert "backend = @{ name = 'd3d11' }" in text
    # Named fields only: never the computer's name, a serial or a UUID.
    code = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    for bad in ("$cs.Name", "SerialNumber", "UUID", "Win32_BIOS", "Win32_NetworkAdapter"):
        assert bad not in code
    assert "$I\\versions\\$v\\manifest.json" in text


@pytest.mark.skipif(not shutil.which("pwsh"), reason="no pwsh")
def test_ps1_parses(ps1):
    path, _ = ps1
    r = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            "'%s', [ref]$null, [ref]$e); $e.Count" % path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.stdout.strip() == "0", r.stdout + r.stderr


def test_run_block_window_and_sampler():
    log = (
        "[ENHANCE] render.scale=2 present.pacing=sdl present.surface=metal\n[PRESENT] x vsync on\n"
    )
    rb = H.run_block({"present_fullscreen": "1", "run_order": {"index": 2}}, log, 250)
    assert rb == {
        "sampler_hz": 250,
        "pacing": "sdl",
        "present_mode": "metal",
        "vsync": "on",
        "window": "fullscreen",
        "order": {"index": 2},
    }
    # A run from before the field reads unknown, never a guessed windowed.
    assert H.run_block({}, "", 0, "spin")["window"] == "unknown"
    assert H.run_block({"present_fullscreen": None}, "", 0)["window"] == "windowed"
    assert H.run_block({"present_fullscreen": "0"}, "", 0)["window"] == "windowed"
    assert H.run_block({}, "", 0, "spin")["pacing"] == "spin"


def test_dxvk_version():
    assert H.dxvk_version("info:  DXVK: v2.6.1-GE\n") == "DXVK v2.6.1-GE"
    assert H.dxvk_version("") is None


def test_build_block_and_manifest(tmp_path):
    d = tmp_path / "versions" / "20261023.0049-c5d8e7b0-t4ec1a73"
    d.mkdir(parents=True)
    exe = d / "game.exe"
    exe.write_bytes(b"MZ")
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "version": "20261023.0049-c5d8e7b0-t4ec1a73",
                "sources": {
                    "skates": {"commit": "c5d8e7b0"},
                    "toolkit": {"commit": "4ec1a73"},
                    "cli": {"commit": "84fd482"},
                },
            }
        )
    )
    m = H.find_manifest(str(exe))
    assert m == str(d / "manifest.json")
    b = H.build_block(m, exe_sha256="ab")
    assert (b["game_sha"], b["toolkit_sha"], b["version_public"]) == (
        "c5d8e7b0",
        "4ec1a73",
        "20261023.0049",
    )
    assert H.build_block() is None


def test_run_refuses_without_a_build(tmp_path):
    a = argparse.Namespace(build_version=None, game_sha=None, toolkit_sha=None)
    host = {"_name": "mac", "kind": "local"}
    with pytest.raises(B.MatrixError, match="--build-version"):
        B.run_build(a, host, {"exe": str(tmp_path / "none")})
    a.build_version = "20261023.0001"
    with pytest.raises(B.MatrixError, match="--game-sha"):
        B.run_build(a, host, {"exe": str(tmp_path / "none")})
    a.game_sha, a.toolkit_sha = "c5d8e7b0", "4ec1a73"
    assert B.run_build(a, host, {"exe": str(tmp_path / "none")})["version_public"] == (
        "20261023.0001"
    )


def test_collect_writes_run_hw_and_build(tmp_path):
    scene = B.load_scene(_scene_file(tmp_path))
    d = tmp_path / "run"
    d.mkdir()
    (d / "game.log").write_text(
        "".join(
            "[PACING] flip %d t_us %d batches 500\n" % (k, 10**9 + k * 16000) for k in range(900)
        )
    )
    (d / "umu.log").write_text("info:  DXVK: v2.6.1\n")
    host = dict(scene["hosts"]["pc"], _name="pc")
    info = B.base_info(scene, host, B.arms(scene)[0], False, "/none", {}, 0, [])
    info["hw"] = {"cpu": {"model": "x"}, "backend": {"name": "d3d11"}, "device": {"x": "myhost-7"}}
    info["build"] = {
        "version": "20261023.0049-c5d8e7b0-t4ec1a73",
        "game_sha": "c5",
        "toolkit_sha": "4e",
    }
    B.write_info(str(d), info)
    r = B.collect(str(d))
    assert r["hw"]["backend"]["translation_version"] == "DXVK v2.6.1"
    assert r["build"]["version_public"] == "20261023.0049"
    assert r["run"]["window"] == "windowed" and r["run"]["sampler_hz"] == 0
    no_plants(r["hw"])
