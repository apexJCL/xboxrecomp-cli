"""enhance/png.py on files the tests build chunk by chunk, so every colour type,
depth and filter is met without an image library.

  uv run pytest tests/test_enhance_png.py
"""

import struct
import zlib

import pytest

from xboxrecomp_cli.enhance import png


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def file_of(w, h, depth, colour, rows, *extra, lace=0):
    """A PNG of `rows`, each with its filter byte already on."""
    head = struct.pack(">IIBBBBB", w, h, depth, colour, 0, 0, lace)
    return (
        png.SIGNATURE
        + chunk(b"IHDR", head)
        + b"".join(extra)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def noise(n):
    return bytes((i * 37 + (i >> 3) * 11 + 5) & 255 for i in range(n))


def refilter(kind, line, prev, bpp):
    """`line` under filter `kind`, the encoder's side of each PNG filter."""
    out = bytearray(len(line))
    for i, x in enumerate(line):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - c - c)
        paeth = a if pa <= pb and pa <= pc else b if pb <= pc else c
        pred = (0, a, b, (a + b) >> 1, paeth)[kind]
        out[i] = (x - pred) & 255
    return bytes([kind]) + bytes(out)


def test_round_trip_and_size(tmp_path):
    rgba = noise(5 * 3 * 4)
    p = str(tmp_path / "a.png")
    png.write(p, 5, 3, rgba)
    assert png.size(p) == (5, 3)
    assert png.read(p) == (5, 3, rgba)


def test_write_leaves_no_part_of_a_file(tmp_path, monkeypatch):
    p = str(tmp_path / "a.png")
    png.write(p, 1, 1, b"\1\2\3\4")

    def full(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(png.os, "replace", full)
    with pytest.raises(OSError):
        png.write(p, 1, 1, b"\5\6\7\x08")
    assert png.read(p)[2] == b"\1\2\3\4"
    assert [f.name for f in tmp_path.iterdir()] == ["a.png"]


def test_write_without_alpha():
    rgba = noise(2 * 2 * 4)
    w, h, got = png.decode(png.encode(2, 2, rgba, alpha=False))
    want = bytearray(rgba)
    want[3::4] = b"\xff" * 4
    assert (w, h, got) == (2, 2, bytes(want))


def test_encode_refuses_the_wrong_length():
    with pytest.raises(png.PngError):
        png.encode(2, 2, b"\0" * 15)


def test_has_alpha():
    assert not png.has_alpha(b"\1\2\3\xff" * 4)
    assert png.has_alpha(b"\1\2\3\xff\1\2\3\xfe")


@pytest.mark.parametrize("bpp,colour", [(4, png.RGBA), (3, png.RGB)])
def test_every_filter(bpp, colour):
    w, h = 7, 5
    lines = [noise(w * bpp * (y + 1))[-w * bpp :] for y in range(h)]
    rows = []
    prev = bytes(w * bpp)
    for y, line in enumerate(lines):
        rows.append(refilter(y, line, prev, bpp))
        prev = line
    got = png.decode(file_of(w, h, 8, colour, rows))[2]
    if colour == png.RGBA:
        assert got == b"".join(lines)
    else:
        want = bytearray(w * h * 4)
        flat = b"".join(lines)
        want[0::4], want[1::4], want[2::4] = flat[0::3], flat[1::3], flat[2::3]
        want[3::4] = b"\xff" * (w * h)
        assert got == bytes(want)


def test_gray_alpha_and_sixteen_bit():
    d = file_of(2, 1, 8, png.GRAY_ALPHA, [b"\0\x10\x20\x30\x40"])
    assert png.decode(d)[2] == b"\x10\x10\x10\x20\x30\x30\x30\x40"
    d = file_of(1, 1, 16, png.RGBA, [b"\0\x11\xaa\x22\xbb\x33\xcc\x44\xdd"])
    assert png.decode(d)[2] == b"\x11\x22\x33\x44"
    d = file_of(1, 1, 16, png.RGB, [b"\0\x11\xaa\x22\xbb\x33\xcc"])
    assert png.decode(d)[2] == b"\x11\x22\x33\xff"
    d = file_of(1, 1, 16, png.GRAY_ALPHA, [b"\0\x11\xaa\x22\xbb"])
    assert png.decode(d)[2] == b"\x11\x11\x11\x22"
    d = file_of(1, 1, 16, png.GRAY, [b"\0\x11\xaa"])
    assert png.decode(d)[2] == b"\x11\x11\x11\xff"


def test_low_depth_gray_is_scaled_and_rows_are_padded():
    # 3 texels at 1 bit: 1 0 1, then 0 1 1, each row one byte.
    d = file_of(3, 2, 1, png.GRAY, [b"\0\xa0", b"\0\x60"])
    v = png.decode(d)[2][0::4]
    assert v == bytes([255, 0, 255, 0, 255, 255])
    d = file_of(2, 1, 4, png.GRAY, [b"\0\x1f"])
    assert png.decode(d)[2][0::4] == bytes([17, 255])
    d = file_of(2, 1, 2, png.GRAY, [b"\0\x70"])  # 01 11
    assert png.decode(d)[2][0::4] == bytes([85, 255])


def test_palette_with_alpha():
    plte = chunk(b"PLTE", bytes([1, 2, 3, 4, 5, 6, 7, 8, 9]))
    trns = chunk(b"tRNS", b"\x80")
    d = file_of(3, 1, 8, png.PALETTE, [b"\0\0\1\2"], plte, trns)
    assert png.decode(d)[2] == bytes([1, 2, 3, 0x80, 4, 5, 6, 255, 7, 8, 9, 255])
    d = file_of(2, 1, 2, png.PALETTE, [b"\0\x60"], plte)  # 01 10
    assert png.decode(d)[2] == bytes([4, 5, 6, 255, 7, 8, 9, 255])


def test_palette_index_past_the_end():
    plte = chunk(b"PLTE", bytes([1, 2, 3]))
    with pytest.raises(png.PngError, match="past the palette"):
        png.decode(file_of(1, 1, 8, png.PALETTE, [b"\0\1"], plte))


def test_colour_keys():
    trns = chunk(b"tRNS", struct.pack(">HHH", 1, 2, 3))
    d = file_of(2, 1, 8, png.RGB, [b"\0\1\2\3\4\5\6"], trns)
    assert png.decode(d)[2] == bytes([1, 2, 3, 0, 4, 5, 6, 255])
    trns = chunk(b"tRNS", struct.pack(">H", 7))
    d = file_of(2, 1, 8, png.GRAY, [b"\0\7\x08"], trns)
    assert png.decode(d)[2] == bytes([7, 7, 7, 0, 8, 8, 8, 255])
    # At 16 bits the key names the whole sample, not its high byte.
    trns = chunk(b"tRNS", struct.pack(">H", 0x0102))
    d = file_of(2, 1, 16, png.GRAY, [b"\0\1\2\1\3"], trns)
    assert png.decode(d)[2] == bytes([1, 1, 1, 0, 1, 1, 1, 255])


def test_several_idat_chunks():
    z = zlib.compress(b"\0\1\2\3\4")
    d = (
        png.SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, png.RGBA, 0, 0, 0))
        + chunk(b"IDAT", z[:3])
        + chunk(b"tEXt", b"a\0b")
        + chunk(b"IDAT", z[3:])
        + chunk(b"IEND", b"")
    )
    assert png.decode(d)[2] == b"\1\2\3\4"


def bad_crc():
    d = bytearray(png.encode(1, 1, b"\1\2\3\4"))
    d[40] ^= 1
    return bytes(d)


@pytest.mark.parametrize(
    "data,why",
    [
        (b"not a png at all, not at all, no", "not a PNG"),
        (png.encode(1, 1, b"\1\2\3\4")[:-6], "cut short"),
        (bad_crc(), "bad checksum"),
        (file_of(1, 1, 8, png.RGBA, [b"\0\1\2\3\4"], lace=1), "interlaced"),
        (file_of(1, 1, 3, png.RGB, [b"\0\1"]), "not PNG"),
        (file_of(1, 1, 8, png.RGBA, [b"\5\1\2\3\4"]), "unknown filter"),
        (file_of(1, 1, 8, png.RGBA, [b"\0\1\2\3"]), "bytes of scanlines"),
        (file_of(1, 1, 8, png.RGBA, [b"\0\1\2\3\4\5"]), "bytes of scanlines"),
        (file_of(0, 1, 8, png.RGBA, [b""]), "empty"),
        (file_of(1 << 15, 1 << 15, 8, png.RGBA, [b""]), "over"),
        (file_of(1, 1, 8, png.PALETTE, [b"\0\0"]), "no palette"),
        (
            file_of(1, 1, 8, png.PALETTE, [b"\0\0"], chunk(b"PLTE", bytes(3 * 257))),
            "over PNG's 256",
        ),
        (file_of(1, 1, 8, png.RGBA, [b"\0\1\2\3\4"], chunk(b"ABCD", b"")), "required chunk"),
    ],
)
def test_refusals(data, why):
    with pytest.raises(png.PngError, match=why):
        png.decode(data)
