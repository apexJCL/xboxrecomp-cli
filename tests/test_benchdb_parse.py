"""benchdb's parsers on synthetic copies of what the bench writes."""

import json

from benchdb_util import (
    CLI_SHA,
    GAME_SHA,
    GOLDEN_TXT,
    MAC_INFO,
    PROTON_INFO,
    SHA_A,
    SHA_B,
    TK_SHA,
    flip_log,
)

from xboxrecomp_cli.benchdb import config, parse


def test_iso_utc_offsets():
    assert parse.iso_utc("2026-10-25T07:21:35-07:00") == "2026-10-25T14:21:35Z"
    assert parse.iso_utc("2026-10-25T07:45:07-0700") == "2026-10-25T14:45:07Z"
    assert parse.iso_utc("2026-10-25T14:21:34Z") == "2026-10-25T14:21:34Z"
    # Local time with no offset: only with the host's offset.
    assert parse.iso_utc("2026-10-25T01:30:48") is None
    assert parse.iso_utc("2026-10-25T01:30:48", -7 * 3600) == "2026-10-25T08:30:48Z"
    assert parse.iso_utc("not a time") is None


def test_run_info_proton():
    i = parse.run_info(PROTON_INFO)
    assert i["format"] == "proton"
    assert i["uname"] == "Box1"
    assert i["time_raw"] == "2026-10-25T07:21:35-07:00"
    assert i["exe_sha256"] == SHA_A
    assert i["exe"] == "build-win/demo_recomp.exe"
    assert i["flips"] == 2063 and i["flips_exact"] == 1
    assert i["trees"]["game"]["sha"] == GAME_SHA
    assert i["trees"]["game"]["branch"] == "main"
    assert i["trees"]["toolkit"]["sha"] == TK_SHA
    assert i["trees"]["cli"]["sha"] == CLI_SHA
    assert i["shipped"]["at"] == "2026-10-25T14:21:34Z"
    assert i["load_start"] == 0.08 and i["load_end"] == 0.44 and i["ncpu"] == 16
    assert i["cpu_stall_pct"] == 0.7
    assert i["busy"] == 0
    assert i["limit_secs"] == 40.0
    assert i["lock_wait_secs"] == 3.0
    assert i["problems"] == []


def test_run_info_windows():
    from benchdb_util import WINDOWS_INFO

    i = parse.run_info(WINDOWS_INFO)
    assert i["format"] == "windows"
    assert i["uname"] == "BOX1"
    assert i["exe_sha256"] == SHA_B
    assert i["secs"] == 41.0 and i["exit_code"] == 0
    assert i["load_unit"] == "%"
    assert i["trees"]["game"]["dirty"] == 1 and i["trees"]["game"]["paths"] == 4
    assert i["trees"]["game"]["branch"] == "feat/x"
    assert i["problems"] == []


def test_run_info_mac():
    i = parse.run_info(MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws="/srv/w"))
    assert i["format"] == "mac"
    assert i["backend"] == "metal"
    assert i["exe_sha256"] == SHA_A
    assert i["trees"]["game"]["sha"] == GAME_SHA and i["trees"]["game"]["dirty"] == 0
    assert i["flips"] == 2312 and i["secs"] == 39.4 and i["stop"] == "done"
    assert i["load_start"] == 2.6 and i["ncpu"] == 14
    assert i["problems"] == []


def test_run_info_mac_dirty_tree():
    t = MAC_INFO.format(exe=SHA_A, game=GAME_SHA, ws="/srv/w").replace(
        GAME_SHA + " clean", GAME_SHA[:12] + " (5 paths dirty)"
    )
    g = parse.run_info(t)["trees"]["game"]
    assert g["dirty"] == 1 and g["paths"] == 5 and g["sha"] == GAME_SHA[:12]


def test_run_info_problems_never_raise():
    i = parse.run_info("junk\nflips: lots\nload: ???\n")
    assert i["format"] == "unknown"
    assert "run-info in no known format" in i["problems"]
    assert "no exe line" in i["problems"]
    assert "no flips line" in i["problems"]
    # A Proton run with no built: lines is partial, not dropped.
    t = "\n".join(x for x in PROTON_INFO.splitlines() if not x.startswith("built:"))
    assert "no built: lines" in parse.run_info(t)["problems"]


def test_host_key_markers_then_names():
    import re

    cfg = config.Config()
    from xboxrecomp_cli.benchdb.config import Host

    cfg.hosts = {
        "hostA": Host("hostA", "linux-proton", "hosta", [], ["Box1"], "", ""),
        "hostB": Host(
            "hostB", "windows", "hostb", [re.compile(r"^os: +windows", re.M)], [], "", ""
        ),
    }
    from benchdb_util import WINDOWS_INFO

    assert parse.host_key(parse.run_info(PROTON_INFO), PROTON_INFO, cfg) == ("hostA", "name")
    assert parse.host_key(parse.run_info(WINDOWS_INFO), WINDOWS_INFO, cfg) == ("hostB", "marker")
    # Names match exactly: BOX1 is not Box1.
    cfg.hosts["hostB"].markers = []
    assert parse.host_key(parse.run_info(WINDOWS_INFO), WINDOWS_INFO, cfg)[0] is None


def test_golden_txt():
    g = parse.golden_txt(GOLDEN_TXT)
    assert g["verdict"] == ("menu", "pass")
    assert g["golden"] == "pass"
    assert len(g["frames"]) == 2
    a, b = g["frames"]
    assert a["result"] == "EXACT" and a["frame"] == "menu/menu-fade" and a["ref"] == "d3d11-proton"
    assert a["dump"] == 5 and a["present"] == 301
    assert b["result"] == "CLOSE" and b["ref"] == "metal"
    assert b["mae_r"] == 0.384 and b["bad_pct"] == 0.8766 and b["tile_pct"] == 23.7
    assert g["problems"] == []
    assert parse.golden_txt("end: x\n")["problems"] == ["golden.txt has no verdict line"]
    assert parse.golden_txt("verdict: menu WHAT\n")["problems"]


def test_sessions_tsv():
    text = (
        "# a comment\n"
        "2026-10-25 07:34:10\tgolden\ttests=nobuild\tonly=menu\t"
        "menu=20261025-073313:pass\trc=0\thost=posix\n"
        "2026-10-25 07:35:07\tgolden\ttests=pass\tonly=\t"
        "menu=20261025-073411-metal-menu:REGRESSION\tstory=20261025-073500:pass\trc=unknown\n"
    )
    s = parse.sessions_tsv(text)
    assert len(s) == 2
    assert s[0]["kind"] == "golden" and s[0]["tests"] == "nobuild" and s[0]["only"] == "menu"
    assert s[0]["scen"] == {"menu": ("20261025-073313", "pass")}
    assert s[0]["rc"] == 0 and s[0]["host"] == "posix"
    assert s[1]["scen"]["menu"] == ("20261025-073411-metal-menu", "REGRESSION")
    assert s[1]["rc"] is None
    u = parse.line_uuid("demo", s[0]["line"])
    assert u.startswith("tsv:demo:") and len(u.split(":")[2]) == 32
    assert u == parse.line_uuid("demo", s[0]["line"])
    assert u != parse.line_uuid("demo", s[1]["line"])
    assert parse.tsv_time("2026-10-25 07:34:10").endswith("Z")
    assert parse.tsv_time("bad") is None


def test_tests_pass_and_small_records():
    t = parse.tests_pass("key: k1\npassed: 2026-10-14 17:26:15\nhost: hostA\ncli: c\n")
    assert t["key"] == "k1" and t["host"] == "hostA" and t["cli"] == "c"
    p = parse.patch_json(
        {
            "version": "v2",
            "base_version": "v1",
            "changed": [{"size": 10}],
            "new": [{"size": 5}],
            "kept": [{"size": 100}],
            "deleted": ["x"],
        }
    )
    assert p["payload_bytes"] == 15 and p["full_bytes"] == 115
    assert p["files"] == {"changed": 1, "new": 1, "deleted": 1, "kept": 1}
    assert parse.patch_json("nope") is None
    b = parse.bundle_index({"f" * 64: {"version": "v1", "size": 3}, "x": "junk"})
    assert b == [{"version": "v1", "size": 3, "manifest_sha256": "f" * 64}]


def test_load_json_bad(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json")
    assert parse.load_json(str(p)) is None
    p.write_text(json.dumps({"a": 1}))
    assert parse.load_json(str(p)) == {"a": 1}


def test_flip_stats(tmp_path):
    p = tmp_path / "game-stdio.log"
    p.write_text(flip_log(200, 16))
    s = parse.flip_stats(str(p))
    assert s["flips"] == 200
    assert s["ms_p50"] == 16.0
    assert s["ms_max"] == 80.0
    assert s["hitches"] == 1
    assert s["coarse"] is True
    # The [GPU] clock starts at 0 at flip 1: no boot time from it.
    assert s["first_s"] is None
    assert 50 < s["flips_per_s"] < 65
    p.write_text(flip_log(200, 16, first=2500))
    assert parse.flip_stats(str(p))["first_s"] == 2.5
    p.write_text("no flips here\n")
    assert parse.flip_stats(str(p)) is None


def test_size_of(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a").write_text("12345")
    (tmp_path / "d" / "b").write_text("1")
    assert parse.size_of(str(tmp_path / "d")) == (6, 2)
    assert parse.size_of(str(tmp_path / "missing")) == (0, 0)
