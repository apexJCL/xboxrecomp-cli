"""The release matcher (spike): two tiny synthetic releases, the second
relinked with a pad and one changed function."""

import struct

from xboxrecomp_cli import match

BASE = 0x10000


def call(at, to):
    return b"\xe8" + struct.pack("<i", to - (at + 5))


def release(text_va, data_va, pad, changed):
    """Three functions in .text: f0 calls f1 and f2 and reads a global;
    f1 reads another; f2 is changed (or not). pad bytes sit before f0, so
    every address moves."""
    g0, g1 = data_va + 0x10, data_va + 0x24
    f0 = text_va + pad
    f1 = f0 + 0x20
    f2 = f1 + 0x20
    body0 = b"\x55\x8b\xec" + b"\xa1" + struct.pack("<I", g0)
    body0 += call(f0 + len(body0), f1)
    body0 += call(f0 + len(body0), f2)
    body0 += b"\x5d\xc3"
    body1 = b"\x8b\x0d" + struct.pack("<I", g1) + b"\x83\xc1\x01\xc3"
    body2 = (b"\x33\xc0\x40\x40\xc3" if changed else b"\x33\xc0\x40\xc3").ljust(8, b"\xcc")
    text = bytearray(b"\xcc" * (pad + 0x60))
    text[pad : pad + len(body0)] = body0
    text[pad + 0x20 : pad + 0x20 + len(body1)] = body1
    text[pad + 0x40 : pad + 0x40 + len(body2)] = body2
    funcs = [
        {"start": hex(f0), "end": hex(f0 + len(body0)), "section": ".text"},
        {"start": hex(f1), "end": hex(f1 + len(body1)), "section": ".text"},
        {"start": hex(f2), "end": hex(f2 + len(body2)), "section": ".text"},
    ]
    xrefs = [
        {"from": hex(f0 + 3), "to": hex(g0), "type": "data_read"},
        {"from": hex(f0 + 8), "to": hex(f1), "type": "call"},
        {"from": hex(f0 + 13), "to": hex(f2), "type": "call"},
        {"from": hex(f1), "to": hex(g1), "type": "data_read"},
    ]
    secs = [(".text", text_va, bytes(text)), (".data", data_va, b"\0" * 0x100)]
    rel = match.Release(BASE, data_va + 0x100 - BASE, secs, funcs, xrefs)
    return rel, (f0, f1, f2), (g0, g1)


def test_relinked_release_maps_functions_and_globals():
    a, fa, ga = release(0x11000, 0x20000, 0, False)
    b, fb, gb = release(0x11000, 0x20400, 0x40, True)
    m = match.Match(a, b)
    m.run()
    # f0 and f1 are the same code at other addresses; f2 changed, and is
    # found through f0's call at the same offset.
    assert m.fn == dict(zip(fa, fb))
    assert m.how[fa[0]] == "exact" and m.how[fa[2]] == "calls"
    assert m.data[ga[0]] == gb[0] and m.data[ga[1]] == gb[1]
    # A field of the second global, which no instruction names.
    assert m.place(ga[1] + 4)[0] == gb[1] + 4
    # An address inside an unchanged function keeps its offset.
    assert m.place(fa[1] + 2)[0] == fb[1] + 2


def test_masking_ignores_moved_addresses_only():
    a, fa, _ = release(0x11000, 0x20000, 0, False)
    b, fb, _ = release(0x11000, 0x30000, 0x10, False)
    for x, y in zip(fa, fb):
        assert match.masked_key(a, a.by_start[x]) == match.masked_key(b, b.by_start[y])
