"""The font atlas: rows of proportionally packed glyphs, re-rendered from
a font file onto the stock layout at the pack's scale.

The game lays text out from its own metrics (a table the HUD spike did not
locate) and keeps the stock atlas in guest memory, so a replacement only
has to put each glyph's ink where the stock glyph's ink is. The atlas is
not a grid: each text row is a band of ink rows (BLiNX 2's Latin atlas has
ten, 1-3 px apart) holding the glyphs of one `font_rows` string packed
left to right with 1-3 px gaps. The fit finds the bands from the alpha
projection, splits each band into column runs, groups the runs into the
string's glyphs (a glyph like `"` is two runs, touching accents can merge
two) by the dynamic programme that best matches the font's ink widths,
and measures each glyph's ink box at alpha >= INK. Per band the baseline
is the most common bottom edge of its capitals and digits (else of every
glyph) and the cap height the most common ink height of its unaccented
capitals (else the atlas-wide one). Each glyph is drawn at the size whose
cap height is the stock cap height times the scale, condensed or widened
then condensed or widened, and shortened or lengthened, so its ink box
is the stock ink box times the scale (an accented capital's accent thus
sits as low as the stock one's, inside the band), white with the
coverage in alpha, which the game tints with the vertex colour as it does
the stock glyphs. A glyph the font lacks, one written as U+FFFD in
`font_rows` (an icon the game draws for that code, kept as it is), and a
run the fit cannot match keep their upscaled stock pixels.
"""

from collections import Counter

INK = 64
KEEP = "�"
# A glyph whose ink the font would have to stretch more than this much
# is a mismatched run, not a glyph: it keeps the stock pixels. The widest
# legitimate stretch is a hairline: the stock underscore is 2 px thick,
# a narrow font's 1.
STRETCH = (0.4, 2.5)
# The share of a band, from its top, that accents and dots live in: the
# runs that separate glyphs are taken below it.
ACCENT = 0.4

CAPS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ÆÐØÞ")
BASE = CAPS | set("ÀÁÂÃÄÅÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝŸ")


class AtlasError(Exception):
    pass


def _alpha_rows(w, h, rgba):
    rows = []
    for y in range(h):
        base = y * w * 4 + 3
        rows.append(any(rgba[base + x * 4] >= INK for x in range(w)))
    return rows


def bands(w, h, rgba):
    """[(top, bottom)] of the maximal ink row runs, bottom exclusive."""
    rows = _alpha_rows(w, h, rgba)
    out = []
    y = 0
    while y < h:
        if rows[y]:
            top = y
            while y < h and rows[y]:
                y += 1
            out.append((top, y))
        else:
            y += 1
    return out


def column_runs(w, rgba, y0, y1, x0=0, x1=None):
    """[(left, right)] of the maximal ink column runs over rows y0..y1."""
    x1 = w if x1 is None else x1
    cols = []
    for x in range(x0, x1):
        cols.append(any(rgba[(y * w + x) * 4 + 3] >= INK for y in range(y0, y1)))
    out = []
    i = 0
    while i < len(cols):
        if cols[i]:
            s = i
            while i < len(cols) and cols[i]:
                i += 1
            out.append((x0 + s, x0 + i))
        else:
            i += 1
    return out


def _ink_count(w, rgba, x, y0, y1):
    return sum(1 for y in range(y0, y1) if rgba[(y * w + x) * 4 + 3] >= INK)


def glyph_runs(w, rgba, top, bottom):
    """The runs that separate a band's glyphs: its column runs, each one
    that holds several runs below the accent zone (touching accents or
    dots, a flag reaching into the next glyph) split at the column with
    the least ink between them. A glyph like `"` stays two runs and a
    T's bar one, which the grouping sorts out."""
    y_fine = top + int(round((bottom - top) * ACCENT))
    out = []
    for s, t in column_runs(w, rgba, top, bottom):
        fine = column_runs(w, rgba, y_fine, bottom, s, t)
        cuts = []
        for (_a, fe), (fs, _b) in zip(fine, fine[1:]):
            cuts.append(
                min(range(fe, fs + 1), key=lambda x: (_ink_count(w, rgba, x, top, bottom), x))
            )
        for a, b in zip([s] + cuts, cuts + [t]):
            out.append((a, b))
    return out


def ink_box(w, rgba, x0, x1, y0, y1):
    """(left, top, right, bottom) of the alpha >= INK pixels in the box,
    absolute, right and bottom exclusive; None when it holds none."""
    left = top = None
    right = bottom = 0
    for y in range(y0, y1):
        row = y * w
        for x in range(x0, x1):
            if rgba[(row + x) * 4 + 3] >= INK:
                if left is None or x < left:
                    left = x
                if top is None:
                    top = y
                if x + 1 > right:
                    right = x + 1
                bottom = y + 1
    return None if left is None else (left, top, right, bottom)


def ink_color(w, h, rgba):
    """The mean RGB of the opaque pixels (white in the stock atlas)."""
    n = r = g = b = 0
    for i in range(w * h):
        if rgba[i * 4 + 3] >= 128:
            r += rgba[i * 4]
            g += rgba[i * 4 + 1]
            b += rgba[i * 4 + 2]
            n += 1
    if not n:
        return (255, 255, 255)
    return (round(r / n), round(g / n), round(b / n))


def _mode(values):
    return Counter(values).most_common(1)[0][0] if values else None


def group_runs(runs, expected):
    """Partition the runs, in order, into len(expected) consecutive groups,
    one per glyph, minimising the sum over glyphs of |group ink width -
    expected width|. A glyph may get no run at all (two glyphs whose ink
    touches) at the cost of its expected width plus the widest expected
    width: a font whose narrow glyph is narrower still must not make
    swallowing it into a neighbour the cheaper fit (PT Sans Narrow's '['
    went to its '\\' at the width alone). Returns per glyph (left, right)
    or None."""
    n, m = len(runs), len(expected)
    if not m:
        return []
    skip = max(expected)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for j in range(1, m + 1):
        ew = expected[j - 1]
        for i in range(n + 1):
            best, bk = inf, i
            for k in range(i + 1):
                c = cost[k][j - 1]
                if c == inf:
                    continue
                c += ew + skip if k == i else abs((runs[i - 1][1] - runs[k][0]) - ew)
                if c < best:
                    best, bk = c, k
            cost[i][j], back[i][j] = best, bk
    if cost[n][m] == inf:
        return None
    out = []
    i = n
    for j in range(m, 0, -1):
        k = back[i][j]
        out.append(None if k == i else (runs[k][0], runs[i - 1][1]))
        i = k
    out.reverse()
    return out


def _coverage_box(cov, width, height):
    left = top = None
    right = bottom = 0
    for y in range(height):
        base = y * width
        for x in range(width):
            if cov[base + x] >= INK:
                if left is None or x < left:
                    left = x
                if top is None:
                    top = y
                if x + 1 > right:
                    right = x + 1
                bottom = y + 1
    return None if left is None else (left, top, right, bottom)


class Glyph:
    """One glyph of the fit: `char`, its slot (the band's rows between the
    midpoints to its neighbours, in stock pixels) and its stock ink box."""

    __slots__ = ("char", "slot", "box", "band")

    def __init__(self, char, slot, box, band):
        self.char = char
        self.slot = slot
        self.box = box
        self.band = band


def measure(w, h, rgba, rows, font):
    """The fit of `rows` (one string per ink band, spaces ignored) onto the
    stock atlas: (glyphs, metrics) where metrics[band] is (baseline,
    cap_height) in stock pixels and glyphs are in atlas order. The font's
    ink widths guide the grouping of runs into glyphs."""
    bs = bands(w, h, rgba)
    if len(bs) != len(rows):
        raise AtlasError(
            "the atlas has %d ink bands (rows %s), font_rows names %d"
            % (len(bs), ", ".join("%d-%d" % (t, b - 1) for t, b in bs), len(rows))
        )
    # The vertical slot of each band: up to the midpoint of the gaps to
    # its neighbours, so a rendered descender or accent has room.
    vslots = []
    for i, (t, b) in enumerate(bs):
        vt = 0 if i == 0 else (bs[i - 1][1] + t) // 2
        vb = h if i == len(bs) - 1 else (b + bs[i + 1][0]) // 2
        vslots.append((vt, vb))
    cap_units = font.cap_height()
    per_band = []
    heights = []
    for i, (t, b) in enumerate(bs):
        chars = [c for c in rows[i] if not c.isspace()]
        runs = glyph_runs(w, rgba, t, b)
        per_band.append((chars, runs))
        heights.append(b - t)
    # A first cap height, from the bands' capital runs' heights, to turn
    # the font's widths into stock pixels for the grouping; the one the
    # fit uses comes from the grouped glyphs below.
    cap_guess = []
    for (chars, runs), (t, b) in zip(per_band, bs):
        if len(runs) == len(chars):
            for c, (s, e) in zip(chars, runs):
                if c in CAPS:
                    box = ink_box(w, rgba, s, e, t, b)
                    if box:
                        cap_guess.append(box[3] - box[1])
    rough = _mode(cap_guess) or max(1, int(round(_mode(heights) * 0.8)))
    k = rough / cap_units
    glyphs = []
    band_caps = []
    for i in range(len(bs)):
        chars, runs = per_band[i]
        vt, vb = vslots[i]
        widths = []
        for c in chars:
            gid = font.glyph_index(ord(c)) if c != KEEP else 0
            bb = font.bbox(gid) if gid else None
            widths.append((bb[2] - bb[0]) * k if bb else None)
        known = sorted(x for x in widths if x is not None)
        median = known[len(known) // 2] if known else rough * 0.6
        expected = [x if x is not None else median for x in widths]
        groups = group_runs(runs, expected) or [None] * len(chars)
        # Slots between the midpoints of the gaps between grouped glyphs.
        placed = [(c, g) for c, g in zip(chars, groups) if g]
        band_glyphs = []
        j = 0
        for c, g in zip(chars, groups):
            if not g:
                # No run of its own: listed without a slot, so the render
                # names it (its ink is in a neighbour's slot).
                band_glyphs.append(Glyph(c, None, None, i))
                continue
            gl, gr = g
            sl = 0 if j == 0 else (placed[j - 1][1][1] + gl) // 2
            sr = w if j == len(placed) - 1 else (gr + placed[j + 1][1][0]) // 2
            box = ink_box(w, rgba, sl, sr, vt, vb)
            band_glyphs.append(Glyph(c, (sl, vt, sr, vb), box, i))
            j += 1
        band_caps.append([g.box[3] - g.box[1] for g in band_glyphs if g.char in CAPS and g.box])
        glyphs.append(band_glyphs)
    global_cap = _mode([c for caps in band_caps for c in caps]) or rough
    metrics = []
    for i, (_t, b) in enumerate(bs):
        bases = [g.box[3] for g in glyphs[i] if g.char in BASE and g.box]
        if bases:
            baseline = _mode(bases)
        else:
            bottoms = [g.box[3] for g in glyphs[i] if g.box]
            baseline = _mode(bottoms) if bottoms else b
        metrics.append((baseline, _mode(band_caps[i]) or global_cap))
    return [g for band in glyphs for g in band], metrics


def render(stock, rows, font, scale, fallback):
    """The atlas at `scale`: stock is (w, h, rgba) of the stock atlas,
    fallback the upscaled stock rgba (the same size as the result) that a
    glyph keeps when the font has no glyph for it. Returns (rgba,
    rendered, notes): the number of glyphs drawn from the font and what
    the fit left as it was."""
    w, h, rgba = stock
    glyphs, metrics = measure(w, h, rgba, rows, font)
    color = ink_color(w, h, rgba)
    out = bytearray(fallback)
    ow = w * scale
    cap_units = font.cap_height()
    rendered = 0
    notes = []
    for g in glyphs:
        if g.char == KEEP:
            continue
        if g.slot is None:
            notes.append("%r: no ink run of its own in the stock atlas, not drawn" % g.char)
            continue
        if g.box is None:
            continue
        gid = font.glyph_index(ord(g.char))
        if not gid or not font.outline(gid):
            notes.append("%r: not in the font, stock pixels kept" % g.char)
            continue
        baseline, cap = metrics[g.band]
        sl, vt, sr, vb = g.slot
        px = cap * scale * font.upem / cap_units
        # The glyph at the band's cap height, then condensed or widened and
        # shortened or lengthened to the stock ink box (from its outline
        # box, which is exact; the coverage box only places the result).
        k = px / font.upem
        bb = font.bbox(gid)
        fw, fh = (bb[2] - bb[0]) * k, (bb[3] - bb[1]) * k
        if fw <= 0 or fh <= 0:
            notes.append("%r: the font glyph is blank, stock pixels kept" % g.char)
            continue
        want_w = (g.box[2] - g.box[0]) * scale
        want_h = (g.box[3] - g.box[1]) * scale
        rx, ry = want_w / fw, want_h / fh
        if not (STRETCH[0] <= rx <= STRETCH[1] and STRETCH[0] <= ry <= STRETCH[1]):
            notes.append(
                "%r: stock ink %dx%d px, the font's %dx%d: not matched, stock pixels kept"
                % (g.char, g.box[2] - g.box[0], g.box[3] - g.box[1], fw / scale, fh / scale)
            )
            continue
        # Scratch: the slot at scale plus a margin for the ink that lands
        # outside it before the shift.
        margin = int(cap * scale * 1.5) + 2
        sw = (sr - sl) * scale + 2 * margin
        sh = (vb - vt) * scale + 2 * margin
        oy = margin + (baseline - vt) * scale
        cov = font.render(gid, px * rx, px * ry, margin, oy, sw, sh)
        cb = _coverage_box(cov, sw, sh)
        if cb is None:
            notes.append("%r: the font glyph is blank, stock pixels kept" % g.char)
            continue
        dx = (g.box[0] - sl) * scale + margin - cb[0]
        dy = (g.box[1] - vt) * scale + margin - cb[1]
        for y in range((vb - vt) * scale):
            sy = y + margin - dy
            orow = (vt * scale + y) * ow
            for x in range((sr - sl) * scale):
                sx = x + margin - dx
                v = cov[sy * sw + sx] if 0 <= sx < sw and 0 <= sy < sh else 0
                o = (orow + sl * scale + x) * 4
                out[o] = color[0]
                out[o + 1] = color[1]
                out[o + 2] = color[2]
                out[o + 3] = v
        rendered += 1
    return bytes(out), rendered, notes
