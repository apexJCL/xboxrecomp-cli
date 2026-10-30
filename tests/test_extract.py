"""xbr extract on data the tests build in code: a synthetic XBE holding
a two-node Ninja tree in both dialects ([extract] vertex_header,
packed_normals, angles), DDS textures, an IPK1 archive and a fake Godot.
No test reads game files.

  uv run pytest tests/test_extract.py
"""

import json
import math
import os
import stat
import struct
import sys

import pytest

from xboxrecomp_cli import host, manifest
from xboxrecomp_cli.enhance import dds, ipk
from xboxrecomp_cli.extract import cli, gltf, godot, ninja, scan, textures
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
    assert textures.alpha_kind(bytes((0, 0, 0, 7))) == "blend"


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
            return dds_raw(2, 1, 32, ARGB8, [0xFFFF0000, 0x8000FF00])

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
    assert (smp["wrapS"], smp["wrapT"]) == (gltf.CLAMP, gltf.REPEAT)
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
