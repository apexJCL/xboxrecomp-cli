"""The toolkit's texture hash, nv2a_tex_hash (src/kernel/nv2a_backend_common.c):
a four-lane FNV-1a over 32-byte groups (one little-endian u64 per lane),
the tail byte by byte through lane 0, the lanes folded at the end. The
GPU backends compute it over every texture they upload; a pack entry is
keyed by it. tests/tex_pack in the toolkit checks the same vectors."""

import struct

MASK = 0xFFFFFFFFFFFFFFFF
PRIME = 0x100000001B3
SEED = 0xCBF29CE484222325
_Q4 = struct.Struct("<4Q")


def tex_hash(data, seed=SEED):
    l0, l1, l2, l3 = seed, seed ^ 1, seed ^ 2, seed ^ 3
    n = len(data)
    i = 0
    unpack = _Q4.unpack_from
    while i + 32 <= n:
        w0, w1, w2, w3 = unpack(data, i)
        l0 = ((l0 ^ w0) * PRIME) & MASK
        l1 = ((l1 ^ w1) * PRIME) & MASK
        l2 = ((l2 ^ w2) * PRIME) & MASK
        l3 = ((l3 ^ w3) * PRIME) & MASK
        i += 32
    while i < n:
        l0 = ((l0 ^ data[i]) * PRIME) & MASK
        i += 1
    return ((l0 ^ ((l1 << 1) & MASK) ^ ((l2 << 2) & MASK) ^ ((l3 << 3) & MASK)) * PRIME) & MASK
