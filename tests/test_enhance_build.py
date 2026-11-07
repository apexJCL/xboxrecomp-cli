"""The pack's parts (texture-upscale phase 3): v3, the encoders, the chain, the
pad, the alpha split and the built-in upscalers, on synthetic texels."""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tomllib

import pytest

from xboxrecomp_cli import host
from xboxrecomp_cli.enhance import (
    alpha,
    bc1,
    bc3,
    build,
    compare,
    edges,
    estimate,
    hud,
    mips,
    pack,
    png,
    scaler,
    textures,
    textures_cfg,
)
from xboxrecomp_cli.enhance.upscalers import BUILTINS, Upscaler, UpscalerError, command, get
from xboxrecomp_cli.enhance.upscalers.builtin import kernel, resample


def noise(w, h, seed=1, opaque=False):
    r = random.Random(seed)
    out = bytearray(r.randbytes(w * h * 4))
    if opaque:
        out[3::4] = b"\xff" * (w * h)
    return bytes(out)


def flat(w, h, px):
    return bytes(px) * (w * h)


def decode_bc1(block):
    """A block's 16 RGB texels and whether any is the transparent index."""
    c0 = int.from_bytes(block[0:2], "little")
    c1 = int.from_bytes(block[2:4], "little")
    bits = int.from_bytes(block[4:8], "little")

    def rgb(c):
        return ((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31)

    a, b = rgb(c0), rgb(c1)
    if c0 > c1:
        pal = [a, b, tuple((2 * x + y) // 3 for x, y in zip(a, b))]
        pal.append(tuple((x + 2 * y) // 3 for x, y in zip(a, b)))
        transparent = False
    else:
        pal = [a, b, tuple((x + y) // 2 for x, y in zip(a, b)), (0, 0, 0)]
        transparent = any((bits >> (2 * i)) & 3 == 3 for i in range(16))
    return [pal[(bits >> (2 * i)) & 3] for i in range(16)], transparent


def test_bc1_vector_and_never_transparent():
    px = bytes((255, 0, 0, 255)) * 4 + bytes((0, 0, 255, 255)) * 12
    assert bc1.encode(4, 4, px).hex().upper() == "00F81F0000555555"
    r = random.Random(3)
    blocks = [noise(4, 4, s) for s in range(200)]
    # A flat block, where the two end colours fit to one, is the trap: c0 == c1
    # is the form with a transparent index.
    blocks += [
        flat(4, 4, (r.randrange(256), r.randrange(256), r.randrange(256), 255)) for _ in range(50)
    ]
    blocks += [flat(4, 4, (0, 0, 0, 255)), flat(4, 4, (255, 255, 255, 255))]
    for px in blocks:
        b = bc1.encode(4, 4, px)
        c0 = int.from_bytes(b[0:2], "little")
        c1 = int.from_bytes(b[2:4], "little")
        _, transparent = decode_bc1(b)
        assert not transparent
        # The opaque form, except where no colour pair can be: all-zero stays
        # opaque as long as no texel uses index 3.
        assert c0 > c1 or (c0 == c1 and int.from_bytes(b[4:8], "little") == 0)


def test_bc1_and_bc3_level_sizes():
    for w, h in ((1, 1), (2, 1), (3, 5), (8, 4)):
        assert len(bc1.encode(w, h, noise(w, h))) == bc1.size(w, h)
        assert len(bc3.encode(w, h, noise(w, h))) == bc3.size(w, h)


def test_bc3_vector_holds():
    px = bytes.fromhex(
        "0aff00002ff2031f54e50c2f79d81b3f9ecb304fc3be4b00e8b16c6f0da4937f"
        "3297c08f578af39f7c7d2cafa1706bbfc663b0cfeb56fbdf10494cef353ca3ff"
    )
    assert bc3.encode(4, 4, px).hex() == "ff00f9ed94e42601ffe98017d5efbbb2"


def test_pack_v3_round_trip(tmp_path):
    p = str(tmp_path / "t.pack")
    base = pack.payload_bytes(8, 4, 4, pack.FLAG_BC1)
    assert base == 16 + 8 + 8 + 8
    world = pack.PackEntry(
        0x20, 8, 4, bytes(base), levels=4, name="w", kind=pack.KIND_WORLD, bc1=True
    )
    ui = pack.PackEntry(0x10, 1, 1, b"\0\0\0\xff", name="u", kind=pack.KIND_ART)
    bc = pack.PackEntry(
        0x30, 4, 4, bytes(16 + 16 + 16), levels=3, name="b", kind=pack.KIND_TEXT, bc3=True
    )
    pack.write(p, [world, bc, ui], 2, 2, version=pack.VERSION_CHAINS)
    version, scale, text_scale, cls, rows = pack.read_index(p)
    assert (version, scale, text_scale) == (3, 2, 2)
    assert cls == 1 << pack.KIND_ART | 1 << pack.KIND_TEXT | 1 << pack.KIND_WORLD
    assert [r[0] for r in rows] == [0x10, 0x20, 0x30]
    assert rows[1][4] == pack.KIND_WORLD | pack.FLAG_BC1
    assert rows[2][3] == 3


def test_pack_v2_refuses_what_only_v3_holds(tmp_path):
    p = str(tmp_path / "t.pack")
    for e in (
        pack.PackEntry(1, 4, 4, bytes(8), kind=pack.KIND_ART, bc1=True, name="a"),
        pack.PackEntry(2, 1, 1, bytes(4), kind=pack.KIND_WORLD, name="b"),
        pack.PackEntry(3, 4, 4, bytes(32), levels=2, bc3=True, name="c"),
    ):
        with pytest.raises(ValueError, match="needs texture pack version 3"):
            pack.write(p, [e], 2)


def test_pack_refuses_partial_blocks_and_two_formats(tmp_path):
    p = str(tmp_path / "t.pack")
    with pytest.raises(ValueError, match="BC1 entry o is 6x4"):
        pack.write(p, [pack.PackEntry(1, 6, 4, bytes(16), bc1=True, name="o")], 2, version=3)
    with pytest.raises(ValueError, match="two block formats"):
        pack.write(
            p, [pack.PackEntry(1, 4, 4, bytes(8), bc1=True, bc3=True, name="o")], 2, version=3
        )


def test_read_index_catches_a_broken_file(tmp_path):
    p = str(tmp_path / "t.pack")
    pack.write(p, [pack.PackEntry(1, 2, 2, bytes(16), name="a")], 2, version=3)
    good = open(p, "rb").read()
    with open(p, "wb") as f:
        f.write(good[:-1])
    with pytest.raises(ValueError, match="past the end"):
        pack.read_index(p)


def test_mips_full_keep_none_and_weights():
    w, h = 16, 8
    rgba = noise(w, h, opaque=True)
    assert [lv[:2] for lv in mips.chain(w, h, rgba, "full")] == [
        (16, 8),
        (8, 4),
        (4, 2),
        (2, 1),
        (1, 1),
    ]
    assert [lv[:2] for lv in mips.chain(w, h, rgba, "keep", 2)] == [(16, 8), (8, 4)]
    assert [lv[:2] for lv in mips.chain(w, h, rgba, "none")] == [(16, 8)]
    # Colour under a transparent texel counts for nothing: red stays red.
    tex = bytes((255, 0, 0, 255)) + bytes((0, 255, 0, 0)) * 3
    _, _, out = mips.down2(2, 2, tex)
    assert out[0:3] == bytes((255, 0, 0))
    assert out[3] == 64 or out[3] == 63


def test_alpha_split_keeps_coverage_and_extends_colour():
    w, h = 16, 16
    rgba = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            if 4 <= x < 12 and 4 <= y < 12:
                rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4] = bytes((200, 40, 40, 255))
    rgba = bytes(rgba)
    ext = alpha.extend_colour(w, h, rgba)
    # Colour bleeds under the transparent neighbours, alpha stays.
    assert ext[(4 * w + 3) * 4 : (4 * w + 3) * 4 + 3] == bytes((200, 40, 40))
    assert ext[3::4] == rgba[3::4]
    up = get("lanczos").upscale_one(w, h, ext, 2, {})
    out = alpha.binarize(up, alpha.coverage(rgba))
    a, b = alpha.coverage(rgba), alpha.coverage(out)
    assert abs(a - b) <= 0.01


def test_edges_wrap_mirror_clamp():
    w, h = 4, 2
    rgba = bytes(i for i in range(w * h) for _ in range(4))
    pw, ph, out = edges.pad(w, h, rgba, "wrap", "clamp", 2)
    assert (pw, ph) == (8, 6)

    def at(x, y):
        return out[(y * pw + x) * 4]

    # Left of texel 0 sits texel 3 (wrap), never its mirror.
    assert at(1, 2) == 3
    assert at(0, 2) == 2
    assert at(6, 2) == 0
    # Above row 0 stays row 0 (clamp).
    assert at(2, 0) == 0
    _, _, m = edges.pad(w, h, rgba, "mirror", "clamp", 2)
    assert m[(2 * pw + 1) * 4] == 0
    assert m[(2 * pw + 0) * 4] == 1
    assert edges.crop(pw, ph, out, 2, 2, w, h) == rgba


def test_tiled_gradient_no_seam_under_wrap():
    """A texture tiled by the runtime: the row across the wrap keeps the
    gradient's step under `wrap`, jumps under `clamp`."""
    w, h = 16, 16
    rgba = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4] = bytes((x * 16, 0, 0, 255))
    rgba = bytes(rgba)
    up = get("lanczos")

    def seam(mode):
        pw, ph, padded = edges.pad(w, h, rgba, mode, mode, edges.PAD)
        out = up.upscale_one(pw, ph, padded, 2, {})
        n = edges.PAD * 2
        c = edges.crop(pw * 2, ph * 2, out, n, n, w * 2, h * 2)
        return abs(c[0] - c[(w * 2 - 1) * 4])

    assert seam("wrap") <= seam("clamp")


@pytest.mark.parametrize("up", BUILTINS, ids=lambda u: u.name)
def test_builtins_reproduce_and_size(up):
    w, h = 12, 8
    rgba = noise(w, h)
    for s in up.scales:
        out = up.upscale([(w, h, rgba)], s)
        assert out[0][:2] == (w * s, h * s)
        assert len(out[0][2]) == w * h * s * s * 4
        assert up.upscale([(w, h, rgba)], s) == out
    assert up.identity()["name"] == up.name
    assert up.reproducible_scope == "any"
    d = up.describe()
    assert d["name"] == up.name and d["kind"] == "builtin"


def test_xbr_defaults_are_the_kernel_the_hud_uses():
    rgba = noise(16, 16)
    for s in (2, 3, 4):
        assert get("xbr").upscale_one(16, 16, rgba, s, {}) == scaler.scale(16, 16, rgba, s)
    # A pin on the kernel's bytes: the hud's payloads hang on them.
    assert (
        hashlib.sha256(scaler.scale(16, 16, rgba, 4)).hexdigest()
        == hashlib.sha256(get("xbr").upscale_one(16, 16, rgba, 4, {})).hexdigest()
    )
    assert get("xbr").upscale_one(16, 16, rgba, 2, {"level": 1}) != scaler.scale(16, 16, rgba, 2)


def test_builtin_params_refused():
    with pytest.raises(UpscalerError, match="has no parameter"):
        get("nearest").check_params({"level": 1})
    with pytest.raises(UpscalerError, match="level"):
        get("xbr").check_params({"level": 9})


def test_lanczos_kernel_and_constant_image():
    for src, dst in ((1, 2), (1, 4), (4, 3), (4, 2), (3, 2)):
        taps = kernel(src, dst)
        assert taps
    # A constant image stays itself, up or down.
    for w2, h2 in ((16, 16), (6, 6), (11, 5)):
        out = resample(8, 8, flat(8, 8, (10, 200, 77, 255)), w2, h2)
        assert out == flat(w2, h2, (10, 200, 77, 255))
    out = resample(8, 8, flat(8, 8, (10, 200, 77, 128)), 12, 12)
    assert set(out[3::4]) == {128}


def test_v2_path_bytes_unchanged_by_the_new_argument(tmp_path):
    a = str(tmp_path / "a.pack")
    e = [pack.PackEntry(2, 1, 1, b"\1\2\3\4", name="x", kind=pack.KIND_TEXT)]
    sha = pack.write(a, e, 4, 2)
    with open(a, "rb") as f:
        data = f.read()
    assert hashlib.sha256(data).hexdigest() == sha
    assert data[4] == 2 and os.path.getsize(a) == 32 + 32 + 4
    assert data[28:32] == bytes(4)


# ---- the build --------------------------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import A, fake_tree, raises, write  # noqa: E402
from test_enhance import dds_file, dxt1_blocks, hud_args, hud_game, ttf, write_archive  # noqa: E402
from test_enhance_textures import dump_texture, game_with, inv_args  # noqa: E402


def build_args(**kw):
    return A(
        **dict(
            dict(
                verb="build",
                preset=None,
                config=None,
                inventory=None,
                dump=[],
                out=None,
                only=None,
                language=[],
                font=None,
                list=False,
                jobs=1,
                ignore_disk_space=False,
            ),
            **kw,
        )
    )


def payloads(path):
    """{hash: (w, h, levels, flags, payload)}, by the runtime's rules."""
    _, _, _, _, rows = pack.read_index(path)
    with open(path, "rb") as f:
        data = f.read()
    return {
        r[0]: (
            r[1],
            r[2],
            r[3],
            r[4],
            data[r[5] : r[5] + pack.payload_bytes(r[1], r[2], r[3], r[4])],
        )
        for r in rows
    }


def test_hud_preset_is_what_enhance_hud_makes(d, capsys):
    """D8's pin: art and font payloads byte for byte; only the header moved."""
    with fake_tree(d):
        G = hud_game(d)
        font_path = os.path.join(d, "test.ttf")
        with open(font_path, "wb") as f:
            f.write(ttf())
        assert hud.run(hud_args(font=font_path, out=os.path.join(d, "old"))) == 0
        assert textures.run(inv_args()) == 0
        assert textures.run(build_args(preset="hud", font=font_path)) == 0
        new = os.path.join(G.dist, "enhance", "hud", "hud.pack")
        old = os.path.join(d, "old", "hud.pack")
        assert pack.read_index(old)[0] == 2
        assert pack.read_index(new)[0] == 3
        assert payloads(new) == payloads(old)
        man = open(os.path.join(G.dist, "enhance", "hud", "hud-pack.toml"), encoding="utf-8").read()
        assert 'font = "test.ttf"' in man and "font (5 glyphs)" in man
        assert G.root not in man
        tomllib.loads(man)
        capsys.readouterr()


WORLD_TOML = """
[enhance.textures]
sources = ["archive", "dump"]
archives = ["media"]
[enhance.textures.classes]
world = ["world/*"]
ui = ["ui_*"]
"""


def world_game(d):
    G = game_with(WORLD_TOML)
    blocks = dxt1_blocks(16, 16)
    write_archive(
        G.game_files,
        "media",
        [
            ("world\\wall.dds", dds_file(16, 16, b"DXT1", blocks), False),
            ("ui_bar.dds", dds_file(16, 16, b"DXT3", bytes(16 * 16)), False),
        ],
    )
    return G


def world_config(d, text):
    path = os.path.join(d, "enhance-textures.toml")
    write(path, "schema = 1\n" + text)
    return path


def test_world_at_4x_is_bc1_with_a_chain_to_1x1(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(d, '[class.world]\nscale = 4\nupscaler = "lanczos"\n')
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        out_dir = os.path.join(G.dist, "enhance", "textures")
        path = os.path.join(out_dir, "textures.pack")
        version, scale, _, classes_word, rows = pack.read_index(path)
        assert version == 3 and classes_word & (1 << pack.KIND_WORLD)
        world = next(r for r in rows if r[4] & pack.KIND_MASK == pack.KIND_WORLD)
        assert world[1:4] == (64, 64, 7)
        assert world[4] & pack.FLAG_BC1
        rec = tomllib.loads(open(os.path.join(out_dir, "textures-pack.toml"), "rb").read().decode())
        assert rec["reproducible"] is True and rec["reproducible_scope"] == "any"
        assert rec["upscaler"]["lanczos"]["backend"] == "builtin"
        assert rec["classes"]["world"]["entries"] == 1
        assert rec["pack_sha256"] == hashlib.sha256(open(path, "rb").read()).hexdigest()
        assert (
            G.root not in open(os.path.join(out_dir, "textures-pack.toml"), encoding="utf-8").read()
        )
        capsys.readouterr()
        # Current until something moves; another job count, the same bytes.
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        assert "is current" in capsys.readouterr().out
        two = os.path.join(d, "two")
        assert textures.run(build_args(preset="all", config=cfgp, out=two, jobs=2)) == 0
        assert open(os.path.join(two, "textures.pack"), "rb").read() == open(path, "rb").read()


def test_list_shows_class_rule_and_reason(d, capsys):
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(d, '[rule."world/*"]\nscale = 3\n\n[rule."ui_*"]\nskip = true\n')
        capsys.readouterr()
        assert textures.run(build_args(preset="all", config=cfgp, list=True)) == 0
        out = capsys.readouterr().out
        wall = next(x for x in out.splitlines() if "wall" in x)
        assert " world " in wall and "rule=world/*" in wall and "3x lanczos" in wall
        bar = next(x for x in out.splitlines() if "ui_bar" in x)
        assert "skipped:" in bar and "rule=ui_*" in bar


def test_a_render_target_upload_is_skipped_until_a_rule_brings_it_back(d, capsys):
    target, plain = "00000000000000AB", "00000000000000AC"
    with fake_tree(d):
        G = game_with(WORLD_TOML)
        write_archive(G.game_files, "media", [("x.dds", b"not", False)])
        dumpd = os.path.join(d, "dump")
        texels = bytes((10, 20, 30, 255)) * 256
        dump_texture(dumpd, target, 16, 16, "A8R8G8B8", texels=texels, rt=True)
        dump_texture(dumpd, plain, 16, 16, "A8R8G8B8", texels=texels)
        assert textures.run(inv_args(dump=[dumpd])) == 0
        path = os.path.join(G.dist, "enhance", "textures", "textures.pack")

        assert textures.run(build_args(preset="all")) == 0
        assert sorted(payloads(path)) == [0xAC]
        capsys.readouterr()
        assert textures.run(build_args(preset="all", list=True)) == 0
        row = next(x for x in capsys.readouterr().out.splitlines() if target in x)
        assert "skipped:" in row and "render target" in row

        cfgp = world_config(d, '[rule."hash:%s"]\nskip = false\n' % target)
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        assert sorted(payloads(path)) == [0xAB, 0xAC]


def test_an_entry_only_a_dump_holds_is_built_from_it(d, capsys):
    with fake_tree(d):
        G = game_with(WORLD_TOML)
        write_archive(G.game_files, "media", [("x.dds", b"not", False)])
        dumpd = os.path.join(d, "dump")
        h = "00000000000000AB"
        texels = bytes((10, 20, 30, 255)) * 256
        dump_texture(dumpd, h, 16, 16, "A8R8G8B8", texels=texels, wrap_u="wrap", wrap_v="clamp")
        assert textures.run(inv_args(dump=[dumpd])) == 0
        assert textures.run(build_args(preset="all")) == 0
        path = os.path.join(G.dist, "enhance", "textures", "textures.pack")
        rows = payloads(path)
        w, ht, levels, flags, payload = rows[0xAB]
        # World defaults: 2x and the chain to 1x1, flat colour stays flat.
        assert (w, ht, levels) == (32, 32, 6)
        # A BGRA dump comes through as its red, 30 in five bits.
        assert flags & pack.FLAG_BC1 and payload[1] >> 3 == 4
        capsys.readouterr()


def test_auto_encode_trusts_the_texels(d):
    e = {"format": "DXT1", "facts": []}
    t = {"encode": "auto", "mips": "none"}
    clear = flat(4, 4, (1, 2, 3, 0))
    enc, note = build.resolve_encode(e, t, clear)
    assert enc == "bc3" and "use alpha" in note
    assert build.resolve_encode(e, t, flat(4, 4, (1, 2, 3, 255))) == ("bc1", "")


def test_partial_blocks_stay_raw(d):
    secs = {}
    e = {"format": "DXT1", "levels": 1, "facts": []}
    t = {"encode": "bc1", "mips": "none"}
    levels, payload, is_bc3, is_bc1, notes = build.encode_chain(
        6, 6, flat(6, 6, (1, 2, 3, 255)), e, t, secs
    )
    assert not is_bc1 and not is_bc3 and "not whole blocks" in notes[0]
    assert len(payload) == 6 * 6 * 4 and levels == 1


def test_job_alpha_split_and_down_to_scale():
    """A 4x-only upscaler reaches 3x inside the pad; a cutout's coverage holds."""
    w, h = 16, 16
    rgba = bytearray(w * h * 4)
    for y in range(4, 12):
        for x in range(4, 12):
            rgba[(y * w + x) * 4 : (y * w + x) * 4 + 4] = bytes((200, 40, 40, 255))
    rgba = bytes(rgba)

    class Four(Upscaler):
        name = "four"
        kind = "command"
        scales = (4,)
        alpha = "rgb"

        def upscale_one(self, iw, ih, img, s, params):
            return get("nearest").upscale_one(iw, ih, img, s, {})

    up = Four()
    t = {"scale": 3, "alpha": "binary", "edges": "clamp"}
    job = build.make_job(up, w, h, rgba, t, {})
    assert job.native == 4
    imgs = job.images()
    assert len(imgs) == 2 and imgs[0][0] == w + 2 * edges.PAD
    uw, uh, out = job.finish(up.upscale(imgs, 4))
    assert (uw, uh) == (48, 48)
    assert abs(alpha.coverage(out) - alpha.coverage(rgba)) <= 0.01
    # A clamp pad a built-in would add itself is saved.
    t = {"scale": 2, "alpha": "split", "edges": "clamp"}
    assert build.make_job(get("xbr"), w, h, rgba, t, {}).pad == 0
    t = {"scale": 2, "alpha": "split", "edges": "wrap"}
    assert build.make_job(get("xbr"), w, h, rgba, t, {}).pad == edges.PAD


def test_cells_are_padded_alone():
    w, h = 8, 4
    rgba = bytes((255, 0, 0, 255)) * (4 * 4)
    rgba = b"".join((bytes((255, 0, 0, 255)) * 4 + bytes((0, 0, 255, 255)) * 4) for _ in range(h))
    t = {"scale": 2, "alpha": "split", "edges": "cells", "cell": [4, 4]}
    job = build.make_job(get("lanczos"), w, h, rgba, t, {})
    imgs = job.images()
    assert len(imgs) == 2
    uw, uh, out = job.finish(get("lanczos").upscale(imgs, 2))
    assert (uw, uh) == (16, 8)
    # No bleed across the cell line: each side stays its own colour.
    assert out[7 * 4 : 7 * 4 + 3] == bytes((255, 0, 0))
    assert out[8 * 4 : 8 * 4 + 3] == bytes((0, 0, 255))


# ---- the config -------------------------------------------------------------


def test_config_refuses_machine_keys_and_merges_local(d):
    path = os.path.join(d, "config", "enhance-textures.toml")
    write(path, 'schema = 1\n[upscaler.mine]\npath = "/x/y"\n')
    with pytest.raises(textures_cfg.ConfigError, match="belongs in enhance-textures.local.toml"):
        textures_cfg.load(path)
    write(path, "schema = 1\n[upscaler.nearest]\n")
    write(textures_cfg.local_path_of(path), '[upscaler.mine]\npath = "/x/y"\n')
    c = textures_cfg.load(path)
    assert c.doc["upscaler"]["mine"]["path"] == "/x/y"
    assert c.local_path.endswith(textures_cfg.LOCAL_NAME)


@pytest.mark.parametrize(
    "text,match",
    [
        ("schema = 2\n", "schema"),
        ("wat = 1\n", "no key wat"),
        ("[class.nope]\nscale = 2\n", "the classes are"),
        ("[class.ui]\nscale = 5\n", "2, 3 or 4"),
        ("[class.ui]\nmips = 1\n", "none\\|keep\\|full"),
        ('[rule."a"]\nedges = "cells"\n', "needs cell"),
        ('[class.ui]\nupscaler = "nobody"\n', "no upscaler nobody"),
        ('[class.ui]\nupscaler = "xbr"\n[upscaler.xbr]\nlevel = 9\n', "level"),
        ("[budget]\npack_mb = -1\n", "pack_mb"),
    ],
)
def test_config_validation(d, text, match):
    path = os.path.join(d, "e.toml")
    write(path, text)
    with pytest.raises(textures_cfg.ConfigError, match=match):
        textures_cfg.load(path)


def test_writer_keeps_comments_and_reads_back(d):
    text = (
        "# my notes\nschema = 1\n\n[class.ui] # the menus\n"
        'upscaler = "xbr" # keep\nscale = 2\n\n[rule."a/*"]\nscale = 3\n'
    )
    new = textures_cfg.set_table_keys(text, "class.ui", {"upscaler": "lanczos", "mips": "none"})
    assert "# my notes" in new and "# the menus" in new and '"lanczos" # keep' in new
    assert tomllib.loads(new)["class"]["ui"] == {"upscaler": "lanczos", "scale": 2, "mips": "none"}
    # A new class table goes before the rules.
    new = textures_cfg.set_table_keys(text, "class.world", {"scale": 3})
    assert new.index("[class.world]") < new.index("[rule.")
    assert tomllib.loads(new)["class"]["world"] == {"scale": 3}
    path = os.path.join(d, "c", "e.toml")
    textures_cfg.write_class_row(path, "world", "lanczos", 3)
    assert textures_cfg.load(path).doc["class"]["world"] == {"upscaler": "lanczos", "scale": 3}
    with pytest.raises(textures_cfg.ConfigError):
        textures_cfg.write_class_row(path, "nope", "lanczos")


# ---- the command backend ----------------------------------------------------


FAKE_TOOL = """#!%s
import os, sys
sys.path.insert(0, %r)
from xboxrecomp_cli.enhance import png
from xboxrecomp_cli.enhance import scaler
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("faketool 1.2")
    sys.exit(0)
ind, outd, s = args[args.index("-i") + 1], args[args.index("-o") + 1], int(args[args.index("-s") + 1])
for n in sorted(os.listdir(ind)):
    w, h, rgba = png.read(os.path.join(ind, n))
    png.write(os.path.join(outd, n), w * s, h * s, scaler.scale_nearest(w, h, rgba, s))
"""


def fake_tool(d, name="faketool"):
    path = os.path.join(d, name)
    src = os.path.dirname(os.path.dirname(os.path.abspath(pack.__file__)))
    write(path, FAKE_TOOL % (sys.executable, os.path.dirname(src)))
    os.chmod(path, 0o755)
    return path


def test_template_is_split_before_substitution():
    nasty = "a b;$(rm -rf x)'\"`.png"
    argv = command.substitute(
        "tool -i {in} -o {out} -s {scale}", {"in": nasty, "out": "o", "scale": 2}
    )
    assert argv == ["tool", "-i", nasty, "-o", "o", "-s", "2"]
    with pytest.raises(UpscalerError, match="no value"):
        command.substitute("tool {model}", {})
    # One pass: a value that looks like a placeholder is still only a value.
    assert command.substitute("t {in} {out}", {"in": "{out}", "out": "o"}) == ["t", "{out}", "o"]


def test_an_owner_model_preset_takes_its_folder_from_the_local_table():
    tool = command.presets()["tool"]["upscayl-bin-owner"]
    assert "-m {model_dir}" in tool["template"] and tool["input_name"] == "data"
    argv = command.substitute(
        tool["template"],
        {"in": "i", "out": "o", "scale": 4, "gpu": 0, "model": "m", "model_dir": "/my models"},
    )
    assert "/my models" in argv
    with pytest.raises(UpscalerError, match="model_dir"):
        command.substitute(
            tool["template"], {"in": "i", "out": "o", "scale": 4, "gpu": 0, "model": "m"}
        )


def test_command_backend_runs_a_batch_and_records_identity(d):
    tool = fake_tool(d)
    table = {
        "fake": {
            "template": "faketool -i {in_dir} -o {out_dir} -s {scale}",
            "path": tool,
            "scales": [2, 4],
        }
    }
    up = command.from_config("fake", table, {})
    assert up.kind == "command" and up.batch > 1
    imgs = [(4, 4, noise(4, 4, opaque=True)), (8, 2, noise(8, 2, opaque=True))]
    out = up.upscale(imgs, 2)
    assert [(o[0], o[1]) for o in out] == [(8, 8), (16, 4)]
    idn = up.identity()
    assert idn["tool_version"] == "1.2" and len(idn["tool_sha256"]) == 64
    assert tool not in " ".join(str(v) for v in idn.values())


def test_a_tool_that_is_not_there_says_how(d, monkeypatch):
    monkeypatch.setenv("PATH", d)
    with pytest.raises(UpscalerError, match="downloads nothing"):
        command.from_config("realesrgan-ncnn-vulkan", {}, {})
    with pytest.raises(UpscalerError, match="no upscaler"):
        command.from_config("whoever", {}, {})


def test_a_tool_of_the_wrong_size_or_a_failure_is_said(d):
    path = os.path.join(d, "bad")
    write(path, "#!/bin/sh\necho broke >&2\nexit 3\n")
    os.chmod(path, 0o755)
    up = command.from_config(
        "bad", {"bad": {"template": "x -i {in_dir} -o {out_dir}", "path": path}}, {}
    )
    with pytest.raises(UpscalerError, match="exit 3: broke"):
        up.upscale([(4, 4, noise(4, 4, opaque=True))], 2)


def test_every_preset_is_a_template_with_a_licence():
    p = command.presets()
    for name, row in p["tool"].items():
        assert row["licence"], name
        assert row["homepage"], name
        assert row["binary"] and row["template"], name
    # Models are named, never fetched: nothing here is a download URL's file.
    for name, row in p["model"].items():
        assert row["licence"] and row["homepage"], name
    assert "custom" in p["model"]["4xHDCube4Compact"]["licence"]
    assert command.model_flag("CC-BY-NC-SA-4.0") and not command.model_flag("CC0-1.0")


def test_build_goes_through_a_command_and_says_it_is_not_reproducible(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        tool = fake_tool(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(
            d,
            '[class.world]\nscale = 2\nupscaler = "fake"\n\n[upscaler.fake]\nscales = [2]\n',
        )
        write(
            textures_cfg.local_path_of(cfgp),
            '[upscaler.fake]\ntemplate = "faketool -i {in_dir} -o {out_dir} -s {scale}"\n'
            'path = "%s"\n' % tool.replace("\\", "/"),
        )
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        out_dir = os.path.join(G.dist, "enhance", "textures")
        rec = tomllib.loads(open(os.path.join(out_dir, "textures-pack.toml"), "rb").read().decode())
        assert rec["reproducible"] is False and rec["reproducible_scope"] == "none"
        assert rec["upscaler"]["fake"]["backend"] == "command"
        assert (
            tool not in open(os.path.join(out_dir, "textures-pack.toml"), encoding="utf-8").read()
        )
        rows = pack.read_index(os.path.join(out_dir, "textures.pack"))[4]
        world = next(r for r in rows if r[4] & pack.KIND_MASK == pack.KIND_WORLD)
        assert world[1:3] == (32, 32)
        capsys.readouterr()
        raises(textures.run, build_args(preset="nope", config=cfgp), match="preset nope")


def test_a_thin_image_is_padded_for_a_tool_that_declares_it_and_cropped_back(d):
    tool = fake_tool(d)
    up = command.from_config(
        "fake",
        {"fake": {"template": "faketool -i {in_dir} -o {out_dir} -s {scale}", "path": tool}},
        {},
    )
    up.min_side = 8
    w, h = 16, 2
    img = noise(w, h, opaque=True)
    [(ow, oh, out)] = up.upscale([(w, h, img)], 2)
    assert (ow, oh) == (32, 4)
    assert out == scaler.scale_nearest(w, h, img, 2)


def test_a_plugin_of_another_contract_is_refused_with_how(d):
    path = os.path.join(d, "xbr-upscale-torch")
    write(path, "#!/bin/sh\necho 'xbr-upscale-torch 9 contract 2.0'\n")
    os.chmod(path, 0o755)
    up = command.from_config("torch", {"torch": {"path": path, "model": "x.safetensors"}}, {})
    with pytest.raises(UpscalerError, match="backend install torch"):
        up.identity()
    write(path, "#!/bin/sh\necho 'xbr-upscale-torch 9 contract 1.7'\n")
    up = command.from_config("torch", {"torch": {"path": path, "model": "/a/b/x.safetensors"}}, {})
    idn = up.identity()
    assert idn["model"] == "x.safetensors"
    assert "newer" in up.warning


# ---- compare, pick, backend ---------------------------------------------------


def compare_args(**kw):
    return A(
        **dict(
            dict(
                verb="compare",
                classes=[],
                upscaler=[],
                scale=None,
                samples=2,
                pick=[],
                crop=8,
                repeat=2,
                out=None,
                config=None,
                inventory=None,
                dump=[],
            ),
            **kw,
        )
    )


def test_compare_writes_grid_json_page_and_a_fake_command_column(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        tool = fake_tool(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(
            d,
            "[upscaler.fake]\nscales = [2, 4]\n",
        )
        write(
            textures_cfg.local_path_of(cfgp),
            '[upscaler.fake]\ntemplate = "faketool -i {in_dir} -o {out_dir} -s {scale}"\n'
            'path = "%s"\n' % tool.replace("\\", "/"),
        )
        out = os.path.join(d, "cmp")
        a = compare_args(
            config=cfgp,
            upscaler=["nearest", "lanczos", "fake"],
            scale=2,
            classes=["world"],
            out=out,
        )
        assert textures.run(a) == 0
        doc = json.load(open(os.path.join(out, "compare.json"), encoding="utf-8"))
        names = [c["name"] for c in doc["columns"]]
        assert names == ["nearest", "lanczos", "fake"]
        byname = {c["name"]: c for c in doc["columns"]}
        for c in doc["columns"]:
            for k in ("identity", "licence", "s_per_mtexel", "deterministic", "repeatable"):
                assert k in c
            assert c["s_per_mtexel"] is not None and not c["error"]
        assert byname["fake"]["kind"] == "command" and byname["fake"]["deterministic"] is False
        assert byname["fake"]["identity"]["tool_version"] == "1.2"
        assert tool not in json.dumps(doc)
        assert byname["nearest"]["repeatable"] is True
        g = doc["classes"]["world"]
        rows, cell = len(g["samples"]), g["cell"]
        assert cell == 16  # the 8-texel crop at 2x
        gw, gh, sheet = png.read(os.path.join(out, g["grid"]))
        assert (gw, gh) == (3 * cell + 4 * compare.GAP, rows * cell + (rows + 1) * compare.GAP)
        h = g["samples"][0]["hash"]
        for name in names:
            w2, h2, rgba = png.read(os.path.join(out, "full", name, h + ".png"))
            assert (w2, h2) == (32, 32)
            assert hashlib.sha256(rgba).hexdigest() == byname[name]["pixels"][h]
        page = open(os.path.join(out, "index.html"), encoding="utf-8").read()
        assert "keep this folder private" in page and "<script" not in page
        assert "grid_world.png" in page
        timings = json.load(open(os.path.join(G.logs, "timings.json"), encoding="utf-8"))
        assert estimate.RATE_PREFIX + "fake" in timings


def test_compare_samples_do_not_depend_on_the_run_and_a_pick_comes_first(d, capsys):
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        docs = []
        for name in ("a", "b"):
            out = os.path.join(d, name)
            a = compare_args(upscaler=["nearest"], repeat=1, out=out, pick=["ui_bar*"])
            assert textures.run(a) == 0
            docs.append(json.load(open(os.path.join(out, "compare.json"), encoding="utf-8")))
        assert docs[0]["classes"] == docs[1]["classes"]
        assert docs[0]["columns"][0]["pixels"] == docs[1]["columns"][0]["pixels"]
        assert docs[0]["classes"]["ui"]["samples"][0]["why"] == "pick"


def test_compare_a_failing_command_is_a_column_not_the_end_of_the_run(d, capsys):
    with fake_tree(d):
        world_game(d)
        bad = os.path.join(d, "badtool")
        write(bad, "#!/bin/sh\nexit 3\n")
        os.chmod(bad, 0o755)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(d, "[upscaler.bad]\nscales = [2]\n")
        write(
            textures_cfg.local_path_of(cfgp),
            '[upscaler.bad]\ntemplate = "badtool -i {in_dir} -o {out_dir}"\npath = "%s"\n' % bad,
        )
        out = os.path.join(d, "c")
        a = compare_args(config=cfgp, upscaler=["box", "bad"], scale=2, repeat=1, out=out)
        assert textures.run(a) == 0
        doc = json.load(open(os.path.join(out, "compare.json"), encoding="utf-8"))
        col = {c["name"]: c for c in doc["columns"]}
        assert col["bad"]["error"] and not col["box"]["error"]
        assert "failed:" in capsys.readouterr().out


def test_a_preset_whose_tool_is_absent_is_named_not_run():
    ups, missing = compare.columns([], {"upscaler": {"nope": {"preset": "chainner"}}})
    assert {u.name for u in BUILTINS} <= {u.name for u in ups}
    assert any(n == "nope" and "not installed" in w for n, w in missing)


def test_busiest_crop_finds_the_detail():
    w, h = 32, 32
    px = bytearray(flat(w, h, (9, 9, 9, 255)))
    block = noise(4, 4, 5, opaque=True)
    for y in range(4):
        o = ((20 + y) * w + 24) * 4
        px[o : o + 16] = block[y * 16 : (y + 1) * 16]
    x, y, cw, ch = compare.busiest_crop(w, h, bytes(px), 8)
    assert (cw, ch) == (8, 8) and x <= 24 < x + 8 and y <= 20 < y + 8
    assert compare.busiest_crop(4, 4, noise(4, 4), 8) == (0, 0, 4, 4)


def test_pick_writes_the_class_row_and_it_reads_back(d, capsys):
    with fake_tree(d):
        world_game(d)
        cfgp = world_config(d, "# my note\n")
        assert (
            textures.run(A(verb="pick", cls="world", upscaler="lanczos", scale=3, config=cfgp)) == 0
        )
        text = open(cfgp, encoding="utf-8").read()
        assert "# my note" in text
        conf = textures_cfg.load(cfgp)
        assert conf.doc["class"]["world"] == {"upscaler": "lanczos", "scale": 3}
        assert (
            textures.run(A(verb="pick", cls="world", upscaler="xbr", scale=None, config=cfgp)) == 0
        )
        assert textures_cfg.load(cfgp).doc["class"]["world"]["upscaler"] == "xbr"


def test_backend_lists_and_prints_the_plugin_line_and_runs_nothing(d, capsys, monkeypatch):
    def no_run(*a, **k):
        raise AssertionError("backend install runs nothing")

    monkeypatch.setattr(subprocess, "run", no_run)
    monkeypatch.setattr(subprocess, "Popen", no_run)
    with fake_tree(d):
        world_game(d)
        assert textures.run(A(verb="backend", action="install", what="torch")) == 0
        out = capsys.readouterr().out
        assert "uv tool install" in out and "#subdirectory=plugins/torch" in out
        assert "download.pytorch.org/whl/cu" in out and "rocm" in out
        for pin in command.PLUGIN_PINS:
            assert pin in out
        monkeypatch.undo()
        assert textures.run(A(verb="backend", action="list", what=None)) == 0
        out = capsys.readouterr().out
        assert "xbr" in out and "realesrgan-ncnn-vulkan" in out and "check its licence" in out


def test_the_plugins_pins_are_the_ones_the_line_prints():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "plugins", "torch", "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)["project"]
    assert tuple(project["dependencies"]) == command.PLUGIN_PINS
    assert not any("extra_arches" in x for x in project["dependencies"])


def test_cli_neither_depends_on_nor_imports_torch():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)["project"]
    assert not any("torch" in x or "spandrel" in x for x in project.get("dependencies", []))
    src = os.path.dirname(os.path.abspath(pack.__file__))
    for base, _, files in os.walk(os.path.dirname(src)):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(base, name), encoding="utf-8") as f:
                    text = f.read()
                assert "import torch" not in text and "import spandrel" not in text, name


def _extend_one_scan_per_pass(w, h, rgba):
    """The rule spelled out: every pass looks at every empty texel."""
    out = bytearray(rgba)
    known = [rgba[i * 4 + 3] != 0 for i in range(w * h)]
    while True:
        filled = []
        for i in range(w * h):
            if known[i]:
                continue
            y, x = divmod(i, w)
            hit = next(
                (
                    (ny * w + nx) * 4
                    for ny in (y, y - 1, y + 1)
                    if 0 <= ny < h
                    for nx in (x, x - 1, x + 1)
                    if 0 <= nx < w and known[ny * w + nx]
                ),
                None,
            )
            if hit is not None:
                filled.append((i, hit))
        if not filled:
            return bytes(out)
        for i, src in filled:
            out[i * 4 : i * 4 + 3] = out[src : src + 3]
            known[i] = True


def test_extend_colour_is_the_rule_on_scattered_cutouts():
    import random

    r = random.Random(7)
    for w, h, density in ((17, 9, 0.02), (32, 32, 0.005), (5, 40, 0.1), (1, 12, 0.1)):
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            colour = r.randrange(1 << 24).to_bytes(3, "big")
            rgba[i * 4 : i * 4 + 3] = colour
            rgba[i * 4 + 3] = r.choice((64, 255)) if r.random() < density else 0
        if not any(rgba[3::4]):
            rgba[3] = 255
        rgba = bytes(rgba)
        assert alpha.extend_colour(w, h, rgba) == _extend_one_scan_per_pass(w, h, rgba)


# ---- what a build's key follows ------------------------------------------------


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _current(capsys, *args, **kw):
    capsys.readouterr()
    assert textures.run(build_args(*args, **kw)) == 0
    return "is current" in capsys.readouterr().out


def test_a_second_build_of_the_same_inputs_is_kept(d, capsys):
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="all")
        assert _current(capsys, preset="all")


def test_a_moved_class_pattern_rebuilds(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="all")
        before = payloads(os.path.join(G.dist, "enhance", "textures", "textures.pack"))
        G = game_with(
            WORLD_TOML.replace('world = ["world/*"]', 'world = ["nothing/*"]').replace(
                'ui = ["ui_*"]', 'ui = ["ui_*", "world/*"]'
            )
        )
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="all")
        after = payloads(os.path.join(G.dist, "enhance", "textures", "textures.pack"))
        assert before != after


def test_a_game_toml_hud_scale_rebuilds(d, capsys):
    with fake_tree(d):
        G = hud_game(d)
        font = os.path.join(d, "test.ttf")
        with open(font, "wb") as f:
            f.write(ttf())
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="hud", font=font)
        assert _current(capsys, preset="hud", font=font)
        G.m["enhance"]["hud"]["scale"] = 2 if G.m["enhance"]["hud"].get("scale", 4) != 2 else 4
        assert not _current(capsys, preset="hud", font=font)


def test_a_recorded_address_mode_rebuilds(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="all")
        path = os.path.join(G.dist, "enhance", "inventory.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for e in doc["entries"]:
            if e["class"] == "world":
                e["wrap_u"] = e["wrap_v"] = "clamp"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        assert not _current(capsys, preset="all")


def test_the_fonts_bytes_rebuild(d, capsys):
    with fake_tree(d):
        hud_game(d)
        font = os.path.join(d, "test.ttf")
        with open(font, "wb") as f:
            f.write(ttf())
        assert textures.run(inv_args()) == 0
        assert not _current(capsys, preset="hud", font=font)
        assert _current(capsys, preset="hud", font=font)
        # The same name and face: only the bytes say it is another font.
        with open(font, "ab") as f:
            f.write(b"\0\0\0\0")
        assert not _current(capsys, preset="hud", font=font)


def test_a_dumps_texels_rebuild_and_a_new_dump_brings_what_was_left_out(d, capsys):
    with fake_tree(d):
        game_with(WORLD_TOML)
        write_archive(game_with(WORLD_TOML).game_files, "media", [("x.dds", b"not", False)])
        dumpd = os.path.join(d, "dump")
        h = "00000000000000AB"
        dump_texture(dumpd, h, 16, 16, "A8R8G8B8", texels=bytes((10, 20, 30, 255)) * 256)
        assert textures.run(inv_args(dump=[dumpd])) == 0
        assert not _current(capsys, preset="all")
        assert _current(capsys, preset="all")
        dump_texture(dumpd, h, 16, 16, "A8R8G8B8", texels=bytes((90, 20, 30, 255)) * 256)
        assert not _current(capsys, preset="all")


def _fake_config(d, tool, extra=""):
    cfgp = world_config(
        d, '[class.world]\nscale = 2\nupscaler = "fake"\n\n[upscaler.fake]\nscales = [2]\n'
    )
    write(
        textures_cfg.local_path_of(cfgp),
        '[upscaler.fake]\ntemplate = "faketool -i {in_dir} -o {out_dir} -s {scale}"\n'
        'path = "%s"\n%s' % (tool.replace("\\", "/"), extra),
    )
    return cfgp


def test_the_tools_and_the_models_bytes_rebuild(d, capsys):
    with fake_tree(d):
        world_game(d)
        tool = fake_tool(d)
        model = os.path.join(d, "m.safetensors")
        write(model, "one")
        assert textures.run(inv_args()) == 0
        cfgp = _fake_config(d, tool, 'model = "%s"\n' % model.replace("\\", "/"))
        assert not _current(capsys, preset="all", config=cfgp)
        assert _current(capsys, preset="all", config=cfgp)
        write(model, "two")
        assert not _current(capsys, preset="all", config=cfgp)
        assert _current(capsys, preset="all", config=cfgp)
        with open(tool, "a", encoding="utf-8") as f:
            f.write("# another build of it\n")
        assert not _current(capsys, preset="all", config=cfgp)


def test_an_ncnn_models_files_in_its_folder_are_part_of_the_key(d, monkeypatch):
    # From a folder holding an unrelated m/: a bare name is not the cwd's.
    cwd = os.path.join(d, "cwd")
    write(os.path.join(cwd, "m", "unrelated"), "x")
    monkeypatch.chdir(cwd)
    tool = fake_tool(d)
    folder = os.path.join(d, "models")
    write(os.path.join(folder, "m.param"), "a")
    write(os.path.join(folder, "other.param"), "b")
    table = {"fake": {"template": "t {in_dir} {out_dir}", "path": tool, "model": "m"}}
    table["fake"]["model_dir"] = folder
    key = command.from_config("fake", table, {}).input_key()
    assert sorted(key["model"]) == ["m.param"]
    write(os.path.join(folder, "m.param"), "c")
    assert command.from_config("fake", table, {}).input_key() != key
    assert folder not in json.dumps(key)


# ---- the tracked file names; the local file says what runs ---------------------


@pytest.mark.parametrize(
    "line",
    [
        "template = \"sh -c 'touch MARK' {in_dir} {out_dir}\"",
        'binary = "sh"',
        'model = "/some/where/x.safetensors"',
        'model = "realesrgan-x4plus"',
    ],
)
def test_a_tracked_file_says_nothing_of_what_runs(d, line):
    cfgp = world_config(d, '[class.world]\nupscaler = "evil"\n[upscaler.evil]\n' + line + "\n")
    with pytest.raises(textures_cfg.ConfigError, match=textures_cfg.LOCAL_NAME):
        textures_cfg.load(cfgp)


def test_a_tracked_template_is_refused_before_anything_runs(d):
    marker = os.path.join(d, "ran")
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(
            d,
            '[class.world]\nscale = 2\nupscaler = "evil"\n[upscaler.evil]\n'
            "template = \"sh -c 'touch %s' {in_dir} {out_dir}\"\n" % marker.replace("\\", "/"),
        )
        raises(textures.run, build_args(preset="all", config=cfgp), match=textures_cfg.LOCAL_NAME)
    assert not os.path.exists(marker)


def test_a_preset_is_still_chosen_by_name_from_the_tracked_file(d):
    cfgp = world_config(
        d,
        '[class.world]\nupscaler = "esrgan"\n[upscaler.esrgan]\n'
        'preset = "realesrgan-ncnn-vulkan"\ntile = 128\n',
    )
    conf = textures_cfg.load(cfgp)
    assert conf.doc["upscaler"]["esrgan"]["preset"] == "realesrgan-ncnn-vulkan"


def test_pick_can_name_a_table_only_the_local_file_has(d):
    cfgp = os.path.join(d, "enhance-textures.toml")
    write(textures_cfg.local_path_of(cfgp), '[upscaler.mine]\ntemplate = "t {in_dir} {out_dir}"\n')
    textures_cfg.write_class_row(cfgp, "world", "mine", 2)
    with open(cfgp, encoding="utf-8") as f:
        assert "template" not in f.read()
    assert textures_cfg.load(cfgp).doc["class"]["world"]["upscaler"] == "mine"


@pytest.mark.parametrize("value", ["2.0", "true", '"2"'])
def test_a_scale_is_a_whole_number(d, value):
    with pytest.raises(textures_cfg.ConfigError, match="scale"):
        textures_cfg.load(world_config(d, "[class.world]\nscale = %s\n" % value))


def test_a_tables_numbers_are_checked(d):
    cfgp = world_config(
        d,
        '[class.world]\nupscaler = "esrgan"\n[upscaler.esrgan]\n'
        'preset = "realesrgan-ncnn-vulkan"\nscales = [2.0]\n',
    )
    with pytest.raises(textures_cfg.ConfigError, match="scales"):
        textures_cfg.load(cfgp)


# ---- an unrecorded address mode ----------------------------------------------


def test_a_world_entry_with_no_recorded_mode_wraps_and_the_rest_clamp():
    assert edges.resolve("auto", {"class": "world"}) == ("wrap", "wrap")
    assert edges.resolve("auto", {"class": "ui"}) == ("clamp", "clamp")
    assert edges.resolve("auto", {"class": "text"}) == ("clamp", "clamp")
    # A recorded mode always wins, one axis at a time.
    seen = {"class": "world", "wrap_u": "clamp_edge"}
    assert edges.resolve("auto", seen) == ("clamp", "wrap")
    assert edges.resolve("clamp", {"class": "world"}) == ("clamp", "clamp")
    assert edges.defaulted({"class": "world"}, "auto")
    assert edges.defaulted(seen, "auto")
    assert not edges.defaulted({"class": "world", "wrap_u": "wrap", "wrap_v": "wrap"}, "auto")
    assert not edges.defaulted({"class": "world"}, "clamp")
    assert not edges.defaulted({"class": "ui"}, "auto")


def test_the_summary_and_the_record_count_the_defaulted_entries(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        assert textures.run(inv_args()) == 0
        capsys.readouterr()
        assert textures.run(build_args(preset="all")) == 0
        assert "1: world textures no dump recorded a mode for" in capsys.readouterr().out
        rec = os.path.join(G.dist, "enhance", "textures", "textures-pack.toml")
        with open(rec, "rb") as f:
            assert tomllib.load(f)["wrap_defaulted"] == 1


def test_an_unrecorded_world_texture_is_seamless_across_its_edge(d):
    # A left half dark and a right half bright: the wrapped pad gives the kernel
    # the bright column at the left edge, the clamped one the dark.
    w = 16
    row = bytes((20, 20, 20, 255)) * (w // 2) + bytes((220, 220, 220, 255)) * (w // 2)
    rgba = row * w
    t = {"scale": 2, "alpha": "split", "edges": "auto", "cell": None}
    up = get("lanczos")
    out = {}
    for cls in ("world", "ui"):
        job = build.make_job(up, w, w, rgba, t, {"class": cls})
        out[cls] = job.finish(up.upscale(job.images(), job.native, {}))[2]
    assert out["world"] != out["ui"]


# ---- the contract comes first ------------------------------------------------


def _plugin(d, version):
    path = os.path.join(d, "xbr-upscale-torch")
    write(
        path,
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then echo "xbr-upscale-torch 9.9 contract %s"; exit 0; fi\n'
        'echo ran >> "%s/ran"\nexit 1\n' % (version, d),
    )
    os.chmod(path, 0o755)
    return path


def test_a_plugin_of_another_contract_costs_nothing_and_writes_no_pack(d):
    with fake_tree(d):
        G = world_game(d)
        path = _plugin(d, "2.0")
        assert textures.run(inv_args()) == 0
        cfgp = world_config(
            d, '[class.world]\nscale = 2\nupscaler = "t"\n[upscaler.t]\npreset = "torch"\n'
        )
        write(
            textures_cfg.local_path_of(cfgp),
            '[upscaler.t]\npath = "%s"\nmodel = "x.safetensors"\n' % path.replace("\\", "/"),
        )
        raises(textures.run, build_args(preset="all", config=cfgp), match="contract 2.0")
        assert not os.path.exists(os.path.join(d, "ran"))
        out_dir = os.path.join(G.dist, "enhance", "textures")
        assert not os.path.exists(os.path.join(out_dir, "textures.pack"))
        assert not os.path.exists(os.path.join(out_dir, "textures.pack.payload.tmp"))


def test_upscale_checks_the_contract_before_the_tool_sees_an_image(d):
    path = _plugin(d, "2.0")
    up = command.from_config("torch", {"torch": {"path": path, "model": "x.safetensors"}}, {})
    with pytest.raises(UpscalerError, match="contract 2.0"):
        up.upscale([(4, 4, noise(4, 4, opaque=True))], 4)
    assert not os.path.exists(os.path.join(d, "ran"))


def test_a_newer_minor_is_said(d):
    path = _plugin(d, "1.7")
    up = command.from_config("torch", {"torch": {"path": path, "model": "x.safetensors"}}, {})
    ups = {"torch": up}
    up.identity()
    t = {"upscaler": "torch", "alpha": "split", "scale": 4}
    assert any("newer" in x for x in build.notes_of([({}, t)], ups))


def test_a_scale_the_tool_cannot_reach_is_refused_before_the_work(d):
    tool = fake_tool(d)
    table = {"fake": {"template": "t {in_dir} {out_dir}", "path": tool, "scales": [2]}}
    with pytest.raises(host.CliError, match="cannot reach 3x"):
        build.upscalers_of([({"hash": "1"}, {"upscaler": "fake", "scale": 3})], {"upscaler": table})


# ---- the record ----------------------------------------------------------------


@pytest.mark.parametrize(
    "line,want",
    [
        ("xbr-upscale-torch 0.1.0 contract 1.0", "0.1.0"),
        ("xbr-upscale-torch 9 contract 2.0", "9"),
        ("tool 1.2.3-carlos.secret", ""),
        ("tool 1.2.3+host.name", ""),
        ("tool: opened 42 files", ""),
        ("faketool 1.2", "1.2"),
        ("tool v2.3.4-rc1 (built somewhere)", "2.3.4-rc1"),
        ("realcugan-ncnn-vulkan 20220728", "20220728"),
        ("error: cannot open /opt/someone/1.2/x.param", ""),
        ("Usage: /opt/someone/tools-1.2/tool -i infile", ""),
        ("someone@somehost: no such option", ""),
        ("", ""),
    ],
)
def test_a_tools_version_line_gives_its_version_and_nothing_else(line, want):
    assert command.version_token(line) == want


def test_a_usage_line_with_a_private_path_stays_out_of_the_identity(d):
    tool = os.path.join(d, "sometool")
    write(tool, '#!/bin/sh\necho "error: cannot open /opt/someone/x.param from %s"\nexit 1\n' % d)
    os.chmod(tool, 0o755)
    up = command.from_config(
        "x", {"x": {"template": "sometool {in_dir} {out_dir}", "path": tool}}, {}
    )
    idn = up.identity()
    assert idn["tool_version"] == ""
    assert "someone" not in json.dumps(idn) and d not in json.dumps(idn)


def test_a_host_font_makes_the_scope_host():
    assert build.scope_of({"xbr": get("xbr")}) == "any"
    assert build.scope_of({"xbr": get("xbr")}, {"name": "f", "sha256": "0"}) == "host"
    bad = Upscaler()
    bad.reproducible_scope = "none"
    assert build.scope_of({"x": bad}, {"name": "f"}) == "none"


def test_the_record_keeps_a_config_out_of_the_root_to_its_name(d, capsys):
    with fake_tree(d):
        G = world_game(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(d, "[class.world]\nscale = 2\n")
        assert not os.path.abspath(cfgp).startswith(os.path.abspath(G.root) + os.sep)
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        rec = os.path.join(G.dist, "enhance", "textures", "textures-pack.toml")
        with open(rec, "rb") as f:
            assert tomllib.load(f)["config_path"] == "enhance-textures.toml"
        inside = os.path.join(G.root, "config", "enhance-textures.toml")
        write(inside, "schema = 1\n")
        assert textures.run(build_args(preset="all", config=inside)) == 0
        with open(rec, "rb") as f:
            assert tomllib.load(f)["config_path"] == "config/enhance-textures.toml"


def test_the_manifest_lists_world_when_it_holds_one(d):
    art = pack.PackEntry(1, 4, 4, bytes(64), name="a", kind=pack.KIND_ART)
    wall = pack.PackEntry(2, 4, 4, bytes(64), name="w", kind=pack.KIND_WORLD)
    text = pack.manifest_text([art, wall], 2, "xbr", "t", "0" * 64)
    assert tomllib.loads(text)["kinds"]["world"]["entries"] == 1
    assert "[kinds.world]" not in pack.manifest_text([art], 2, "xbr", "t", "0" * 64)


# ---- what is said once ----------------------------------------------------------


def test_a_max_side_clamp_and_a_split_are_said(d, capsys):
    with fake_tree(d):
        world_game(d)
        tool = fake_tool(d)
        assert textures.run(inv_args()) == 0
        cfgp = world_config(
            d,
            '[class.world]\nscale = 4\nmax_side = 32\nalpha = "native"\nupscaler = "fake"\n\n'
            "[upscaler.fake]\nscales = [2]\n",
        )
        write(
            textures_cfg.local_path_of(cfgp),
            '[upscaler.fake]\ntemplate = "faketool -i {in_dir} -o {out_dir} -s {scale}"\n'
            'path = "%s"\n' % tool.replace("\\", "/"),
        )
        capsys.readouterr()
        assert textures.run(build_args(preset="all", config=cfgp)) == 0
        out = capsys.readouterr().out
        assert "at a lower scale than asked: max_side" in out
        assert "fake drops alpha, so split and not native" in out


def test_an_explicit_bc1_of_a_texture_with_alpha_says_it_drops_it():
    used = bytes((1, 2, 3, 0)) * 16
    enc, note = build.resolve_encode({}, {"encode": "bc1"}, used)
    assert enc == "bc1" and "drops the alpha" in note
    enc, note = build.resolve_encode({}, {"encode": "bc1"}, bytes((1, 2, 3, 255)) * 16)
    assert enc == "bc1" and not note


# ---- the presets --------------------------------------------------------------


def test_a_fixed_4x_model_offers_only_4x(d):
    tool = fake_tool(d)

    def scales(**table):
        return command.from_config(
            "realesrgan-ncnn-vulkan", {"realesrgan-ncnn-vulkan": dict(table, path=tool)}, {}
        ).scales

    assert scales() == (4,)
    assert scales(model="realesrgan-x4plus-anime") == (4,)
    assert scales(model="realesr-animevideov3") == (2, 3, 4)


def test_the_python_real_esrgan_preset_keeps_the_names_and_says_it_is_unverified():
    row = command.presets()["tool"]["real-esrgan"]
    argv = command.substitute(
        row["template"],
        {"model": "m", "in_dir": "i", "out_dir": "o", "scale": 4, "tile": 0},
    )
    # The script names its output <name>_<suffix>: empty, as this CLI reads it.
    assert argv[argv.index("--suffix") + 1] == "" and argv[argv.index("--ext") + 1] == "png"
    assert row["flag"].startswith("unverified")


# ---- a tracked file cannot run a program, whatever shape it takes ------------


EVIL_FORMS = (
    '[upscaler]\nevil = [["template", "%s"]]\n',
    '[[upscaler.evil]]\ntemplate = 1\n"%s" = 2\n',
)


def _marker_template(d):
    marker = os.path.join(d, "ran").replace("\\", "/")
    return marker, "sh -c 'touch %s; exit 1' {in_dir} {out_dir}" % marker


@pytest.mark.parametrize("form", EVIL_FORMS)
@pytest.mark.parametrize("named", [True, False])
def test_compare_refuses_a_tracked_upscaler_that_is_not_a_table(d, form, named):
    marker, cmd = _marker_template(d)
    cfgp = world_config(d, form % cmd)
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        with pytest.raises(host.CliError, match=r"\[upscaler\.evil\]: not a table"):
            textures.run(compare_args(config=cfgp, upscaler=["evil"] if named else []))
    assert not os.path.exists(marker)


@pytest.mark.parametrize("form", EVIL_FORMS)
def test_build_refuses_a_tracked_upscaler_that_is_not_a_table(d, form):
    marker, cmd = _marker_template(d)
    cfgp = world_config(d, '[class.world]\nupscaler = "evil"\n' + form % cmd)
    with fake_tree(d):
        world_game(d)
        assert textures.run(inv_args()) == 0
        with pytest.raises(host.CliError, match=r"\[upscaler\.evil\]: not a table"):
            textures.run(build_args(preset="all", config=cfgp))
    assert not os.path.exists(marker)


def test_the_local_file_and_the_upscaler_key_are_held_to_the_same_shape(d):
    cfgp = world_config(d, "")
    write(textures_cfg.local_path_of(cfgp), '[upscaler]\nx = [["template", "y"]]\n')
    with pytest.raises(textures_cfg.ConfigError, match=r"\[upscaler\.x\]: not a table"):
        textures_cfg.load(cfgp)
    os.unlink(textures_cfg.local_path_of(cfgp))
    with pytest.raises(textures_cfg.ConfigError, match="upscaler: not a table"):
        textures_cfg.load(world_config(d, 'upscaler = "x"\n'))


@pytest.mark.parametrize("config,local", [({"e": [["template", "x"]]}, {}), ({}, {"e": 1})])
def test_from_config_refuses_what_is_not_a_table(config, local):
    with pytest.raises(UpscalerError, match="not a table"):
        command.from_config("e", config, local)


@pytest.mark.parametrize(
    "row",
    ['sha256 = "--evil"', 'sha256 = "abc"', "sha256 = 1", 'tile = "x"', "batch = -1"],
)
def test_every_table_is_checked_even_one_no_row_uses(d, row):
    with pytest.raises(textures_cfg.ConfigError, match="upscaler.spare"):
        textures_cfg.load(world_config(d, "[upscaler.spare]\n%s\n" % row))


def test_a_hash_of_the_right_shape_is_accepted(d):
    textures_cfg.load(world_config(d, '[upscaler.spare]\nsha256 = "%s"\n' % ("aB" * 32)))


# ---- models beside the tool --------------------------------------------------


def _model_beside(d, capsys, model, path):
    world_game(d)
    bin_dir = os.path.join(d, "bin")
    os.makedirs(os.path.dirname(os.path.join(bin_dir, path)))
    tool = fake_tool(bin_dir)
    write(os.path.join(bin_dir, path), "one")
    assert textures.run(inv_args()) == 0
    cfgp = _fake_config(d, tool, 'model = "%s"\n' % model)
    assert not _current(capsys, preset="all", config=cfgp)
    assert _current(capsys, preset="all", config=cfgp)
    write(os.path.join(bin_dir, path), "two")
    assert not _current(capsys, preset="all", config=cfgp)
    return bin_dir, tool, cfgp


def test_a_changed_model_in_the_folder_beside_the_tool_rebuilds(d, capsys):
    with fake_tree(d):
        _model_beside(d, capsys, "mymodel", "models/mymodel.bin")


def test_a_changed_file_in_a_model_folder_beside_the_tool_rebuilds(d, capsys):
    # waifu2x and RealCUGAN name a folder: -m models-se.
    with fake_tree(d):
        _model_beside(d, capsys, "models-se", "models-se/up2x.bin")


def test_another_models_file_beside_the_tool_is_no_reason_to_rebuild(d, capsys):
    with fake_tree(d):
        bin_dir, _, cfgp = _model_beside(d, capsys, "mymodel", "models/mymodel.bin")
        assert _current(capsys, preset="all", config=cfgp)
        write(os.path.join(bin_dir, "models", "mymodel-anime.bin"), "other")
        assert _current(capsys, preset="all", config=cfgp)


def test_a_moved_tool_with_its_models_stays_current_and_a_changed_one_does_not(d, capsys):
    with fake_tree(d):
        bin_dir, tool, cfgp = _model_beside(d, capsys, "mymodel", "models/mymodel.bin")
        assert _current(capsys, preset="all", config=cfgp)
        moved = os.path.join(d, "elsewhere")
        shutil.move(bin_dir, moved)
        new_tool = os.path.join(moved, os.path.basename(tool)).replace("\\", "/")
        loc = textures_cfg.local_path_of(cfgp)
        with open(loc, encoding="utf-8") as f:
            text = f.read()
        write(loc, text.replace(tool.replace("\\", "/"), new_tool))
        assert _current(capsys, preset="all", config=cfgp)
        write(os.path.join(moved, "models", "mymodel.bin"), "three")
        assert not _current(capsys, preset="all", config=cfgp)


# ---- cells --------------------------------------------------------------------


def _sheet(cell, nx, ny):
    """Solid cells of one colour each, a clear rim of 2 around each: what the
    colour under a clear texel must not borrow across a cell's border."""
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)]
    w, h = cell * nx, cell * ny
    img = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            if 2 <= x % cell < cell - 2 and 2 <= y % cell < cell - 2:
                c = cols[(x // cell + y // cell * nx) % len(cols)]
                img[(y * w + x) * 4 : (y * w + x) * 4 + 4] = bytes(c + (255,))
    return w, h, bytes(img), cols


@pytest.mark.parametrize("name", ["lanczos", "xbr", "nearest"])
@pytest.mark.parametrize("alpha_mode", ["split", "binary"])
def test_no_cell_takes_a_neighbours_colour(name, alpha_mode):
    w, h, img, cols = _sheet(16, 4, 2)
    up = get(name)
    native = up.native_for(2)
    job = build.Job(w, h, img, 2, native, alpha_mode, "cells", "cells", (16, 16))
    uw, _, out = job.finish(up.upscale(job.images(), native, {}))
    wrong = 0
    for i in range(0, len(out), 4):
        y, x = divmod(i // 4, uw)
        c = cols[(x // 32 + y // 32 * 4) % len(cols)]
        if out[i + 3] and max(abs(out[i + k] - c[k]) for k in range(3)) > 40:
            wrong += 1
    assert wrong == 0


@pytest.mark.parametrize("edges_row", ["", 'edges = "auto"\n', 'edges = "clamp"\n'])
def test_a_cell_without_edges_cells_is_refused(d, edges_row):
    cfgp = world_config(d, "[class.ui]\ncell = [16, 16]\n" + edges_row)
    with pytest.raises(textures_cfg.ConfigError, match='cell needs edges = "cells"'):
        textures_cfg.load(cfgp)


def test_the_cell_and_edges_cells_together_are_accepted(d):
    textures_cfg.load(world_config(d, '[class.ui]\ncell = [16, 16]\nedges = "cells"\n'))


# ---- scenes that disagree ------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,want",
    [
        ("wrap", "clamp_edge", "wrap"),
        ("mirror", "clamp", "mirror"),
        ("wrap", "mirror", "wrap"),
        ("clamp", "border", "clamp"),
        (None, "clamp_edge", "clamp_edge"),
        ("mirror", None, "mirror"),
        ("weird", "clamp", "clamp"),
        (None, None, None),
    ],
)
def test_the_mode_that_repeats_wins_in_either_order(a, b, want):
    assert edges.merged_address(a, b) == want
    assert edges.merged_address(b, a) == want


def _two_scenes(d, first, second):
    a, b = os.path.join(d, "a"), os.path.join(d, "b")
    h = "00000000000000AC"
    texels = bytes((10, 20, 30, 255)) * 256
    dump_texture(a, h, 16, 16, "A8R8G8B8", texels=texels, **first)
    dump_texture(b, h, 16, 16, "A8R8G8B8", texels=texels, **second)
    return a, b, h


def test_the_order_of_the_dumps_cannot_pick_an_address_mode(d, capsys):
    with fake_tree(d):
        G = game_with(WORLD_TOML)
        write_archive(G.game_files, "media", [("x.dds", b"not", False)])
        a, b, h = _two_scenes(
            d, dict(wrap_u="clamp_edge"), dict(wrap_u="wrap", wrap_v="clamp_edge")
        )
        got = []
        for dumps in ([a, b], [b, a]):
            assert textures.run(inv_args(dump=dumps)) == 0
            with open(os.path.join(G.dist, "enhance", "inventory.json"), encoding="utf-8") as f:
                e = {x["hash"]: x for x in json.load(f)["entries"]}[h]
            got.append((e["wrap_u"], e["wrap_v"]))
        assert got == [("wrap", "clamp_edge")] * 2


# ---- what a new scene costs ----------------------------------------------------


def test_a_scene_that_only_counts_stays_current_and_a_fact_the_build_reads_does_not(d, capsys):
    with fake_tree(d):
        G = game_with(WORLD_TOML)
        write_archive(G.game_files, "media", [("x.dds", b"not", False)])
        first = dict(wrap_u="clamp", mag_scale1=3.0, mag_draws=1)
        a, b, _ = _two_scenes(d, first, dict(first, mag_draws=7))
        os.rename(b, b + ".new")
        assert textures.run(inv_args(dump=[a])) == 0
        assert not _current(capsys, preset="all", dump=[a])
        path = os.path.join(G.dist, "enhance", "textures", "textures.pack")
        with open(path, "rb") as f:
            before = f.read()
        os.rename(b + ".new", b)
        inventory = os.path.join(G.dist, "enhance", "inventory.json")
        assert textures.run(inv_args(dump=[a, b])) == 0
        with open(inventory, encoding="utf-8") as f:
            e = json.load(f)["entries"][0]
        assert e["uses"] > 1 and e["draws"] == 8 and len(e["scenes"]) == 2
        assert _current(capsys, preset="all", dump=[a, b])
        # What it would have written, to prove the counts change no byte.
        other = os.path.join(d, "other")
        assert not _current(capsys, preset="all", dump=[a, b], out=other)
        with open(os.path.join(other, "textures.pack"), "rb") as f:
            assert f.read() == before
        assert _current(capsys, preset="all", dump=[a, b], out=other)
        # The same second scene with what the build reads of it: not current.
        for changed in (dict(first, wrap_u="wrap"), dict(first, mag_scale1=9.0)):
            _two_scenes(d, first, changed)
            assert textures.run(inv_args(dump=[a, b])) == 0
            assert not _current(capsys, preset="all", dump=[a, b], out=other)
            _two_scenes(d, first, first)
            assert textures.run(inv_args(dump=[a, b])) == 0
            assert not _current(capsys, preset="all", dump=[a, b], out=other)


def test_the_counts_left_out_of_the_key_are_read_by_nothing_that_makes_a_pack():
    # A fact on this list goes unseen by the stage key, so a new reader of one
    # would let a pack go stale in silence: it fails here first. `estimate`
    # reports the scenes and makes no pack; inventory writes them; build names them.
    src = os.path.dirname(os.path.abspath(pack.__file__))
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py") or name in ("inventory.py", "estimate.py", "build.py"):
            continue
        with open(os.path.join(src, name), encoding="utf-8") as f:
            text = f.read()
        assert not any('"%s"' % fact in text for fact in build.COUNTS_ONLY), name


def test_a_scale_named_model_file_is_its_model_and_a_longer_name_is_another(d):
    tool = fake_tool(d)
    folder = os.path.join(d, "models")
    for name in (
        "realesr-animevideov3-x2.bin",
        "realesr-animevideov3-x2.param",
        "realesrgan-x4plus.bin",
        "realesrgan-x4plus-anime.bin",
    ):
        write(os.path.join(folder, name), "a")

    def files(model):
        table = {"fake": {"template": "t {in_dir} {out_dir}", "path": tool, "model": model}}
        return sorted(command.from_config("fake", table, {}).input_key()["model"])

    assert files("realesr-animevideov3") == [
        "realesr-animevideov3-x2.bin",
        "realesr-animevideov3-x2.param",
    ]
    assert files("realesrgan-x4plus") == ["realesrgan-x4plus.bin"]


@pytest.mark.parametrize(
    "row",
    ["model = 1", 'gpu = "0"', "gpu = true", "path = 1", "overrides = 1", 'allow_plain_http = "y"'],
)
def test_a_local_key_of_the_wrong_type_is_a_config_error(d, row):
    cfgp = world_config(d, "")
    write(textures_cfg.local_path_of(cfgp), "[upscaler.fake]\n%s\n" % row)
    with pytest.raises(textures_cfg.ConfigError, match="upscaler.fake"):
        textures_cfg.load(cfgp)


@pytest.mark.parametrize("value", ['"$(touch x)"', "1"])
def test_allow_model_mismatch_is_true_or_false(d, value):
    with pytest.raises(textures_cfg.ConfigError, match="allow_model_mismatch"):
        textures_cfg.load(world_config(d, "[upscaler.fake]\nallow_model_mismatch = %s\n" % value))
    textures_cfg.load(world_config(d, "[upscaler.fake]\nallow_model_mismatch = true\n"))
