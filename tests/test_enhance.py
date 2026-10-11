"""xbr enhance hud and its parts, on fixtures the tests build themselves:
IPK1 archives (with an LZSS encoder of the test's own), DXT textures, a
TrueType font of rectangles, and packs. No test reads game files.

  uv run pytest tests/test_enhance.py
"""

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import A, fake_tree, raises, write  # noqa: E402

from xboxrecomp_cli import host, manifest  # noqa: E402
from xboxrecomp_cli import package as pkg  # noqa: E402
from xboxrecomp_cli.enhance import (  # noqa: E402
    atlas,
    bc3,
    dds,
    font,
    hud,
    ipk,
    pack,
    scaler,
    texhash,
    xpr,
)

# ---- fixtures ---------------------------------------------------------------


def lzss_encode(src):
    """Okumura LZSS the way ipk.lzss_decode reads it: greedy longest match
    in the last 512 ring positions, never reading what the copy itself
    writes."""
    ring = bytearray(ipk.RING)
    r = ipk.RING - ipk.MATCH_MAX
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        flags = 0
        items = bytearray()
        for bit in range(8):
            if i >= n:
                break
            best = (0, 0)
            for back in range(1, 513):
                pos = (r - back) & (ipk.RING - 1)
                length = 0
                while (
                    length < ipk.MATCH_MAX
                    and i + length < n
                    and ((pos + length - r) & (ipk.RING - 1)) >= ipk.MATCH_MAX
                    and ring[(pos + length) & (ipk.RING - 1)] == src[i + length]
                ):
                    length += 1
                if length > best[0]:
                    best = (length, pos)
            length, pos = best
            if length > ipk.THRESHOLD:
                items += bytes([pos & 0xFF, ((pos >> 4) & 0xF0) | (length - ipk.THRESHOLD - 1)])
                for k in range(length):
                    ring[r] = src[i + k]
                    r = (r + 1) & (ipk.RING - 1)
                i += length
            else:
                flags |= 1 << bit
                items.append(src[i])
                ring[r] = src[i]
                r = (r + 1) & (ipk.RING - 1)
                i += 1
        out.append(flags)
        out += items
    return bytes(out)


def write_archive(directory, name, entries):
    """entries: [(name, data, compressed)] -> <dir>/<name>.iph + .ipk."""
    body = bytearray()
    recs = bytearray()
    for n, data, comp in entries:
        stored = lzss_encode(data) if comp else data
        recs += ipk.ENTRY.pack(
            n.encode("latin-1"), ipk.FLAG_LZSS if comp else 0, len(stored), len(body), len(data)
        )
        body += stored + b"\0" * (-len(stored) % 16)
    with open(os.path.join(directory, name + ".iph"), "wb") as f:
        f.write(ipk.HEADER.pack(ipk.MAGIC, 16, len(entries), len(body)) + recs)
    with open(os.path.join(directory, name + ".ipk"), "wb") as f:
        f.write(body)


def dxt5_blocks(w, h, ink):
    """DXT5 blocks of a white texture whose alpha is 255 where ink(x, y)
    and 0 elsewhere."""
    out = bytearray()
    for by in range(h // 4):
        for bx in range(w // 4):
            bits = 0
            for k in range(16):
                if not ink(bx * 4 + (k & 3), by * 4 + (k >> 2)):
                    bits |= 1 << (3 * k)
            out += bytes([255, 0]) + bits.to_bytes(6, "little")
            out += struct.pack("<HHI", 0xFFFF, 0xFFFF, 0)
    return bytes(out)


def dxt1_blocks(w, h, idx=0):
    """DXT1 blocks: c0 red, c1 blue (four-colour mode), every texel idx."""
    n = ((w + 3) // 4) * ((h + 3) // 4)
    sel = sum(idx << (2 * k) for k in range(16))
    return struct.pack("<HHI", 0xF800, 0x001F, sel) * n


def dds_file(w, h, fourcc, data, mips=0):
    hdr = bytearray(dds.HEADER)
    hdr[0:4] = dds.MAGIC
    flags = 0x1007 | (dds.DDSD_MIPMAPCOUNT if mips else 0)
    struct.pack_into("<IIII", hdr, 4, 124, flags, h, w)
    struct.pack_into("<I", hdr, 28, mips)
    struct.pack_into("<II4s", hdr, 76, 32, dds.DDPF_FOURCC, fourcc)
    return bytes(hdr) + data


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]


# The test font: rectangles, so every ink box is exact. H is the cap
# height reference (700 units); i has a dot; " is two marks.
GLYPHS = {
    "H": [rect(0, 0, 600, 700)],
    "I": [rect(0, 0, 200, 700)],
    "i": [rect(0, 0, 200, 500), rect(0, 600, 200, 700)],
    "o": [rect(0, 0, 500, 500)],
    '"': [rect(0, 500, 150, 700), rect(250, 500, 400, 700)],
}


def name_table(full):
    """A name table with the full name (ID 4) as a Windows UTF-16 record."""
    raw = full.encode("utf-16-be")
    return struct.pack(">HHH", 0, 1, 18) + struct.pack(">6H", 3, 1, 0x409, 4, len(raw), 0) + raw


def ttf(glyphs=GLYPHS, upem=1000, name=None):
    """A TrueType font of all-on-curve contours: glyph 0 empty, then one
    per character in code point order; cmap format 4, long loca; a name
    table when name is given."""
    chars = sorted(glyphs)
    outlines = [[]] + [glyphs[c] for c in chars]
    glyf = bytearray()
    loca = [0]
    for contours in outlines:
        if contours:
            pts = [p for c in contours for p in c]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            g = struct.pack(">hhhhh", len(contours), min(xs), min(ys), max(xs), max(ys))
            ends, end = [], -1
            for c in contours:
                end += len(c)
                ends.append(end)
            g += struct.pack(">%dH" % len(ends), *ends) + struct.pack(">H", 0)
            g += bytes([1] * len(pts))  # on-curve, int16 deltas
            px = py = 0
            dx, dy = b"", b""
            for x, y in pts:
                dx += struct.pack(">h", x - px)
                dy += struct.pack(">h", y - py)
                px, py = x, y
            g += dx + dy + b"\0" * (-len(dx + dy + g) % 4)
            glyf += g
        loca.append(len(glyf))
    n = len(outlines)
    head = bytearray(54)
    struct.pack_into(">H", head, 18, upem)
    struct.pack_into(">h", head, 50, 1)
    maxp = struct.pack(">IH", 0x00005000, n)
    hhea = bytearray(36)
    struct.pack_into(">hh", hhea, 4, 800, -200)
    struct.pack_into(">H", hhea, 34, n)
    hmtx = b"".join(struct.pack(">Hh", 600, 0) for _ in range(n))
    segs = [(ord(c), (i + 1 - ord(c)) & 0xFFFF) for i, c in enumerate(chars)] + [(0xFFFF, 1)]
    sc = len(segs)
    sub = struct.pack(">HHHHHHH", 4, 16 + 8 * sc, 0, sc * 2, 0, 0, 0)
    sub += struct.pack(">%dH" % sc, *[c for c, _ in segs]) + b"\0\0"
    sub += struct.pack(">%dH" % sc, *[c for c, _ in segs])
    sub += struct.pack(">%dH" % sc, *[d for _, d in segs])
    sub += struct.pack(">%dH" % sc, *([0] * sc))
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    tables = [
        (b"cmap", cmap),
        (b"glyf", bytes(glyf)),
        (b"head", bytes(head)),
        (b"hhea", bytes(hhea)),
        (b"hmtx", hmtx),
        (b"loca", struct.pack(">%dI" % len(loca), *loca)),
        (b"maxp", maxp),
    ]
    if name is not None:
        tables.append((b"name", name_table(name)))
    off = 12 + 16 * len(tables)
    directory, body = b"", b""
    for tag, data in tables:
        directory += struct.pack(">4sIII", tag, 0, off + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0) + directory + body


def ttc(faces):
    """A collection of the given single fonts: each face's directory moves
    to its place in the file and its table offsets with it (a collection's
    offsets are from the start of the file)."""
    head = 12 + 4 * len(faces)
    out, offsets = bytearray(), []
    for f in faces:
        base = head + len(out)
        offsets.append(base)
        g = bytearray(f)
        for i in range(struct.unpack_from(">H", g, 4)[0]):
            off = struct.unpack_from(">I", g, 12 + 16 * i + 8)[0]
            struct.pack_into(">I", g, 12 + 16 * i + 8, off + base)
        out += g
    return (
        struct.pack(">4sHHI", b"ttcf", 1, 0, len(faces))
        + struct.pack(">%dI" % len(faces), *offsets)
        + bytes(out)
    )


# The synthetic stock atlas, 32x32: two bands of rectangles. Boxes are
# (left, top, right, bottom), right and bottom exclusive, in stock pixels.
ATLAS_W = ATLAS_H = 32
STOCK = {
    "H": [(2, 2, 8, 11)],
    "I": [(10, 2, 12, 11)],
    '"': [(14, 2, 15, 5), (17, 2, 18, 5)],
    "i": [(2, 20, 4, 28), (2, 16, 4, 18)],
    "o": [(6, 20, 12, 28)],
    "z": [(14, 18, 20, 28)],  # not in the test font
    atlas.KEEP: [(22, 18, 28, 28)],
}
ROWS = ['HI"', "ioz" + atlas.KEEP]


def stock_ink(x, y):
    return any(
        x0 <= x < x1 and y0 <= y < y1 for boxes in STOCK.values() for x0, y0, x1, y1 in boxes
    )


def stock_box(char):
    boxes = STOCK[char]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def stock_atlas():
    return dds.decode(
        dds.parse(dds_file(ATLAS_W, ATLAS_H, b"DXT5", dxt5_blocks(ATLAS_W, ATLAS_H, stock_ink)))
    )


# ---- ipk --------------------------------------------------------------------


def test_lzss_round_trip():
    data = (b"abcabcabcabcXYZ" * 20 + bytes(range(256)) + b"\0" * 100) * 2
    enc = lzss_encode(data)
    assert len(enc) < len(data)
    assert ipk.lzss_decode(enc, len(data)) == data
    with pytest.raises(ipk.ArchiveError, match="ended after"):
        ipk.lzss_decode(enc[: len(enc) // 2], len(data))


def test_archive_read(d):
    a = b"stored entry " * 10
    b = b"compressed entry " * 40
    write_archive(d, "media", [("DDS\\A.dds", a, False), ("dds\\B.DDS", b, True)])
    arc = ipk.Archive(d, "media")
    assert [e.path for e in arc.entries] == ["dds/a.dds", "dds/b.dds"]
    assert [e.compressed for e in arc.entries] == [False, True]
    assert arc.read(arc.entries[0]) == a
    assert arc.read(arc.entries[1]) == b
    assert arc.entries[1].stored < len(b)


def test_index_errors():
    with pytest.raises(ipk.ArchiveError, match="not an IPK1"):
        ipk.read_index(b"IPK2" + b"\0" * 12)
    with pytest.raises(ipk.ArchiveError, match="truncated"):
        ipk.read_index(ipk.HEADER.pack(ipk.MAGIC, 16, 3, 0) + b"\0" * 80)


# ---- dds --------------------------------------------------------------------


def test_dds_parse_and_extent():
    assert dds.extent(b"DXT1", 16, 8, 1) == 4 * 2 * 8
    assert dds.extent(b"DXT5", 16, 16, 3) == 16 * 16 + 16 * 4 + 16
    t = dds.parse(dds_file(16, 8, b"DXT1", dxt1_blocks(16, 8) + b"tail"))
    assert (t.width, t.height, t.levels, t.fourcc, len(t.data)) == (16, 8, 1, b"DXT1", 64)
    t = dds.parse(dds_file(16, 16, b"DXT5", b"\0" * dds.extent(b"DXT5", 16, 16, 3), mips=3))
    assert t.levels == 3 and len(t.data) == dds.extent(b"DXT5", 16, 16, 3)
    with pytest.raises(dds.DdsError, match="truncated"):
        dds.parse(dds_file(16, 8, b"DXT1", b"\0" * 10))
    with pytest.raises(dds.DdsError, match="not a DDS"):
        dds.parse(b"PNG" + b"\0" * 200)
    raw = dds.parse(
        dds_file(4, 4, b"\0\0\0\0", b"\1" * 64).replace(b"\x04\0\0\0", b"\x40\0\0\0", 1)
    )
    assert not raw.dxt


def test_dxt_decode():
    w, h, rgba = dds.decode(dds.parse(dds_file(8, 4, b"DXT1", dxt1_blocks(8, 4, 1))))
    assert (w, h) == (8, 4) and rgba[:4] == bytes((0, 0, 255, 255))
    w, h, rgba = stock_atlas()
    assert rgba[(2 * ATLAS_W + 2) * 4 : (2 * ATLAS_W + 2) * 4 + 4] == bytes((255, 255, 255, 255))
    assert rgba[3] == 0
    assert atlas.ink_box(w, rgba, 0, 9, 0, 14) == (2, 2, 8, 11)


# ---- hash, scaler, pack -----------------------------------------------------


def test_tex_hash_vectors():
    """The vectors the toolkit's tests/tex_pack checks."""
    assert texhash.tex_hash(b"") == 0x40500CA3D9E617F3
    assert texhash.tex_hash(b"\0" * 64) == 0x69597731194007AB
    assert texhash.tex_hash(bytes(range(256)) * 4 + b"xboxrecomp") == 0x0B060D91FB09F186


def test_scaler():
    flat = bytes((10, 20, 30, 255)) * 16
    up = scaler.scale(4, 4, flat, 4)
    assert len(up) == 16 * 16 * 4 and set(up[i : i + 4] for i in range(0, len(up), 4)) == {flat[:4]}
    # A diagonal edge: xBR smooths it, nearest does not. (A checkerboard
    # is no edge to xBR, which keeps it as it is.)
    diag = b"".join(
        bytes((255, 255, 255, 255)) if x > y else bytes((0, 0, 0, 255))
        for y in range(4)
        for x in range(4)
    )
    a = scaler.scale(4, 4, diag, 2)
    assert a == scaler.scale(4, 4, diag, 2) and len(a) == 8 * 8 * 4
    assert a != scaler.scale_nearest(4, 4, diag, 2)
    assert scaler.scale_nearest(2, 1, bytes((1, 2, 3, 4, 5, 6, 7, 8)), 2) == bytes(
        (1, 2, 3, 4, 1, 2, 3, 4, 5, 6, 7, 8, 5, 6, 7, 8) * 2
    )


def test_pack_write_and_read(d):
    p = os.path.join(d, "hud.pack")
    e1 = pack.PackEntry(0x20, 2, 1, pack.rgba_to_argb(bytes((1, 2, 3, 4, 5, 6, 7, 8))), name="a")
    e2 = pack.PackEntry(0x10, 1, 1, b"\0\0\0\xff", name="b", source="xbr", guest=(1, 1))
    sha = pack.write(p, [e1, e2], 4)
    assert len(sha) == 64
    assert pack.read_header(p) == (2, 4)
    with open(p, "rb") as f:
        data = f.read()
    hashes = [pack.ENTRY.unpack_from(data, pack.HEADER.size + 32 * i)[0] for i in range(2)]
    assert hashes == [0x10, 0x20]
    off = pack.ENTRY.unpack_from(data, pack.HEADER.size + 32)[5]
    assert data[off : off + 8] == bytes((3, 2, 1, 4, 7, 6, 5, 8))
    with pytest.raises(ValueError, match="two entries"):
        pack.write(p, [e1, e1], 4)
    write(os.path.join(d, "bad.pack"), "not a pack at all, really not, and long enough to say so")
    with pytest.raises(ValueError, match="bad magic"):
        pack.read_header(os.path.join(d, "bad.pack"))
    text = pack.manifest_text([e1, e2], 4, "k", "t", "x" * 64, {"name": "f.ttf", "sha256": "s"})
    assert 'font = "f.ttf"' in text and text.count("[[entry]]") == 2
    assert text.index('hash = "0000000000000010"') < text.index('hash = "0000000000000020"')


def test_pack_v2_kinds_and_bc3(d):
    """v2: the kind in flag bits 0-1, FLAG_BC3, the text scale in the
    header; a BC3 entry must be whole blocks; v1 still reads."""
    p = os.path.join(d, "hud.pack")
    art = pack.PackEntry(0x30, 1, 1, b"\0\0\0\xff", name="art")
    fnt = pack.PackEntry(0x20, 1, 1, b"\0\0\0\xff", name="font", kind=pack.KIND_FONT)
    txt = pack.PackEntry(
        0x10, 4, 4, bytes(16), name="text", source="xbr", kind=pack.KIND_TEXT, bc3=True
    )
    pack.write(p, [art, fnt, txt], 4, 2)
    with open(p, "rb") as f:
        data = f.read()
    magic, version, count, scale, index_off, text_scale = pack.HEADER.unpack_from(data)
    assert (version, count, scale, text_scale) == (2, 3, 4, 2)
    flags = [pack.ENTRY.unpack_from(data, index_off + 32 * i)[4] for i in range(3)]
    assert flags == [pack.KIND_TEXT | pack.FLAG_BC3, pack.KIND_FONT, pack.KIND_ART]
    assert len(data) == index_off + 3 * 32 + 16 + 4 + 4
    assert pack.read_header(p) == (3, 4)
    odd = pack.PackEntry(1, 6, 4, bytes(32), kind=pack.KIND_TEXT, bc3=True, name="odd")
    with pytest.raises(ValueError, match="BC3 entry odd is 6x4"):
        pack.write(p, [odd], 4)
    v1 = bytearray(data)
    struct.pack_into("<I", v1, 4, 1)
    write(os.path.join(d, "v1.pack"), "")
    with open(os.path.join(d, "v1.pack"), "wb") as f:
        f.write(v1)
    assert pack.read_header(os.path.join(d, "v1.pack")) == (3, 4)
    struct.pack_into("<I", v1, 4, 3)
    with open(os.path.join(d, "v3.pack"), "wb") as f:
        f.write(v1)
    with pytest.raises(ValueError, match="version 3"):
        pack.read_header(os.path.join(d, "v3.pack"))
    text = pack.manifest_text([art, fnt, txt], 4, "k", "t", "x" * 64, None, 2, ["us"])
    assert 'languages = ["us"]' in text
    assert "[kinds.text]\nentries = 1\nscale = 2\nbc3 = 1\nbytes = 16\n" in text
    assert "[kinds.art]\nentries = 1\nscale = 4\nbc3 = 0\nbytes = 4\n" in text
    assert 'kind = "text"\nbc3 = true' in text
    assert pack.kind_stats([art, fnt, txt]) == {
        "art": (1, 0, 4),
        "font": (1, 0, 4),
        "text": (1, 1, 16),
    }


# The block vector the toolkit's tests/tex_pack decodes too: RGBA texels in
# row order, the encoder's 16 bytes, and what the DXT5 decoder draws.
BC3_P = bytes.fromhex(
    "0aff00002ff2031f54e50c2f79d81b3f9ecb304fc3be4b00e8b16c6f0da4937f"
    "3297c08f578af39f7c7d2cafa1706bbfc663b0cfeb56fbdf10494cef353ca3ff"
)
BC3_B = bytes.fromhex("ff00f9ed94e42601ffe98017d5efbbb2")
BC3_D = bytes.fromhex(
    "10f3000010f3002410f300245ab655485ab655485ab65500a479aa6d5ab65591"
    "5ab65591a479aa915ab655b6a479aab6a479aadaef3cffda5ab655ffa479aaff"
)


def test_bc3_vector():
    assert bc3.encode(4, 4, BC3_P) == BC3_B
    assert dds.decode_level(b"DXT5", 4, 4, BC3_B) == BC3_D
    # Flat blocks: equal endpoints, every index 0.
    flat = bytes((200, 100, 50, 255)) * 16
    blk = bc3.encode(4, 4, flat)
    assert blk[0:2] == bytes((255, 255)) and blk[2:8] == bytes(6) and blk[12:] == bytes(4)
    assert dds.decode_level(b"DXT5", 4, 4, blk) == bytes((198, 101, 49, 255)) * 16
    # Four-colour mode always: c0 > c1 when the colours differ.
    ramp = b"".join(bytes((i * 16, 0, 0, 255)) for i in range(16))
    c0, c1 = struct.unpack_from("<HH", bc3.encode(4, 4, ramp), 8)
    assert c0 > c1
    # Anti-aliased white text: alpha within one step of the eight-value table.
    aa = b"".join(bytes((255, 255, 255, (i * 17) & 255)) for i in range(16))
    out = dds.decode_level(b"DXT5", 4, 4, bc3.encode(4, 4, aa))
    assert max(abs(a - b) for a, b in zip(aa[3::4], out[3::4])) <= 19
    assert out[0::4] == bytes((255,)) * 16
    # Edge blocks repeat the last row and column; the size is whole blocks.
    assert bc3.size(6, 2) == 32 and len(bc3.encode(6, 2, bytes(6 * 2 * 4))) == 32
    with pytest.raises(ValueError, match="needs 64 RGBA bytes"):
        bc3.encode(4, 4, bytes(8))


# ---- font and atlas ---------------------------------------------------------


def test_font_reader_and_rasteriser():
    f = font.Font(ttf())
    assert f.upem == 1000 and f.cap_height() == 700
    assert f.glyph_index(ord("H")) and not f.glyph_index(ord("Z"))
    assert f.bbox(f.glyph_index(ord("i"))) == (0, 0, 200, 700)
    # H at 10 px per em, origin (2, 9): a 6x7 block whose edges are exact.
    cov = f.render(f.glyph_index(ord("H")), 10, 10, 2, 9, 12, 12)
    box = atlas._coverage_box(cov, 12, 12)
    assert box == (2, 2, 8, 9), box
    assert cov[4 * 12 + 4] == 255 and cov[0] == 0
    # Condensed: half as wide, as tall.
    cov = f.render(f.glyph_index(ord("H")), 5, 10, 2, 9, 12, 12)
    assert atlas._coverage_box(cov, 12, 12) == (2, 2, 5, 9)
    with pytest.raises(font.FontError):
        font.Font(b"OTTO" + b"\0" * 100)


def test_font_collection_and_names(d):
    regular = ttf(name="Test Sans")
    narrow = ttf(GLYPHS, 2000, name="Test Sans Narrow Bold")
    assert font.full_name(regular) == "Test Sans" and font.full_name(ttf()) == ""
    data = ttc([regular, narrow])
    offsets = font.face_offsets(data)
    assert len(offsets) == 2 and font.face_offsets(regular) == [0]
    assert [font.full_name(data, o) for o in offsets] == ["Test Sans", "Test Sans Narrow Bold"]
    path = os.path.join(d, "test.ttc")
    with open(path, "wb") as f:
        f.write(data)
    # Picked by full name, case and spacing aside; the outlines are the face's.
    f = font.load(path, "test sans  narrow bold")
    assert f.name == "Test Sans Narrow Bold" and f.upem == 2000
    assert f.bbox(f.glyph_index(ord("i"))) == (0, 0, 200, 700)
    with pytest.raises(font.FontError, match=r"2 faces \(Test Sans, Test Sans Narrow Bold\); name"):
        font.load(path)
    with pytest.raises(font.FontError, match="none of them 'Other'"):
        font.load(path, "Other")
    # A one-font file is taken whatever its name: the user pointed at it.
    single = os.path.join(d, "one.ttf")
    with open(single, "wb") as f:
        f.write(regular)
    assert font.load(single, "Other").name == "Test Sans"
    assert font._file_names(path) == ["Test Sans", "Test Sans Narrow Bold"]


def test_font_find(d):
    a, b = os.path.join(d, "user"), os.path.join(d, "system", "sub")
    os.makedirs(a)
    os.makedirs(b)
    write(os.path.join(a, "broken.ttf"), "not a font")
    write(os.path.join(a, "notes.txt"), "Test Sans Narrow Bold")
    with open(os.path.join(a, "regular.ttf"), "wb") as f:
        f.write(ttf(name="Test Sans"))
    with open(os.path.join(b, "family.TTC"), "wb") as f:
        f.write(ttc([ttf(name="Test Sans"), ttf(name="Test Sans Narrow Bold")]))
    dirs = [a, os.path.join(d, "system"), os.path.join(d, "absent")]
    assert font.find("Test Sans Narrow Bold", dirs) == os.path.join(b, "family.TTC")
    assert font.find("Test Sans", dirs) == os.path.join(a, "regular.ttf")
    assert font.find("Nothing", dirs) is None


def test_group_runs():
    runs = [(2, 8), (10, 12), (14, 15), (17, 18)]
    assert atlas.group_runs(runs, [7.7, 2.6, 5.1]) == [(2, 8), (10, 12), (14, 18)]
    # A glyph with no run is allowed, at the cost of its width.
    assert atlas.group_runs([(2, 8)], [6.0, 2.0]) == [(2, 8), None]
    assert atlas.group_runs([], []) == []
    # A narrow glyph is not swallowed into its neighbour when each has a
    # run: at the width alone, skipping the 1-wide one would cost 1 + 2.
    assert atlas.group_runs([(0, 4), (6, 10)], [1.0, 8.0]) == [(0, 4), (6, 10)]


def test_atlas_fit():
    w, h, rgba = stock_atlas()
    f = font.Font(ttf())
    assert atlas.bands(w, h, rgba) == [(2, 11), (16, 28)]
    glyphs, metrics = atlas.measure(w, h, rgba, ROWS, f)
    assert [g.char for g in glyphs] == ["H", "I", '"', "i", "o", "z", atlas.KEEP]
    assert [g.box for g in glyphs] == [stock_box(g.char) for g in glyphs]
    assert metrics == [(11, 9), (28, 9)]
    fallback = scaler.scale_nearest(w, h, rgba, 4)
    out, n, notes = atlas.render((w, h, rgba), ROWS, f, 4, fallback)
    assert n == 5 and len(notes) == 1 and "'z'" in notes[0]
    ow = w * 4
    for g in glyphs:
        sl, vt, sr, vb = g.slot
        got = atlas.ink_box(ow, out, sl * 4, sr * 4, vt * 4, vb * 4)
        want = tuple(v * 4 for v in g.box)
        if g.char in ("z", atlas.KEEP):
            # Untouched: the xBR pixels of the slot.
            for y in range(vt * 4, vb * 4):
                a, b = (y * ow + sl * 4) * 4, (y * ow + sr * 4) * 4
                assert out[a:b] == fallback[a:b]
        else:
            assert got == want, (g.char, got, want)
    # The i's dot stays a separate mark: a gap row between dot and stem.
    i_box = stock_box("i")
    ys = [
        y
        for y in range(i_box[1] * 4, i_box[3] * 4)
        if out[((y * ow) + i_box[0] * 4) * 4 + 3] >= atlas.INK
    ]
    assert len(ys) < (i_box[3] - i_box[1]) * 4
    assert out[((2 * 4 * ow) + 2 * 4) * 4 : ((2 * 4 * ow) + 2 * 4) * 4 + 4] == bytes(
        (255, 255, 255, 255)
    )


def test_atlas_glyph_without_run():
    """More glyphs than runs in a band: each one left over is named in the
    render's notes, not dropped silently."""
    w, h, rgba = stock_atlas()
    rows = ['HI"HI', ROWS[1]]
    runs = atlas.glyph_runs(w, rgba, *atlas.bands(w, h, rgba)[0])
    glyphs, _ = atlas.measure(w, h, rgba, rows, font.Font(ttf()))
    assert [g.char for g in glyphs][:5] == ["H", "I", '"', "H", "I"]
    assert sum(1 for g in glyphs[:5] if g.slot is None) == 5 - len(runs)
    fallback = scaler.scale_nearest(w, h, rgba, 4)
    _, _, notes = atlas.render((w, h, rgba), rows, font.Font(ttf()), 4, fallback)
    assert sum("no ink run of its own" in x for x in notes) == 5 - len(runs), notes


def test_atlas_rows_mismatch():
    w, h, rgba = stock_atlas()
    with pytest.raises(atlas.AtlasError, match="2 ink bands .rows 2-10, 16-27., font_rows names 3"):
        atlas.measure(w, h, rgba, ROWS + ["x"], font.Font(ttf()))


# ---- the command ------------------------------------------------------------

HUD_TOML = """
[enhance.hud]
archives = ["media"]
textures = ["dds/ui_*", "dds/font_*"]
scale = 2
max_texels = 1024
font_atlas = "dds/font_test.dds"
font_rows = ["HI\\"", "ioz\\uFFFD"]
"""


def hud_game(d):
    """The fake game with a media archive: an 8x4 DXT1 UI texture (stored),
    the 32x32 atlas (LZSS), a 64x64 one over max_texels, one that is no
    DDS, and one outside the patterns."""
    G = host.g()
    G.m["enhance"] = manifest.parse_text(open(os.path.join(G.root, "game.toml")).read() + HUD_TOML)[
        "enhance"
    ]
    atlas_dds = dds_file(ATLAS_W, ATLAS_H, b"DXT5", dxt5_blocks(ATLAS_W, ATLAS_H, stock_ink))
    write_archive(
        G.game_files,
        "media",
        [
            ("dds\\ui_bar.dds", dds_file(8, 4, b"DXT1", dxt1_blocks(8, 4)), False),
            ("dds\\font_test.dds", atlas_dds, True),
            ("dds\\ui_big.dds", dds_file(64, 64, b"DXT1", dxt1_blocks(64, 64)), False),
            ("dds\\other.dds", dds_file(4, 4, b"DXT1", dxt1_blocks(4, 4)), False),
            ("dds\\ui_notes.dds", b"hello, not a texture", False),
        ],
    )
    return G


def hud_args(**kw):
    a = A(scale=None, out=None, font=None, jobs=1, only=[], list=False, language=[])
    a.__dict__.update(kw)
    return a


def test_hud_list_and_pack(d, capsys):
    with fake_tree(d):
        G = hud_game(d)
        assert hud.run(hud_args(list=True)) == 0
        out = capsys.readouterr().out
        assert "dds/ui_bar.dds" in out and "DXT1 8x4" in out
        assert "dds/ui_big.dds" in out and "over max_texels" in out
        assert "dds/ui_notes.dds" in out and "skipped: not a DDS" in out
        assert "dds/other.dds" not in out
        assert "4 entries, 2 usable" in out
        font_path = os.path.join(d, "test.ttf")
        with open(font_path, "wb") as f:
            f.write(ttf())
        assert hud.run(hud_args(font=font_path)) == 0
        out = capsys.readouterr().out
        assert "5 glyphs rendered" in out and "'z': not in the font" in out
        p = os.path.join(G.dist, "enhance", "hud", "hud.pack")
        assert pack.read_header(p) == (2, 2)
        with open(os.path.join(G.dist, "enhance", "hud", "hud-pack.toml")) as f:
            man = f.read()
        assert 'font = "test.ttf"' in man and "font (5 glyphs)" in man
        assert 'name = "media:dds/ui_bar.dds"' in man and "guest = [8, 4]" in man
        assert "size = [16, 8]" in man
        # The same bytes again, from one process or several, into another dir.
        hud.run(hud_args(font=font_path, out=os.path.join(d, "again"), jobs=2))
        assert open(p, "rb").read() == open(os.path.join(d, "again", "hud.pack"), "rb").read()
        capsys.readouterr()
        # No font: the hint, and the atlas goes through xBR.
        hud.run(hud_args(out=os.path.join(d, "nofont")))
        out = capsys.readouterr().out
        assert "no font" in out and "glyphs rendered" not in out
        raises(hud.run, hud_args(font=os.path.join(d, "missing.ttf")), match="no such file")
        # game.toml's font, found in the host's font folders, by its name.
        fonts = os.path.join(d, "fonts")
        os.makedirs(fonts)
        with open(os.path.join(fonts, "family.ttc"), "wb") as f:
            f.write(ttc([ttf(name="Test Sans"), ttf(name="Test Narrow")]))
        G.m["enhance"]["hud"]["font"] = "Test Narrow"
        old = font.font_dirs
        font.font_dirs = lambda: [fonts]
        try:
            assert hud.run(hud_args(out=os.path.join(d, "byname"))) == 0
            out = capsys.readouterr().out
            assert "font: Test Narrow, " in out and "5 glyphs rendered from Test Narrow" in out
            man = open(os.path.join(d, "byname", "hud-pack.toml")).read()
            assert 'font = "family.ttc"' in man and 'font_face = "Test Narrow"' in man
            # The same glyphs as the one-font file: the face is the same font.
            assert open(os.path.join(d, "byname", "hud.pack"), "rb").read() == open(p, "rb").read()
            # --font as a name; a name nothing has; game.toml's font missing.
            assert hud.run(hud_args(font="test sans", out=os.path.join(d, "n2"))) == 0
            raises(hud.run, hud_args(font="Nothing Sans"), match="no installed font")
            G.m["enhance"]["hud"]["font"] = "Absent Sans"
            hud.run(hud_args(out=os.path.join(d, "absent")))
            assert "install Absent Sans" in capsys.readouterr().out
        finally:
            font.font_dirs = old
        raises(hud.run, hud_args(only=["nothing*"]), match="no DXT texture matched")


def test_hud_refusals(d):
    with fake_tree(d):
        raises(hud.run, hud_args(), match="no [enhance.hud] table")
        G = hud_game(d)
        rows = G.m["enhance"]["hud"]["font_rows"]
        G.m["enhance"]["hud"]["font_rows"] = rows + ["extra"]
        font_path = os.path.join(d, "test.ttf")
        with open(font_path, "wb") as f:
            f.write(ttf())
        raises(hud.run, hud_args(font=font_path), match="font_rows names 3")
        G.m["enhance"]["hud"]["font_rows"] = rows
        write(font_path, "not a font")
        raises(hud.run, hud_args(font=font_path), match="not a TrueType font")
        os.remove(os.path.join(G.game_files, "media.iph"))
        raises(hud.run, hud_args(), match="media.iph")


def test_hud_dedupe(capsys):
    def item(path, w, h, data, h64=7):
        it = hud.Item(None, type("E", (), {"path": path})())
        it.dds = type("D", (), {"width": w, "height": h, "data": data})()
        it.hash = h64
        return it

    a, b = item("a.dds", 4, 4, b"x" * 8), item("b.dds", 4, 4, b"x" * 8)
    c = item("c.dds", 4, 4, b"y" * 8, h64=8)
    assert hud.dedupe([a, c, b]) == [a, c]
    assert "b.dds: same texels as a.dds, one copy kept" in capsys.readouterr().out
    # The font atlas is the copy kept, in the first copy's place.
    assert hud.dedupe([a, c, b], font_atlas="b.dds") == [b, c]
    assert "a.dds: same texels as b.dds, one copy kept" in capsys.readouterr().out
    raises(hud.dedupe, [a, item("z.dds", 4, 4, b"z" * 8)], match="hash alike")
    raises(hud.dedupe, [a, item("w.dds", 8, 2, b"x" * 8)], match="hash alike")


def mix_file(textures):
    """A MIXF with one XPR0 of textures [(name, colour format, w, h, data)
    and optionally (low format bits, size word)], laid out as the game's:
    names, then the XPR0 at a 0x800 boundary with its texels after a 0x800
    header. A name may be str or raw bytes."""
    n = len(textures)
    head = bytearray(b"MIXF" + struct.pack("<IIIIIII", 0x20, 32 * n, 0x800, 0, 0, 0, n))
    for name, *_ in textures:
        raw = name if isinstance(name, bytes) else name.encode("latin-1")
        head += raw.ljust(32, b"\0")
    head = head.ljust(0x800, b"\0")
    hdrs = bytearray()
    body = bytearray()
    for _, color, w, h, data, *extra in textures:
        low, size = extra if extra else (0x29, 0)  # 2D, as the game's
        fmt = (color << 8) | (1 << 16) | ((w.bit_length() - 1) << 20) | ((h.bit_length() - 1) << 24)
        hdrs += struct.pack("<5I", 0x00040001, len(body), 0, fmt | low, size)
        body += data
    hdrs += struct.pack("<I", 0xFFFFFFFF)
    xhead = bytearray(b"XPR0" + struct.pack("<II", 0x800 + len(body), 0x800)) + hdrs
    return bytes(head + xhead.ljust(0x800, b"\xad") + body)


def test_mix_parse():
    a, b = dxt1_blocks(8, 4), dxt5_blocks(4, 4, lambda x, y: x < 2)
    data = mix_file(
        [("mis_a", 0x0C, 8, 4, a), ("mis_b", 0x0F, 4, 4, b), ("mis_c", 0x06, 4, 4, b"\0" * 64)]
    )
    assert xpr.is_mix(data) and not xpr.is_mix(b"DDS ")
    (n0, d0, _), (n1, d1, _), (n2, d2, note) = xpr.parse_mix(data)
    assert (n0, d0.width, d0.height, d0.fourcc, d0.data) == ("mis_a", 8, 4, b"DXT1", a)
    assert (n1, d1.fourcc, d1.data) == ("mis_b", b"DXT5", b)
    assert n2 == "mis_c" and d2 is None and "not DXT (format 0x06)" in note
    # The runtime hashes the data block as the game uploads it.
    assert texhash.tex_hash(d0.data) == texhash.tex_hash(a)
    with pytest.raises(xpr.XprError, match="not a MIXF"):
        xpr.parse_mix(b"XPR0")
    with pytest.raises(xpr.XprError, match="no XPR0"):
        xpr.parse_mix(data[:0x800] + b"nope" + data[0x804:])


def test_mix_truncated(monkeypatch):
    """A cut-off or malformed MIX is an XprError, never a struct.error."""
    data = mix_file([("mis_a", 0x0C, 8, 4, dxt1_blocks(8, 4))])
    for cut in (0x10, 0x24, 0x40, 0x802, 0x806, 0x80A, 0x80C, 0x820, 0x1000, len(data) - 1):
        with pytest.raises(xpr.XprError):
            xpr.parse_mix(data[:cut])
    # An XPR0 header size under its 12 fixed bytes, or a header size or
    # total past the end.
    for hsize, total in ((4, 0x820), (0x10000, 0x820), (0x800, 0x10000)):
        bad = bytearray(data)
        struct.pack_into("<II", bad, 0x804, total, hsize)
        with pytest.raises(xpr.XprError):
            xpr.parse_mix(bytes(bad))
    # A MIX header size under 0x20 would put the name table in the header.
    bad = bytearray(data)
    struct.pack_into("<I", bad, 4, 0x10)
    with pytest.raises(xpr.XprError, match="header size"):
        xpr.parse_mix(bytes(bad))
    # The backstop: a short read that slips past the checks.
    monkeypatch.setattr(xpr, "parse_xpr", lambda data, base: struct.unpack_from("<I", b""))
    with pytest.raises(xpr.XprError, match="MIXF truncated"):
        xpr.parse_mix(data)


def test_hud_corrupt_mix_skipped():
    """load_items notes a corrupt .mix as skipped and goes on."""
    good = dxt1_blocks(8, 4)
    blobs = {"mis/bad.mix": mix_file([("bad", 0x0C, 8, 4, good)])[:0x806], "mis/ok.mix": None}
    blobs["mis/ok.mix"] = mix_file([("ok", 0x0C, 8, 4, good)])
    arc = type("A", (), {"name": "lng", "read": lambda self, e: blobs[e.path]})()
    entries = [type("E", (), {"path": p, "name": p})() for p in blobs]
    bad, ok = hud.load_items([(arc, e) for e in entries])
    assert bad.dds is None and bad.note.startswith("skipped:") and bad.label == "mis/bad.mix"
    assert ok.dds is not None and ok.label == "mis/ok.mix/ok"


def test_mix_names_sanitised():
    """Names come from the dump: empty, unprintable or path-like ones become
    their index, repeats get one, and the label is lower case."""
    t = dxt1_blocks(4, 4)
    data = mix_file(
        [
            (b"", 0x0C, 4, 4, t),
            (b"a\x01b", 0x0C, 4, 4, t),
            (b"caf\xe9", 0x0C, 4, 4, t),
            (b"../up", 0x0C, 4, 4, t),
            (b"dir\\x", 0x0C, 4, 4, t),
            (b"Mis_A", 0x0C, 4, 4, t),
            (b"Mis_A", 0x0C, 4, 4, t),
            (b'q"uote', 0x0C, 4, 4, t),
        ]
    )
    names = [n for n, _, _ in xpr.parse_mix(data)]
    assert names == ["#0", "#1", "#2", "#3", "#4", "Mis_A", "Mis_A#6", 'q"uote']
    arc = type("A", (), {"name": "lng"})()
    it = hud.Item(arc, type("E", (), {"path": "mis/x.mix", "name": "MIS\\X.MIX"})(), "Mis_A")
    assert it.label == "mis/x.mix/mis_a" and it.name == "lng:MIS\\X.MIX/Mis_A"


def test_manifest_name_escaped():
    """A name with a quote stays a valid TOML string in hud-pack.toml."""
    import tomllib

    e = pack.PackEntry(1, 1, 1, b"\0\0\0\xff", name='lng:a\\b.mix/q"u\xe9', guest=(1, 1))
    text = pack.manifest_text([e], 4, "k", "t", "0" * 64)
    assert tomllib.loads(text)["entry"][0]["name"] == 'lng:a/b.mix/q"u\xe9'


def test_mix_not_2d_swizzled():
    """Cube maps, volumes and linear textures are left out."""
    t = dxt1_blocks(4, 4)
    data = mix_file(
        [
            ("cube", 0x0C, 4, 4, t, 0x2D, 0),
            ("vol", 0x0C, 4, 4, t, 0x39, 0),
            ("linear", 0x0C, 4, 4, t, 0x29, 0x1234),
            ("ok", 0x0C, 4, 4, t),
        ]
    )
    out = xpr.parse_mix(data)
    for name, d, note in out[:3]:
        assert d is None and "not a 2D swizzled texture" in note, name
    assert out[3][0] == "ok" and out[3][1] is not None


def test_mix_no_textures(d, capsys):
    """A MIX whose XPR0 holds no texture is one skipped item, so --list
    shows it."""
    assert xpr.parse_mix(mix_file([])) == [("", None, "skipped: no textures")]
    with fake_tree(d):
        G = hud_game(d)
        write_archive(G.game_files, "lng", [("mis\\empty.mix", mix_file([]), True)])
        cfg = G.m["enhance"]["hud"]
        cfg["archives"] = cfg["archives"] + ["lng"]
        cfg["textures"] = cfg["textures"] + ["mis/*"]
        assert hud.run(hud_args(list=True)) == 0
        out = capsys.readouterr().out
        assert "mis/empty.mix" in out and "skipped: no textures" in out


def test_hud_mix_textures(d, capsys):
    """Pre-rendered text in a .mix of a second archive joins the pack, one
    entry per texture, named after the MIX's name table."""
    with fake_tree(d):
        G = hud_game(d)
        text = dxt1_blocks(8, 4, idx=1)
        write_archive(
            G.game_files,
            "lng",
            [("mis\\mis_one.mix", mix_file([("mis_one", 0x0C, 8, 4, text)]), True)],
        )
        cfg = G.m["enhance"]["hud"]
        cfg["archives"] = cfg["archives"] + ["lng"]
        cfg["textures"] = cfg["textures"] + ["mis/*"]
        assert hud.run(hud_args(list=True)) == 0
        out = capsys.readouterr().out
        assert "%016X  mis/mis_one.mix/mis_one" % texhash.tex_hash(text) in out
        assert "5 entries, 3 usable" in out
        assert hud.run(hud_args()) == 0
        man = open(os.path.join(G.dist, "enhance", "hud", "hud-pack.toml")).read()
        assert 'name = "lng:mis/mis_one.mix/mis_one"' in man


def read_pack(path):
    """{hash: (w, h, flags, payload)} of a pack."""
    with open(path, "rb") as f:
        data = f.read()
    _, _, count, _, index_off, _ = pack.HEADER.unpack_from(data)
    recs = [pack.ENTRY.unpack_from(data, index_off + 32 * i) for i in range(count)]
    out = {}
    for i, (h, w, hh, _, flags, off) in enumerate(recs):
        end = recs[i + 1][5] if i + 1 < count else len(data)
        out[h] = (w, hh, flags, data[off:end])
    return out


def kinds_game(d):
    """hud_game plus two language archives of .mix text, a picture .mix in
    media (art: a MIX alone is not text) and a baked-text .dds in media the
    text patterns name."""
    G = hud_game(d)
    us = dxt1_blocks(8, 4, idx=1)
    jp = dxt1_blocks(8, 4, idx=2)
    pic = dxt1_blocks(8, 8, idx=3)
    sm = dxt1_blocks(4, 4, idx=1)
    write_archive(
        G.game_files, "lng_us", [("mis\\m.mix", mix_file([("m_us", 0x0C, 8, 4, us)]), True)]
    )
    write_archive(
        G.game_files, "lng_jp", [("mis\\m.mix", mix_file([("m_jp", 0x0C, 8, 4, jp)]), True)]
    )
    write_archive(
        G.game_files,
        "media",
        [
            ("dds\\ui_bar.dds", dds_file(8, 4, b"DXT1", dxt1_blocks(8, 4)), False),
            (
                "dds\\font_test.dds",
                dds_file(ATLAS_W, ATLAS_H, b"DXT5", dxt5_blocks(ATLAS_W, ATLAS_H, stock_ink)),
                True,
            ),
            ("dds\\ui_pic.mix", mix_file([("pic", 0x0C, 8, 8, pic)]), True),
            ("dds\\ui_sm_go.dds", dds_file(4, 4, b"DXT1", sm), False),
        ],
    )
    cfg = G.m["enhance"]["hud"]
    cfg["archives"] = ["media", "lng_us"]
    cfg["textures"] = cfg["textures"] + ["mis/*"]
    cfg["languages"] = {"us": ["lng_us"], "jp": ["lng_jp"]}
    cfg["text"] = ["dds/ui_sm_*"]
    return G, {"us": us, "jp": jp, "pic": pic, "sm": sm}


def test_hud_kinds_and_languages(d, capsys):
    with fake_tree(d):
        G, tex = kinds_game(d)
        assert hud.run(hud_args(list=True)) == 0
        out = capsys.readouterr().out
        h = texhash.tex_hash
        assert "language jp (lng_jp): not picked" in out
        assert "%016X  %-40s %-7s DXT1" % (h(tex["us"]), "mis/m.mix/m_us", "text/us") in out
        assert "%016X  %-40s %-7s DXT1" % (h(tex["pic"]), "dds/ui_pic.mix/pic", "art") in out
        assert "%016X  %-40s %-7s DXT1" % (h(tex["sm"]), "dds/ui_sm_go.dds", "text") in out
        assert "dds/font_test.dds" in out and " font    DXT5" in out
        assert "m_jp" not in out
        assert "(2 art, 1 font, 2 text)" in out
        assert hud.run(hud_args(list=True, language=["jp"])) == 0
        out = capsys.readouterr().out
        assert "m_jp" in out and "text/jp" in out and "m_us" not in out
        assert hud.run(hud_args(list=True, language=["us", "jp"])) == 0
        out = capsys.readouterr().out
        assert "m_jp" in out and "m_us" in out and "not picked" not in out
        raises(hud.run, hud_args(language=["fr"]), match="languages has us, jp")
        # The pack: text at 2x in BC3, art and the atlas at the scale, raw.
        assert hud.run(hud_args()) == 0
        out = capsys.readouterr().out
        assert "(2 art, 1 font, 2 text)" in out
        p = os.path.join(G.dist, "enhance", "hud", "hud.pack")
        with open(p, "rb") as f:
            assert pack.HEADER.unpack_from(f.read())[5] == hud.TEXT_SCALE
        entries = read_pack(p)
        w, hh, flags, payload = entries[h(tex["us"])]
        assert (w, hh, flags) == (16, 8, pack.KIND_TEXT | pack.FLAG_BC3)
        assert len(payload) == bc3.size(16, 8)
        _, _, rgba = dds.decode(dds.Dds(8, 4, 1, b"DXT1", tex["us"]))
        assert payload == bc3.encode(16, 8, scaler.scale(8, 4, rgba, 2))
        assert entries[h(tex["pic"])][:3] == (16, 16, pack.KIND_ART)
        assert entries[h(tex["sm"])][2] == pack.KIND_TEXT | pack.FLAG_BC3
        assert entries[h(dxt5_blocks(ATLAS_W, ATLAS_H, stock_ink))][2] == pack.KIND_FONT
        man = open(os.path.join(G.dist, "enhance", "hud", "hud-pack.toml")).read()
        assert (
            'languages = ["us"]' in man and "[kinds.text]\nentries = 2\nscale = 2\nbc3 = 2" in man
        )


def test_hud_art_payload_unchanged(d, capsys):
    """Without languages or text patterns every entry is art or the atlas,
    built as before: xBR at the scale, A8R8G8B8, no BC3 (the v1 payload
    bytes; only the header version and the kind flags differ)."""
    with fake_tree(d):
        G = hud_game(d)
        assert hud.run(hud_args()) == 0
        capsys.readouterr()
        p = os.path.join(G.dist, "enhance", "hud", "hud.pack")
        with open(p, "rb") as f:
            assert pack.HEADER.unpack_from(f.read())[5] == 0
        entries = read_pack(p)
        bar = dxt1_blocks(8, 4)
        _, _, rgba = dds.decode(dds.Dds(8, 4, 1, b"DXT1", bar))
        assert entries[texhash.tex_hash(bar)] == (
            16,
            8,
            pack.KIND_ART,
            pack.rgba_to_argb(scaler.scale(8, 4, rgba, 2)),
        )
        atlas_blocks = dxt5_blocks(ATLAS_W, ATLAS_H, stock_ink)
        _, _, rgba = dds.decode(dds.Dds(ATLAS_W, ATLAS_H, 1, b"DXT5", atlas_blocks))
        assert entries[texhash.tex_hash(atlas_blocks)] == (
            ATLAS_W * 2,
            ATLAS_H * 2,
            pack.KIND_FONT,
            pack.rgba_to_argb(scaler.scale(ATLAS_W, ATLAS_H, rgba, 2)),
        )


# ---- package --hud-pack -----------------------------------------------------


def test_package_hud_pack(d):
    with fake_tree(d):
        G = host.g()
        raises(pkg.hud_pack_path, None, match="make it with")
        p = os.path.join(G.dist, "enhance", "hud")
        os.makedirs(p)
        pack.write(os.path.join(p, "hud.pack"), [pack.PackEntry(1, 1, 1, b"\0\0\0\xff")], 4)
        info = pkg.hud_pack_path(None)
        assert info["entries"] == 1 and info["scale"] == 4
        assert pkg.hud_pack_path(p) == info
        write(os.path.join(d, "x", "hud.pack"), "nope nope nope nope nope nope nope")
        raises(pkg.hud_pack_path, os.path.join(d, "x"), match="bad magic")
        for target, rel in (
            ("windows", "enhance/hud.pack"),
            ("macos", "../Resources/enhance/hud.pack"),
        ):
            payload = os.path.join(d, "payload-" + target)
            write(os.path.join(payload, "config.default.toml"), "[render]\nscale = 1\n")
            os.makedirs(os.path.join(payload, "enhance"))  # a payload that has one
            staged = pkg.stage_hud_pack(payload, target, info)
            assert staged["path"] == rel and staged["entries"] == 1
            assert pack.read_header(os.path.join(payload, "enhance", "hud.pack")) == (1, 4)
            with open(os.path.join(payload, "config.default.toml")) as f:
                text = f.read()
            assert text.startswith("[render]\nscale = 1\n") and "[textures]" in text
            assert 'pack = "%s"' % rel in text
            # The defaults file is read under the player's config.toml, so
            # the key applies to an upgrade; the comment says how to opt out.
            assert "applies to an upgrade" in text and 'pack = "none"' in text
            assert "config/config.toml" in text
            assert not os.path.exists(os.path.join(payload, "enhance.toml.default"))
            # The appended table parses: one [textures], the pack key in it.
            assert text.count("[textures]") == 2  # the header and the comment's mention
            assert text.rstrip().endswith('pack = "%s"' % rel)
        # A defaults file that already opens [textures] would get a second
        # header, and the layer would reject the whole file: refused.
        payload = os.path.join(d, "payload-dup")
        write(os.path.join(payload, "config.default.toml"), '[textures]\npack = "none"\n')
        raises(pkg.stage_hud_pack, payload, "windows", info, match="already has a")
        write(os.path.join(payload, "config.default.toml"), '# [textures]\n# pack = "x"\n')
        assert pkg.stage_hud_pack(payload, "windows", info)["entries"] == 1
