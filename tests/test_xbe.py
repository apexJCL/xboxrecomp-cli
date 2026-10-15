"""xbe.py: the header fields `new` and `package` read, on synthetic
headers (never a dump).

  uv run pytest tests/test_xbe.py
"""

import struct

import pytest

from xboxrecomp_cli import xbe

BASE = 0x10000
CERT = 0x180


def header(title="Some Game", title_id=0x4B4C0001, entry=0x00123456, key=xbe.ENTRY_RETAIL_XOR):
    """A header whose image spans 0x10000..0x110000, with the certificate
    at +0x180 and the entry point encoded with key."""
    h = bytearray(0x400)
    h[:4] = b"XBEH"
    struct.pack_into("<3I", h, 0x104, BASE, 0x400, 0x100000)
    struct.pack_into("<I", h, 0x118, BASE + CERT)
    struct.pack_into("<I", h, 0x128, entry ^ key)
    struct.pack_into("<I", h, CERT + 8, title_id)
    h[CERT + 0xC : CERT + 0xC + 80] = title.encode("utf-16-le").ljust(80, b"\0")
    return bytes(h)


def test_retail_header():
    h = xbe.read_header(header())
    assert h == {
        "title": "Some Game",
        "title_id": 0x4B4C0001,
        "base": BASE,
        "entry": 0x00123456,
        "kind": "retail",
    }
    assert xbe.title_id(header()) == 0x4B4C0001


def test_debug_key_when_retail_lands_outside_the_image():
    h = xbe.read_header(header(entry=0x00023456, key=xbe.ENTRY_DEBUG_XOR))
    assert (h["entry"], h["kind"]) == (0x00023456, "debug")
    # Neither key inside the image: the retail decode is reported as it is.
    h = xbe.read_header(header(entry=0x00200000))
    assert (h["entry"], h["kind"]) == (0x00200000, "retail")


def test_empty_title_and_bad_headers():
    assert xbe.read_header(header(title=""))["title"] == ""
    for bad in (b"MZ" + bytes(0x400), header()[:0x100]):
        with pytest.raises(ValueError):
            xbe.read_header(bad)
    # A certificate address past the buffer: the title ID is unreadable.
    h = bytearray(header())
    struct.pack_into("<I", h, 0x118, BASE + 0x10000)
    with pytest.raises(ValueError):
        xbe.title_id(bytes(h))


# D3D's fence wait as the three XDK 5849 titles have it, the device pointer
# and the two displacements filled in: the D3D8 form (BLiNX 2, Spartan) and
# the LTCG form, which parks the fence in edi (Burnout 3).
def fence_wait(ptr, get=0x30, put=0x2C, ltcg=False):
    return (
        b"\x56\x8b\x35"
        + struct.pack("<I", ptr)
        + (b"\x57\x8b\xf8" if ltcg else b"")
        + bytes([0x8B, 0x46, get, 0x8B, 0x08, 0x8B, 0x46, put, 0x8B, 0xD0, 0x2B, 0xD1])
    )


HEADER_SIZE = 0x1000
SECTIONS_AT = 0x400
NAMES_AT = 0x800
LIBS_AT = 0xA00


def image(sections=(), libraries=(), tables_at=0, **kw):
    """A whole XBE: header() grown to HEADER_SIZE (plus tables_at), with a
    section table of (name, flags, bytes) and a library table of (name,
    major, minor, build), both moved tables_at further in. Each section's
    raw bytes follow the header, loaded at base plus their file offset."""
    size = HEADER_SIZE + tables_at
    secs_at, names_at, libs_at = (x + tables_at for x in (SECTIONS_AT, NAMES_AT, LIBS_AT))
    h = bytearray(header(**kw).ljust(size, b"\0"))
    struct.pack_into("<I", h, 0x108, size)
    struct.pack_into("<2I", h, 0x11C, len(sections), BASE + secs_at)
    struct.pack_into("<2I", h, 0x160, len(libraries), BASE + libs_at)
    body, name_off = b"", names_at
    for i, (name, flags, raw) in enumerate(sections):
        off = size + len(body)
        struct.pack_into(
            "<6I", h, secs_at + i * xbe.SECTION_LEN,
            flags, BASE + off, len(raw), off, len(raw), BASE + name_off,
        )  # fmt: skip
        h[name_off : name_off + len(name) + 1] = name.encode() + b"\0"
        name_off += len(name) + 1
        body += raw + b"\0" * (-len(raw) % 16)
    for i, (name, major, minor, build) in enumerate(libraries):
        struct.pack_into(
            "<8s3H", h, libs_at + i * xbe.LIBRARY_LEN, name.encode(), major, minor, build
        )
    return bytes(h) + body


XDK = [("XAPILIB", 1, 0, 5849), ("D3D8", 1, 0, 5849), ("DSOUND", 1, 0, 5849)]


def test_sections_and_libraries():
    data = image(
        [(".text", 0x6, b"\x90" * 32), ("D3D", 0x6, b"\xcc" * 20), (".data", 0x1, b"\0" * 8)], XDK
    )
    assert xbe.header_size(data) == HEADER_SIZE
    secs = xbe.sections(data)
    assert [(s.name, s.flags, s.vsize, s.raw_size) for s in secs] == [
        (".text", 6, 32, 32),
        ("D3D", 6, 20, 20),
        (".data", 1, 8, 8),
    ]
    d3d = secs[1]
    assert d3d.va == BASE + d3d.raw_off and data[d3d.raw_off : d3d.raw_off + 20] == b"\xcc" * 20
    assert xbe.libraries(data) == [
        ("XAPILIB", "1.0.5849"),
        ("D3D8", "1.0.5849"),
        ("DSOUND", "1.0.5849"),
    ]
    assert xbe.sections(image()) == [] and xbe.libraries(image()) == []
    # A table that runs past the headers is an error, not a guess.
    bad = bytearray(data)
    struct.pack_into("<I", bad, 0x11C, 200)
    with pytest.raises(xbe.XbeError):
        xbe.sections(bytes(bad))
    # Too short to hold the table fields: an XbeError, not a struct.error.
    for reader in (xbe.sections, xbe.libraries):
        with pytest.raises(xbe.XbeError):
            reader(data[: xbe.TABLES_LEN - 4])
    # No entries: the table's address is not looked at.
    empty = bytearray(image())
    struct.pack_into("<I", empty, 0x120, 0xFFFFFFFF)
    assert xbe.sections(bytes(empty)) == []


def test_find_d3d_fence_forms():
    pad = b"\xcc" * 7
    va = 0x00228000
    # D3D8: the wait VA is the push esi.
    f = xbe.find_d3d_fence(pad + fence_wait(0x00235528) + pad, va)
    assert f == xbe.Fence(wait=va + 7, device=0x00235528, put=0x2C, get=0x30)
    # LTCG: push edi; mov edi, eax between the pointer load and the fields.
    f = xbe.find_d3d_fence(fence_wait(0x0035FB48, ltcg=True), 0x00351770)
    assert f == (0x00351770, 0x0035FB48, 0x2C, 0x30)
    # No push esi before it: the wait is reported at the pointer load.
    f = xbe.find_d3d_fence(b"\x90" + fence_wait(0x002F1FB8)[1:], 0x1000)
    assert f.wait == 0x1001 and f.device == 0x002F1FB8
    # Other displacements are reported as found, not forced to 0x2C/0x30.
    f = xbe.find_d3d_fence(fence_wait(0x00400000, get=0x34, put=0x28), 0)
    assert (f.put, f.get) == (0x28, 0x34)
    # None, or two: no answer.
    assert xbe.find_d3d_fence(pad * 10, va) is None
    two = fence_wait(0x1000) + pad + fence_wait(0x2000)
    assert xbe.find_d3d_fence(two, va) is None
    assert [x.device for x in xbe.find_d3d_fences(two, va)] == [0x1000, 0x2000]


def test_the_three_titles_reproduced():
    """Each title's fence wait rebuilt from its documented bytes (never
    read from a dump) gives that title's pointer, through a whole XBE."""
    for ptr, wait, ltcg in (
        (0x002F1FB8, 0x002E8110, False),  # BLiNX 2, D3D8 1.0.5849
        (0x00235528, 0x00228FA0, False),  # Spartan, D3D8 1.0.5849
        (0x0035FB48, 0x00351770, True),  # Burnout 3, D3D8LTCG 1.0.5849
    ):
        data = image([("D3D", 0x6, b"\xcc" * 16 + fence_wait(ptr, ltcg=ltcg))], XDK)
        (d3d,) = xbe.sections(data)
        code = data[d3d.raw_off : d3d.raw_off + d3d.raw_size]
        f = xbe.find_d3d_fence(code, wait - 16)
        assert f == (wait, ptr, 0x2C, 0x30)
