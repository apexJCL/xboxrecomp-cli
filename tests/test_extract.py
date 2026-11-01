"""xbr extract on data the tests build in code: a synthetic XBE holding
a two-node Ninja tree in both dialects ([extract] vertex_header,
packed_normals, angles), DDS textures, an IPK1 archive and a fake Godot.
No test reads game files.

  uv run pytest tests/test_extract.py
"""

import copy
import hashlib
import json
import math
import os
import stat
import struct
import sys

import pytest

from xboxrecomp_cli import host, manifest
from xboxrecomp_cli.enhance import dds, ipk
from xboxrecomp_cli.extract import cli, collision, gltf, godot, ninja, placements, scan, textures
from xboxrecomp_cli.extract.image import Image, ImageError

BASE = 0x10000
SEC = 0x20000
SHA = "0123456789abcdef0123456789abcdef01234567"
GAME_TOML = """schema = 1
[game]
name = "Some Game"
slug = "some-game"
[xbe]
title_id = 0x12345678
[cli]
commit = "%s"
[toolkit]
url = "https://example.invalid/tk.git"
branch = "main"
commit = "%s"
[pipeline]
game_name = "some"
gen = "src/gen"
[build]
exe = "some_recomp"
""" % (SHA, SHA)
QUAD = [
    (0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (1.0, 0.0, 1.0),
]  # wound to face +Y, as its normals
UVS = [(0, 0), (256, 0), (0, 256), (256, 256)]  # strip 65: 1/256 units


# ---- fixtures ---------------------------------------------------------------


def xbe(sections):
    """A minimal XBE: header, section table and names, then each section's
    bytes at its VA (the image's raw layout is not its VA layout)."""
    hdr = bytearray(0x1000)
    hdr[:4] = b"XBEH"
    table, names = 0x180, 0x300
    struct.pack_into("<3I", hdr, 0x104, BASE, len(hdr), 0x100000)
    struct.pack_into("<I", hdr, 0x118, BASE + 0x800)
    struct.pack_into("<I", hdr, 0x11C, len(sections))
    struct.pack_into("<I", hdr, 0x120, BASE + table)
    struct.pack_into("<I", hdr, 0x128, (BASE + 0x2000) ^ 0xA8FC57AB)
    out = bytearray(hdr)
    n = names
    for i, (name, va, body) in enumerate(sections):
        out[n : n + len(name) + 1] = name.encode() + b"\0"
        # vsize past the raw bytes: a BSS tail that reads as zero.
        struct.pack_into(
            "<6I", out, table + 0x38 * i, 0, va, len(body) + 0x40, len(out) + 0, len(body), BASE + n
        )
        n += len(name) + 1
    for i, (_, _, body) in enumerate(sections):
        struct.pack_into("<I", out, table + 0x38 * i + 12, len(out))
        out += body
    return bytes(out)


def normpacked(x, y, z):
    def q(v, bits):
        return int(round(v * ((1 << (bits - 1)) - 1))) & ((1 << bits) - 1)

    return q(x, 11) | (q(y, 11) << 11) | (q(z, 10) << 22)


def stage_section(variant, extra_poly=b""):
    """The MAP section: root (rotated a quarter turn about Y) -> child
    (at 1,2,3) -> model: four vertices, a clamped texture, a diffuse
    colour, one double-sided UV strip of two triangles, then extra_poly.
    variant "b1": 16-byte vertex headers, packed normals (type 48), BAMS,
    32-byte names; "b2": 8-byte headers, float normals (type 41),
    radians, 36-byte names."""
    s = bytearray(0x400)
    b1 = variant == "b1"
    if b1:
        root = struct.pack("<II3f3i3fII", 0, 0, 0, 0, 0, 0, 0x4000, 0, 1, 1, 1, SEC + 0x40, 0)
    else:
        root = struct.pack("<II3f3f3fII", 0, 0, 0, 0, 0, 0, math.pi / 2, 0, 1, 1, 1, SEC + 0x40, 0)
    s[0:0x34] = root
    child_fmt = "<II3f3i3fII" if b1 else "<II3f3f3fII"
    s[0x40:0x74] = struct.pack(child_fmt, 0, SEC + 0x80, 1, 2, 3, 0, 0, 0, 1, 1, 1, 0, 0)
    s[0x80:0x98] = struct.pack("<II4f", SEC + 0x100, SEC + 0x200, 0, 0, 0, 2)
    if b1:
        v = struct.pack("<BBHHH", 48, 0, 99, 0, 4) + bytes(8)
        v += b"".join(struct.pack("<3fI", *p, normpacked(0, 1, 0)) for p in QUAD)
    else:
        v = struct.pack("<BBHHH", 41, 0, 99, 0, 4)
        v += b"".join(struct.pack("<3f3f", *p, 0, 1, 0) for p in QUAD)
    v += b"\xff\0"
    s[0x100 : 0x100 + len(v)] = v
    p = struct.pack("<BBH", 8, 0x20, 0)  # tiny: texture 0, clamp U
    p += struct.pack("<BBH", 17, 0x25, 2) + bytes((0x40, 0x80, 0xC0, 0xFF))  # diffuse BGRA
    strip = struct.pack("<Hh", 1, 4) + b"".join(struct.pack("<Hhh", i, *UVS[i]) for i in range(4))
    p += struct.pack("<BBH", 65, 0x10, len(strip) // 2) + strip
    p += extra_poly + b"\xff\0"
    s[0x200 : 0x200 + len(p)] = p
    size = 32 if b1 else 36
    s[0x300:0x308] = struct.pack("<II", SEC + 0x320, 1)
    s[0x320 : 0x320 + size] = b"Tex0".ljust(size, b"\0")
    return bytes(s)


def dds_raw(w, h, bits, masks, pixels):
    hdr = bytearray(dds.HEADER)
    hdr[0:4] = dds.MAGIC
    struct.pack_into("<IIII", hdr, 4, 124, 0x100F, h, w)
    struct.pack_into("<II4s5I", hdr, 76, 32, 0x41, b"\0\0\0\0", bits, *masks)
    fmt = "<%d%s" % (len(pixels), "I" if bits == 32 else "H")
    return bytes(hdr) + struct.pack(fmt, *pixels)


ARGB8 = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
ARGB4 = (0x0F00, 0x00F0, 0x000F, 0xF000)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


@pytest.fixture
def game(tmp_path):
    """A game whose dump is the b1 synthetic XBE plus media/tex0.dds (the
    texlist says Tex0: matched without case); the current game while the
    test runs."""
    root = str(tmp_path / "game")
    toml = GAME_TOML + EXTRACT_B1
    write(os.path.join(root, "game.toml"), toml)
    write(
        os.path.join(root, "game_files", "default.xbe"),
        xbe([("MAP11", SEC, stage_section("b1"))]),
    )
    write(
        os.path.join(root, "game_files", "media", "tex0.dds"),
        dds_raw(2, 1, 32, ARGB8, [0xFFFF0000, 0x8000FF00]),
    )
    saved = manifest.current()
    G = manifest.load(root)
    manifest.use(G)
    yield G
    manifest.use(saved)


EXTRACT_B1 = """
[extract]
vertex_header = 16
packed_normals = true
textures = "media/{name}.dds"
[[extract.stage]]
name = "MAP11"
section = "MAP11"
root = 0x20000
texlist = 0x20300
"""


class Args:
    def __init__(self, **kw):
        self.stage, self.out, self.do_import, self.list, self.scan = [], None, False, False, None
        self.collision = self.placements = False
        self.__dict__.update(kw)


# ---- the reader -------------------------------------------------------------


def read(variant, extra=b""):
    img = Image(xbe([("MAP11", SEC, stage_section(variant, extra))]))
    fmt = ninja.Format(16, "bams", True) if variant == "b1" else ninja.Format(8, "radians", False)
    r = ninja.Reader(img, fmt)
    return r, r.tree(SEC)


@pytest.mark.parametrize("variant", ["b1", "b2"])
def test_both_dialects_read_the_same_tree(variant):
    r, roots = read(variant)
    assert len(roots) == 1 and len(roots[0].children) == 1
    root, child = roots[0], roots[0].children[0]
    assert root.ang == pytest.approx((0, math.pi / 2, 0))
    assert child.pos == (1, 2, 3) and child.model is not None
    m = child.model
    assert [m.verts[i] for i in range(4)] == QUAD
    assert all(m.normals[i] == pytest.approx((0, 1, 0), abs=1e-3) for i in range(4))
    (s,) = m.strips
    assert s.kind == 65 and s.tex == 0 and s.flags == 0x10
    assert s.diffuse == bytes((0x40, 0x80, 0xC0, 0xFF))
    # Two triangles, the second with its winding flipped.
    assert [[c[0] for c in t] for t in s.tris] == [[0, 1, 2], [2, 1, 3]]
    assert s.tris[0][1][1:] == (1.0, 0.0)
    assert not r.unsupported


def test_bams_and_radians_give_one_rotation():
    q1 = ninja.quat(ninja.Format(angles="bams").radians((0x4000, 0, 0)), 0)
    q2 = ninja.quat(ninja.Format(angles="radians").radians((math.pi / 2, 0, 0)), 0)
    assert q1 == pytest.approx(q2)
    s = math.sqrt(0.5)
    assert q1 == pytest.approx([s, 0, 0, s])


def test_rotation_order_x_first_and_zxy():
    def rot(q, v):
        # v' = v + 2w(u x v) + 2u x (u x v), u the vector part.
        def cross(a, b):
            return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

        u, w = q[:3], q[3]
        t = cross(u, v)
        t2 = cross(u, t)
        return tuple(v[i] + 2 * w * t[i] + 2 * t2[i] for i in range(3))

    a = (math.pi / 2, math.pi / 2, 0)
    # Rz*Ry*Rx: X first takes +Y to +Z, then Y takes +Z to +X.
    assert rot(ninja.quat(a, 0), (0, 1, 0)) == pytest.approx((1, 0, 0), abs=1e-9)
    # Ry*Rx*Rz (ZXY), Z = 0 here: the same X then Y.
    assert rot(ninja.quat(a, ninja.EVAL_ZXY), (0, 1, 0)) == pytest.approx((1, 0, 0), abs=1e-9)
    b = (0, math.pi / 2, math.pi / 2)
    # Default: Y takes +X to -Z, then Z leaves it.
    assert rot(ninja.quat(b, 0), (1, 0, 0)) == pytest.approx((0, 0, -1), abs=1e-9)
    # ZXY: Z first takes +X to +Y, then Y leaves it.
    assert rot(ninja.quat(b, ninja.EVAL_ZXY), (1, 0, 0)) == pytest.approx((0, 1, 0), abs=1e-9)


def test_stride_comes_from_the_type_not_size():
    assert ninja.Format(16, packed_normals=True).stride(44) == 20
    assert ninja.Format(8).stride(44) == 28
    assert ninja.Format(8).stride(48) == 16
    assert ninja.Format(8).stride(41) == 24


def test_unknown_chunks_are_counted_not_fatal():
    extra = struct.pack("<BBH", 56, 0, 1) + bytes(2) + struct.pack("<BB", 4, 0)
    extra += struct.pack("<BBH", 18, 0x00, 2) + bytes(4)  # material, blend 0x00
    r, roots = read("b1", extra)
    assert roots[0].children[0].model.strips
    assert r.unsupported == {
        "polygon chunk type 56": 1,
        "cached polygon list": 1,
        "blend mode 0x00": 1,
    }


def test_strip_counts_win_over_an_undercounted_size():
    # One strip of three UV points (2 + 2 + 3 * 6 = 22 bytes) sized as if
    # each point were 4 bytes (16): the counts give the length, and the
    # chunk after it (a second copy) is still read.
    strip = struct.pack("<Hh", 1, 3) + b"".join(struct.pack("<Hhh", i, 0, 0) for i in range(3))
    extra = (struct.pack("<BBH", 65, 0, 8) + strip) * 2
    r, roots = read("b2", extra)
    strips = roots[0].children[0].model.strips
    assert [len(s.tris) for s in strips] == [2, 1, 1]
    assert r.stats["strip chunk longer than its size"] == 2
    assert not r.unsupported


def test_formats_refuse_bad_values():
    with pytest.raises(ValueError, match="angles"):
        ninja.Format(angles="degrees")
    with pytest.raises(ValueError, match="vertex_header"):
        ninja.Format(4)


def test_image_reads_bss_as_zero_and_refuses_outside():
    img = Image(xbe([("MAP11", SEC, b"\x01\x02")]))
    assert img.read(SEC, 4) == b"\x01\x02\0\0"
    with pytest.raises(ImageError, match="outside"):
        img.read(SEC + 0x1000, 4)
    with pytest.raises(ImageError, match="no section"):
        img.section("MAP99")


# ---- textures ---------------------------------------------------------------


def test_uncompressed_dds_by_masks():
    w, h, rgba, label = textures.decode(dds_raw(2, 1, 32, ARGB8, [0xFF102030, 0x80405060]))
    assert (w, h, label) == (2, 1, "A32")
    assert rgba == bytes((0x10, 0x20, 0x30, 0xFF, 0x40, 0x50, 0x60, 0x80))
    _, _, rgba, label = textures.decode(dds_raw(1, 1, 16, ARGB4, [0xF0A5]))
    assert label == "A16" and rgba == bytes((0x00, 0xAA, 0x55, 0xFF))
    assert textures.alpha_kind(rgba) == "opaque"
    assert textures.alpha_kind(bytes((0, 0, 0, 0, 0, 0, 0, 255))) == "mask"
    assert textures.alpha_kind(bytes((0, 0, 0, 7))) == "translucent"
    assert textures.alpha_kind(bytes((0, 0, 0, 250, 0, 0, 0, 255))) == "near-opaque"
    assert textures.alpha_kind(bytes((0, 0, 0, 7, 0, 0, 0, 0))) == "blend"


def test_png_is_valid():
    import zlib

    data = textures.png(2, 1, bytes(range(8)))
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    ln = struct.unpack(">I", data[33:37])[0]
    assert data[37:41] == b"IDAT"
    assert zlib.decompress(data[41 : 41 + ln]) == b"\0" + bytes(range(8))


def test_texture_sources(tmp_path):
    with pytest.raises(ValueError, match="ipk"):
        textures.parse_source("ipk:media")
    with pytest.raises(ValueError, match="name"):
        textures.parse_source("media/x.dds")
    assert textures.parse_source("ipk:media:DDS\\{name}.dds") == ("ipk", "media", "dds/{name}.dds")
    d = str(tmp_path)
    body = b"DDS data"
    rec = ipk.ENTRY.pack(b"dds\\Tex0.dds", 0, len(body), 0, len(body))
    write(os.path.join(d, "media.iph"), ipk.HEADER.pack(ipk.MAGIC, 16, 1, len(body)) + rec)
    write(os.path.join(d, "media.ipk"), body)
    src = textures.Source(d, "ipk:media:dds\\{name}.dds")
    assert src.read("TEX0") == body and src.read("nope") is None
    write(os.path.join(d, "Media", "Tex0.DDS"), b"loose")
    src = textures.Source(d, "media/{name}.dds")
    assert src.read("tex0") == b"loose" and src.read("nope") is None


# ---- glTF -------------------------------------------------------------------


def test_gltf_is_valid_and_complete(tmp_path):
    img = Image(xbe([("MAP11", SEC, stage_section("b1"))]))
    r = ninja.Reader(img, ninja.Format(16, "bams", True))

    class Src:
        def read(self, name):
            assert name == "Tex0"
            return dds_raw(2, 1, 32, ARGB8, [0xFFFF0000, 0x0000FF00])

    names = textures.texlist(img, SEC + 0x300, 32)
    assert names == ["Tex0"]
    w = gltf.Writer("MAP11", names, Src(), up="-y")
    w.add_tree(SEC, r.tree(SEC))
    out = str(tmp_path)
    s = w.write(out)
    assert (s.meshes, s.triangles, s.materials, s.textures) == (1, 2, 1, 1)
    j = json.load(open(os.path.join(out, "MAP11.gltf")))
    binary = open(os.path.join(out, "MAP11.bin"), "rb").read()
    assert j["asset"]["version"] == "2.0"
    assert j["buffers"] == [{"uri": "MAP11.bin", "byteLength": len(binary)}]
    for v in j["bufferViews"]:
        assert v["byteOffset"] % 4 == 0 and v["byteOffset"] + v["byteLength"] <= len(binary)
    (prim,) = j["meshes"][0]["primitives"]
    pos = j["accessors"][prim["attributes"]["POSITION"]]
    assert pos["count"] == 4 and pos["min"] == [0, 0, 0] and pos["max"] == [1, 0, 1]
    idx = j["accessors"][prim["indices"]]
    view = j["bufferViews"][idx["bufferView"]]
    tri = struct.unpack_from("<6I", binary, view["byteOffset"])
    assert max(tri) < pos["count"] and idx["count"] == 6
    mat = j["materials"][prim["material"]]
    assert mat["doubleSided"] is True and mat["alphaMode"] == "MASK"
    f = mat["pbrMetallicRoughness"]["baseColorFactor"]
    assert f == pytest.approx([0xC0 / 255, 0x80 / 255, 0x40 / 255, 1.0])
    tex = j["textures"][mat["pbrMetallicRoughness"]["baseColorTexture"]["index"]]
    smp = j["samplers"][tex["sampler"]]
    # U is clamped, V repeats, both stay inside [0, 1]: one wrap for both,
    # clamp, so Godot's one repeat flag cannot bleed an edge in.
    assert (smp["wrapS"], smp["wrapT"]) == (gltf.CLAMP, gltf.CLAMP)
    assert s.approximated == {"texture wrap kept inside [0, 1] (written as the other axis's)": 1}
    assert j["images"][tex["source"]]["uri"] == "textures/Tex0.png"
    assert os.path.isfile(os.path.join(out, "textures", "Tex0.png"))
    # The scene's top node turns the Y-down world into Y-up.
    top = j["nodes"][j["scenes"][0]["nodes"][0]]
    assert top["rotation"] == [1.0, 0.0, 0.0, 0.0]
    child = [n for n in j["nodes"] if "mesh" in n][0]
    assert child["translation"] == [1, 2, 3]


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ]


def _qrot(q, v):
    return _qmul(_qmul(q, [*v, 0.0]), [-q[0], -q[1], -q[2], q[3]])[:3]


def up_facing_share(out, name):
    """Share of the stage's horizontal area (|n.y| > 0.8, the normal from
    the triangle's winding, in world space) that faces +Y: a floor that
    imports upside down shows here."""
    j = json.load(open(os.path.join(out, name + ".gltf")))
    binary = open(os.path.join(out, name + ".bin"), "rb").read()

    def read(i):
        a = j["accessors"][i]
        v = j["bufferViews"][a["bufferView"]]
        n = {"SCALAR": 1, "VEC3": 3}[a["type"]]
        fmt = "<%d%s" % (a["count"] * n, "f" if a["componentType"] == gltf.FLOAT else "I")
        flat = struct.unpack_from(fmt, binary, v["byteOffset"] + a.get("byteOffset", 0))
        return [flat[k : k + n] for k in range(0, len(flat), n)]

    area = {True: 0.0, False: 0.0}

    def walk(i, q):
        node = j["nodes"][i]
        assert "matrix" not in node and node.get("scale", [1, 1, 1]) == [1, 1, 1]
        q = _qmul(q, node.get("rotation", [0.0, 0.0, 0.0, 1.0]))
        for p in j["meshes"][node["mesh"]]["primitives"] if "mesh" in node else []:
            pos = [_qrot(q, v) for v in read(p["attributes"]["POSITION"])]
            idx = [t[0] for t in read(p["indices"])]
            for k in range(0, len(idx), 3):
                a, b, c = (pos[x] for x in idx[k : k + 3])
                u = [b[n] - a[n] for n in range(3)]
                w = [c[n] - a[n] for n in range(3)]
                cross = [
                    u[1] * w[2] - u[2] * w[1],
                    u[2] * w[0] - u[0] * w[2],
                    u[0] * w[1] - u[1] * w[0],
                ]
                length = math.sqrt(sum(x * x for x in cross))
                if length and abs(cross[1]) > 0.8 * length:
                    area[cross[1] > 0] += length
        for c in node.get("children", []):
            walk(c, q)

    for r in j["scenes"][0]["nodes"]:
        walk(r, [0.0, 0.0, 0.0, 1.0])
    return area[True] / (area[True] + area[False])


@pytest.mark.parametrize("variant", ["b1", "b2"])
def test_floors_face_up(tmp_path, variant):
    # The fixture's floor has +Y normals and the game's winding: with the
    # default up it must import facing +Y, its winding agreeing with its
    # normals. up = "-y" is for a world whose floors face -Y in the data,
    # so it turns this one over (BLiNX 2 was set to "-y" from a
    # view-matrix flip and imported upside down).
    img = Image(xbe([("MAP11", SEC, stage_section(variant))]))
    fmt = ninja.Format(16, "bams", True) if variant == "b1" else ninja.Format(8, "radians", False)
    for up, share in (("y", 1.0), ("-y", 0.0)):
        out = os.path.join(str(tmp_path), up)
        os.makedirs(out)
        w = gltf.Writer("MAP11", [], None, up=up)
        w.add_tree(SEC, ninja.Reader(img, fmt).tree(SEC))
        w.write(out)
        assert up_facing_share(out, "MAP11") == share


def test_empty_stage_writes_valid_gltf(tmp_path):
    # A root with no model: no buffer, no empty arrays, no .bin.
    s = bytearray(0x40)
    s[0:0x34] = struct.pack("<II3f3f3fII", 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0)
    img = Image(xbe([("MAP11", SEC, bytes(s))]))
    r = ninja.Reader(img, ninja.Format(8, "radians", False))
    w = gltf.Writer("E", [], None)
    w.add_tree(SEC, r.tree(SEC))
    w.write(str(tmp_path))
    j = json.load(open(os.path.join(str(tmp_path), "E.gltf")))
    assert "buffers" not in j and "bufferViews" not in j and "accessors" not in j
    assert not any(isinstance(v, list) and not v for v in j.values())
    assert not os.path.exists(os.path.join(str(tmp_path), "E.bin"))


def test_texture_names_stay_file_names(tmp_path):
    class Src:
        def read(self, name):
            return dds_raw(1, 1, 32, ARGB8, [0xFFFFFFFF])

    for bad in ("../x", "a/b", "a\\b", "..", ""):
        assert not textures.file_name(bad)
        w = gltf.Writer("S", [bad], Src())
        assert w._image(bad) is None
        assert any("not a file name" in k for k in w.summary.unsupported)
    w = gltf.Writer("S", ["a b#1"], Src())
    i = w._image("a b#1")
    assert w.doc.j["images"][i]["uri"] == "textures/a%20b%231.png"
    w.write(str(tmp_path))
    assert os.path.isfile(os.path.join(str(tmp_path), "textures", "a b#1.png"))


def test_wraps_mirror_wins_over_clamp():
    assert gltf.wraps(0xF0 << 16) == (gltf.MIRROR, gltf.MIRROR)
    assert gltf.wraps(0x30 << 16) == (gltf.CLAMP, gltf.CLAMP)
    assert gltf.wraps(0) == (gltf.REPEAT, gltf.REPEAT)


def _one_strip_writer(tex_alpha=0xFF, textured_colour="diffuse", size=(1, 1)):
    """The b2 quad with its one strip, a Writer over a size texture (1x1)."""
    roots = read("b2")[1]
    (strip,) = roots[0].children[0].model.strips

    class Src:
        def read(self, name):
            return dds_raw(*size, 32, ARGB8, [tex_alpha << 24 | 0xFFFFFF] * (size[0] * size[1]))

    return roots, strip, gltf.Writer("M", ["Tex0"], Src(), textured_colour=textured_colour)


def _sampler_of(w, mat=0):
    j = w.doc.j
    tex = j["textures"][j["materials"][mat]["pbrMetallicRoughness"]["baseColorTexture"]["index"]]
    smp = j["samplers"][tex["sampler"]]
    return smp["wrapS"], smp["wrapT"]


def _scaled(strip, su, sv):
    strip.tris = [tuple((vi, u * su, v * sv) for vi, u, v in t) for t in strip.tris]


KEPT_INSIDE = "texture wrap kept inside [0, 1] (written as the other axis's)"
MIRRORED = "mirrored texture wrap (Godot repeats it)"


def _wrapped(tmp_path, tflags, su, sv):
    """The quad with these tiny-chunk flags and UVs scaled to [0, su] by
    [0, sv]: its sampler's wraps and the Summary."""
    roots, strip, w = _one_strip_writer()
    strip.tflags = tflags << 16
    _scaled(strip, su, sv)
    w.add_tree(SEC, roots)
    s = w.write(str(tmp_path))
    return _sampler_of(w), s


def test_wrap_inside_the_unit_range_takes_the_other_axis(tmp_path):
    # tunagi: clamp U (U stays in [0, 1]), V tiles to 2: both repeat.
    got, s = _wrapped(tmp_path, 0x20, 1, 2)
    assert got == (gltf.REPEAT, gltf.REPEAT) and not s.unsupported
    assert s.approximated == {KEPT_INSIDE: 1}


def test_clamp_on_both_axes_stays_clamp(tmp_path):
    # Repeat would bleed the opposite edge in under bilinear and mips.
    got, s = _wrapped(tmp_path, 0x30, 1, 1)
    assert got == (gltf.CLAMP, gltf.CLAMP)
    assert not s.unsupported and not s.approximated
    got, s = _wrapped(tmp_path, 0x30, 1, 2)
    assert got == (gltf.CLAMP, gltf.CLAMP)
    assert not s.unsupported and not s.approximated


def test_clamp_past_the_range_wins_over_repeat_inside(tmp_path):
    got, s = _wrapped(tmp_path, 0x20, 3, 1)
    assert got == (gltf.CLAMP, gltf.CLAMP) and not s.unsupported
    assert s.approximated == {KEPT_INSIDE: 1}


def test_mirror_past_the_range_wins_and_is_counted(tmp_path):
    got, s = _wrapped(tmp_path, 0x80, 3, 1)
    assert got == (gltf.MIRROR, gltf.MIRROR) and not s.unsupported
    assert s.approximated == {KEPT_INSIDE: 1, MIRRORED: 1}


CUT = "clamped axis cut into the mesh (Godot keeps one wrap)"


def _prim_uvs(w):
    j = w.doc.j
    prim = j["meshes"][0]["primitives"][0]
    acc = j["accessors"][prim["attributes"]["TEXCOORD_0"]]
    view = j["bufferViews"][acc["bufferView"]]
    return list(struct.iter_unpack("<2f", w.doc.bin[view["byteOffset"] :][: view["byteLength"]]))


def _cut_quad(tmp_path, tflags, su, sv, size):
    roots, strip, w = _one_strip_writer(size=size)
    strip.tflags = tflags << 16
    _scaled(strip, su, sv)
    w.add_tree(SEC, roots)
    return w, w.write(str(tmp_path))


@pytest.mark.parametrize(
    "tflags, su, sv, axis, size, centres",
    [
        # A 4x2 texture: the clamped axis's own size sets its edge texels.
        (0x20, 3, 2, 0, (4, 2), [0.125, 0.875]),
        (0x10, 2, 3, 1, (4, 2), [0.25, 0.75]),
        # One texel across: clamp samples it everywhere, so it is pinned
        # whole and nothing is cut.
        (0x20, 3, 2, 0, (1, 1), [0.5]),
    ],
)
def test_clamp_past_the_range_is_cut_when_the_other_axis_tiles(
    tmp_path, tflags, su, sv, axis, size, centres
):
    # M34's walls: U tiles to 7, V clamps and dips just past 0. Godot's one
    # wrap would clamp U too and smear the wall into bands, so the mesh
    # carries V's clamp and the material repeats.
    w, s = _cut_quad(tmp_path, tflags, su, sv, size)
    assert _sampler_of(w) == (gltf.REPEAT, gltf.REPEAT)
    assert not s.unsupported
    assert s.approximated[CUT] == 2 and s.approximated[KEPT_INSIDE] == 1
    # The quad's corners all sit past the edge texels: what is left on the
    # axis is the cut at each centre (the inside piece runs between them)
    # and the pins, never the half texel where repeat blends the far edge.
    got = sorted({round(uv[axis], 6) for uv in _prim_uvs(w)})
    assert got == centres
    assert (s.triangles > 2) == (len(centres) > 1)


def test_mirror_and_clamp_is_cut_and_mirrors(tmp_path):
    # Flip U, clamp V: V is cut, then sits inside and takes U's mirror.
    w, s = _cut_quad(tmp_path, 0x80 | 0x10, 3, 2, (4, 2))
    assert _sampler_of(w) == (gltf.MIRROR, gltf.MIRROR)
    assert not s.unsupported
    assert s.approximated[CUT] == 2 and s.approximated[MIRRORED] == 1
    assert sorted({round(uv[1], 6) for uv in _prim_uvs(w)}) == [0.25, 0.75]


def test_clamp_past_the_range_with_the_other_axis_inside_is_not_cut(tmp_path):
    # One clamp for both axes samples like the game: nothing to cut.
    got, s = _wrapped(tmp_path, 0x20, 3, 1)
    assert got == (gltf.CLAMP, gltf.CLAMP) and CUT not in s.approximated
    assert s.triangles == 2


def _area(poly):
    (x0, y0), (x1, y1) = poly[0][3], poly[1][3]
    a = 0.0
    for i in range(1, len(poly) - 1):
        (x1, y1), (x2, y2) = poly[i][3], poly[i + 1][3]
        a += (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    return a / 2


def _corner(i, u, v, n=(0.0, 1.0, 0.0)):
    return ((i, u, v), (u * 10, 0.0, v * 10), n, (u, v))


def _parea(poly):
    return _area([(None, None, None, (c[1][0], c[1][2])) for c in poly])


def test_bake_cuts_at_both_edges_and_pins_the_outside():
    tri = [_corner(0, -0.5, 0.0), _corner(1, 1.5, 0.0), _corner(2, 0.5, 1.0)]
    below, inside, above = gltf._bake(tri, 0, 0.125, 0.875)
    assert {c[3][0] for c in below} == {0.125}
    assert {c[3][0] for c in above} == {0.875}
    assert all(0.125 <= c[3][0] <= 0.875 for c in inside)
    # The pieces tile the triangle (positions, which keep the cut's area)
    # and keep its winding.
    assert sum(_parea(p) for p in (below, inside, above)) == pytest.approx(_parea(tri))
    assert all(_parea(p) > 0 for p in (below, inside, above))


def test_bake_welds_a_shared_cut_edge():
    # Two triangles share the edge 1-2, which crosses both cut lines. Every
    # cut point must come out at one position from the pieces on either
    # side of its line, within a triangle and across the two (one key, one
    # index, between the triangles), whatever clip order made it: the inside piece cuts at 0.875 an
    # edge already cut at 0.125, and is cut on the whole edge all the same.
    p = [_corner(0, -0.4, 0.0), _corner(1, 1.3, 0.1), _corner(2, -0.2, 0.9), _corner(3, 1.1, 1.0)]

    def cuts(tri):
        below, inside, above = gltf._bake(tri, 0, 0.125, 0.875)

        def on(piece, at):
            # A pinned cut keeps the cut's position and UV under its own key.
            return {(pos, uv) for k, pos, _n, uv in piece if "cut" in k[:2] and uv[0] == at}

        assert on(below, 0.125) == on(inside, 0.125)
        assert on(above, 0.875) == on(inside, 0.875)
        return on(inside, 0.125) | on(inside, 0.875)

    shared = cuts([p[0], p[1], p[2]]) & cuts([p[2], p[1], p[3]])
    assert {uv[0] for _pos, uv in shared} == {0.125, 0.875}


@pytest.mark.parametrize("u", [0.125, 0.125 + 5e-5, 0.125 - 5e-5])
def test_bake_corner_on_the_cut_line_makes_no_sliver(u):
    tri = [_corner(0, -0.5, 0.0), _corner(1, u, 0.0), _corner(2, 0.5, 1.0)]
    pieces = gltf._bake(tri, 0, 0.125, 0.875)
    for piece in pieces:
        assert len(piece) >= 3 and _parea(piece) > 1e-3
        corners = [(c[1], c[3]) for c in piece]
        assert len(set(corners)) == len(corners)
    assert sum(_parea(q) for q in pieces) == pytest.approx(_parea(tri), abs=1e-2)


def test_corner_inside_with_tolerance_is_not_cut():
    tri = [_corner(0, 0.125 - 5e-5, 0.0), _corner(1, 0.5, 0.0), _corner(2, 0.875 + 5e-5, 1.0)]
    assert not gltf._needs_bake(tri, 0, 0.125, 0.875)
    assert gltf._needs_bake(tri, 0, 0.25, 0.75)


def test_lerp_corner_is_the_same_from_either_end_and_unit_normal():
    up, down = (0.0, 1.0, 0.0), (0.0, -1.0, 0.0)
    a, b = _corner(0, -0.5, 0.0, up), _corner(1, 1.5, 0.0, down)
    c = gltf._lerp_corner(a, b, 0, 0.5)
    assert c == gltf._lerp_corner(b, a, 0, 0.5)
    # Opposed normals cancel at the midpoint; glTF needs a unit normal.
    assert sum(x * x for x in c[2]) == pytest.approx(1.0)


def test_env_mapped_strip_gets_uvs_from_its_normals(tmp_path):
    roots, strip, w = _one_strip_writer()
    strip.flags |= ninja.STRIP_FLAG_ENV_MAP
    strip.uv = False  # kind 64: no UVs in the data
    w.add_tree(SEC, roots)
    s = w.write(str(tmp_path))
    j = w.doc.j
    assert "baseColorTexture" in j["materials"][0]["pbrMetallicRoughness"]
    prim = j["meshes"][0]["primitives"][0]
    uv = j["accessors"][prim["attributes"]["TEXCOORD_0"]]
    view = j["bufferViews"][uv["bufferView"]]
    got = struct.unpack_from("<2f", w.doc.bin, view["byteOffset"])
    # The quad's normals are +Y: the middle of the map's top row.
    assert got == pytest.approx((0.5, 0.0))
    assert s.approximated["environment map (UVs from model-space normals)"] == 1
    assert not s.unsupported


def test_env_mapped_strip_with_uvs_keeps_them(tmp_path):
    roots, strip, w = _one_strip_writer()
    strip.flags |= ninja.STRIP_FLAG_ENV_MAP
    assert strip.uv
    w.add_tree(SEC, roots)
    s = w.write(str(tmp_path))
    j = w.doc.j
    uv = j["accessors"][j["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]]
    view = j["bufferViews"][uv["bufferView"]]
    got = struct.unpack_from("<%df" % (2 * uv["count"]), w.doc.bin, view["byteOffset"])
    assert sorted(zip(got[::2], got[1::2])) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert s.approximated["environment map (the strip's own UVs)"] == 1
    assert "environment map (UVs from model-space normals)" not in s.approximated


def test_translucent_texture_blends_without_the_use_alpha_flag(tmp_path):
    roots, strip, w = _one_strip_writer(tex_alpha=0x77)
    assert not strip.flags & ninja.STRIP_FLAG_USE_ALPHA
    w.add_tree(SEC, roots)
    s = w.write(str(tmp_path))
    assert w.doc.j["materials"][0]["alphaMode"] == "BLEND"
    assert s.approximated["translucent texture blended without use-alpha"] == 1


@pytest.mark.parametrize("alpha", [240, 250])
def test_near_opaque_texture_stays_opaque(tmp_path, alpha):
    roots, strip, w = _one_strip_writer(tex_alpha=alpha)
    w.add_tree(SEC, roots)
    s = w.write(str(tmp_path))
    assert "alphaMode" not in w.doc.j["materials"][0]
    assert "translucent texture blended without use-alpha" not in s.approximated


def _colour(tmp_path, textured_colour, black=False, **strip):
    """The quad's one material colour (and whether it is textured) under
    this textured_colour, its material chunk's diffuse black (BLiNX 2's
    M121) or the fixture's 0x40, 0x80, 0xC0."""
    roots, s, w = _one_strip_writer(textured_colour=textured_colour)
    if black:
        s.diffuse = bytes((0, 0, 0, 0xFF))
    for k, v in strip.items():
        setattr(s, k, v)
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    (mat,) = w.doc.j["materials"]
    pbr = mat["pbrMetallicRoughness"]
    return pbr["baseColorFactor"], "baseColorTexture" in pbr


def test_textured_strip_takes_the_diffuse_by_default(tmp_path):
    f, textured = _colour(tmp_path, "diffuse", black=True)
    assert textured and f == [0.0, 0.0, 0.0, 1.0]


def test_textured_strip_draws_white_when_the_engine_ignores_the_diffuse(tmp_path):
    # M121's materials: black diffuse, drawn fully textured in the game.
    f, textured = _colour(tmp_path, "white", black=True)
    assert textured and f == [1.0, 1.0, 1.0, 1.0]
    f, _ = _colour(tmp_path, "white")
    assert f == [1.0, 1.0, 1.0, 1.0]


def test_white_covers_environment_mapped_strips(tmp_path):
    # The drawer's env-map flag takes the constant colour on both kinds:
    # 64 (no UVs) and 65 (UVs).
    for kind, uv in ((64, False), (65, True)):
        f, textured = _colour(
            tmp_path, "white", black=True, kind=kind, uv=uv, flags=ninja.STRIP_FLAG_ENV_MAP
        )
        assert textured and f == [1.0, 1.0, 1.0, 1.0]


def test_white_drops_the_diffuse_alpha_of_a_use_alpha_strip(tmp_path):
    # The constant's alpha replaces the diffuse's, so a see-through diffuse
    # no longer blends an opaque texture; the texture's own alpha still
    # sets the mode.
    roots, s, w = _one_strip_writer(textured_colour="white")
    s.diffuse = bytes((0xC0, 0x80, 0x40, 0x80))
    s.flags |= ninja.STRIP_FLAG_USE_ALPHA
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    (mat,) = w.doc.j["materials"]
    assert mat["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]
    assert "alphaMode" not in mat
    roots, s, w = _one_strip_writer(tex_alpha=0x00, textured_colour="white")
    s.flags |= ninja.STRIP_FLAG_USE_ALPHA
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert w.doc.j["materials"][0].get("alphaMode") in ("MASK", "BLEND")


def test_white_counts_strip_kinds_the_drawer_does_not_know(tmp_path):
    roots, s, w = _one_strip_writer(textured_colour="white")
    s.kind = 66
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert w.summary.approximated["strip kind other than 64 and 65 coloured like its UV twin"] == 1
    roots, s, w = _one_strip_writer()
    s.kind = 66
    w.add_tree(SEC, roots)
    assert not w.summary.approximated


def test_white_keeps_the_diffuse_where_no_texture_is_sampled(tmp_path):
    # Kind 64 without a map: untextured, the diffuse.
    f, textured = _colour(tmp_path, "white", uv=False, kind=64)
    assert not textured and f == pytest.approx([0xC0 / 255, 0x80 / 255, 0x40 / 255, 1.0])
    # A texlist of 0 (no textures): strips keep their colours.
    roots, s, _ = _one_strip_writer()
    w = gltf.Writer("M", [], None, textured_colour="white")
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    f = w.doc.j["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"]
    assert f == pytest.approx([0xC0 / 255, 0x80 / 255, 0x40 / 255, 1.0])


def test_white_merges_materials_that_differ_only_in_diffuse(tmp_path):
    roots, s, w = _one_strip_writer(textured_colour="white")
    model = roots[0].children[0].model
    other = copy.copy(s)
    other.diffuse = bytes((0, 0, 0, 0xFF))
    model.strips = [s, other]
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert len(w.doc.j["materials"]) == 1


def test_extract_passes_textured_colour_to_the_writer(game, capsys):
    toml = os.path.join(game.root, "game.toml")
    body = open(toml).read().replace("[extract]\n", '[extract]\ntextured_colour = "white"\n')
    with open(toml, "w") as f:
        f.write(body)
    manifest.use(manifest.load(game.root))
    assert cli.run(Args()) == 0
    j = json.load(open(game.path("dist", "extract", "stages", "MAP11", "MAP11.gltf")))
    assert j["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]


def test_hidden_node_keeps_the_node_not_the_mesh():
    img = Image(xbe([("MAP11", SEC, stage_section("b1"))]))
    roots = ninja.Reader(img, ninja.Format(16, "bams", True)).tree(SEC)
    roots[0].children[0].flags |= ninja.EVAL_HIDE
    w = gltf.Writer("M", [], None)
    w.add_tree(SEC, roots)
    assert w.summary.meshes == 0 and len(w.doc.j["nodes"]) == 3


# ---- the manifest -----------------------------------------------------------


def test_manifest_extract_defaults_and_root_list():
    m = manifest.parse_text(GAME_TOML + EXTRACT_B1)
    x = m["extract"]
    assert x["out"] == "dist/extract" and x["angles"] == "bams" and x["up"] == "y"
    assert x["textured_colour"] == "diffuse"
    assert x["texname_size"] == 32
    assert x["stage"] == [
        {"name": "MAP11", "section": "MAP11", "root": [0x20000], "texlist": [0x20300]}
    ]
    assert "extract" not in manifest.parse_text(GAME_TOML)


@pytest.mark.parametrize(
    "extra, why",
    [
        ('angles = "degrees"', "extract.angles: one of bams, radians"),
        ('up = "z"', "extract.up"),
        ('textured_colour = "grey"', "extract.textured_colour: one of diffuse, white"),
        ("vertex_header = 6", "extract.vertex_header"),
        ('out = "../x"', "extract.out"),
    ],
)
def test_manifest_extract_errors(extra, why):
    with pytest.raises(manifest.ManifestError, match=why.replace(".", r"\.")):
        key = extra.split(" =")[0]
        body = "\n".join(ln for ln in EXTRACT_B1.split("\n") if not ln.startswith(key + " ="))
        manifest.parse_text(GAME_TOML + body.replace("[extract]\n", "[extract]\n%s\n" % extra))


def test_manifest_extract_stage_errors():
    bad = GAME_TOML + EXTRACT_B1 + '\n[[extract.stage]]\nname = "MAP11"\nroot = "auto"\nbogus = 1\n'
    with pytest.raises(manifest.ManifestError) as e:
        manifest.parse_text(bad)
    msg = str(e.value)
    assert "extract.stage[1].bogus: unknown key" in msg
    assert "extract.stage[1].name: 'MAP11' twice" in msg
    assert "extract.stage[1].root: a VA or a list of VAs" in msg
    two = GAME_TOML + EXTRACT_B1.replace(
        "root = 0x20000\ntexlist = 0x20300", "root = [1, 2]\ntexlist = [3]"
    )
    with pytest.raises(manifest.ManifestError, match="texlist: a VA, or one VA per root"):
        manifest.parse_text(two)
    no_tex = GAME_TOML + "[extract]\n"
    with pytest.raises(manifest.ManifestError, match="extract.textures: required"):
        manifest.parse_text(no_tex)


# ---- the verb ---------------------------------------------------------------


def test_missing_extract_table_names_it():
    with pytest.raises(host.CliError, match=r"no \[extract\] table"):
        cli.config({})


def test_extract_writes_the_project(game, capsys):
    assert cli.run(Args()) == 0
    out = capsys.readouterr().out
    assert "MAP11: 1 meshes, 2 triangles, 1 materials, 1 textures" in out
    root = game.path("dist", "extract")
    for rel in (
        "project.godot",
        "view.gd",
        ".gitignore",
        "MAP11.tscn",
        "stages/MAP11/MAP11.gltf",
        "stages/MAP11/MAP11.bin",
        "stages/MAP11/textures/Tex0.png",
    ):
        assert os.path.isfile(os.path.join(root, rel)), rel
    tscn = open(os.path.join(root, "MAP11.tscn")).read()
    assert 'path="res://stages/MAP11/MAP11.gltf"' in tscn
    proj = open(os.path.join(root, "project.godot")).read()
    assert 'run/main_scene="res://MAP11.tscn"' in proj and 'driver/driver="Dummy"' in proj
    assert not [p for p in os.listdir(root) if p.endswith(".pck")]
    rep = json.load(open(os.path.join(root, "stages", "MAP11", "report.json")))
    assert rep["trees"] == [{"root": "0x00020000", "texlist": "0x00020300"}]
    assert (rep["meshes"], rep["triangles"], rep["missing"]) == (1, 2, [])
    txt = open(os.path.join(root, "report.txt")).read()
    assert txt.startswith("MAP11: 1 meshes, 2 triangles") and "approximated:" in txt


def test_report_txt_keeps_every_stage(game, capsys):
    # A second stage over the same tree; re-running one keeps both.
    toml = os.path.join(game.root, "game.toml")
    with open(toml, "a") as f:
        f.write('[[extract.stage]]\nname = "MAP12"\nsection = "MAP11"\n')
        f.write("root = 0x20000\ntexlist = 0x20300\n")
    manifest.use(manifest.load(game.root))
    assert cli.run(Args()) == 0
    assert cli.run(Args(stage=["MAP12"])) == 0
    out = os.path.join(game.root, "dist", "extract")
    txt = open(os.path.join(out, "report.txt")).read()
    assert "MAP11: 1 meshes" in txt and "MAP12: 1 meshes" in txt
    os.remove(os.path.join(out, "stages", "MAP11", "report.json"))
    assert cli.run(Args(stage=["MAP12"])) == 0
    txt = open(os.path.join(out, "report.txt")).read()
    assert "MAP11: no report (re-extract)\n" in txt and "MAP12: 1 meshes" in txt


def test_extract_list_and_unknown_stage(game, capsys):
    assert cli.run(Args(list=True)) == 0
    assert "MAP11" in capsys.readouterr().out and not os.path.exists(game.dist)
    with pytest.raises(host.CliError, match=r"no stage 'MAP99'.*extract --list"):
        cli.run(Args(stage=["MAP99"]))


def test_stage_names_match_any_case(game, capsys):
    assert cli.run(Args(stage=["map11"])) == 0
    assert os.path.isfile(os.path.join(game.dist, "extract", "stages", "MAP11", "MAP11.gltf"))


def test_extract_out_and_missing_texture(game, capsys, tmp_path):
    os.remove(os.path.join(game.game_files, "media", "tex0.dds"))
    out = str(tmp_path / "elsewhere")
    assert cli.run(Args(out=out)) == 0
    text = capsys.readouterr().out
    assert "1 missing" in text and "missing textures: Tex0" in text
    assert os.path.isfile(os.path.join(out, "stages", "MAP11", "MAP11.gltf"))


def test_scan_finds_the_root_and_texlist(game, capsys):
    img = Image.load(game.xbe)
    fmt = ninja.Format(16, "bams", True)
    assert scan.roots(img, "MAP11", fmt) == [(SEC, 2, 1)]
    assert scan.texlists(img, "MAP11", 32) == [(SEC + 0x300, 1, "Tex0")]
    assert scan.model_pointers(img, SEC, fmt) == [SEC + 0x80]
    assert cli.run(Args(scan="MAP11")) == 0
    assert "0x00020000" in capsys.readouterr().out


# ---- Godot ------------------------------------------------------------------


def fake_godot(path, version="4.7.2.stable.official.ed1daf0bf", log="", rc=0):
    """A stand-in binary: prints the version, or the log on import."""
    write(
        path,
        "#!%s\nimport sys\nif '--version' in sys.argv:\n    print(%r)\nelse:\n"
        "    print(%r)\n    sys.exit(%d)\n" % (sys.executable, version, log, rc),
    )
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="a #! stand-in")
def test_godot_override_and_version_check(tmp_path):
    good = fake_godot(str(tmp_path / "godot"))
    assert godot.ensure(str(tmp_path), {godot.ENV: good}) == good
    bad = fake_godot(str(tmp_path / "old"), version="4.3.stable")
    with pytest.raises(host.CliError, match="needs Godot 4.7.2"):
        godot.ensure(str(tmp_path), {godot.ENV: bad})


@pytest.mark.skipif(sys.platform == "win32", reason="a #! stand-in")
def test_godot_import_fails_on_errors(tmp_path):
    out = str(tmp_path)
    ok = fake_godot(str(tmp_path / "ok"), log="Importing...")
    assert godot.import_project(ok, out, os.path.join(out, "import.log")) == ["Importing..."]
    bad = fake_godot(str(tmp_path / "bad"), log="ERROR: Cannot load texture")
    with pytest.raises(host.CliError, match="1 errors"):
        godot.import_project(bad, out, os.path.join(out, "import.log"))


@pytest.mark.skipif(sys.platform == "win32", reason="a #! stand-in")
def test_extract_import_runs_godot(game, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(godot.ENV, fake_godot(str(tmp_path / "godot"), log="ok"))
    assert cli.run(Args(do_import=True)) == 0
    assert "imported with no errors" in capsys.readouterr().out


def test_godot_pins_cover_every_host():
    for key in ("macos-x86_64", "macos-aarch64", "linux-x86_64", "linux-aarch64", "windows-x86_64"):
        asset, sha, size, inner = godot.ASSETS[key]
        assert asset.startswith("Godot_v%s-stable_" % godot.VERSION) and len(sha) == 64
        assert size > 0 and inner


def test_each_tree_draws_with_its_own_texlist():
    img = Image(xbe([("MAP11", SEC, stage_section("b2"))]))
    tree = ninja.Reader(img, ninja.Format(8, "radians")).tree(SEC)

    class Src:
        def read(self, name):
            return dds_raw(1, 1, 32, ARGB8, [0xFFFFFFFF])

    w = gltf.Writer("M", [], Src())
    w.add_tree(SEC, tree, ["Tex0"])
    w.add_tree(SEC, tree, ["tex0"])  # the same file, another spelling
    w.add_tree(SEC, tree, ["Other"])
    assert w.summary.meshes == 2 and len(w.doc.j["images"]) == 2
    assert len(w.doc.j["materials"]) == 2


def test_additive_blend_is_supported_not_unsupported():
    # Material chunk with Ninja blend 0x21 (source alpha, one) before a
    # second copy of the strip: the reader counts it as supported.
    strip = struct.pack("<Hh", 1, 3) + b"".join(struct.pack("<Hhh", i, 0, 0) for i in range(3))
    extra = struct.pack("<BBH", 17, ninja.BLEND_ADD, 2) + bytes((0, 0, 0, 0xFF))
    extra += struct.pack("<BBH", 65, 0, len(strip) // 2) + strip
    r, roots = read("b2", extra)
    assert "blend mode 0x21" not in r.unsupported
    assert r.supported == {ninja.SUPPORTED_ADD: 1}
    strips = roots[0].children[0].model.strips
    assert [s.blend for s in strips] == [ninja.BLEND_ALPHA, ninja.BLEND_ADD]


def test_additive_strip_marks_its_material_for_godot(tmp_path):
    roots, strip, w = _one_strip_writer()
    strip.blend = ninja.BLEND_ADD
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    (mat,) = w.doc.j["materials"]
    assert mat["alphaMode"] == "BLEND"
    assert mat["extras"] == {gltf.EXTRA_BLEND: "add"}
    # The Godot side reads the same key.
    assert '"%s"' % gltf.EXTRA_BLEND in godot.STAGE_GD
    assert "BLEND_MODE_ADD" in godot.STAGE_GD


def test_alpha_blend_strip_has_no_extras(tmp_path):
    roots, _strip, w = _one_strip_writer()
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert "extras" not in w.doc.j["materials"][0]


def test_stage_scene_applies_stage_gd(tmp_path):
    godot.write_project(str(tmp_path), "T", ["S1"])
    assert os.path.isfile(tmp_path / "stage.gd")
    tscn = (tmp_path / "S1.tscn").read_text()
    assert 'path="res://stage.gd"' in tscn
    # The script sits on the glTF instance, not the viewer root.
    assert tscn.rstrip().endswith('instance=ExtResource("2")]\nscript = ExtResource("3")')


def test_report_lists_supported():
    rep = {
        "stage": "S",
        "meshes": 1,
        "triangles": 2,
        "materials": 1,
        "textures": 1,
        "missing": [],
        "unsupported": {},
        "approximated": {},
        "supported": {ninja.SUPPORTED_ADD: 3},
    }
    assert "    supported: %s (3)" % ninja.SUPPORTED_ADD in cli._report_lines(rep)
    del rep["supported"]  # a report from before the key
    assert len(cli._report_lines(rep)) == 1


# ---- per-root transforms (a root the game draws again, scaled) ------------

SKY_TWICE = """
[[extract.stage]]
name = "SKY"
section = "MAP11"
root = [0x20000, 0x20000]
texlist = 0x20300
transform = [{}, {scale = [1, -1, 1], translation = [0, 5, 0]}]
"""


def test_manifest_transform_one_table_per_root():
    m = manifest.parse_text(GAME_TOML + EXTRACT_B1 + SKY_TWICE)
    plain, sky = m["extract"]["stage"]
    assert "transform" not in plain
    assert sky["transform"] == [{}, {"scale": [1, -1, 1], "translation": [0, 5, 0]}]


@pytest.mark.parametrize(
    "transform, why",
    [
        ("[{}]", "transform: one table per root"),
        ("{scale = [1, 1, 1]}", "transform: one table per root"),
        ('[{}, "x"]', r"transform\[1\]: a table"),
        ("[{}, {scale = [1, 0, 1]}]", r"transform\[1\]\.scale: three finite non-zero numbers"),
        ("[{}, {scale = [1, -1]}]", r"transform\[1\]\.scale: three finite non-zero numbers"),
        (
            "[{}, {translation = [0, true, 0]}]",
            r"transform\[1\]\.translation: three finite numbers",
        ),
        ("[{}, {rotation = [0, 0, 0]}]", r"transform\[1\]\.rotation: unknown key"),
        # NaN and inf would reach the glTF as JSON it may not hold.
        ("[{}, {scale = [1, nan, 1]}]", r"transform\[1\]\.scale: three finite"),
        ("[{}, {scale = [1, -inf, 1]}]", r"transform\[1\]\.scale: three finite"),
        ("[{}, {translation = [0, nan, 0]}]", r"transform\[1\]\.translation: three finite"),
        ("[{}, {translation = [inf, 0, 0]}]", r"transform\[1\]\.translation: three finite"),
        # The same draw twice: {} and a unit scale are the same matrix.
        ("[{}, {}]", r"root\[1\]: 0x00020000 drawn again .* as root\[0\]"),
        ("[{}, {scale = [1, 1, 1]}]", r"root\[1\]: 0x00020000 drawn again .* as root\[0\]"),
        ("[{}, {translation = [0, 0, 0.0]}]", r"root\[1\]: 0x00020000 drawn again"),
    ],
)
def test_manifest_transform_errors(transform, why):
    bad = (GAME_TOML + EXTRACT_B1 + SKY_TWICE).replace(
        "transform = [{}, {scale = [1, -1, 1], translation = [0, 5, 0]}]",
        "transform = %s" % transform,
    )
    with pytest.raises(manifest.ManifestError, match=r"extract\.stage\[1\]\." + why):
        manifest.parse_text(bad)


def test_transform_wraps_the_tree_inside_the_up_turn(tmp_path):
    img = Image(xbe([("MAP11", SEC, stage_section("b2"))]))
    tree = ninja.Reader(img, ninja.Format(8, "radians")).tree(SEC)
    w = gltf.Writer("M", ["Tex0"], None, up="-y")
    w.add_tree(SEC, tree, transform={"scale": [1, 1, 1]})  # identity: no extra node
    w.add_tree(SEC, tree, transform={"scale": [1, -1, 1], "translation": [0, 5, 0]})
    w.add_tree(SEC, tree, transform={"translation": [0, 0, 7]})
    nodes = w.doc.j["nodes"]
    a, b, c = (nodes[i] for i in w.doc.j["scenes"][0]["nodes"])
    assert [a["name"], b["name"], c["name"]] == ["M_00020000", "M_00020000_2", "M_00020000_3"]
    # The up turn stays outermost; the game's scale and translation sit
    # under it, around the tree's own nodes, which are unchanged.
    assert b["rotation"] == a["rotation"] == gltf.UP_ROTATION["-y"]
    (xf,) = (nodes[i] for i in b["children"])
    assert xf["scale"] == [1.0, -1.0, 1.0] and xf["translation"] == [0.0, 5.0, 0.0]
    (xf3,) = (nodes[i] for i in c["children"])
    assert "scale" not in xf3 and xf3["translation"] == [0.0, 0.0, 7.0]

    def names(n):
        return [nodes[i]["name"] for i in n["children"]]

    assert names(xf) == names(xf3) == names(a) == ["obj_00020000"]
    # The mirrored copy reuses the mesh: one model, one mesh.
    assert w.summary.meshes == 1


def test_extract_reports_and_lists_the_transform(game, capsys):
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(SKY_TWICE)
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(list=True)) == 0
    assert "0x00020000/0x00020300 scale(1, -1, 1) translation(0, 5, 0)" in capsys.readouterr().out
    assert cli.run(Args(stage=["SKY"])) == 0
    root = game.path("dist", "extract")
    rep = json.load(open(os.path.join(root, "stages", "SKY", "report.json")))
    assert rep["trees"] == [
        {"root": "0x00020000", "texlist": "0x00020300"},
        {
            "root": "0x00020000",
            "texlist": "0x00020300",
            "transform": {"scale": [1, -1, 1], "translation": [0, 5, 0]},
        },
    ]
    j = json.load(open(os.path.join(root, "stages", "SKY", "SKY.gltf")))
    assert any(n.get("scale") == [1.0, -1.0, 1.0] for n in j["nodes"])


def test_single_root_takes_a_transform(game):
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(
            '[[extract.stage]]\nname = "ONE"\nsection = "MAP11"\nroot = 0x20000\n'
            "texlist = 0x20300\ntransform = [{scale = [2, 2, 2]}]\n"
        )
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(stage=["ONE"])) == 0
    root = game.path("dist", "extract", "stages", "ONE")
    rep = json.load(open(os.path.join(root, "report.json")))
    assert rep["trees"] == [
        {"root": "0x00020000", "texlist": "0x00020300", "transform": {"scale": [2, 2, 2]}}
    ]
    j = json.load(open(os.path.join(root, "ONE.gltf")))
    assert any(n.get("scale") == [2.0, 2.0, 2.0] for n in j["nodes"])


def test_extract_reports_an_additive_strip_as_supported(game):
    # The same b1 stage with a 0x21 material chunk and a second strip:
    # report.json carries the reader's supported count.
    strip = struct.pack("<Hh", 1, 3) + b"".join(struct.pack("<Hhh", i, 0, 0) for i in range(3))
    extra = struct.pack("<BBH", 17, ninja.BLEND_ADD, 2) + bytes((0, 0, 0, 0xFF))
    extra += struct.pack("<BBH", 65, 0, len(strip) // 2) + strip
    write(
        os.path.join(game.root, "game_files", "default.xbe"),
        xbe([("MAP11", SEC, stage_section("b1", extra))]),
    )
    assert cli.run(Args(stage=["MAP11"])) == 0
    out = game.path("dist", "extract")
    rep = json.load(open(os.path.join(out, "stages", "MAP11", "report.json")))
    assert rep["supported"] == {ninja.SUPPORTED_ADD: 1}
    assert "blend mode 0x21" not in rep["unsupported"]
    assert "supported: %s (1)" % ninja.SUPPORTED_ADD in open(os.path.join(out, "report.txt")).read()


# ---- texture_alpha (a floor a planar reflection shows through) -------------

FLOOR = """
[[extract.stage]]
name = "FLOOR"
section = "MAP11"
root = [0x20000, 0x20000]
texlist = 0x20300
transform = [{}, {scale = [1, -1, 1]}]
texture_alpha = {tex0 = 0.5}
"""


def test_texture_alpha_blends_the_named_texture(tmp_path):
    # The texlist says Tex0; the table matches it in any case.
    roots, _, w = _one_strip_writer()
    w.texture_alpha = {"tex0": 0.5}
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    (mat,) = w.doc.j["materials"]
    assert mat["alphaMode"] == "BLEND"
    assert mat["pbrMetallicRoughness"]["baseColorFactor"][3] == 0.5
    assert w.summary.approximated["blend pass as a translucent texture (texture_alpha)"] == 1


def test_texture_alpha_scales_the_white_constant_and_leaves_others_alone(tmp_path):
    roots, _, _ = _one_strip_writer()
    src = _one_strip_writer()[2].source
    w = gltf.Writer("M", ["Tex0"], src, textured_colour="white", texture_alpha={"TEX0": 0.25})
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert w.doc.j["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] == [1, 1, 1, 0.25]
    roots, _, w = _one_strip_writer()
    w.texture_alpha = {"other": 0.5}
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert "alphaMode" not in w.doc.j["materials"][0]
    assert "blend pass as a translucent texture (texture_alpha)" not in w.summary.approximated


def test_extract_reports_the_texture_alpha(game):
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(FLOOR)
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(stage=["FLOOR", "MAP11"])) == 0
    root = game.path("dist", "extract", "stages", "FLOOR")
    rep = json.load(open(os.path.join(root, "report.json")))
    assert rep["texture_alpha"] == {"tex0": 0.5}
    j = json.load(open(os.path.join(root, "FLOOR.gltf")))
    (mat,) = j["materials"]
    assert mat["alphaMode"] == "BLEND" and mat["pbrMetallicRoughness"]["baseColorFactor"][3] == 0.5
    plain = json.load(open(game.path("dist", "extract", "stages", "MAP11", "report.json")))
    assert "texture_alpha" not in plain


@pytest.mark.parametrize(
    "table, why",
    [
        ("0.5", "texture_alpha: a table of texture names"),
        ("{tex0 = 1.5}", r"texture_alpha\.tex0: an opacity from 0 to 1"),
        ("{tex0 = -0.1}", r"texture_alpha\.tex0: an opacity from 0 to 1"),
        ('{tex0 = "half"}', r"texture_alpha\.tex0: an opacity from 0 to 1"),
        ("{tex0 = true}", r"texture_alpha\.tex0: an opacity from 0 to 1"),
        ("{tex0 = nan}", r"texture_alpha\.tex0: an opacity from 0 to 1"),
        ("{tex0 = 0.5, TEX0 = 0.4}", r"texture_alpha\.TEX0: the same texture as tex0"),
    ],
)
def test_manifest_texture_alpha_errors(table, why):
    bad = (GAME_TOML + EXTRACT_B1 + FLOOR).replace(
        "texture_alpha = {tex0 = 0.5}", "texture_alpha = %s" % table
    )
    with pytest.raises(manifest.ManifestError, match=r"extract\.stage\[1\]\." + why):
        manifest.parse_text(bad)


def test_texture_alpha_of_one_keeps_the_alpha_mode(tmp_path):
    # Fully opaque hides nothing below: no BLEND, nothing approximated.
    roots, _, w = _one_strip_writer()
    w = gltf.Writer("M", ["Tex0"], w.source, texture_alpha={"tex0": 1})
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    (mat,) = w.doc.j["materials"]
    assert "alphaMode" not in mat
    assert mat["pbrMetallicRoughness"]["baseColorFactor"][3] == 1
    assert "blend pass as a translucent texture (texture_alpha)" not in w.summary.approximated
    assert w.unmatched_texture_alpha() == []


def test_texture_alpha_lists_the_names_no_strip_drew_with(tmp_path):
    roots, _, w = _one_strip_writer()
    w = gltf.Writer("M", ["Tex0"], w.source, texture_alpha={"TEX0": 0.5, "Floor9": 0.4})
    w.add_tree(SEC, roots)
    w.write(str(tmp_path))
    assert w.unmatched_texture_alpha() == ["Floor9"]


def test_extract_warns_of_an_unmatched_texture_alpha(game):
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(FLOOR.replace("{tex0 = 0.5}", "{tex0 = 0.5, Floor9 = 0.4}"))
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(stage=["FLOOR", "MAP11"])) == 0
    rep = json.load(open(game.path("dist", "extract", "stages", "FLOOR", "report.json")))
    assert rep["texture_alpha_unmatched"] == ["Floor9"]
    txt = open(game.path("dist", "extract", "report.txt")).read()
    assert "warning: texture_alpha matched no texture: Floor9" in txt
    plain = json.load(open(game.path("dist", "extract", "stages", "MAP11", "report.json")))
    assert "texture_alpha_unmatched" not in plain


# ---- backdrop (a render target's clear colour behind a mirrored tree) ------


def _backdrop(tmp_path, transform):
    roots, _, w = _one_strip_writer()
    w.add_tree(SEC, roots, transform=transform)
    w.write(str(tmp_path))
    j = w.doc.j
    (mesh,) = [m for m in j["meshes"] if m["name"].endswith("_backdrop")]
    (prim,) = mesh["primitives"]
    return w, j, prim


def test_backdrop_boxes_the_mirrored_tree_open_on_the_plane(tmp_path):
    # The quad lies at y = 2; mirrored about y = 1 it lands at y = 0.
    w, j, prim = _backdrop(
        tmp_path, {"scale": [1, -1, 1], "translation": [0, 2, 0], "backdrop": [0, 0, 0.5]}
    )
    pos = j["accessors"][prim["attributes"]["POSITION"]]
    assert pos["max"][1] == 1.0 and pos["min"][1] < 0
    assert pos["min"][0] < 3 and pos["max"][0] > 4
    assert j["accessors"][prim["indices"]]["count"] == 30  # five faces, no lid
    mat = j["materials"][prim["material"]]
    assert mat["pbrMetallicRoughness"]["baseColorFactor"] == [0, 0, 0.5, 1]
    assert gltf.UNLIT in mat["extensions"] and gltf.UNLIT in j["extensionsUsed"]
    assert w.summary.approximated["render target clear colour as a backdrop box"] == 1
    assert w.summary.triangles == 2 + 10
    # The box sits beside the mirrored tree, in the game's space.
    top = j["nodes"][j["scenes"][0]["nodes"][0]]
    names = [j["nodes"][k]["name"] for k in top["children"]]
    assert names == ["M_00020000_transform", "M_00020000_backdrop"]


def _rows(tmp_path, j, acc, n):
    """An accessor's rows as M.bin stores them."""
    with open(os.path.join(str(tmp_path), "M.bin"), "rb") as f:
        raw = f.read()
    a = j["accessors"][acc]
    bv = j["bufferViews"][a["bufferView"]]
    fmt = "<" + ("f" if a["componentType"] == gltf.FLOAT else "I") * n
    size = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, raw, bv["byteOffset"] + i * size) for i in range(a["count"])]


# The b2 quad spans x 3..4, y 2, z -2..-1. Each case mirrors one axis about
# translation / 2: the plane above the copy (y 2, y -0.1), below it (y 6,
# x 20, open on the low side), or through it (z -3.3).
MIRRORS = [(1, 2), (1, 6), (1, -0.1), (0, 20), (2, -3.3)]


@pytest.mark.parametrize("axis, move", MIRRORS)
def test_backdrop_is_open_on_the_plane_and_faces_inward(tmp_path, axis, move):
    scale, tr = [1, 1, 1], [0, 0, 0]
    scale[axis], tr[axis] = -1, move
    _, j, prim = _backdrop(tmp_path, {"scale": scale, "translation": tr, "backdrop": [0, 0, 0]})
    pos = _rows(tmp_path, j, prim["attributes"]["POSITION"], 3)
    idx = [i for (i,) in _rows(tmp_path, j, prim["indices"], 1)]
    assert len(idx) == 30  # five faces, no lid
    lo, hi = gltf._tree_bounds(read("b2")[1])
    ends = [sorted((lo[i] * scale[i] + tr[i], hi[i] * scale[i] + tr[i])) for i in range(3)]
    box = [(min(p[i] for p in pos), max(p[i] for p in pos)) for i in range(3)]
    for i in range(3):
        if i != axis:
            assert box[i][0] < ends[i][0] and box[i][1] > ends[i][1]
    plane = move / 2
    open_low = plane < (ends[axis][0] + ends[axis][1]) / 2
    assert open_low == (move in (6, 20))
    shut = box[axis][1] if open_low else box[axis][0]
    assert box[axis][0 if open_low else 1] == pytest.approx(plane, abs=1e-5)
    faces = [pos[k : k + 4] for k in range(0, len(pos), 4)]
    at = [f[0][axis] for f in faces if len({p[axis] for p in f}) == 1]
    assert at == [shut]  # the far face only; the plane's side is open
    centre = [(box[i][0] + box[i][1]) / 2 for i in range(3)]
    for t in range(0, len(idx), 3):
        a, b, c = (pos[k] for k in idx[t : t + 3])
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        # Counter-clockwise front faces point at the box's inside.
        assert sum(n[i] * (centre[i] - a[i]) for i in range(3)) > 0


@pytest.mark.parametrize("axis, move", MIRRORS)
def test_backdrop_bounds_are_the_stored_values(tmp_path, axis, move):
    # glTF's POSITION min/max must match the float32s in the buffer, not
    # the float64 corners they were packed from.
    scale, tr = [1, 1, 1], [0, 0, 0]
    scale[axis], tr[axis] = -1, move
    _, j, prim = _backdrop(tmp_path, {"scale": scale, "translation": tr, "backdrop": [0, 0, 0]})
    acc = prim["attributes"]["POSITION"]
    pos = _rows(tmp_path, j, acc, 3)
    assert j["accessors"][acc]["min"] == [min(p[i] for p in pos) for i in range(3)]
    assert j["accessors"][acc]["max"] == [max(p[i] for p in pos) for i in range(3)]


def _node(pos=(0, 0, 0), ang=(0, 0, 0), scl=(1, 1, 1), flags=0, verts=None, children=()):
    n = ninja.Node()
    n.va, n.flags, n.pos, n.ang, n.scl = 0, flags, pos, ang, scl
    n.children = list(children)
    n.model = None
    if verts:
        n.model = ninja.Model()
        n.model.verts = dict(enumerate(verts))
    return n


def test_tree_bounds_places_children_and_skips_hidden_nodes():
    # The root turns 90 degrees about Y (x -> -z, z -> x) and moves by
    # (10, 0, 0); its child adds (0, 5, 0) and doubles its vertex. A hidden
    # node's vertices, far off, count for nothing, its children still do.
    child = _node(pos=(0, 5, 0), scl=(2, 2, 2), verts=[(1, 0, 0)])
    hidden = _node(
        flags=ninja.EVAL_HIDE, verts=[(1000, 1000, 1000)], children=[_node(verts=[(0, 0, 1)])]
    )
    root = _node(
        pos=(10, 0, 0), ang=(0, math.pi / 2, 0), verts=[(0, 0, 0)], children=[child, hidden]
    )
    lo, hi = gltf._tree_bounds([root])
    # Root vertex -> (10, 0, 0); the child's (2, 5, 0) turned -> (0, 5, -2),
    # + (10, 0, 0); hidden's child (0, 0, 1) turned -> (1, 0, 0), + (10, 0, 0).
    assert lo == pytest.approx([10, 0, -2])
    assert hi == pytest.approx([11, 5, 0])
    assert gltf._tree_bounds([_node(flags=ninja.EVAL_HIDE, verts=[(1, 1, 1)])]) == (None, None)


def test_extract_reports_the_backdrop(game):
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(FLOOR.replace("{scale = [1, -1, 1]}", "{scale = [1, -1, 1], backdrop = [0, 0, 0]}"))
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(stage=["FLOOR"])) == 0
    out = game.path("dist", "extract")
    rep = json.load(open(os.path.join(out, "stages", "FLOOR", "report.json")))
    assert rep["approximated"]["render target clear colour as a backdrop box"] == 1
    assert rep["trees"][1]["transform"]["backdrop"] == [0, 0, 0]
    txt = open(os.path.join(out, "report.txt")).read()
    assert "approximated: render target clear colour as a backdrop box (1)" in txt


def test_no_backdrop_without_the_key(tmp_path):
    roots, _, w = _one_strip_writer()
    w.add_tree(SEC, roots, transform={"scale": [1, -1, 1], "translation": [0, 2, 0]})
    w.write(str(tmp_path))
    assert not any(m["name"].endswith("_backdrop") for m in w.doc.j["meshes"])


@pytest.mark.parametrize(
    "xf, why",
    [
        ("{scale = [1, -1, 1], backdrop = [0, 0]}", "backdrop: an RGB colour"),
        ("{scale = [1, -1, 1], backdrop = [0, 0, 2]}", "backdrop: an RGB colour"),
        ("{backdrop = [0, 0, 0]}", "backdrop: needs a scale that mirrors one axis"),
        ("{scale = [-1, -1, 1], backdrop = [0, 0, 0]}", "backdrop: needs a scale"),
    ],
)
def test_manifest_backdrop_errors(xf, why):
    bad = (GAME_TOML + EXTRACT_B1 + FLOOR).replace("{scale = [1, -1, 1]}", xf)
    with pytest.raises(
        manifest.ManifestError, match=r"extract\.stage\[1\]\.transform\[1\]\." + why
    ):
        manifest.parse_text(bad)


def test_manifest_backdrop_leaves_a_bad_scale_its_own_error():
    bad = (GAME_TOML + EXTRACT_B1 + FLOOR).replace(
        "{scale = [1, -1, 1]}", "{scale = [1, 0, 1], backdrop = [0, 0, 0]}"
    )
    with pytest.raises(manifest.ManifestError) as e:
        manifest.parse_text(bad)
    assert "transform[1].scale: three finite non-zero numbers" in str(e.value)
    assert "needs a scale" not in str(e.value)


def test_manifest_takes_a_backdrop():
    ok = (GAME_TOML + EXTRACT_B1 + FLOOR).replace(
        "{scale = [1, -1, 1]}", "{scale = [1, -1, 1], backdrop = [0, 0, 0]}"
    )
    manifest.parse_text(ok)


# ---- the collision layer ----------------------------------------------------

# stage_section's diffuse bytes (B, G, R, A) as the class 0xRRGGBB.
CLASS = 0xC08040


def _hidden_tree():
    img = Image(xbe([("MAP11", SEC, stage_section("b1"))]))
    roots = ninja.Reader(img, ninja.Format(16, "bams", True)).tree(SEC)
    # A collision tree is never drawn: its nodes carry the hide flag.
    roots[0].flags |= ninja.EVAL_HIDE
    roots[0].children[0].flags |= ninja.EVAL_HIDE
    return roots


def test_collision_bakes_hidden_trees_to_world_space():
    tris = collision.world_triangles(_hidden_tree())
    assert list(tris) == [CLASS] and len(tris[CLASS]) == 2
    # The child's (1, 2, 3), then the root's quarter turn about Y:
    # (x, y, z) -> (z, y, -x).
    want = {(round(z + 3, 4), round(y + 2, 4), round(-(x + 1), 4)) for x, y, z in QUAD}
    got = {tuple(round(c, 4) for c in p) for t in tris[CLASS] for p in t}
    assert got == want


def test_collision_applies_transforms_whatever_the_eval_flags():
    roots = _hidden_tree()
    roots[0].children[0].flags |= ninja.EVAL_UNIT_POS
    got = {p for t in collision.world_triangles(roots)[CLASS] for p in t}
    assert all(round(p[1], 4) == 2 for p in got)


def test_collision_rotates_x_first_even_with_the_zxy_flag():
    # The game's bake rotates every node X first, then Y, then Z, and
    # ignores EVAL_ZXY; a two-axis turn tells the two orders apart.
    def bake(flags):
        roots = _hidden_tree()
        roots[0].ang = (math.pi / 2, 0.0, math.pi / 2)
        roots[0].flags |= flags
        return {
            tuple(round(c, 4) for c in p)
            for t in collision.world_triangles(roots)[CLASS]
            for p in t
        }

    assert bake(ninja.EVAL_ZXY) == bake(0)
    assert ninja.quat((math.pi / 2, 0.0, math.pi / 2), ninja.EVAL_ZXY) != ninja.quat(
        (math.pi / 2, 0.0, math.pi / 2), 0
    )


def test_collision_gltf_names_shapes_and_skips(tmp_path):
    w = collision.Writer("M", "-y")
    w.add_trees([_hidden_tree()])
    rep = w.write(str(tmp_path))
    j = json.load(open(tmp_path / "M_col.gltf"))
    assert os.path.getsize(tmp_path / "M_col.bin") == j["buffers"][0]["byteLength"]
    names = [n["name"] for n in j["nodes"]]
    assert names == ["col_c08040-col", "M_col"]
    assert j["nodes"][1]["rotation"] == gltf.UP_ROTATION["-y"]
    mat = j["materials"][0]
    assert mat["alphaMode"] == "BLEND" and mat["doubleSided"]
    assert mat["pbrMetallicRoughness"]["baseColorFactor"][3] == collision.DEBUG_ALPHA
    assert gltf.UNLIT in j["extensionsUsed"]
    assert rep == {
        "triangles": 2,
        "classes": {"C08040": {"triangles": 2, "shape": True}},
        "left_out": {},
    }
    assert json.load(open(tmp_path / "collision.json")) == rep
    w = collision.Writer("M", skip=[CLASS])
    w.add_trees([_hidden_tree()])
    rep = w.write(str(tmp_path))
    j = json.load(open(tmp_path / "M_col.gltf"))
    assert j["nodes"][0]["name"] == "col_c08040" and "rotation" not in j["nodes"][1]
    assert rep["classes"]["C08040"]["shape"] is False


def test_collision_counts_strips_without_a_colour():
    roots = _hidden_tree()
    roots[0].children[0].model.strips[0].diffuse = None
    stats = collision.Counter()
    assert collision.world_triangles(roots, stats) == {}
    assert stats == {"strips without a class colour": 1}


@pytest.fixture
def col_game(game):
    toml = os.path.join(game.root, "game.toml")
    with open(toml, "a") as f:
        f.write("collision = 0x20000\n")
    G = manifest.load(game.root)
    manifest.use(G)
    return G


def test_extract_collision_is_opt_in(col_game, capsys):
    root = col_game.path("dist", "extract")
    assert cli.run(Args()) == 0
    assert not os.path.exists(os.path.join(root, "stages", "MAP11", "MAP11_col.gltf"))
    assert not os.path.exists(os.path.join(root, "collision.gd"))
    assert open(os.path.join(root, "MAP11.tscn")).read() == godot.SCENE.format(name="MAP11")
    assert cli.run(Args(collision=True)) == 0
    assert "MAP11 collision: 2 triangles in 1 classes" in capsys.readouterr().out
    for rel in ("collision.gd", "stages/MAP11/MAP11_col.gltf", "stages/MAP11/collision.json"):
        assert os.path.isfile(os.path.join(root, rel)), rel
    tscn = open(os.path.join(root, "MAP11.tscn")).read()
    assert 'path="res://stages/MAP11/MAP11_col.gltf"' in tscn
    assert 'path="res://collision.gd"' in tscn
    # A later run without the flag drops the layer again.
    assert cli.run(Args()) == 0
    assert open(os.path.join(root, "MAP11.tscn")).read() == godot.SCENE.format(name="MAP11")
    assert not os.path.exists(os.path.join(root, "collision.gd"))


def test_extract_list_names_collision_trees(col_game, capsys):
    assert cli.run(Args(list=True)) == 0
    assert "; collision 0x00020000" in capsys.readouterr().out


def test_manifest_extract_collision_keys():
    m = manifest.parse_text(GAME_TOML + EXTRACT_B1 + "collision = 0x20000\n")
    assert m["extract"]["stage"][0]["collision"] == [0x20000]
    assert m["extract"]["collision_skip"] == []
    bad = GAME_TOML + EXTRACT_B1 + 'collision = "x"\n'
    with pytest.raises(manifest.ManifestError, match=r"stage\[0\]\.collision: a VA"):
        manifest.parse_text(bad)
    skip = GAME_TOML + EXTRACT_B1.replace(
        "[extract]\n", "[extract]\ncollision_skip = [0x1000000]\n"
    )
    with pytest.raises(manifest.ManifestError, match="extract.collision_skip"):
        manifest.parse_text(skip)
    flag = GAME_TOML + EXTRACT_B1.replace("[extract]\n", "[extract]\ncollision_skip = [true]\n")
    with pytest.raises(manifest.ManifestError, match="extract.collision_skip"):
        manifest.parse_text(flag)


# ---- the placements layer ---------------------------------------------------

DATA = 0x30000
TURNS = 3 * 2 * math.pi  # record angles may run many turns


def data_section():
    """Placement tables: a set table indexed by stage (stage 1 -> a list of
    two 0x20-byte records; stage 1 + layer 1 -> three more), a variant
    table for class 1 whose variant 1 is stage_section's tree, a pair for
    class 3 where only the flagged table names it, and an object whose
    model pointer leads nowhere."""
    d = bytearray(0x300)
    struct.pack_into("<3I", d, 0, 0, DATA + 0x40, DATA + 0x48)
    struct.pack_into("<2I", d, 0x40, 2, DATA + 0x80)
    # class dword, pos, a 1.0 (as the game's records hold), rotation.
    struct.pack_into("<I3ff3f", d, 0x80, 0x0101, 1, 2, 3, 1, 0, math.pi / 2 + TURNS, 0)
    struct.pack_into("<I3ff3f", d, 0xA0, 0x01000002, 4, 5, 6, 1, 0, 0, 0)
    struct.pack_into("<4I", d, 0x100, 0, 0, SEC, SEC + 0x300)
    # Layer 1: a flagged class 3, variant 1; a fixed class 4; class 5,
    # whose tree cannot be read.
    struct.pack_into("<2I", d, 0x48, 3, DATA + 0x140)
    struct.pack_into("<I3ff3f", d, 0x140, 0x01000103, 7, 8, 9, 1, 0, 0, 0)
    struct.pack_into("<I3ff3f", d, 0x160, 0x04, 0, 0, 0, 1, 0, 0, 0)
    struct.pack_into("<I3ff3f", d, 0x180, 0x05, 0, 0, 0, 1, 0, 0, 0)
    struct.pack_into("<4I", d, 0x1C0, 0, 0, SEC, SEC + 0x300)  # class 3, flagged
    struct.pack_into("<2I", d, 0x200, 0, 0x7FFF0000)  # eval flags, a model nowhere
    return bytes(d)


PLACEMENTS = """
[[extract.placement_set]]
name = "enemy"
table = 0x30000
stride = 0x20
pos_at = 4
rot_at = 0x14
[[extract.placement_model]]
set = "enemy"
class = 1
table = 0x30100
stride = 8
"""
# Layer 1's classes: a flagged table, a fixed object, a tree that derails.
PLACEMENTS_L1 = """
[[extract.placement_model]]
set = "enemy"
class = 3
table = 0x301A0
table_flagged = 0x301C0
stride = 8
[[extract.placement_model]]
set = "enemy"
class = 4
object = 0x%X
texlist = 0x%X
[[extract.placement_model]]
set = "enemy"
class = 5
object = 0x30200
texlist = 0
""" % (SEC, SEC + 0x300)


def _pl_cfg(world="y", up="y", extra=""):
    toml = GAME_TOML + EXTRACT_B1.replace(
        "[extract]\n", '[extract]\nup = "%s"\nplacement_world = "%s"\n' % (up, world)
    )
    return manifest.parse_text(toml + "placements = 1\n" + PLACEMENTS + extra)["extract"]


def _pl_image(data=None):
    return Image(xbe([("MAP11", SEC, stage_section("b1")), ("DATA", DATA, data or data_section())]))


def test_placements_read_records_and_resolve_models():
    cfg = _pl_cfg()
    img = _pl_image()
    ps, errors = placements.read(img, cfg["placement_set"], 1)
    assert errors == []
    assert [(p.cls, p.variant, p.flags, p.pos) for p in ps] == [
        (1, 1, 0, (1, 2, 3)),
        (2, 0, 1, (4, 5, 6)),
    ]
    assert ps[0].name == "enemy_l0_000_cls101"
    assert placements.resolve(img, cfg["placement_model"], ps) == [("enemy", 2)]
    assert (ps[0].model, ps[0].texlist) == (SEC, SEC + 0x300)
    assert ps[1].model == 0
    # Stage 0's entry is empty: nothing, not an error.
    assert placements.read(img, cfg["placement_set"], 0) == ([], [])


def test_placements_gltf_draws_rigid_models_and_marks_the_rest(tmp_path):
    cfg = _pl_cfg()
    stage = cfg["stage"][0]
    rep = placements.extract(_pl_image(), cli.fmt_of(cfg), cfg, stage, None, str(tmp_path))
    assert rep["sets"] == {
        "enemy": {"records": 2, "drawn": 1, "skinned": 0, "unreadable": 0, "marker": 1}
    }
    assert rep["classes_without_model"] == ["enemy 0x02"]
    j = json.load(open(tmp_path / "MAP11_placements.gltf"))
    nodes = {n["name"]: n for n in j["nodes"]}
    top = j["nodes"][j["scenes"][0]["nodes"][0]]
    assert top["name"] == "enemy" and "rotation" not in top
    a = nodes["enemy_l0_000_cls101"]
    assert a["translation"] == [1, 2, 3]
    # The many turns fold away: a quarter turn about Y.
    s = math.sqrt(0.5)
    assert a["rotation"] == pytest.approx([0, s, 0, s])
    assert a["extras"]["model"] == "0x%08X" % SEC and a["extras"]["skinned"] is False
    model = j["nodes"][a["children"][0]]
    assert model["name"] == "enemy_l0_000_cls101_model" and "rotation" not in model
    assert j["nodes"][model["children"][0]]["name"] == "obj_%08x" % SEC
    b = nodes["enemy_l0_001_cls2"]
    marker = j["nodes"][b["children"][0]]
    assert marker["name"] == "enemy_l0_001_cls2_marker"
    assert j["meshes"][marker["mesh"]]["name"] == "marker_enemy"
    rows = json.load(open(tmp_path / "placements.json"))["placements"]
    assert rows[1]["raw"] == data_section()[0xA0:0xC0].hex()


@pytest.mark.parametrize(
    "world, up, top, turn",
    [
        ("y", "y", False, False),
        ("-y", "y", True, True),
        ("-y", "-y", False, True),
        ("y", "-y", True, False),
    ],
)
def test_placements_turn_the_world_back_into_the_data(tmp_path, world, up, top, turn):
    cfg = _pl_cfg(world, up)
    t, m = placements._turns(cfg)
    half = gltf.UP_ROTATION["-y"]
    assert (t == half, m == half) == (top, turn)
    assert (t is None) != top and (m is None) != turn
    # The turn sits on the placement node, outside its rotation; the
    # model under it carries none.
    placements.extract(_pl_image(), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path))
    j = json.load(open(tmp_path / "MAP11_placements.gltf"))
    nodes = {n["name"]: n for n in j["nodes"]}
    assert "rotation" not in nodes["enemy_l0_000_cls101_model"]
    s = math.sqrt(0.5)
    want = _qmul(half, [0, s, 0, s]) if turn else [0, s, 0, s]
    assert nodes["enemy_l0_000_cls101"]["rotation"] == pytest.approx(want)
    marker = nodes["enemy_l0_001_cls2"]
    assert marker.get("rotation") == (half if turn else None)


def _mat(axis, a):
    """A right-handed rotation by a about axis (0 X, 1 Y, 2 Z), for
    column vectors."""
    c, s = math.cos(a), math.sin(a)
    i, k = [(1, 2), (2, 0), (0, 1)][axis]
    m = [[float(r == q) for q in range(3)] for r in range(3)]
    m[i][i], m[i][k], m[k][i], m[k][k] = c, -s, s, c
    return m


def _apply(m, v):
    return [sum(m[r][q] * v[q] for q in range(3)) for r in range(3)]


def test_placements_follow_the_games_turn_order(tmp_path):
    """The game draws a placement as T * Rx(pi) * Rz * Ry * Rx (column
    vectors, the record's X angle applied first): the half turn pushed
    between the position and the record's rotation. Every angle non-zero,
    so a mirrored Y or Z, or another order, shows."""
    rx, ry, rz = 0.3, 0.5, 0.7
    d = bytearray(data_section())
    struct.pack_into("<3f", d, 0x80 + 0x14, rx, ry, rz)
    cfg = _pl_cfg("-y", "y")
    placements.extract(
        _pl_image(bytes(d)), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path)
    )
    j = json.load(open(tmp_path / "MAP11_placements.gltf"))
    q = {n["name"]: n for n in j["nodes"]}["enemy_l0_000_cls101"]["rotation"]
    for v in ([1, 0, 0], [0, 1, 0], [0, 0, 1], [0.2, -0.4, 0.9]):
        want = v
        for axis, a in ((0, rx), (1, ry), (2, rz), (0, math.pi)):
            want = _apply(_mat(axis, a), want)
        assert _qrot(q, v) == pytest.approx(want, abs=1e-5)


def test_placements_call_weighted_models_skinned():
    r = ninja.Reader(_pl_image(), ninja.Format(16, "bams", True))
    r.tree(SEC)
    assert not placements.skinned(r)
    r.stats["vertex chunk VN_NF"] += 1
    assert placements.skinned(r)
    r = ninja.Reader(_pl_image(), ninja.Format(16, "bams", True))
    r.unsupported["cached polygon list"] += 1
    assert placements.skinned(r)


def test_placements_skip_a_list_too_long():
    d = bytearray(data_section())
    struct.pack_into("<I", d, 0x40, placements.MAX_RECORDS + 1)
    ps, errors = placements.read(_pl_image(bytes(d)), _pl_cfg()["placement_set"], 1)
    assert ps == [] and len(errors) == 1
    assert errors[0][:2] == ("enemy", 0) and "not a placement list" in errors[0][2]


def test_placements_carry_on_past_a_bad_layer(tmp_path):
    """A table entry that points nowhere is its layer's error; the other
    layer is still exported."""
    d = bytearray(data_section())
    struct.pack_into("<I", d, 4, 0x7FFF0000)  # stage 1, layer 0
    cfg = _pl_cfg(extra=PLACEMENTS_L1)
    cfg["placement_set"][0]["layers"] = [0, 1]
    rep = placements.extract(
        _pl_image(bytes(d)), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path)
    )
    c = rep["sets"]["enemy"]
    assert c["records"] == 3 and len(c["errors"]) == 1
    assert c["errors"][0].startswith("layer 0: ")
    assert json.load(open(tmp_path / "placements.json"))["sets"] == rep["sets"]


def test_placements_stage_layers_replace_a_sets_own(tmp_path):
    """placement_layers reads other entries on that stage alone: a
    negative offset reaches a lower stage's list, [] reads nothing."""
    cfg = _pl_cfg(extra=PLACEMENTS_L1)
    sets = cfg["placement_set"]
    stage = dict(cfg["stage"][0], placements=2, placement_layers={"enemy": [-1, 0]})
    assert placements.stage_layers(sets, stage) == {"enemy": [-1, 0]}
    assert placements.stage_layers(sets, cfg["stage"][0]) == {"enemy": [0]}
    rep = placements.extract(_pl_image(), cli.fmt_of(cfg), cfg, stage, None, str(tmp_path))
    # Stage 2 - 1 is stage 1's list; stage 2 + 0 is its layer-1 list.
    assert rep["sets"]["enemy"]["records"] == 5 and rep["layers"] == {"enemy": [-1, 0]}
    names = {r["name"] for r in rep["placements"]}
    assert "enemy_l-1_000_cls101" in names and "enemy_l0_000_cls103" in names
    assert json.load(open(tmp_path / "placements.json"))["layers"] == {"enemy": [-1, 0]}
    stage["placement_layers"] = {"enemy": []}
    rep = placements.extract(_pl_image(), cli.fmt_of(cfg), cfg, stage, None, str(tmp_path))
    assert rep["sets"] == {} and rep["layers"] == {"enemy": []}


def test_placements_layers_flagged_tables_fixed_objects_and_bad_trees(tmp_path):
    cfg = _pl_cfg(extra=PLACEMENTS_L1)
    cfg["placement_set"][0]["layers"] = [0, 1]
    rep = placements.extract(
        _pl_image(), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path)
    )
    assert rep["sets"]["enemy"] == {
        "records": 5,
        "drawn": 3,
        "skinned": 0,
        "unreadable": 1,
        "marker": 1,
    }
    rows = {r["name"]: r for r in rep["placements"]}
    # The flags byte picks table_flagged: the plain table names nothing.
    flagged = rows["enemy_l1_000_cls103"]
    assert (flagged["layer"], flagged["flags"], flagged["model"]) == (1, 1, "0x%08X" % SEC)
    assert rows["enemy_l1_001_cls4"]["model"] == "0x%08X" % SEC
    bad = rows["enemy_l1_002_cls5"]
    assert bad["unreadable"] is True and bad["model"] == "0x00030200"
    j = json.load(open(tmp_path / "MAP11_placements.gltf"))
    nodes = {n["name"]: n for n in j["nodes"]}
    assert nodes["enemy_l1_000_cls103"]["translation"] == [7, 8, 9]
    assert "enemy_l1_002_cls5_marker" in nodes and "enemy_l1_002_cls5_model" not in nodes


@pytest.mark.parametrize("at, value", [(0x08, float("nan")), (0x1C, float("inf"))])
def test_placements_without_a_finite_transform_are_unreadable(tmp_path, at, value):
    d = bytearray(data_section())
    struct.pack_into("<f", d, 0x80 + at, value)
    cfg = _pl_cfg()
    rep = placements.extract(
        _pl_image(bytes(d)), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path)
    )
    a = rep["placements"][0]
    assert a["unreadable"] is True and "model" in a
    assert a["raw"] == bytes(d[0x80:0xA0]).hex()
    assert None in a["position"] + a["rotation_radians"]
    assert rep["sets"]["enemy"]["unreadable"] == 1
    # Strict JSON both: no bare NaN or Infinity.
    for f in ("placements.json", "MAP11_placements.gltf"):
        json.loads(open(tmp_path / f).read(), parse_constant=pytest.fail)
    j = json.load(open(tmp_path / "MAP11_placements.gltf"))
    n = {n["name"]: n for n in j["nodes"]}["enemy_l0_000_cls101"]
    assert "translation" not in n and "rotation" not in n
    assert j["nodes"][n["children"][0]]["name"] == "enemy_l0_000_cls101_marker"


def test_placements_variant_past_the_table_is_a_marker(tmp_path):
    cfg = _pl_cfg(extra="")
    cfg["placement_model"][0]["variants"] = 1
    rep = placements.extract(
        _pl_image(), cli.fmt_of(cfg), cfg, cfg["stage"][0], None, str(tmp_path)
    )
    a = rep["placements"][0]
    assert a["variant"] == 1 and a["variant_out_of_range"] is True and "model" not in a
    assert rep["sets"]["enemy"]["marker"] == 2


def test_godot_scene_adds_the_placements_layer(monkeypatch):
    assert godot.scene("S") == godot.SCENE.format(name="S")
    assert godot.scene("S", True) == godot.SCENE_COLLISION.format(name="S")
    # The id and step count follow the template, not its line count.
    extra = '[ext_resource type="Script" path="res://x.gd" id="9"]\n'
    monkeypatch.setattr(
        godot,
        "SCENE",
        godot.SCENE.replace("load_steps=4", "load_steps=5").replace(
            "\n[node ", extra + "\n[node ", 1
        ),
    )
    t = godot.scene("S", False, True)
    assert "load_steps=6 " in t and 'S_placements.gltf" id="10"]' in t
    monkeypatch.undo()
    for col, n in ((False, 4), (True, 6)):
        t = godot.scene("S", col, True)
        assert t.startswith("[gd_scene load_steps=%d " % (n + 1))
        assert 'path="res://stages/S/S_placements.gltf" id="%d"]' % n in t
        assert t.endswith('[node name="placements" parent="." instance=ExtResource("%d")]\n' % n)


@pytest.fixture
def pl_game(game):
    write(
        game.xbe,
        xbe([("MAP11", SEC, stage_section("b1")), ("DATA", DATA, data_section())]),
    )
    toml = os.path.join(game.root, "game.toml")
    with open(toml, "a") as f:
        f.write("placements = 1\n" + PLACEMENTS)
    G = manifest.load(game.root)
    manifest.use(G)
    return G


def test_extract_placements_is_opt_in(pl_game, capsys):
    root = pl_game.path("dist", "extract")
    sdir = os.path.join(root, "stages", "MAP11")
    assert cli.run(Args()) == 0
    assert not os.path.exists(os.path.join(sdir, "MAP11_placements.gltf"))
    assert cli.run(Args(placements=True)) == 0
    out = capsys.readouterr().out
    assert "MAP11 placements: enemy 2 (1 drawn, 0 skinned, 0 unreadable, 1 markers)" in out
    for rel in ("MAP11_placements.gltf", "MAP11_placements.bin", "placements.json"):
        assert os.path.isfile(os.path.join(sdir, rel)), rel
    assert open(os.path.join(root, "MAP11.tscn")).read() == godot.scene("MAP11", False, True)
    assert cli.run(Args()) == 0
    assert open(os.path.join(root, "MAP11.tscn")).read() == godot.SCENE.format(name="MAP11")
    assert cli.run(Args(list=True)) == 0
    assert "; placements 1 (enemy [0])" in capsys.readouterr().out


def test_extract_list_shows_a_stages_placement_layers(game, capsys):
    """--list prints the layers the stage reads: its own placement_layers
    over each set's, [] for a set it does not load."""
    second = PLACEMENTS.split("[[extract.placement_model]]")[0].replace("enemy", "emitter")
    with open(os.path.join(game.root, "game.toml"), "a") as f:
        f.write(
            "placements = 2\nplacement_layers = {enemy = [-2], emitter = []}\n"
            + PLACEMENTS
            + second
        )
    manifest.use(manifest.load(game.root))
    assert cli.run(Args(list=True)) == 0
    assert "; placements 2 (enemy [-2], emitter [])" in capsys.readouterr().out


def _tree_shas(d, skip=()):
    out = {}
    for base, _, files in os.walk(d):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), d)
            if f not in skip:
                out[rel] = hashlib.sha256(open(os.path.join(base, f), "rb").read()).hexdigest()
    return out


def test_extract_placements_leave_the_stage_as_it_was(pl_game):
    sdir = pl_game.path("dist", "extract", "stages", "MAP11")
    assert cli.run(Args()) == 0
    before = _tree_shas(sdir)
    assert cli.run(Args(placements=True)) == 0
    skip = ("MAP11_placements.gltf", "MAP11_placements.bin", "placements.json")
    assert _tree_shas(sdir, skip) == before


def test_extract_placements_warn_on_a_bad_layer(pl_game, capsys):
    d = bytearray(data_section())
    struct.pack_into("<I", d, 4, 0x7FFF0000)
    write(pl_game.xbe, xbe([("MAP11", SEC, stage_section("b1")), ("DATA", DATA, bytes(d))]))
    assert cli.run(Args(placements=True)) == 0
    out = capsys.readouterr().out
    assert "MAP11 placements: enemy 0 (" in out
    assert "warning: enemy layer 0: " in out and ", skipped" in out


@pytest.mark.parametrize(
    "edit, why",
    [
        (("stride = 0x20", "stride = 0x10"), r"placement_set\[0\]\.rot_at: 12 bytes inside"),
        (('set = "enemy"', 'set = "item"'), r"placement_model\[0\]\.set: no \[\[extract"),
        (("table = 0x30100", "table = 0x30100\nobject = 1"), "a table or an object"),
        (("class = 1", "class = 0x101"), r"class: the class byte"),
        (('name = "enemy"', 'name = "enemy"\ncolor = 1'), r"placement_set\[0\]\.color: unknown"),
        (("pos_at = 4\n", ""), r"placement_set\[0\]\.pos_at: required"),
        (("rot_at = 0x14\n", ""), r"placement_set\[0\]\.rot_at: required"),
        (("stride = 0x20", "stride = 0x20\nlayers = [0, 1, 0]"), r"layers: an offset twice"),
        (('name = "enemy"', 'name = "enemy-col"'), r"name: 'enemy-col' ends in a Godot import"),
        (('name = "enemy"', 'name = "enemy_Rigid"'), r"ends in a Godot import hint"),
        (("stride = 8", "stride = 8\ntexlist_at = 6"), r"texlist_at: 4 bytes inside the stride"),
        (("stride = 8", "stride = 8\nobject_at = 8"), r"object_at: 4 bytes inside the stride"),
        (("table = 0x30100", "table = -1"), r"placement_model\[0\]\.table: a VA, 0 or more"),
        (("stride = 8", "stride = 8\ntable_flagged = -4"), r"table_flagged: a VA, 0 or more"),
        (("stride = 8", "stride = 8\nvariants = 0"), r"variants: must be positive"),
        (
            ("table = 0x30100\nstride = 8", "object = -1\ntexlist = 0"),
            r"placement_model\[0\]\.object: a VA, 0 or more",
        ),
        (
            ("table = 0x30100\nstride = 8", "object = 1\ntexlist = -1"),
            r"placement_model\[0\]\.texlist: a VA, 0 or more",
        ),
        (
            ("table = 0x30100\nstride = 8", "object = 1\ntexlist = 0\ntable_flagged = 2"),
            r"table_flagged: only with table, not object",
        ),
    ],
)
def test_manifest_placement_errors(edit, why):
    assert edit[0] in PLACEMENTS
    bad = GAME_TOML + EXTRACT_B1 + "placements = 1\n" + PLACEMENTS.replace(*edit, 1)
    with pytest.raises(manifest.ManifestError, match=why):
        manifest.parse_text(bad)


@pytest.mark.parametrize(
    "layers, index, why",
    [
        ("{item = [0]}", 1, r"placement_layers\.item: no \[\[extract\.placement_set\]\] named"),
        ("{enemy = [0, 0]}", 1, r"placement_layers\.enemy: an offset twice"),
        ("{enemy = [-2]}", 1, r"placement_layers\.enemy: placements plus each offset"),
        ("{enemy = 0}", 1, r"placement_layers: a table of set name"),
        ("[0]", 1, r"placement_layers: a table of set name"),
        ("{enemy = [0]}", None, r"placement_layers: needs placements"),
    ],
)
def test_manifest_stage_placement_layers_errors(layers, index, why):
    stage = (
        "placements = %d\n" % index if index is not None else ""
    ) + "placement_layers = %s\n" % layers
    with pytest.raises(manifest.ManifestError, match=why):
        manifest.parse_text(GAME_TOML + EXTRACT_B1 + stage + PLACEMENTS)


def test_manifest_stage_placement_layers():
    toml = GAME_TOML + EXTRACT_B1 + "placements = 2\nplacement_layers = {enemy = [-2, 0]}\n"
    st = manifest.parse_text(toml + PLACEMENTS)["extract"]["stage"][0]
    assert st["placement_layers"] == {"enemy": [-2, 0]}
    st = manifest.parse_text(GAME_TOML + EXTRACT_B1 + "placements = 1\n" + PLACEMENTS)
    assert "placement_layers" not in st["extract"]["stage"][0]
    toml = GAME_TOML + EXTRACT_B1 + "placements = 1\nplacement_layers = {enemy = []}\n"
    assert manifest.parse_text(toml + PLACEMENTS)["extract"]["stage"][0]["placement_layers"] == {
        "enemy": []
    }


def test_placements_empty_stage_layers_change_nothing():
    """placement_layers = {} names no set: every set keeps its layers."""
    toml = GAME_TOML + EXTRACT_B1 + "placements = 1\nplacement_layers = {}\n"
    x = manifest.parse_text(toml + PLACEMENTS)["extract"]
    st = x["stage"][0]
    assert st["placement_layers"] == {}
    assert placements.stage_layers(x["placement_set"], st) == {"enemy": [0]}


def test_manifest_placement_defaults_and_a_lone_index():
    x = manifest.parse_text(GAME_TOML + EXTRACT_B1 + "placements = 1\n" + PLACEMENTS)["extract"]
    p = x["placement_set"][0]
    assert (p["layers"], p["class_at"]) == ([0], 0)
    assert "colour" not in p and x["placement_world"] == "y"
    assert "variants" not in x["placement_model"][0]
    with pytest.raises(manifest.ManifestError, match=r"stage\[0\]\.placements: no \[\[extract"):
        manifest.parse_text(GAME_TOML + EXTRACT_B1 + "placements = 1\n")
    with pytest.raises(manifest.ManifestError, match=r"stage\[0\]\.placements: an index of 0"):
        manifest.parse_text(GAME_TOML + EXTRACT_B1 + "placements = -1\n" + PLACEMENTS)
    bad = GAME_TOML + EXTRACT_B1.replace("[extract]\n", '[extract]\nplacement_world = "z"\n')
    with pytest.raises(manifest.ManifestError, match="extract.placement_world"):
        manifest.parse_text(bad)
