"""The toolkit's d3d8_swizzle (src/d3d/d3d8_swizzle.h, xbox_swizzle_rect): the
Morton-like layout the console holds an uncompressed texture in. The game's
D3DX swizzles a DDS on load, so the file's bytes have to go through this
before they hash as the guest's do. Sides are powers of two."""


def _masks(width, height):
    """The address bits of x and of y, interleaved from bit 0: x takes a bit
    while it has one left, then y."""
    mx = my = 0
    bit = mask = 1
    while bit < width or bit < height:
        if bit < width:
            mx |= mask
            mask <<= 1
        if bit < height:
            my |= mask
            mask <<= 1
        bit <<= 1
    return mx, my


def _offsets(n, mask):
    out = []
    off = 0
    for _ in range(n):
        out.append(off)
        # Add one with every bit outside the mask carrying through.
        off = (off - mask) & mask
    return out


def is_pow2(n):
    return n > 0 and n & (n - 1) == 0


def swizzle(src, width, height, bpp):
    """`src` (rows top to bottom, `bpp` bytes a texel) in the guest's layout."""
    if not is_pow2(width) or not is_pow2(height):
        raise ValueError("%dx%d: the swizzle needs powers of two" % (width, height))
    n = width * height * bpp
    if len(src) < n:
        raise ValueError("%d bytes, a %dx%d texture needs %d" % (len(src), width, height, n))
    mx, my = _masks(width, height)
    xs = _offsets(width, mx)
    ys = _offsets(height, my)
    out = bytearray(n)
    for y, oy in enumerate(ys):
        row = y * width * bpp
        for x, ox in enumerate(xs):
            d = (oy + ox) * bpp
            s = row + x * bpp
            out[d : d + bpp] = src[s : s + bpp]
    return bytes(out)
