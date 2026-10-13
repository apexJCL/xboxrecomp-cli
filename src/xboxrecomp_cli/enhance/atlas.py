"""The font atlas: rows of proportionally packed glyphs, re-rendered from
a font file onto the stock layout at the pack's scale.

The game lays text out from its own metrics (a table the HUD spike did not
locate) and keeps the stock atlas in guest memory, so a replacement only
has to keep each glyph's ink inside the texels its quad samples. The atlas
is not a grid: each text row is a band of ink rows (BLiNX 2's Latin atlas
has ten, 1-3 px apart) holding the glyphs of one `font_rows` string packed
left to right with 1-3 px gaps. The fit finds the bands from the alpha
projection, splits each band into column runs, groups the runs into the
string's glyphs (a glyph like `"` is two runs, touching accents can merge
two) by the dynamic programme that best matches the font's ink widths,
and measures each glyph's ink box at alpha >= INK. Per band the baseline
is the most common bottom edge of its capitals and digits (else of every
glyph) and the cap height the most common ink height of its unaccented
capitals (else the atlas-wide one).

Every glyph is drawn at one scale on both axes, the one that makes the
font's cap height the stock cap height times the scale, so the stems,
counters and curves are the font's own and the weight is even. Its
outline box is centred across on the stock ink box and its baseline is
the band's. A glyph that would overrun the region its quad samples (the
stock ink box grown by `font_margin`, or the slot between its
neighbours) is shifted inside it where there is slack, else shrunk by one
factor on both axes, about the stock centre and the baseline: never
enlarged. Descenders and overshoots keep the
baseline; only a mark the font draws clear of it (a dash, an underscore,
a quote) may move up or down to fit. A shrink beyond FIT is named.
Two options trade some of that evenness for the stock look: `stretch`
widens or narrows each glyph towards its stock ink width (the game
advances by the slot, so ink narrower than the stock opens the gaps),
and the `full` glyphs (brackets) span their stock ink box top to bottom,
so they enclose the text as the stock ones do. The
ink is white with the coverage in alpha, which the game tints with the
vertex colour as it does the stock glyphs. A glyph the font lacks, one
written as U+FFFD in `font_rows` (an icon the game draws for that code,
kept as it is), and a run the fit cannot match keep their upscaled stock
pixels.
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
# The largest shrink a glyph gets silently to stay inside the region its
# quad samples: 8% of a stem is under a fifth of a guest pixel. A larger
# one is still applied, so no glyph overruns, but the build names it.
FIT = 0.08

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


def _fit(lo, hi, a0, a1):
    """The shift that moves [lo, hi] inside [a0, a1], 0 when it is."""
    if lo < a0:
        return a0 - lo
    if hi > a1:
        return a1 - hi
    return 0.0


def render(stock, rows, font, scale, fallback, margin=None, stretch=0, full=""):
    """The atlas at `scale`: stock is (w, h, rgba) of the stock atlas,
    fallback the upscaled stock rgba (the same size as the result) that a
    glyph keeps when the font has no glyph for it. `margin` is [left,
    top, right, bottom] in stock atlas texels: how far past its stock
    ink box a glyph may draw (None: its whole slot). `stretch` is the most,
    in percent, a glyph is widened or narrowed towards its stock ink width,
    and `full` the glyphs drawn at their stock ink box's height and place.
    Returns (rgba, rendered, notes): the number of glyphs drawn from the
    font, then a line on the fit and what it left as it was or shrank by
    more than FIT."""
    w, h, rgba = stock
    glyphs, metrics = measure(w, h, rgba, rows, font)
    color = ink_color(w, h, rgba)
    out = bytearray(fallback)
    ow = w * scale
    cap_units = font.cap_height()
    rendered = 0
    shrunk = 0
    limited = 0
    notes = []
    where = "slot" if margin is None else "stock ink box and margin"
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
        bl, bt, br, bb_ = g.box
        # One scale on both axes, from the band's cap height: the glyph
        # keeps the font's proportions, so every stem is the font's own.
        k = cap * scale / cap_units
        bb = font.bbox(gid)
        fw, fh = (bb[2] - bb[0]) * k, (bb[3] - bb[1]) * k
        if fw <= 0 or fh <= 0:
            notes.append("%r: the font glyph is blank, stock pixels kept" % g.char)
            continue
        # A run whose ink the font would have to stretch this much is not
        # the glyph: the grouping went wrong, or the shapes differ.
        rx = (br - bl) * scale / fw
        ry = (bb_ - bt) * scale / fh
        if not (STRETCH[0] <= rx <= STRETCH[1] and STRETCH[0] <= ry <= STRETCH[1]):
            notes.append(
                "%r: stock ink %dx%d px, the font's %dx%d: not matched, stock pixels kept"
                % (g.char, br - bl, bb_ - bt, fw / scale, fh / scale)
            )
            continue
        # The game advances by the slot, so a glyph narrower or wider than
        # the stock one changes the gaps to its neighbours: it is widened or
        # narrowed towards the stock ink width, by `stretch` at most, which
        # keeps the stems within that much of each other.
        sx = 1.0
        if stretch:
            sx = min(max(rx, 1 - stretch / 100), 1 + stretch / 100)
            if abs(sx - rx) > 1e-9:
                limited += 1
        kx, ky = k * sx, k
        # A bracket spans the stock ink box: the typefaces disagree on how
        # far one reaches above the caps and below the baseline, and one
        # taller than the stock sits low against the text it encloses.
        tall = g.char in full
        if tall:
            ky = (bb_ - bt) * scale / (bb[3] - bb[1])
        fw, fh = (bb[2] - bb[0]) * kx, (bb[3] - bb[1]) * ky
        # The allowed box, in pack pixels: what the game's quad samples,
        # never past the slot (a neighbour's ink).
        if margin is None:
            a = (sl, vt, sr, vb)
        else:
            a = (
                max(sl, bl - margin[0]),
                max(vt, bt - margin[1]),
                min(sr, br + margin[2]),
                min(vb, bb_ + margin[3]),
            )
        a0, at, a1, ab = (v * scale for v in a)
        # The outline box centred across on the stock ink box, the font's
        # baseline on the band's.
        cx = (bl + br) * scale / 2
        base = baseline * scale
        # Anchored on the baseline unless the font draws the glyph clear of
        # it (a dash, a quote, an underscore), which may move up or down to
        # fit. The stock ink cannot tell: Arial's descenders and overshoots
        # end off the baseline too, and moving those lifts `g` off the line.
        floats = bb[1] > 0 or bb[3] <= 0
        # The shrink that fits it, about the stock centre across and the
        # baseline down; a shift alone does when there is slack.
        f = min(1.0, (a1 - a0) / fw)
        if floats or tall:
            f = min(f, (ab - at) / fh)
        else:
            above, below = bb[3] * ky, -bb[1] * ky
            if above > 0:
                f = min(f, (base - at) / above)
            if below > 0:
                f = min(f, (ab - base) / below)
        f = max(f, 0.0)
        kxf, kyf = kx * f, ky * f
        x0 = cx - fw * f / 2
        x0 += _fit(x0, x0 + fw * f, a0, a1)
        if tall:
            top = (bt + bb_) * scale / 2 - fh * f / 2
        else:
            top = base - bb[3] * kyf
        if floats:
            top += _fit(top, top + fh * f, at, ab)
        if f < 1.0 - 1e-9:
            if f < 1.0 - FIT:
                notes.append(
                    "%r: shrunk %.2f to stay in its %s (more than %d%%: it is thinner and shorter)"
                    % (g.char, f, where, round(FIT * 100))
                )
            else:
                shrunk += 1
        # Straight into the slot at scale: the origin is a float, so the
        # placement is exact and the same on every host.
        sw, sh = (sr - sl) * scale, (vb - vt) * scale
        ox = x0 - bb[0] * kxf - sl * scale
        oy = top + bb[3] * kyf - vt * scale
        cov = font.render(gid, kxf * font.upem, kyf * font.upem, ox, oy, sw, sh)
        for y in range(sh):
            orow = (vt * scale + y) * ow + sl * scale
            for x in range(sw):
                o = (orow + x) * 4
                out[o] = color[0]
                out[o + 1] = color[1]
                out[o + 2] = color[2]
                out[o + 3] = cov[y * sw + x]
        rendered += 1
    caps = sorted({cap for _b, cap in metrics})
    capstr = str(caps[0]) if len(caps) == 1 else "%d-%d" % (caps[0], caps[-1])
    notes.insert(
        0,
        "font: cap %s px, one proportional scale; %d glyphs shrunk within %d%% to fit their %s"
        % (capstr, shrunk, round(FIT * 100), where),
    )
    if stretch:
        notes.insert(
            1,
            "font: each glyph widened or narrowed towards its stock ink width; %d stopped at %d%%"
            % (limited, stretch),
        )
    return bytes(out), rendered, notes
