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
