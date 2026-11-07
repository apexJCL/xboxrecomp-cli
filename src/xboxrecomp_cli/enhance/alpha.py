"""What an upscaler gets of a texture with alpha (D7), and how it comes back.

split   the colour, with what lies under transparent texels replaced by the
        nearest opaque colour, then the alpha as a grey image, both through
        the same upscaler, recombined. A model trained on photographs has
        never seen a texture's undefined colour under alpha 0: unreplaced, it
        rings along the edge of every cutout. The default.
native  RGBA straight into an upscaler that takes it (the built-ins do).
binary  as split, then a threshold on the alpha chosen so the upscaled image
        covers the fraction of the source it covered, within 1%: a cutout
        stays a cutout, never a soft halo the game's alpha test would cut
        through unevenly.
"""

MODES = ("split", "native", "binary")
# The alpha test's own cut: the source is binary, or near it, if nearly every
# texel is on one side or the other.
COVER_AT = 128


def opaque(rgba):
    return rgba[3::4].count(255) * 4 == len(rgba)


def extend_colour(w, h, rgba):
    """rgba with every fully transparent texel's colour taken from the nearest
    texel that is not, by passes outward (a chessboard distance, in row order,
    so one result on any machine). The alpha is left as it is."""
    alpha = rgba[3::4]
    if alpha.count(0) == 0 or alpha.count(0) == w * h:
        return rgba
    out = bytearray(rgba)
    known = bytearray(1 if a else 0 for a in alpha)
    frontier = [i for i in range(w * h) if not known[i]]
    while frontier:
        filled = []
        for i in frontier:
            y, x = divmod(i, w)
            for ny in (y, y - 1, y + 1):
                if not 0 <= ny < h:
                    continue
                for nx in (x, x - 1, x + 1):
                    if 0 <= nx < w and known[ny * w + nx]:
                        filled.append((i, (ny * w + nx) * 4))
                        break
                else:
                    continue
                break
        for i, src in filled:
            out[i * 4 : i * 4 + 3] = out[src : src + 3]
            known[i] = 1
        # Only a neighbour of what this pass filled can be filled by the next:
        # rescanning every empty texel per pass took a text atlas minutes. The
        # pass reads `known` as it stood, so the order here changes nothing.
        frontier = set()
        for i, _ in filled:
            y, x = divmod(i, w)
            for ny in range(max(0, y - 1), min(h, y + 2)):
                for nx in range(max(0, x - 1), min(w, x + 2)):
                    if not known[ny * w + nx]:
                        frontier.add(ny * w + nx)
    return bytes(out)


def colour_image(rgba):
    """The RGB with the alpha at 255."""
    out = bytearray(rgba)
    out[3::4] = b"\xff" * (len(rgba) // 4)
    return bytes(out)


def alpha_image(rgba):
    """The alpha as an opaque grey image."""
    a = rgba[3::4]
    out = bytearray(len(rgba))
    out[0::4] = a
    out[1::4] = a
    out[2::4] = a
    out[3::4] = b"\xff" * len(a)
    return bytes(out)


def recombine(colour, grey):
    """The colour image's RGB with the grey image's red as alpha."""
    out = bytearray(colour)
    out[3::4] = grey[0::4]
    return bytes(out)


def coverage(rgba):
    """The fraction of texels at or over COVER_AT."""
    a = rgba[3::4]
    return sum(1 for v in a if v >= COVER_AT) / max(1, len(a))


def binarize(rgba, cover):
    """Alpha to 0 or 255, at the threshold whose coverage is nearest `cover`;
    the lowest such threshold on a tie."""
    a = rgba[3::4]
    hist = [0] * 256
    for v in a:
        hist[v] += 1
    n = len(a)
    want = cover * n
    # hist[t:] summed is what a threshold of t keeps.
    best_t, best_d = 256, want
    kept = 0
    for t in range(255, -1, -1):
        kept += hist[t]
        d = abs(kept - want)
        if d <= best_d:
            best_t, best_d = t, d
    table = bytes(255 if v >= best_t else 0 for v in range(256))
    out = bytearray(rgba)
    out[3::4] = a.translate(table)
    return bytes(out)
