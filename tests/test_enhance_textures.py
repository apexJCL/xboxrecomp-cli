"""xbr enhance textures: inventory, classes, proposals and estimate, on archives
and dumps the tests build themselves. No test reads game files.

  uv run pytest tests/test_enhance_textures.py
"""

import json
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import A, fake_tree, raises, write  # noqa: E402
from test_enhance import dds_file, dxt1_blocks, mix_file, write_archive  # noqa: E402

from xboxrecomp_cli import host, manifest  # noqa: E402
from xboxrecomp_cli.enhance import (  # noqa: E402
    classes,
    dds,
    estimate,
    inventory,
    swizzle,
    texhash,
    textures,
    traced,
    xpr,
)
from xboxrecomp_cli.enhance.sources import DumpSource, LooseSource  # noqa: E402
from xboxrecomp_cli.enhance.sources.dump import XTD_HEADER, hash_text  # noqa: E402

# ---- what the two sides share ------------------------------------------------


def pattern(n):
    """The byte pattern of the shared vectors below."""
    return bytes((i * 7 + 3) & 255 for i in range(n))


# Made by a C program over the toolkit's own xbox_swizzle_rect
# (src/d3d/d3d8_swizzle.h) and nv2a_tex_hash: a port that drifts fails here. The
# toolkit's tests/tex_dump is to hold the same vectors (a follow-up).
SWIZZLE_16X8_2 = "D6A24029D6A587DB"
SWIZZLE_4X16_4 = "339B7F3DDC39241B"
CHAIN_8_4 = "19CAF3E151783615"  # 8x8, 4x4, 2x2, 1x1, each swizzled alone
CHAIN_8_4_LEVEL0 = "EEA21AFD0C47739B"


def test_swizzle_matches_the_toolkit():
    out = swizzle.swizzle(pattern(16 * 8 * 2), 16, 8, 2)
    assert hash_text(texhash.tex_hash(out)) == SWIZZLE_16X8_2
    out = swizzle.swizzle(pattern(4 * 16 * 4), 4, 16, 4)
    assert hash_text(texhash.tex_hash(out)) == SWIZZLE_4X16_4


def test_prefix_hashes_equal_whole_hashes():
    data = pattern(1034)
    lengths = [0, 1, 31, 32, 33, 64, 500, 1034]
    assert texhash.tex_hash_prefixes(data, lengths) == [texhash.tex_hash(data[:n]) for n in lengths]


def raw_dds(w, h, data, bits, masks, mips=0, flags=0x41):
    """An uncompressed DDS: pixel-format flags and masks as given."""
    hdr = bytearray(dds_file(w, h, b"\0\0\0\0", b"", mips=mips))
    struct.pack_into("<I", hdr, 80, flags)
    struct.pack_into("<I", hdr, 88, bits)
    struct.pack_into("<IIII", hdr, 92, *masks)
    return bytes(hdr) + data


A4R4G4B4 = (16, (0xF00, 0xF0, 0xF, 0xF000))
A8R8G8B8 = (32, (0xFF0000, 0xFF00, 0xFF, 0xFF000000))


def cand(data, name="dds/a.dds", reencodes=False):
    out = inventory.candidates("media", name, data, reencodes)
    assert len(out) == 1
    return out[0]


def test_uncompressed_dds_hashes_as_the_guest_holds_it():
    c = cand(raw_dds(16, 8, pattern(16 * 8 * 2), *A4R4G4B4))
    assert c.format == "A4R4G4B4" and c.layout == "swizzled"
    # One level in the file stays one level: the load generates none.
    assert c.hashes == [(1, SWIZZLE_16X8_2)]


def test_mipped_dds_hashes_once_per_level_prefix():
    data = b"".join(pattern(w * w * 4) for w in (8, 4, 2, 1))
    c = cand(raw_dds(8, 8, data, *A8R8G8B8, mips=4))
    assert c.levels == 4
    assert [h for _, h in c.hashes][0] == CHAIN_8_4_LEVEL0
    assert c.hashes[-1] == (4, CHAIN_8_4)
    assert len(c.hashes) == 4
    # Swizzling has no blocks to re-encode: this hash is no guess.
    assert not c.guess


def test_xpr_data_is_never_swizzled():
    data = pattern(16 * 8 * 2)
    rows = inventory.candidates("media", "mix/a.mix", mix_file([("t", 0x04, 16, 8, data)]))
    assert rows[0].hashes == [(1, hash_text(texhash.tex_hash(data)))]
    assert rows[0].name == "mix/a.mix/t" and rows[0].entry == "mix/a.mix"
    # The HUD path keeps only DXT: `raw` is the inventory's alone.
    assert xpr.parse_mix(mix_file([("t", 0x04, 16, 8, data)]))[0][1] is None


def test_what_cannot_be_hashed_says_why():
    assert "palette" in cand(raw_dds(4, 4, bytes(16), 8, (0, 0, 0, 0), flags=0x20)).note
    assert "power of two" in cand(raw_dds(6, 4, bytes(48), *A4R4G4B4)).note
    assert "TGA" in cand(b"\0" * 20, "t/a.tga").note
    assert "truncated" in cand(raw_dds(8, 8, bytes(4), *A4R4G4B4)).note
    assert inventory.candidates("media", "readme.txt", b"hello") == []


def test_a_level_under_4x4_makes_the_hash_a_guess():
    # 16x16 down to 4x4: every level is whole blocks, the file's bytes are
    # the guest's. Down to 2x2: a game with such a load re-encodes.
    whole = dxt1_blocks(16, 16) + dxt1_blocks(8, 8) + dxt1_blocks(4, 4)
    chain4 = dds_file(16, 16, b"DXT1", whole, mips=3)
    chain2 = dds_file(16, 16, b"DXT1", whole + dxt1_blocks(2, 2), mips=4)
    assert not cand(chain4, reencodes=True).guess
    assert cand(chain2, reencodes=True).guess
    assert cand(chain2, reencodes=True).probe
    # The game decides: with no such load, the file's bytes are the guest's.
    assert not cand(chain2).guess
    # The measurement was of chains: one level under 4 is hashed as it is.
    assert not cand(dds_file(4, 2, b"DXT1", dxt1_blocks(4, 2)), reencodes=True).guess
    assert not cand(dds_file(4, 4, b"DXT1", dxt1_blocks(4, 4)), reencodes=True).guess


def test_a_npot_dxt_is_a_guess_on_any_game():
    # The GPU takes no such DXT, so the load resizes it, as an uncompressed one.
    c = cand(dds_file(12, 8, b"DXT1", dxt1_blocks(12, 8)))
    assert c.guess and c.hashes and c.probe
    raw = cand(raw_dds(12, 8, bytes(12 * 8 * 2), *A4R4G4B4))
    assert not raw.hashes and "power of two" in raw.note


# ---- a dump, as the runtime writes one --------------------------------------


def dump_texture(directory, h, w, height, fmt="DXT1", levels=1, texels=None, **meta):
    """A sidecar, and the .xtd when texels (BGRA bytes of every level) are given."""
    os.makedirs(directory, exist_ok=True)
    side = {
        "hash": h,
        "width": w,
        "height": height,
        "levels": levels,
        "format": fmt,
        "layout": "linear" if fmt.startswith("LIN_") else "dxt",
        "texels": texels is not None,
    }
    side.update(meta)
    write(os.path.join(directory, h + ".json"), json.dumps(side))
    if texels is not None:
        head = XTD_HEADER.pack(
            b"XRTD",
            1,
            int(h, 16),
            w,
            height,
            levels,
            0,
            0,
            0,
            0,
            XTD_HEADER.size,
            len(texels) // 4,
            0,
        )
        with open(os.path.join(directory, h + ".xtd"), "wb") as f:
            f.write(head + texels)


def blocks_hash(data):
    return hash_text(texhash.tex_hash(data))


TOML = """
[enhance.textures]
sources = ["archive", "dump"]
archives = ["media"]
"""
REENCODES = TOML + "reencodes_small_levels = true\n"


def game_with(extra=TOML):
    G = host.g()
    G.m["enhance"] = manifest.parse_text(
        open(os.path.join(G.root, "game.toml"), encoding="utf-8").read() + extra
    )["enhance"]
    return G


def inv_args(**kw):
    return A(
        **dict(
            dict(
                verb="inventory",
                dump=[],
                source=[],
                out=None,
                json=False,
                list=False,
                propose=None,
                untraced=None,
                write_rules=None,
            ),
            **kw,
        )
    )


def est_args(**kw):
    return A(
        **dict(
            dict(verb="estimate", preset="all", config=None, inventory=None, json=False, jobs=1),
            **kw,
        )
    )


def load_doc(G):
    return inventory.load(os.path.join(G.dist, "enhance", inventory.FILE_NAME))


def by_hash(doc):
    return {e["hash"]: e for e in doc["entries"]}


def test_join_three_in_the_archive_two_in_the_dump_one_unnamed(d, capsys):
    a, b, c = dxt1_blocks(8, 8, 0), dxt1_blocks(8, 8, 1), dxt1_blocks(8, 8, 2)
    with fake_tree(d):
        G = game_with()
        write_archive(
            G.game_files,
            "media",
            [
                ("dds\\a.dds", dds_file(8, 8, b"DXT1", a), False),
                ("dds\\b.dds", dds_file(8, 8, b"DXT1", b), True),
                ("dds\\c.dds", dds_file(8, 8, b"DXT1", c), False),
            ],
        )
        dump = os.path.join(d, "scene")
        dump_texture(dump, blocks_hash(a), 8, 8)
        dump_texture(dump, blocks_hash(b), 8, 8)
        dump_texture(dump, "00000000000000AA", 16, 16)
        assert textures.run(inv_args(dump=[dump])) == 0
        out = capsys.readouterr().out
        doc = load_doc(G)
        e = by_hash(doc)
        assert len(e) == 4
        assert e[blocks_hash(a)]["status"] == inventory.JOINED
        assert e[blocks_hash(a)]["origins"] == [{"source": "media", "name": "dds/a.dds"}]
        assert e[blocks_hash(b)]["status"] == inventory.JOINED
        assert e[blocks_hash(c)]["status"] == inventory.NOT_SEEN
        assert e["00000000000000AA"]["status"] == inventory.UNNAMED
        assert "4 entries: 2 joined, 1 not seen, 1 unnamed" in out
        assert "world" in out
        assert "not seen (in the sources" in out and "dds/c.dds" in out
        assert "unnamed (in a dump" in out and "00000000000000AA" in out


def test_dump_alone_is_a_full_inventory(d, capsys):
    """No archive reader: every entry unnamed, classed, and the command says so."""
    with fake_tree(d):
        G = game_with('\n[enhance.textures]\nsources = ["dump", "loose"]\nloose = ["art/*"]\n')
        data = dxt1_blocks(8, 8, 1)
        write(os.path.join(G.game_files, "art", "x"), "")
        with open(os.path.join(G.game_files, "art", "named.dds"), "wb") as f:
            f.write(dds_file(8, 8, b"DXT1", data))
        dump = os.path.join(d, "scene")
        dump_texture(dump, blocks_hash(data), 8, 8)
        dump_texture(dump, "00000000000000BB", 8, 8)
        textures.run(inv_args(dump=[dump]))
        out = capsys.readouterr().out
        assert len([ln for ln in out.splitlines() if "no archive reader" in ln]) == 1
        e = by_hash(load_doc(G))
        assert e["00000000000000BB"]["status"] == inventory.UNNAMED
        assert e[blocks_hash(data)]["origins"][0]["name"] == "art/named.dds"


def test_a_game_with_no_enhance_table_runs_on_a_dump(d, capsys):
    with fake_tree(d):
        G = host.g()
        G.m.pop("enhance", None)
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000CC", 8, 8)
        assert textures.run(inv_args(dump=[dump])) == 0
        assert "no archive reader" in capsys.readouterr().out
        assert by_hash(load_doc(G))["00000000000000CC"]["class"] == "world"


def test_dump_only_with_no_folder_says_how(d):
    with fake_tree(d):
        game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        raises(textures.run, inv_args(), match="RECOMP_DEBUG=tex_dump")


def test_a_mipped_file_joins_at_the_level_count_the_runtime_bound(d):
    with fake_tree(d):
        G = game_with()
        one, two = dxt1_blocks(16, 16), dxt1_blocks(8, 8)
        write_archive(
            G.game_files,
            "media",
            [("dds\\m.dds", dds_file(16, 16, b"DXT1", one + two + dxt1_blocks(4, 4), mips=3), 0)],
        )
        dump = os.path.join(d, "scene")
        # The game clamped MAX_LOD: it bound and hashed two of the file's three.
        dump_texture(dump, blocks_hash(one + two), 16, 16, levels=2)
        textures.run(inv_args(dump=[dump]))
        e = by_hash(load_doc(G))
        assert list(e) == [blocks_hash(one + two)]
        assert e[blocks_hash(one + two)]["status"] == inventory.JOINED
        assert e[blocks_hash(one + two)]["levels"] == 2


def bgra(rgba):
    o = bytearray(rgba)
    o[0::4], o[2::4] = rgba[2::4], rgba[0::4]
    return bytes(o)


def chain(w, h, idx):
    """A chain to 1x1 of DXT1 levels and its decoded texels, in dump order."""
    data, texels, levels = b"", b"", 0
    while True:
        lv = dxt1_blocks(w, h, idx)
        data += lv
        texels += bgra(bytes(dds.decode_level(b"DXT1", w, h, lv)))
        levels += 1
        if w == 1 and h == 1:
            return data, texels, levels
        w, h = max(1, w // 2), max(1, h // 2)


def test_a_reencoded_chain_is_named_by_content(d):
    """The load re-encodes its 2x2 and 1x1: no hash of the file's bytes is the
    dump's, so it joins on what a level decodes to, under the dump's hash."""
    with fake_tree(d):
        G = game_with(REENCODES)
        same, st, levels = chain(16, 16, 1)
        other, _, _ = chain(16, 16, 2)
        write_archive(
            G.game_files,
            "media",
            [
                ("dds\\same.dds", dds_file(16, 16, b"DXT1", same, mips=levels), 0),
                ("dds\\other.dds", dds_file(16, 16, b"DXT1", other, mips=levels), 0),
            ],
        )
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000DD", 16, 16, levels=levels, texels=st)
        textures.run(inv_args(dump=[dump]))
        e = by_hash(load_doc(G))
        got = e["00000000000000DD"]
        assert got["status"] == inventory.JOINED
        assert got["origins"] == [
            {"source": "media", "name": "dds/same.dds", "levels": levels, "by": "content"}
        ]
        left = e[blocks_hash(other)]
        assert left["status"] == inventory.NOT_SEEN and left["hash_guess"]
        assert left["skip"] == "needs_dump"
        # A rule cannot bring it in: an entry under this hash would match nothing.
        config = {"rule": {"dds/other*": {"skip": False}}}
        assert classes.treatment(left, config)["skip"] == "needs_dump"


def test_same_level_allows_rounding_only():
    a = bytes([10, 20, 30, 255])
    assert inventory._same_level(a, bytes([31, 21, 9, 255]))
    assert not inventory._same_level(a, bytes([33, 21, 9, 255]))
    assert not inventory._same_level(a, bytes([30, 20, 10, 250]))
    # A transparent texel's color is the encoder's noise.
    assert inventory._same_level(bytes([1, 2, 3, 0]), bytes([200, 200, 200, 0]))
    assert not inventory._same_level(a, a + a)


def test_inventory_holds_no_texels(d):
    with fake_tree(d):
        G = game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        dump = os.path.join(d, "scene")
        marker = b"TEXELMARKERTEXEL" * 4
        dump_texture(dump, "00000000000000EE", 4, 4, texels=marker)
        textures.run(inv_args(dump=[dump]))
        path = os.path.join(G.dist, "enhance", inventory.FILE_NAME)
        raw = open(path, encoding="utf-8").read()
        assert "TEXELMARKER" not in raw and d not in raw
        keys = set(json.loads(raw))
        assert keys == {"schema", "sources", "dumps", "entries", "unhashed"}


def test_inventory_json_and_list(d, capsys):
    with fake_tree(d):
        game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000F1", 256, 1)
        textures.run(inv_args(dump=[dump], json=True))
        doc = json.loads(capsys.readouterr().out)
        assert doc["entries"][0]["skip"] == "data"
        textures.run(inv_args(dump=[dump], list=True))
        assert "skipped: a ramp or lookup table" in capsys.readouterr().out


def test_counts_and_volatile_from_the_dump(d):
    dump = os.path.join(d, "scene")
    dump_texture(dump, "00000000000000A1", 8, 8)
    dump_texture(dump, "00000000000000A2", 1024, 512, "LIN_YUY2")
    write(os.path.join(dump, "counts.json"), json.dumps({"00000000000000A1": 7}))
    write(
        os.path.join(dump, "volatile.json"),
        json.dumps(
            [{"offset": "0x1000", "width": 1024, "height": 512, "hashes": ["00000000000000A2"]}]
        ),
    )
    src = DumpSource(dump)
    cfg = classes.game_config({})
    entries, _ = inventory.join([], [src])
    assert entries["00000000000000A1"]["uses"] == 7
    assert entries["00000000000000A2"]["uses"] == 1  # a partial counts.json is fine
    movie = classes.classify(entries["00000000000000A2"], cfg)
    assert movie["skip"] == "volatile" and {"linear", "volatile"} <= set(movie["facts"])


def test_two_scenes_add_up(d):
    one, two = os.path.join(d, "one"), os.path.join(d, "two")
    # The fields docs/formats/texture-dump.md gives the trace, in the toolkit.
    dump_texture(
        one, "00000000000000A1", 8, 8, mag_max=1.5, mag_scale1=0.5, mag_draws=2, render_scale=3
    )
    dump_texture(two, "00000000000000A1", 8, 8, mag_scale1=1.5, mag_draws=3)
    write(os.path.join(two, "counts.json"), json.dumps({"00000000000000A1": 4}))
    entries, _ = inventory.join([], [DumpSource(one), DumpSource(two)])
    e = entries["00000000000000A1"]
    assert e["scenes"] == {"one": 1, "two": 4} and e["uses"] == 5
    assert e["mag_scale1"] == 1.5 and e["draws"] == 5


def test_palette_variants_count_per_image(d):
    dump = os.path.join(d, "scene")
    px = bytes(64 * 64 * 4)
    for h in ("00000000000000A1", "00000000000000A2", "00000000000000A3"):
        dump_texture(dump, h, 64, 64, "P8", texels=px, palette=True, offset="0x10")
    dump_texture(dump, "00000000000000A4", 64, 64, "P8", texels=px, palette=True, offset="0x20")
    entries, _ = inventory.join([], [DumpSource(dump)])
    assert entries["00000000000000A1"]["palette_variants"] == 3
    assert entries["00000000000000A4"]["palette_variants"] == 1
    e = classes.classify(entries["00000000000000A1"], classes.game_config({}))
    assert "palette" in e["facts"] and e["skip"] == ""


def test_loose_source_finds_by_pattern(d):
    os.makedirs(os.path.join(d, "art", "sub"))
    for name in ("art/a.dds", "art/sub/B.DDS", "art/c.txt", "other.dds"):
        with open(os.path.join(d, name), "wb") as f:
            f.write(b"x")
    src = LooseSource(d, ["art/*"])
    assert sorted(n for n, _ in src.items()) == ["art/a.dds", "art/sub/b.dds"]


# ---- classes -----------------------------------------------------------------


def entry(fmt="DXT1", w=64, h=64, levels=1, name=None, source="media", **kw):
    e = {
        "hash": kw.pop("hash", "00000000000000A1"),
        "status": inventory.JOINED if name else inventory.UNNAMED,
        "origins": [{"source": source, "name": name}] if name else [],
        "width": w,
        "height": h,
        "levels": levels,
        "format": fmt,
        "texels": w * h,
        "scenes": {},
        "uses": 0,
    }
    e.update(kw)
    return e


def cfg_of(**tex):
    return classes.game_config({"enhance": {"textures": tex}})


@pytest.mark.parametrize(
    "e,skip,fact",
    [
        (entry(w=256, h=1), "data", "data"),
        (entry(w=8, h=8), "data", "data"),
        (entry(w=16, h=16, mag_filter="point", min_filter="point", mag_scale1=1), "data", "data"),
        (entry("G8B8"), "normal", "normal"),
        (entry("V8U8"), "normal", "normal"),
        (entry("LIN_A8R8G8B8"), "linear", "linear"),
        (entry(layout="linear"), "linear", "linear"),
        (entry("LIN_YUY2", volatile=True), "volatile", "volatile"),
        (entry(rt=True), "rt", "rt"),
    ],
)
def test_exclusions(e, skip, fact):
    got = classes.classify(e, cfg_of())
    assert fact in got["facts"] and got["skip"] == skip and classes.why(skip)


def test_a_wall_is_world_and_packable():
    got = classes.classify(entry(), cfg_of())
    assert got["class"] == "world" and got["skip"] == "" and got["facts"] == []


def test_alpha_goes_by_the_dump_then_the_format():
    cfg = cfg_of()
    assert "alpha" in classes.classify(entry("DXT5"), cfg)["facts"]
    assert "alpha" not in classes.classify(entry("DXT1"), cfg)["facts"]
    assert "alpha" in classes.classify(entry("DXT1", alpha_used=True), cfg)["facts"]
    assert "alpha" not in classes.classify(entry("DXT5", alpha_used=False), cfg)["facts"]
    # A decoder's fill is no alpha.
    assert "alpha" not in classes.classify(entry("R5G6B5", alpha_used=True), cfg)["facts"]


def test_hud_table_maps_to_classes():
    cfg = classes.game_config(
        {
            "enhance": {
                "hud": {
                    "archives": ["media"],
                    "languages": {"us": ["lng_us"]},
                    "textures": ["dds/ui_*"],
                    "exclude": ["dds/ui_old*"],
                    "text": ["dds/words_*"],
                    "font_atlas": "dds/font.dds",
                    "scale": 4,
                    "max_texels": 1000,
                }
            }
        }
    )
    assert cfg["sources"] == ["archive", "dump"] and cfg["archives"] == ["media", "lng_us"]
    assert classes.classify(entry(name="dds/font.dds"), cfg)["class"] == "font"
    assert "grid" in classes.classify(entry(name="dds/font.dds"), cfg)["facts"]
    assert classes.classify(entry(name="dds/ui_bar.dds"), cfg)["class"] == "ui"
    assert classes.classify(entry(name="dds/ui_old.dds"), cfg)["class"] == "world"
    assert classes.classify(entry(name="dds/words_a.dds"), cfg)["class"] == "text"
    # The hud preset is what `enhance hud` picks: DXT, one level, in the patterns.
    assert classes.in_preset(entry(name="dds/ui_bar.dds"), "hud", cfg)
    assert not classes.in_preset(entry(name="dds/ui_old.dds"), "hud", cfg)
    assert not classes.in_preset(entry(name="dds/ui_bar.dds", levels=2), "hud", cfg)
    assert not classes.in_preset(entry("A8R8G8B8", name="dds/ui_bar.dds"), "hud", cfg)
    e = classes.classify(entry(name="dds/ui_bar.dds", w=64, h=64), cfg)
    t = classes.treatment(e, {"render_scale": 1}, "hud", cfg)
    assert (t["scale"], t["encode"], t["mips"]) == (4, "raw", "none")
    assert t["skip"] == "over max_texels 1000"


def test_a_hook_game_keeps_its_reader():
    cfg = classes.game_config(
        {"enhance": {"hud": {"source": "hook", "hook": "s.py", "archives": ["a.dat"]}}}
    )
    assert cfg["sources"] == ["hook", "dump"] and cfg["hook"] == "s.py"


def lang_cfg():
    return classes.game_config(
        {"enhance": {"hud": {"archives": ["media"], "languages": {"us": ["lu"], "fr": ["lf"]}}}}
    )


def test_text_is_what_only_one_language_holds():
    cfg = lang_cfg()
    only = entry(name="x.dds", source="lu")
    assert classes.classify(only, cfg)["class"] == "text" and only["language"] == "us"
    shared = entry(name="x.dds", source="lu")
    shared["origins"].append({"source": "media", "name": "x.dds"})
    assert classes.classify(shared, cfg)["class"] == "world"
    both = entry(name="x.dds", source="lu")
    both["origins"].append({"source": "lf", "name": "x.dds"})
    assert classes.classify(both, cfg)["class"] == "world"


def test_a_language_not_picked_is_left_out():
    e = classes.classify(entry(name="x.dds", source="lf"), lang_cfg())
    config = {"coverage": {"languages": ["us"]}}
    assert classes.treatment(e, config)["skip"] == "language fr not picked"
    assert classes.treatment(e, {})["skip"] == ""


def test_exclude_and_world_patterns():
    cfg = cfg_of(exclude=["dds/test_*"], classes={"world": ["dds/w_*"]})
    assert classes.classify(entry(name="dds/test_a.dds"), cfg)["skip"] == "excluded"
    assert classes.classify(entry(name="dds/w_a.dds"), cfg)["skip"] == ""
    assert classes.classify(entry(name="dds/z_a.dds"), cfg)["skip"] == "excluded"
    # An unnamed entry has no name to test.
    assert classes.classify(entry(), cfg)["skip"] == ""


def test_patterns_see_the_container_too():
    cfg = cfg_of(classes={"ui": ["mix/hud.mix"]})
    e = entry(name="mix/hud.mix/bar")
    e["origins"][0]["entry"] = "mix/hud.mix"
    assert classes.classify(e, cfg)["class"] == "ui"


def test_class_scale_follows_the_render_scale():
    assert classes.class_defaults("ui", 1)["scale"] == 2
    assert classes.class_defaults("ui", 3)["scale"] == 3
    assert classes.class_defaults("ui", 9)["scale"] == 4
    assert classes.class_defaults("font", 1)["scale"] == 4
    assert classes.class_defaults("text", 4)["scale"] == 2
    assert classes.class_defaults("world", 4)["scale"] == 2  # a resampler
    assert classes.class_defaults("world", 4, "realesr-general-x4v3")["scale"] == 3
    assert classes.class_defaults("world", 1, "realesr-general-x4v3")["scale"] == 2


def test_rules_apply_in_order_and_cannot_bring_in_what_never_matches():
    cfg = cfg_of()
    linear = classes.classify(entry("LIN_A8R8G8B8", name="linear/a"), cfg)
    assert classes.treatment(linear, {})["skip"] == "linear"
    config = {"rule": {"linear/*": {"skip": False, "scale": 3}}}
    t = classes.treatment(linear, config)
    assert t["skip"] == "" and t["scale"] == 3
    movie = classes.classify(entry("LIN_YUY2", name="linear/m", volatile=True), cfg)
    assert classes.treatment(movie, config)["skip"] == "volatile"
    wall = classes.classify(entry(name="dds/a", hash="00000000000000B2"), cfg)
    config = {
        "rule": {
            "dds/*": {"scale": 3},
            "hash:00000000000000b2": {"skip": True},
        }
    }
    t = classes.treatment(wall, config)
    assert t["scale"] == 3 and t["skip"].startswith('rule "hash:')


def test_caps():
    wall = classes.classify(entry(w=64, h=64), cfg_of())
    config = {"class": {"world": {"max_texels": 100}}}
    assert classes.treatment(wall, config)["skip"] == "over max_texels 100"
    assert classes.treatment(wall, {"class": {"world": {"max_side": 130}}})["scale"] == 2
    config = {"class": {"world": {"scale": 4, "max_side": 200}}}
    assert classes.treatment(wall, config)["scale"] == 3
    assert classes.treatment(wall, {"class": {"world": {"max_side": 100}}})["skip"]


def test_game_presets():
    cfg = cfg_of(presets={"mine": ["ui", "dds/hero*"]})
    e = classes.classify(entry(name="dds/hero.dds"), cfg)
    assert e["class"] == "world" and classes.in_preset(e, "mine", cfg)
    e = classes.classify(entry(name="dds/wall.dds"), cfg)
    assert not classes.in_preset(e, "mine", cfg)
    assert classes.in_preset(e, "world", cfg) and classes.in_preset(e, "all", cfg)
    assert not classes.in_preset(e, "ui", cfg)


def test_manifest_checks_the_table():
    base = open(
        os.path.join(os.path.dirname(__file__), "testdata", "game", "game.toml"), encoding="utf-8"
    ).read()

    def bad(text, match):
        with pytest.raises(manifest.ManifestError, match=match):
            manifest.parse_text(base + "\n[enhance.textures]\n" + text)

    bad('sources = ["ftp"]', "is not one of")
    bad('classes = { sky = ["a"] }', "enhance.textures.classes.sky")
    bad('presets = { hud = ["a"] }', "fixed")
    bad('sources = ["hook"]', "required")
    bad('archives = ["../up"]', "enhance.textures.archives")
    doc = manifest.parse_text(base + '\n[enhance.textures]\nsources = ["dump"]\n')
    assert doc["enhance"]["textures"]["sources"] == ["dump"]


# ---- traced ------------------------------------------------------------------


@pytest.mark.parametrize(
    "mag,r,want",
    [
        (0.9, 1, traced.SKIP),
        (0.9, 2, 2),
        (0.9, 3, 3),
        (1.3, 1, 2),
        (1.3, 2, 3),
        (1.3, 3, 4),
        (1.0, 1, traced.SKIP),
        (0.5, 2, traced.SKIP),
        (2.0, 1, 2),
        (9.0, 1, 4),
    ],
)
def test_proposal_arithmetic(mag, r, want):
    assert traced.propose(mag, r) == want


def test_proposal_rows_and_untraced():
    cfg = cfg_of()
    on = classes.classify(entry(name="a", hash="00000000000000A1", mag_scale1=0.5), cfg)
    off = classes.classify(entry(name="b", hash="00000000000000A2"), cfg)
    rows = {r["hash"]: r for r in traced.table([on, off], 1)}
    a, b = rows[on["hash"]], rows[off["hash"]]
    assert a["proposal"] == traced.SKIP and a["bytes"] == 0 and a["saved"] == a["default_bytes"] > 0
    assert b["proposal"] == 2 and b["note"] == "untraced" and b["saved"] == 0
    skipped = traced.row(off, 1, untraced="skip")
    assert skipped["proposal"] == traced.SKIP and skipped["saved"] == skipped["default_bytes"]
    # A class-skipped entry stays out: the trace cannot make it packable.
    ramp = classes.classify(entry(w=256, h=1, mag_scale1=4), cfg)
    assert traced.row(ramp, 2)["proposal"] == traced.SKIP


def test_3x_on_a_mipped_entry_is_noted():
    e = classes.classify(entry(levels=4, mag_scale1=1.5, name="a"), cfg_of())
    assert traced.row(e, 2)["proposal"] == 3
    assert "fractional" in traced.row(e, 2)["note"]
    flat = classes.classify(entry(mag_scale1=1.5, name="a"), cfg_of())
    assert traced.row(flat, 2)["note"]  # a world entry gets a full chain by default


def test_rules_round_trip(d):
    path = os.path.join(d, "c.toml")
    write(path, 'schema = 1\nrender_scale = 2\n\n[rule."dds/keep*"]\nscale = 2\n')
    choices = {"00000000000000A1": traced.SKIP, "00000000000000A2": 3}
    textures._write_rules(path, choices)
    conf = classes.load_config(path)
    assert traced.read_rules(conf) == choices
    assert conf["rule"]["dds/keep*"] == {"scale": 2} and conf["render_scale"] == 2
    # Written again, the block is replaced, the owner's lines stay.
    textures._write_rules(path, {"00000000000000A3": 4})
    conf = classes.load_config(path)
    assert traced.read_rules(conf) == {"00000000000000A3": 4}
    assert conf["rule"]["dds/keep*"] == {"scale": 2}
    # And a rule does what the proposal said.
    e = classes.classify(entry(hash="00000000000000A3"), cfg_of())
    assert classes.treatment(e, conf)["scale"] == 4


def test_write_rules_makes_the_file_and_refuses_a_broken_one(d):
    path = os.path.join(d, "new", "c.toml")
    textures._write_rules(path, {"00000000000000A1": 2})
    assert classes.load_config(path)["schema"] == classes.CONFIG_SCHEMA
    bad = os.path.join(d, "bad.toml")
    write(bad, "schema = 1\n" + traced.BEGIN + "\n")
    raises(textures._write_rules, bad, {}, match="no end line")
    write(bad, "this is not toml")
    raises(textures._write_rules, bad, {})
    assert open(bad, encoding="utf-8").read() == "this is not toml"


def test_propose_from_the_command(d, capsys):
    with fake_tree(d):
        G = game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        dump = os.path.join(d, "scene")
        px = bytes(64 * 64 * 4)
        dump_texture(dump, "00000000000000A1", 64, 64, texels=px, mag_scale1=0.9, mag_draws=3)
        dump_texture(dump, "00000000000000A2", 64, 64, texels=px)
        rules = os.path.join(G.root, "config", "t.toml")
        textures.run(inv_args(dump=[dump], propose=1, write_rules=rules))
        out = capsys.readouterr().out
        assert "1 traced, 1 untraced" in out and "skip" in out
        assert "bytes saved against the class defaults" in out
        assert traced.read_rules(classes.load_config(rules)) == {"00000000000000A1": traced.SKIP}
        # A dump from before the trace says what to do.
        no = os.path.join(d, "old")
        dump_texture(no, "00000000000000A3", 64, 64, texels=px)
        textures.run(inv_args(dump=[no], propose=2))
        assert "RECOMP_TRACE=tex_mag" in capsys.readouterr().out
        raises(textures.run, inv_args(dump=[no], propose=0), match="render scale")


# ---- estimate ----------------------------------------------------------------


def test_pack_bytes_arithmetic():
    e = entry(w=8, h=8, levels=4)
    # 16x16 down to 1x1: 5 levels of 16, 4, 1, 1, 1 blocks; or 341 * 4 texels.
    assert classes.pack_bytes(e, 2, "bc1", "full") == 21 * 8 + 16
    assert classes.pack_bytes(e, 2, "bc3", "full") == (21 * 8 + 16) * 2
    assert classes.pack_bytes(e, 2, "raw", "full") == (256 + 64 + 16 + 4 + 1) * 4
    assert classes.pack_bytes(e, 2, "raw", "none") == 256 * 4
    # keep: the source's four levels, each doubled.
    assert classes.pack_bytes(e, 2, "raw", "keep") == (256 + 64 + 16 + 4) * 4
    a = classes.classify(entry("DXT5", w=8, h=8), cfg_of())
    assert classes.pack_bytes(a, 2, "auto", "none") == 16 * 16
    assert classes.pack_bytes(entry(w=8, h=8), 2, "auto", "none") == 16 * 8


def test_rates():
    assert estimate.rate("xbr", {}) == 8.2
    assert estimate.rate("atlas", {}) == 8.2
    assert estimate.rate("lanczos", {}) is None
    assert estimate.rate("lanczos", {estimate.RATE_PREFIX + "lanczos": 2.5}) == 2.5


def test_save_rate_keeps_the_durations_package_reads(d):
    write(os.path.join(d, "timings.json"), json.dumps({"setup: x": 12.5}))
    estimate.save_rate(d, "lanczos", 1.23456)
    doc = json.load(open(os.path.join(d, "timings.json"), encoding="utf-8"))
    assert doc == {"setup: x": 12.5, estimate.RATE_PREFIX + "lanczos": 1.235}


def est(entries, preset="all", config=None, cfg=None, timings=None, jobs=1):
    cfg = cfg or cfg_of()
    rows = [classes.classify(e, cfg) for e in entries]
    return estimate.estimate(rows, preset, config or {}, cfg, timings or {}, jobs)


def test_estimate_arithmetic():
    wall = entry(w=16, h=16, name="w", hash="00000000000000A1", scenes={"s": 1})
    ui = entry("DXT5", w=16, h=16, name="dds/ui", hash="00000000000000A2", scenes={"s": 1})
    ramp = entry(w=256, h=1, name="ramp", hash="00000000000000A3", scenes={"s": 1})
    cfg = cfg_of(classes={"ui": ["dds/ui*"]})
    doc = est([wall, ui, ramp], cfg=cfg)
    world, u = doc["classes"]["world"], doc["classes"]["ui"]
    assert world["entries"] == 1 and world["texels"] == 256
    assert world["pack_bytes"] == (64 + 16 + 4 + 1 + 1 + 1) * 8  # 2x, BC1 opaque, full chain
    assert world["by_scale"][4]["raw"] == classes.pack_bytes(wall, 4, "raw", "full")
    assert u["pack_bytes"] == 32 * 32 * 4  # 2x at R=1, raw, no mips
    assert u["build_seconds"] == pytest.approx(8.2 * 256 / 1e6)
    assert world["unmeasured"] == ["lanczos"] and "not measured yet" in estimate.minutes(world)
    assert doc["total_bytes"] == world["pack_bytes"] + u["pack_bytes"]
    # The ramp stays as the guest had it; the pack entries replace their stock.
    s = doc["scenes"]["s"]
    assert s["entries"] == 3
    assert s["stock_bytes"] == (256 + 256 + 256) * 4
    assert s["pack_bytes"] == doc["total_bytes"] + 256 * 4


def test_estimate_jobs_and_measured_rate():
    wall = entry(w=1024, h=1024, name="w")
    timings = {estimate.RATE_PREFIX + "lanczos": 4.0}
    doc = est([wall], timings=timings, jobs=2)
    assert doc["classes"]["world"]["build_seconds"] == pytest.approx(4.0 * 1.048576 / 2)
    assert estimate.minutes(doc["classes"]["world"]) == "%.1f" % (4.0 * 1.048576 / 2 / 60)


def test_estimate_presets_and_coverage():
    ui = entry(name="dds/ui", hash="00000000000000A1")
    wall = entry(name="w", hash="00000000000000A2", mag_scale1=0.5)
    hot = entry(name="h", hash="00000000000000A3", mag_scale1=1.5)
    cold = entry(name="c", hash="00000000000000A4")
    cfg = cfg_of(classes={"ui": ["dds/ui*"]})
    assert set(est([ui, wall], "ui", cfg=cfg)["classes"]) == {"ui"}
    assert set(est([ui, wall], "world", cfg=cfg)["classes"]) == {"ui", "world"}
    doc = est([wall, hot, cold], "traced")
    assert doc["classes"]["world"]["entries"] == 2  # wall never shows; cold has its class
    config = {"coverage": {"mode": "traced", "untraced": "skip"}}
    assert est([wall, hot, cold], "all", config)["classes"]["world"]["entries"] == 1


def test_budget_flags_only():
    wall = entry(w=1024, h=1024, name="w", scenes={"s": 1})
    doc = est([wall], config={"budget": {"pack_mb": 1, "vram_mb": 1}})
    assert len(doc["over_budget"]) == 2 and doc["total_bytes"]
    assert not est([wall], config={"budget": {"pack_mb": 99, "vram_mb": 99}})["over_budget"]


def test_estimate_from_the_command(d, capsys):
    with fake_tree(d):
        G = game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        raises(textures.run, est_args(), match="no inventory yet")
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000A1", 64, 64, texels=bytes(64 * 64 * 4))
        textures.run(inv_args(dump=[dump]))
        capsys.readouterr()
        assert textures.run(est_args()) == 0
        out = capsys.readouterr().out
        assert "preset all at render scale 1" in out and "not measured yet (lanczos)" in out
        assert "resident VRAM per dumped scene" in out and "scene" in out
        textures.run(est_args(json=True))
        assert json.loads(capsys.readouterr().out)["total_bytes"] > 0
        raises(textures.run, est_args(preset="nope"), match="preset nope")
        raises(textures.run, est_args(preset="hud"), match="no [enhance.hud]")
        write(
            os.path.join(G.root, textures.DEFAULT_CONFIG),
            'schema = 1\nrender_scale = 3\n\n[class.world]\nupscaler = "xbr"\n',
        )
        textures.run(est_args())
        out = capsys.readouterr().out
        assert "render scale 3" in out and "not measured" not in out
        write(os.path.join(G.root, textures.DEFAULT_CONFIG), "schema = 9\n")
        raises(textures.run, est_args(), match="schema 9")


def test_inventory_load_refuses_another_schema(d):
    path = os.path.join(d, "i.json")
    write(path, json.dumps({"schema": 99}))
    with pytest.raises(ValueError, match="schema"):
        inventory.load(path)


def test_the_cli_parses_the_verbs(d, capsys):
    from xboxrecomp_cli import main

    with fake_tree(d):
        game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000A1", 64, 64)
        out = os.path.join(d, "o", "inv.json")
        root = host.g().root
        common = ["--game", root, "--prog", "mygame", "enhance", "textures"]
        # The fake tree's game.toml has no [enhance.textures]: the dump is enough.
        assert main.main(common + ["inventory", "--dump", dump, "--out", out]) == 0
        assert main.main(common + ["estimate", "--inventory", out, "--preset", "all"]) == 0
        assert "preset all" in capsys.readouterr().out


# ---- the review of phase 2 ----------------------------------------------------


def test_the_owners_rule_beats_the_trace_and_the_cap_holds():
    e = entry(name="a", mag_scale1=9.0)
    traced_cfg = {"coverage": {"mode": "traced"}}

    def scale_of(config):
        rows = estimate.treated([classes.classify(e, cfg_of())], "all", config, cfg_of())
        return rows[0][1]["scale"]

    assert scale_of(traced_cfg) == 4
    assert scale_of(dict(traced_cfg, rule={"a": {"scale": 2}})) == 2
    assert scale_of(dict(traced_cfg, rule={"a": {"max_side": 192}})) == 3


def test_a_rule_answers_the_traces_no():
    never = classes.classify(entry(name="a", mag_scale1=0.5), cfg_of())
    traced_cfg = {"coverage": {"mode": "traced"}}

    def picked(config):
        return estimate.treated([never], "all", config, cfg_of())

    assert picked(traced_cfg) == []
    assert picked(dict(traced_cfg, rule={"a": {"skip": False}}))
    assert picked(dict(traced_cfg, rule={"a": {"scale": 3}}))[0][1]["scale"] == 3
    # The trace's yes does not bring back what a class or a rule left out.
    yes = classes.classify(entry(name="a", mag_scale1=2.0), cfg_of())
    assert (
        estimate.treated([yes], "all", dict(traced_cfg, rule={"a": {"skip": True}}), cfg_of()) == []
    )
    ramp = classes.classify(entry(w=256, h=1, name="a", mag_scale1=2.0), cfg_of())
    assert estimate.treated([ramp], "all", traced_cfg, cfg_of()) == []
    # An untraced entry, when the config says to skip those.
    off = classes.classify(entry(name="b"), cfg_of())
    skip_cfg = {"coverage": {"mode": "traced", "untraced": "skip"}}
    assert estimate.treated([off], "all", skip_cfg, cfg_of()) == []
    assert estimate.treated([off], "all", dict(skip_cfg, rule={"b": {"scale": 2}}), cfg_of())


def test_estimate_defaults_to_the_configs_preset(d, capsys):
    with fake_tree(d):
        G = game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000A1", 64, 64, texels=bytes(64 * 64 * 4))
        textures.run(inv_args(dump=[dump]))
        capsys.readouterr()
        conf = os.path.join(G.root, textures.DEFAULT_CONFIG)
        textures.run(est_args(preset=None))
        assert "preset all at" in capsys.readouterr().out
        write(conf, 'schema = 1\npreset = "ui"\n')
        textures.run(est_args(preset=None))
        assert "preset ui at" in capsys.readouterr().out
        # The command line wins; a config's own classes and rules are a preset.
        textures.run(est_args(preset="world"))
        assert "preset world at" in capsys.readouterr().out
        write(conf, 'schema = 1\npreset = "custom"\n[class.world]\nscale = 3\n')
        textures.run(est_args(preset=None, json=True))
        assert json.loads(capsys.readouterr().out)["classes"]["world"]["entries"] == 1
        write(conf, 'schema = 1\npreset = "nope"\n')
        raises(textures.run, est_args(preset=None), match="preset nope")


def test_the_switch_is_the_games(d):
    """With no such load, a chain's bytes are the guest's: not seen, packable."""
    with fake_tree(d):
        G = game_with()
        data, _, levels = chain(16, 16, 1)
        write_archive(
            G.game_files, "media", [("dds\\a.dds", dds_file(16, 16, b"DXT1", data, mips=levels), 0)]
        )
        textures.run(inv_args(source=["archive"]))
        e = load_doc(G)["entries"][0]
        assert e["status"] == inventory.NOT_SEEN and "hash_guess" not in e and e["skip"] == ""
        G = game_with(REENCODES)
        textures.run(inv_args(source=["archive"]))
        assert load_doc(G)["entries"][0]["skip"] == "needs_dump"


def test_content_naming_has_level_0_confirm_it(d):
    """Two chains that share every level from 16x16 down: only level 0 differs."""
    with fake_tree(d):
        G = game_with(REENCODES)
        low, low_px, low_levels = chain(16, 16, 1)
        top_a, top_b = dxt1_blocks(32, 32, 2), dxt1_blocks(32, 32, 3)
        levels = low_levels + 1
        write_archive(
            G.game_files,
            "media",
            [
                ("dds\\a.dds", dds_file(32, 32, b"DXT1", top_a + low, mips=levels), 0),
                ("dds\\b.dds", dds_file(32, 32, b"DXT1", top_b + low, mips=levels), 0),
            ],
        )
        px = bgra(bytes(dds.decode_level(b"DXT1", 32, 32, top_a))) + low_px
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000DD", 32, 32, levels=levels, texels=px)
        textures.run(inv_args(dump=[dump]))
        e = by_hash(load_doc(G))
        assert [o["name"] for o in e["00000000000000DD"]["origins"]] == ["dds/a.dds"]
        assert e[blocks_hash(top_b + low)]["status"] == inventory.NOT_SEEN


def test_scan_keeps_a_guess_blocks_only_for_a_shape_a_dump_holds(d):
    with fake_tree(d):
        G = game_with(REENCODES)
        data, px, levels = chain(16, 16, 1)
        other, _, other_levels = chain(32, 32, 1)
        write_archive(
            G.game_files,
            "media",
            [
                ("dds\\a.dds", dds_file(16, 16, b"DXT1", data, mips=levels), 0),
                ("dds\\b.dds", dds_file(32, 32, b"DXT1", other, mips=other_levels), 0),
            ],
        )
        dump = os.path.join(d, "scene")
        dump_texture(dump, "00000000000000DD", 16, 16, levels=levels, texels=px)
        feeders, dumps = inventory.open_sources(
            classes.game_config(G.m), ["archive", "dump"], [dump], G.game_files, G.root
        )
        rows = {c.name: c for c in inventory.scan(feeders, dumps, True)}
        assert rows["dds/a.dds"].top and rows["dds/a.dds"].probe
        assert rows["dds/b.dds"].guess and rows["dds/b.dds"].top is None


@pytest.mark.parametrize(
    "kw,match",
    [
        (dict(write_rules="x.toml"), "go with --propose"),
        (dict(untraced="skip"), "go with --propose"),
        (dict(propose=2, json=True), "run them apart"),
        (dict(propose=2, json=True, write_rules="x.toml"), "run them apart"),
        (dict(propose=0), "render scale"),
    ],
)
def test_propose_options_are_refused_not_ignored(d, kw, match):
    with fake_tree(d):
        game_with('\n[enhance.textures]\nsources = ["dump"]\n')
        raises(textures.run, inv_args(dump=[os.path.join(d, "none")], **kw), match=match)


def test_the_owners_rules_come_after_the_block(d):
    path = os.path.join(d, "c.toml")
    owner = 'schema = 1\n\n[class.ui]\nscale = 2\n\n# Mine.\n[rule."dds/*"]\nscale = 3\n'
    write(path, owner)
    choices = {"00000000000000A1": 2}
    textures._write_rules(path, choices)
    conf = classes.load_config(path)
    assert list(conf["rule"]) == ["hash:00000000000000A1", "dds/*"]
    text = open(path, encoding="utf-8").read()
    assert "# Mine.\n[rule." in text and text.index(traced.END) < text.index("# Mine.")
    # So the owner's glob has the last word over a proposal.
    e = classes.classify(entry(name="dds/a"), cfg_of())
    assert classes.treatment(e, conf)["scale"] == 3
    # Written again it is the same, whatever the proposals' order.
    textures._write_rules(path, choices)
    assert open(path, encoding="utf-8").read() == text


def test_a_hash_the_owner_rules_stays_theirs(d):
    path = os.path.join(d, "c.toml")
    write(path, 'schema = 1\n[rule."hash:00000000000000a1"]\nscale = 4\n')
    textures._write_rules(path, {"00000000000000A1": 2, "00000000000000A2": 3})
    conf = classes.load_config(path)
    assert conf["rule"]["hash:00000000000000a1"] == {"scale": 4}
    assert conf["rule"]["hash:00000000000000A2"] == {"scale": 3}


def test_a_refused_write_leaves_no_tmp(d):
    bad = os.path.join(d, "bad.toml")
    write(bad, "this is not toml")
    raises(textures._write_rules, bad, {})
    write(bad, "schema = 99\n")
    raises(textures._write_rules, bad, {}, match="schema")
    assert os.listdir(d) == ["bad.toml"]


def test_a_bad_xtd_is_a_problem_and_no_texels(d):
    dump = os.path.join(d, "scene")
    dump_texture(dump, "00000000000000A1", 64, 64, texels=bytes(64 * 64 * 4))
    dump_texture(dump, "00000000000000A2", 64, 64, texels=bytes(64 * 64 * 4))
    with open(os.path.join(dump, "00000000000000A2.xtd"), "r+b") as f:
        f.truncate(100)
    src = DumpSource(dump)
    assert len(src.problems) == 1 and "00000000000000A2.xtd" in src.problems[0]
    assert src.textures["00000000000000A2"].path is None
    assert [e.name for e in src.entries] == ["00000000000000A1.xtd"]
    rows, _ = inventory.join([], [src])
    cfg = classes.game_config({})
    assert classes.classify(rows["00000000000000A1"], cfg)["skip"] == ""
    bad = classes.classify(rows["00000000000000A2"], cfg)
    assert bad["has_texels"] is False and bad["skip"] == "no_texels"
    # A rule cannot bring it back: there is nothing to upscale.
    assert classes.treatment(bad, {"rule": {"hash:00000000000000A2": {"skip": False}}})["skip"]


def test_a_bad_xtd_with_a_source_stays_packable(d):
    data = dxt1_blocks(64, 64, 1)
    dump = os.path.join(d, "scene")
    h = blocks_hash(data)
    dump_texture(dump, h, 64, 64, texels=bytes(64 * 64 * 4))
    with open(os.path.join(dump, h + ".xtd"), "r+b") as f:
        f.truncate(100)
    src = DumpSource(dump)
    rows, _ = inventory.join([cand(dds_file(64, 64, b"DXT1", data))], [src])
    e = classes.classify(rows[h], classes.game_config({}))
    assert src.problems and e["status"] == inventory.JOINED and e["skip"] == ""


def test_a_cube_is_never_packable():
    e = classes.classify(entry(cube=True, name="a"), cfg_of())
    assert "cube" in e["facts"] and e["skip"] == "cube"
    assert classes.treatment(e, {"rule": {"a": {"skip": False}}})["skip"] == "cube"
    assert classes.classify(entry(cube=False, name="a"), cfg_of())["skip"] == ""


def test_a_rule_on_a_linear_dump_entry_builds_nothing(d):
    dump = os.path.join(d, "scene")
    dump_texture(dump, "00000000000000A1", 64, 64, "LIN_A8R8G8B8")
    rows, _ = inventory.join([], [DumpSource(dump)])
    e = classes.classify(rows["00000000000000A1"], classes.game_config({}))
    assert e["skip"] == "linear"
    rule = {"rule": {"hash:00000000000000A1": {"skip": False}}}
    assert classes.treatment(e, rule)["skip"] == "no_texels"
    assert est([e], config=rule)["total_bytes"] == 0
    # With a source to read, the rule stands (D3).
    named = classes.classify(entry("LIN_A8R8G8B8", name="a", has_texels=False), cfg_of())
    assert named["skip"] == "linear"
    assert classes.treatment(named, {"rule": {"a": {"skip": False}}})["skip"] == ""


def test_loose_leaves_out_a_link_to_another_drive(d, monkeypatch):
    write(os.path.join(d, "art", "a.dds"), "x")

    def other_drive(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", other_drive)
    assert LooseSource(d, ["art/*"]).entries == []


def test_the_hud_preset_is_what_enhance_hud_reads():
    hud = {"textures": ["dds/*"], "scale": 4, "languages": {"us": ["lng_us"], "fr": ["lng_fr"]}}
    cfg = classes.game_config({"enhance": {"hud": hud}})
    flat = entry(name="dds/a")
    assert classes.in_preset(flat, "hud", cfg)
    # A mipped file: the dump's one bound level, and enhance hud refuses the file.
    mipped = entry(name="dds/b")
    mipped["origins"][0]["levels"] = 6
    assert not classes.in_preset(mipped, "hud", cfg)
    mipped["origins"].append({"source": "media", "name": "dds/c"})
    assert classes.in_preset(mipped, "hud", cfg)
    # As its --language, the first listed unless a config picks.
    fr = classes.classify(entry(name="dds/d", source="lng_fr"), cfg)
    assert fr["language"] == "fr"
    assert classes.treatment(fr, {}, "hud", cfg)["skip"] == "language fr not picked"
    assert classes.treatment(fr, {"coverage": {"languages": ["fr"]}}, "hud", cfg)["skip"] == ""
    assert classes.treatment(fr, {}, "all", cfg)["skip"] == ""
