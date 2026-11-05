"""The port's names ([pipeline] names; docs/names.md): the checks
on load, the release picked for a dump, gen/recomp_names.h, the source
scan that stops on a name a release lacks, seeds by name, recomp's
--names, the release file match writes, and a game without the key
keeping its argv.

  uv run pytest tests/test_names.py
"""

import json
import os
from types import SimpleNamespace

import pytest
from test_cli import fake_tree, raises, write

from xboxrecomp_cli import fingerprint, host, manifest, names, pipeline, release_map

NAMES = """
[[name]]
name = "gfx_sphere_visible"
kind = "function"
addr = 0x0009AD80
note = "the Hor+ wrap"

[[name]]
name = "gfx_lens_flare"
kind = "function"
addr = 0x00096BB0

[[name]]
name = "sys_thread_a"
kind = "function"
addr = 0x000F9860

[[name]]
name = "cam_proj_11"
kind = "global"
addr = 0x003AE6A0

[[name]]
name = "d3d_fence_put"
kind = "value"
value = 0x2C
"""

PAL_SHA = "3e" * 32

PAL = (
    """
[release]
id = "pal-v3"
sha256 = ["%s"]
region = 0x4
version = 3

[[name]]
name = "gfx_sphere_visible"
ref = 0x0009AD80
addr = 0x00034F90
verdict = "unsure"
how = "function (calls)"

[[name]]
name = "sys_thread_a"
ref = 0x000F9860
addr = 0x000FA350
verdict = "reviewed"

[[name]]
name = "cam_proj_11"
ref = 0x003AE6A0
addr = 0x0039DE50
verdict = "sure"

[[name]]
name = "d3d_fence_put"
ref = 0x2C
value = 0x30
verdict = "reviewed"
"""
    % PAL_SHA
)


def with_names(root, pal=True):
    write(os.path.join(root, "config", "names.toml"), NAMES)
    if pal:
        write(os.path.join(root, "config", "releases", "pal-v3.toml"), PAL)
    m = host.g().m
    m["pipeline"]["names"] = "config/names.toml"
    manifest.use(manifest.Game(root, m))
    return host.g()


def pal_verdict(sha=PAL_SHA):
    return fingerprint.Verdict(
        sha, fingerprint.CODE, [".text"], "", 4, 3, "NTSC-U, version 6", True
    )


def test_manifest_keys_default_off():
    m = manifest.parse_text(
        open(
            os.path.join(os.path.dirname(__file__), "testdata", "game", "game.toml"),
            encoding="utf-8",
        ).read()
    )
    assert m["pipeline"]["names"] == "" and m["pipeline"]["releases"] == "config/releases"


@pytest.mark.parametrize(
    "text, error",
    [
        ('[[name]]\nname = "a-b"\nkind = "global"\naddr = 1', "not a C identifier"),
        ('[[name]]\nname = "sub_00012000"\nkind = "function"\naddr = 1', "reserved"),
        ('[[name]]\nname = "a_b"\nkind = "label"\naddr = 1', "kind 'label'"),
        ('[[name]]\nname = "a_b"\nkind = "global"', "not a 32-bit VA"),
        ('[[name]]\nname = "a_b"\nkind = "value"', "needs value"),
        ('[[name]]\nname = "a_b"\nkind = "value"\nvalue = 1\naddr = 2', "has no addr"),
        (
            '[[name]]\nname = "a_b"\nkind = "global"\naddr = 1\n'
            '[[name]]\nname = "a_b"\nkind = "global"\naddr = 2',
            "the name is also",
        ),
        ("name = 1", "must be [[name]] tables"),
    ],
)
def test_load_refuses(tmp_path, text, error):
    p = tmp_path / "names.toml"
    p.write_text(text)
    raises(names.load, str(p), match=error)


@pytest.mark.parametrize(
    "extra, error",
    [
        ('[[name]]\nname = "nope"\naddr = 1', "not a name in names.toml"),
        ('[[name]]\nname = "cam_proj_11"\naddr = 1\nverdict = "maybe"', "verdict 'maybe'"),
        ('[[name]]\nname = "d3d_fence_put"\naddr = 1', "a value has no addr"),
        ('[[name]]\nname = "cam_proj_11"\nvalue = 1', "has no value"),
    ],
)
def test_load_release_refuses(tmp_path, extra, error):
    (tmp_path / "names.toml").write_text(NAMES)
    rows = names.load(str(tmp_path / "names.toml"))
    p = tmp_path / "r.toml"
    p.write_text('[release]\nid = "x"\nsha256 = []\n' + extra)
    raises(names.load_release, str(p), rows, match=error)


def test_release_id():
    assert names.release_id(4, 3) == "pal-v3"
    assert names.release_id(1, 6) == "ntsc-u-v6"
    assert names.release_id(7, 1) == "region-free-v1"
    assert names.release_id(None, None) == "reference"


def test_no_names_no_selection(d):
    with fake_tree(d):
        assert names.select(host.g()) is None
        assert "--names" not in json.dumps(pipeline.recomp_cmds())


def test_the_reference_is_names_toml(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        sel = names.select(G)
        assert sel.path == "" and sel.names["gfx_sphere_visible"].addr == 0x9AD80
        assert names.addresses(sel)["cam_proj_11"] == 0x3AE6A0
        assert names.unsure(sel) == [] and names.missing(sel) == []
        # A mod of the reference release keeps the reference's names.
        mod = fingerprint.Verdict("ab" * 32, fingerprint.CODE, [".text"])
        assert names.select(G, mod).path == ""


def test_another_release_by_its_sha256(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        sel = names.select(G, pal_verdict())
        assert sel.id == "pal-v3" and sel.path.endswith("pal-v3.toml")
        assert names.addresses(sel) == {
            "gfx_sphere_visible": 0x34F90,
            "sys_thread_a": 0xFA350,
            "cam_proj_11": 0x39DE50,
        }
        assert names.missing(sel) == ["gfx_lens_flare"]
        # A "sure" the matcher gave still wants a person's review.
        assert names.unsure(sel) == [("gfx_sphere_visible", "unsure"), ("cam_proj_11", "sure")]
        assert sel.names["d3d_fence_put"].value == 0x30


def test_another_release_without_a_map_names_match(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root, pal=False)
        e = raises(names.select, G, pal_verdict("cd" * 32), match="match game_files")
        assert "PAL, version 3" in e and "no release map" in e


def test_header_text(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        h = names.header_text(names.select(G, pal_verdict()), G)
        assert '#define XBR_RELEASE "pal-v3"' in h
        assert "#define XBR_FN(n)   XBR_FN_##n" in h
        assert "#define XBR_FN_gfx_sphere_visible sub_00034F90" in h
        assert "#define XBR_GEN_gfx_sphere_visible sub_00034F90_gen" in h
        assert "#define XBR_VA_cam_proj_11 0x0039DE50u" in h
        assert "XBR_FN_cam_proj_11" not in h
        assert "#define XBR_HAVE_gfx_lens_flare 0" in h and "XBR_VA_gfx_lens_flare" not in h
        assert "#define XBR_VAL_d3d_fence_put 0x30" in h
        assert "unsure (function (calls))" in h


SRC = """\
#include "gen/recomp_names.h"
/* XBR_VA(nothing) in a comment does not count */
void XBR_FN(gfx_sphere_visible)(void) { MEMF(XBR_VA(cam_proj_11)) = 1.0f; }
#if XBR_HAVE(gfx_lens_flare)
void XBR_FN(gfx_lens_flare)(void) { RECOMP_ABI_CALL(XBR_VA(gfx_lens_flare), XBR_GEN(gfx_lens_flare)); }
#else
static int no_flare;
#endif
"""


def test_uses_and_guards():
    got = [(line, form, name, sorted(g)) for line, form, name, g in names.uses(SRC)]
    assert got[0] == (3, "FN", "gfx_sphere_visible", [])
    assert (5, "GEN", "gfx_lens_flare", ["gfx_lens_flare"]) in got
    assert all(n != "nothing" for _, _, n, _ in got)


def test_check_sources(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(G.path("src", "manual.c"), SRC)
        names.check_sources(G, names.select(G, pal_verdict()))
        # On the #else side, or bare, a missing name stops by name.
        write(
            G.path("src", "other.c"),
            "#if XBR_HAVE(gfx_lens_flare)\n#else\nint x = XBR_VA(gfx_lens_flare);\n#endif\n",
        )
        e = raises(names.check_sources, G, names.select(G, pal_verdict()), match="src/other.c:3")
        assert "release pal-v3 has no gfx_lens_flare" in e
        write(G.path("src", "other.c"), "int x = XBR_VAL(cam_proj_11) + XBR_VA(nope);\n")
        e = raises(names.check_sources, G, names.select(G), match="not a value")
        assert "nope): not a name" in e
        # gen/ is not the game's source.
        os.remove(G.path("src", "other.c"))
        write(os.path.join(G.gen, "x.c"), "int y = XBR_VA(nope);\n")
        names.check_sources(G, names.select(G))


def test_seeds_by_name(d, capsys):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        rows = [
            {"name": "sys_thread_a", "observed": True},
            {"name": "gfx_lens_flare"},
            {"start": "0x00012000"},
        ]
        write(G.seeds[0], json.dumps(rows))
        resolved = names.seed_path(G, G.seeds[0])
        assert resolved.startswith(os.path.join(G.out, "seeds"))
        assert "--seed-functions" in json.dumps(pipeline.disasm_cmds()) and resolved in json.dumps(
            pipeline.disasm_cmds()
        )
        names.write_seeds(G, names.select(G, pal_verdict()))
        with open(resolved, encoding="utf-8") as f:
            got = json.load(f)
        assert got == [
            {"name": "sys_thread_a", "observed": True, "start": "0x000FA350"},
            {"start": "0x00012000"},
        ]
        assert "has no gfx_lens_flare" in capsys.readouterr().out
        # A file by address is passed as it is.
        write(G.seeds[0], json.dumps([{"start": "0x00012000"}]))
        assert names.seed_path(G, G.seeds[0]) == G.seeds[0]


def test_recomp_inputs_and_argv(d, capsys):
    with fake_tree(d) as (root, _tk):
        argv = pipeline.stage_argv()
        inputs = pipeline.gen_inputs()
        G = with_names(root)
        assert "--names" in json.dumps(pipeline.recomp_cmds())
        assert pipeline.stage_argv() != argv
        assert set(pipeline.gen_inputs()) - set(inputs) == {"$ROOT/config/names.toml", "release"}
        sel = names.select(G, pal_verdict())
        names.write_recomp_inputs(G, sel)
        with open(names.names_json(G), encoding="utf-8") as f:
            got = json.load(f)
        assert got["addresses"]["gfx_sphere_visible"] == "0x00034F90"
        # Values are in the have list, so the manual scan keeps their blocks.
        assert "d3d_fence_put" in got["have"] and "d3d_fence_put" not in got["addresses"]
        assert "gfx_lens_flare" not in got["have"]
        assert os.path.isfile(os.path.join(G.gen, names.HEADER))
        out = capsys.readouterr().out
        assert (
            "built without gfx_lens_flare" in out
            and "2 name(s) placed but not reviewed (gfx_sphere_visible, unsure, cam_proj_11, sure)"
            in out
        )


class FakeMatch:
    def __init__(self, table, starts):
        self.table = table
        self.other = SimpleNamespace(by_start=set(starts))

    def place(self, va):
        return self.table.get(va, (None, "in unmatched function 0x%X" % va))


def test_place_rows_and_release_text_round_trip(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root, pal=False)
        rows = names.load(G.names)
        m = FakeMatch(
            {
                0x9AD80: (0x34F90, "unsure: function (calls)"),
                0xF9860: (0xFA350, "unsure: code pointer (pushed by a matched function)"),
                0x3AE6A0: (0x39DE50, "sure: global (referenced)"),
            },
            starts={0x34F90},
        )
        placed = release_map.place_rows(m, rows)
        assert placed["gfx_sphere_visible"] == {
            "addr": 0x34F90,
            "verdict": "unsure",
            "how": "function (calls)",
        }
        assert placed["sys_thread_a"]["addr"] == 0xFA350 and "check" not in placed["sys_thread_a"]
        assert placed["gfx_lens_flare"] == {"why": "in unmatched function 0x96BB0"}
        assert "d3d_fence_put" not in placed
        rel = {
            "id": "pal-v3",
            "sha256": [PAL_SHA],
            "region": 4,
            "version": 3,
            "reference": "ab" * 32,
        }
        text = names.release_text(G, rel, rows, placed)
        p = os.path.join(G.releases, "pal-v3.toml")
        write(p, text)
        got_rel, got = names.load_release(p, rows)
        assert got_rel["sha256"] == [PAL_SHA] and got_rel["region"] == 4
        assert got["gfx_sphere_visible"]["addr"] == 0x34F90 and "addr" not in got["gfx_lens_flare"]
        # The unplaced rows are missing names of that release.
        sel = names.select(G, pal_verdict())
        assert names.missing(sel) == ["gfx_lens_flare", "d3d_fence_put"]
        assert "# unplaced: in unmatched function 0x96BB0 (the reference has 0x00096BB0)" in text
        assert "# unplaced: a value, which match does not place (the reference has 0x2C);" in text


def test_a_function_inside_another_is_unsure():
    rows = [{"name": "f", "kind": "function", "addr": 0x1000}]
    m = FakeMatch({0x1000: (0x2010, "sure: inside function 0x1000 +0x10")}, starts=set())
    got = release_map.place_rows(m, rows)["f"]
    assert got["verdict"] == "unsure" and "not a function start" in got["check"]


def test_match_needs_names_and_the_reference(d):
    with fake_tree(d) as (root, _tk):
        raises(release_map.run, "x", match="no [pipeline] names")
        with_names(root)
        raises(release_map.run, "x", match="is not it (unknown)")


def test_a_patched_reference_keeps_the_reference(d):
    # A region or title patch of the reference (HEADER) or a data mod
    # (DATA) carries the reference's code, whatever its certificate says.
    with fake_tree(d) as (root, _tk):
        G = with_names(root, pal=False)
        for verdict in (fingerprint.HEADER, fingerprint.DATA):
            v = fingerprint.Verdict(
                "cd" * 32, verdict, ["header"], "", 4, 3, "NTSC-U, version 6", True
            )
            assert names.select(G, v).path == ""


def test_the_sha256_wins_over_the_fingerprint(d):
    # A dump a release map lists is that release, even when the fingerprint
    # takes it for a code mod of the reference (other=False).
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        mod = fingerprint.Verdict(PAL_SHA, fingerprint.CODE, [".text"])
        assert names.select(G, mod).id == "pal-v3"
        hdr = fingerprint.Verdict(PAL_SHA, fingerprint.HEADER, ["header"])
        assert names.select(G, hdr).id == "pal-v3"


def test_release_files_refuse_shared_sha256(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(os.path.join(G.releases, "pal-v4.toml"), PAL.replace('"pal-v3"', '"pal-v4"'))
        raises(names.select, G, pal_verdict(), match="both list 3e3e")
        os.remove(os.path.join(G.releases, "pal-v4.toml"))
        ref = G.m["xbe"]["sha256"][0]
        write(os.path.join(G.releases, "pal-v3.toml"), PAL.replace(PAL_SHA, ref))
        raises(names.select, G, pal_verdict(), match="the reference's own sha256")


def test_a_stale_ref_stops_select_and_match_rereviews(d, capsys):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(G.names, NAMES.replace("0x000F9860", "0x000F9870"))
        raises(names.select, G, pal_verdict(), match="other reference addresses of sys_thread_a")
        # No ref at all: a warning, not a stop.
        write(G.names, NAMES)
        write(os.path.join(G.releases, "pal-v3.toml"), PAL.replace("ref = 0x000F9860\n", ""))
        names.select(G, pal_verdict())
        assert "does not record the reference address of sys_thread_a" in capsys.readouterr().out


def test_the_reference_not_in_xbe_sha256_warns(d, capsys):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        p = os.path.join(G.releases, "pal-v3.toml")
        write(p, PAL.replace("version = 3", 'version = 3\nreference = "%s"' % ("ef" * 32)))
        names.select(G, pal_verdict())
        assert (
            "matched against efefefefefef, which is not in [xbe] sha256" in capsys.readouterr().out
        )


def test_check_sources_refuses_have_typos_and_other_forms(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(G.path("src", "a.c"), "#if XBR_HAVE(gfx_lens_flar)\nint a;\n#endif\n")
        raises(
            names.check_sources,
            G,
            names.select(G),
            match="a.c:1: XBR_HAVE(gfx_lens_flar): not a name",
        )
        for text in (
            "#if !XBR_HAVE(gfx_lens_flare)\n#endif\n",
            "#if XBR_HAVE(gfx_lens_flare) && XBR_HAVE(sys_thread_a)\n#endif\n",
            "#if 0\n#elif XBR_HAVE(gfx_lens_flare)\n#endif\n",
            "#define HAVE_FLARE XBR_HAVE(gfx_lens_flare)\n",
        ):
            write(G.path("src", "a.c"), text)
            raises(names.check_sources, G, names.select(G), match="only a plain")
        # In a comment it does not count; the plain form passes.
        write(
            G.path("src", "a.c"),
            "/* #if !XBR_HAVE(x) */\n#if XBR_HAVE(gfx_lens_flare) /* the flare */\n#endif\n",
        )
        names.check_sources(G, names.select(G))


def test_a_seed_row_with_name_and_start_is_refused(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(G.seeds[0], json.dumps([{"name": "sys_thread_a", "start": "0x000F9860"}]))
        raises(names.write_seeds, G, names.select(G, pal_verdict()), match="both name and start")


def test_resolved_seeds_are_keyed_by_path(d):
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        a = G.path("config", "a", "seeds.json")
        b = G.path("config", "b", "seeds.json")
        assert names.seed_path(G, a) == a  # not there yet: passed as it is
        for p in (a, b):
            write(p, json.dumps([{"name": "sys_thread_a"}]))
        assert names.seed_path(G, a) != names.seed_path(G, b)
        assert names.seed_path(G, a).endswith(os.path.join("seeds", "config", "a", "seeds.json"))


def test_negative_and_float_values(d, tmp_path):
    assert names.c_value(-5) == "-5" and names.c_value(0x30) == "0x30"
    assert names.c_value(1.5) == "1.5"
    for bad in ("inf", "nan", "-inf"):
        p = tmp_path / ("v%s.toml" % bad)
        p.write_text('[[name]]\nname = "v"\nkind = "value"\nvalue = %s\n' % bad)
        raises(names.load, str(p), match="finite float")
    with fake_tree(d) as (root, _tk):
        G = with_names(root, pal=False)
        write(G.names, NAMES.replace("value = 0x2C", "value = -4"))
        h = names.header_text(names.select(G), G)
        assert "#define XBR_VAL_d3d_fence_put -4" in h


def test_match_rereviews_a_moved_row(d, monkeypatch, capsys):
    # The merge step of match: a reviewed row whose ref differs from
    # names.toml now goes back to unsure; one whose ref agrees is kept.
    with fake_tree(d) as (root, _tk):
        G = with_names(root)
        write(G.names, NAMES.replace("0x000F9860", "0x000F9870"))
        rows = names.load(G.names)
        _, old = names.load_release(os.path.join(G.releases, "pal-v3.toml"), rows)
        placed = {"sys_thread_a": {"addr": 0xFA360, "verdict": "sure", "how": "x"}}
        out = release_map.merge_reviewed(rows, placed, old)
        assert out.stale == ["sys_thread_a"] and out.kept == 1
        assert placed["sys_thread_a"]["verdict"] == "unsure"
        assert placed["sys_thread_a"]["addr"] == 0xFA360
        assert "moved it from 0xF9860" in placed["sys_thread_a"]["check"]
        assert placed["d3d_fence_put"]["value"] == 0x30
