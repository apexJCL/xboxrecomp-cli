"""Padding before an upscale, so its kernel sees what the sampler will see past
a texture's edge (D7): `wrap` takes the opposite edge (a mirror there would put
a seam in every tiled wall), `mirror` reflects the edge, as the guest's MIRROR
mode does, `clamp` extends it. `cells` is a font's or a sprite sheet's: each
cell is padded and upscaled alone, so no stroke crosses a cell's border.

An image is (w, h, rgba), RGBA bytes top down. pad gives the bigger image and
crop takes the result back to the upscaled size.
"""

PAD = 8
MODES = ("wrap", "mirror", "clamp", "cells")
# The recorded address modes of a dump's sidecar. A border colour is the game's
# drawing the texture inside a frame: nothing to continue, so as clamp.
ADDRESS = {
    "wrap": "wrap",
    "mirror": "mirror",
    "clamp": "clamp",
    "clamp_edge": "clamp",
    "clamp_ogl": "clamp",
    "border": "clamp",
}
# What a class's texture gets when no dump recorded its mode. A world texture
# tiles far more often than not (BLiNX 2's dumps: 415 wrap, 14 clamp), and a
# clamped pad on a tile shows at its seam (mean step 23.8 against wrap's 7.0);
# everything else is drawn once, in a frame.
UNRECORDED = {"world": "wrap"}


# Whose mode a texture gets when scenes disagree: a clamped pad on a tile shows
# at its seam, a repeating pad on a texture drawn once only touches its rim.
_ADDRESS_RANK = {"wrap": 2, "mirror": 1, "clamp": 0}


def merged_address(a, b):
    """The sidecar mode to keep of two scenes': the one that repeats, a recorded
    one over none. Commutative, so the order of the dumps cannot pick."""
    names = [n for n in (a, b) if n]
    return max(names, key=lambda n: (_ADDRESS_RANK.get(ADDRESS.get(n), -1), n), default=None)


def address_mode(name, unrecorded="clamp"):
    """The pad mode of a sidecar's wrap_u or wrap_v; `unrecorded` when it has none."""
    return ADDRESS.get(name or "", unrecorded)


def resolve(edges, entry):
    """(mode across, mode down) of a treatment's `edges` for an inventory entry."""
    if edges == "auto":
        unrecorded = UNRECORDED.get(entry.get("class"), "clamp")
        return (
            address_mode(entry.get("wrap_u"), unrecorded),
            address_mode(entry.get("wrap_v"), unrecorded),
        )
    return edges, edges


def defaulted(entry, edges):
    """Whether an axis of this entry's pad comes from UNRECORDED and not a dump."""
    return (
        edges == "auto"
        and entry.get("class") in UNRECORDED
        and not (entry.get("wrap_u") in ADDRESS and entry.get("wrap_v") in ADDRESS)
    )


def _index(i, n, mode):
    if 0 <= i < n:
        return i
    if mode == "wrap":
        return i % n
    if mode == "mirror":
        # ... 2 1 0 | 0 1 2 ... n-1 | n-1 n-2 ...: the edge texel twice, as a
        # mirrored repeat samples it.
        i %= 2 * n
        return i if i < n else 2 * n - 1 - i
    return 0 if i < 0 else n - 1


def pad(w, h, rgba, mode_u, mode_v, n=PAD):
    """(w + 2n, h + 2n, rgba) with n texels of edge on every side."""
    xs = [_index(x, w, mode_u) for x in range(-n, w + n)]
    ow = w + 2 * n
    rows = {}
    out = bytearray()
    for y in range(-n, h + n):
        sy = _index(y, h, mode_v)
        row = rows.get(sy)
        if row is None:
            base = sy * w * 4
            src = rgba[base : base + w * 4]
            row = b"".join(src[x * 4 : x * 4 + 4] for x in xs)
            rows[sy] = row
        out += row
    return ow, h + 2 * n, bytes(out)


def crop(w, h, rgba, x0, y0, cw, ch):
    """The rectangle of an image, RGBA."""
    out = bytearray()
    for y in range(y0, y0 + ch):
        o = (y * w + x0) * 4
        out += rgba[o : o + cw * 4]
    return bytes(out)


def blit(dw, dst, x0, y0, cw, ch, cell):
    """A cell's rgba into a bytearray image `dw` texels wide."""
    for y in range(ch):
        o = ((y0 + y) * dw + x0) * 4
        s = y * cw * 4
        dst[o : o + cw * 4] = cell[s : s + cw * 4]
