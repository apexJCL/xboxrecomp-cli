"""xbr enhance textures compare: every upscaler on a few of the game's textures,
side by side (D10). Each goes through the build's own Job, so a cell is what a
pack would hold before the encode. `render` is the function a later preview's
compare panel calls.

The output is made from the owner's game files and stays with the owner.
"""

import hashlib
import html
import json
import os
import time

from .. import host
from . import build, estimate, png
from .upscalers import BUILTINS, UpscalerError, by_name

SCHEMA = 1
DEFAULT_SAMPLES = 4
DEFAULT_CROP = 128
# Decoded per size band to find the busiest: a game's thousands would take
# longer than the compare, and a few show the band as well.
CANDIDATES = 6
GAP = 4
CHECK = 8
CHECK_TONES = (0x66, 0x99)


def band(e):
    """The size band: textures within a power of two of each other."""
    return max(e["width"], e["height"]).bit_length()


def energy(w, h, rgba):
    """Per texel, the green and alpha step to the right and down: where the
    detail is, which is what an upscaler gets wrong."""
    g = rgba[1::4]
    a = rgba[3::4]
    out = [0] * (w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            right = i + 1 if x + 1 < w else i
            down = i + w if y + 1 < h else i
            out[i] = (
                abs(g[i] - g[right])
                + abs(g[i] - g[down])
                + abs(a[i] - a[right])
                + abs(a[i] - a[down])
            )
    return out


def busiest_crop(w, h, rgba, n):
    """(x, y, cw, ch) of the n x n window with the most detail, the first of the
    equal ones, stepping a quarter of the window."""
    cw, ch = min(n, w), min(n, h)
    if cw == w and ch == h:
        return 0, 0, w, h
    e = energy(w, h, rgba)
    step = max(1, min(cw, ch) // 4)
    # A summed-area table: a window's total in four reads.
    sat = [0] * ((w + 1) * (h + 1))
    for y in range(h):
        run = 0
        for x in range(w):
            run += e[y * w + x]
            sat[(y + 1) * (w + 1) + x + 1] = sat[y * (w + 1) + x + 1] + run
    best, at = -1, (0, 0)
    for y in sorted({*range(0, h - ch, step), h - ch}):
        for x in sorted({*range(0, w - cw, step), w - cw}):
            total = (
                sat[(y + ch) * (w + 1) + x + cw]
                - sat[y * (w + 1) + x + cw]
                - sat[(y + ch) * (w + 1) + x]
                + sat[y * (w + 1) + x]
            )
            if total > best:
                best, at = total, (x, y)
    return at[0], at[1], cw, ch


def _read(pairs, feeders, dumps):
    """{hash: (w, h, rgba)} of the pairs whose texels something holds."""
    out = {}
    wanted = {e["hash"]: (e, t) for e, t in pairs}
    for e, _, src in build.sources_of(wanted, feeders, dumps):
        if src is not None:
            out[e["hash"]] = build.decode_source(src)
    return out


class Sample:
    __slots__ = ("entry", "treatment", "image", "crop", "why")

    def __init__(self, entry, treatment, image, crop, why):
        self.entry, self.treatment = entry, treatment
        self.image, self.crop = image, crop
        self.why = why

    def name(self):
        e = self.entry
        return e["origins"][0]["name"] if e.get("origins") else e["hash"]


def _rank(pairs, images):
    """Busiest first, the hash breaking a tie: the same samples on every run."""

    def score(pair):
        h = pair[0]["hash"]
        w, ht, rgba = images[h]
        return (-sum(energy(w, ht, rgba)) / (w * ht), h)

    return sorted((p for p in pairs if p[0]["hash"] in images), key=score)


def pick_samples(pairs, cls, n, picks, preview, crop, feeders, dumps):
    """[Sample] of one class: `--pick`, then the game's preview picks, then the
    busiest texture of each size band, largest band first, then the next busiest
    of each until there are n. A pick counts over n: it was asked for."""
    rows = [p for p in pairs if p[0]["class"] == cls]
    chosen, why = [], {}

    def take(patterns, label):
        for pat in patterns:
            for p in build.filter_only(rows, [pat]):
                if p[0]["hash"] not in why:
                    why[p[0]["hash"]] = label
                    chosen.append(p)

    take(picks, "pick")
    take(preview, "preview")
    bands = {}
    for p in sorted(rows, key=lambda p: p[0]["hash"]):
        bands.setdefault(band(p[0]), []).append(p)
    cands = [p for b in bands.values() for p in b[: max(CANDIDATES, n)]]
    images = _read(chosen + [p for p in cands if p[0]["hash"] not in why], feeders, dumps)
    ranked = {b: _rank(v[: max(CANDIDATES, n)], images) for b, v in bands.items()}
    forced = len([p for p in chosen if p[0]["hash"] in images])
    while forced < n and any(ranked.values()):
        for b in sorted(ranked, reverse=True):
            while ranked[b] and ranked[b][0][0]["hash"] in why:
                ranked[b].pop(0)
            if ranked[b] and forced < n:
                p = ranked[b].pop(0)
                why[p[0]["hash"]] = "busiest of its size band"
                chosen.append(p)
                forced += 1
    out = []
    for e, t in chosen:
        img = images.get(e["hash"])
        if img:
            out.append(Sample(e, t, img, busiest_crop(*img, crop), why[e["hash"]]))
    return out


def columns(names, config):
    """([upscaler], [(name, why it is not here)]): the built-ins, the config's
    own and every preset whose tool is installed; or what --upscaler names."""
    from .upscalers import command

    tables = config.get("upscaler", {})
    if names:
        order = list(names)
    else:
        order = [u.name for u in BUILTINS]
        order += [k for k in sorted(tables) if k not in order]
        order += [r["name"] for r in command.available() if r["found"] and r["name"] not in order]
    ups, missing = [], []
    for name in order:
        try:
            ups.append(by_name(name, tables))
        except UpscalerError as e:
            missing.append((name, str(e)))
    return ups, missing


def _table_of(up, config):
    if up.kind == "builtin":
        return build.params_of(config, up)
    return config.get("upscaler", {}).get(up.name, {})


def _pixel_sha(rgba):
    # Pixels and not the PNG's bytes: those follow the host's zlib.
    return hashlib.sha256(rgba).hexdigest()


def run_one(up, samples, scale_of, config):
    """One pass of an upscaler over the samples: ({hash: (w, h, rgba)}, seconds
    in the upscaler itself, source texels, {hash: why}). A command's seconds are
    the subprocess's, start-up included, which is what a build pays."""
    outs, why = {}, {}
    work = []
    for s in samples:
        t = dict(s.treatment, scale=scale_of(s), upscaler=up.name)
        w, h, rgba = s.image
        if up.native_for(t["scale"]) is None:
            why[s.entry["hash"]] = "cannot reach %dx" % t["scale"]
            continue
        job = build.make_job(up, w, h, rgba, t, s.entry)
        work.append((s, job, job.images()))
    table = _table_of(up, config)
    seconds, texels = 0.0, 0
    # One call per native scale, as the build batches a command.
    for nat in sorted({job.native for _, job, _ in work}):
        group = [x for x in work if x[1].native == nat]
        flat = [i for _, _, imgs in group for i in imgs]
        t0 = time.perf_counter()
        results = iter(up.upscale(flat, nat, table))
        seconds += time.perf_counter() - t0
        for s, job, imgs in group:
            outs[s.entry["hash"]] = job.finish([next(results) for _ in imgs])
            texels += job.w * job.h
    return outs, seconds, texels, why


def _column(up, samples, scale_of, config, repeat, full_dir):
    """The row compare.json gives an upscaler, and its images."""
    row = {
        "name": up.name,
        "kind": up.kind,
        "identity": {},
        "licence": up.licence,
        "homepage": up.homepage,
        "flag": up.flag,
        "deterministic": up.deterministic,
        "reproducible_scope": up.reproducible_scope,
        "error": "",
        "not_reached": {},
        "pixels": {},
        "s_per_mtexel": None,
        "repeats": repeat,
        "repeatable": None,
    }
    images = {}
    runs = []
    try:
        for _ in range(max(1, repeat)):
            outs, seconds, texels, why = run_one(up, samples, scale_of, config)
            images = outs
            runs.append(({h: _pixel_sha(o[2]) for h, o in outs.items()}, seconds, texels))
            row["not_reached"] = why
    except UpscalerError as e:
        row["error"] = str(e)
        return row, {}
    row["identity"] = up.identity()
    if getattr(up, "warning", ""):
        host.say(up.warning)
    row["reproducible_scope"] = up.reproducible_scope
    row["pixels"] = runs[0][0]
    row["repeatable"] = all(r[0] == runs[0][0] for r in runs) if len(runs) > 1 else None
    total = sum(r[1] for r in runs)
    mt = sum(r[2] for r in runs) / 1e6
    row["s_per_mtexel"] = round(total / mt, 3) if mt else None
    os.makedirs(os.path.join(full_dir, up.name), exist_ok=True)
    for h, (w, ht, rgba) in images.items():
        png.write(os.path.join(full_dir, up.name, h + ".png"), w, ht, rgba)
    return row, images


def _checker_row(width, y):
    """One row of the board, its 3-byte pixels."""
    start = (y // CHECK) & 1
    out = bytearray()
    x = 0
    while x < width:
        n = min(CHECK, width - x)
        tone = CHECK_TONES[(start + x // CHECK) & 1]
        out += bytes((tone,)) * (n * 3)
        x += n
    return bytes(out)


def _over_board(w, h, rgba):
    """(w, h, RGBA) opaque, over the checkerboard: alpha is what a sprite is."""
    out = bytearray(w * h * 4)
    out[3::4] = b"\xff" * (w * h)
    for y in range(h):
        board = _checker_row(w, y)
        row = rgba[y * w * 4 : (y + 1) * w * 4]
        for x in range(w):
            o = x * 4
            a = row[o + 3]
            d = (y * w + x) * 4
            for c in range(3):
                b = board[x * 3 + c]
                out[d + c] = (row[o + c] * a + b * (255 - a) + 127) // 255
    return bytes(out)


def grid_png(path, rows, cols, cell):
    """`rows` of `cols` images each, (w, h, rgba) or None, centred in square
    cells over a grey sheet: the file a reader looks at first."""
    gw = cols * cell + (cols + 1) * GAP
    gh = len(rows) * cell + (len(rows) + 1) * GAP
    sheet = bytearray(b"\x20\x20\x20\xff" * (gw * gh))
    for r, row in enumerate(rows):
        for c, img in enumerate(row):
            if img is None:
                continue
            w, h, rgba = img
            flat = _over_board(w, h, rgba)
            ox = GAP + c * (cell + GAP) + (cell - w) // 2
            oy = GAP + r * (cell + GAP) + (cell - h) // 2
            for y in range(h):
                d = ((oy + y) * gw + ox) * 4
                sheet[d : d + w * 4] = flat[y * w * 4 : (y + 1) * w * 4]
    png.write(path, gw, gh, bytes(sheet), alpha=False)
    return gw, gh


def _crop_at(img, crop, scale):
    from .edges import crop as cut

    w, h, rgba = img
    x, y, cw, ch = crop
    return cw * scale, ch * scale, cut(w, h, rgba, x * scale, y * scale, cw * scale, ch * scale)


def _index(doc):
    """A page with no script and no fetch: it opens from the folder."""
    q = html.escape
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Texture compare</title>",
        "<style>body{font:14px sans-serif;background:#181818;color:#ddd}"
        "img{max-width:100%}th,td{padding:2px 8px;text-align:left}a{color:#8cf}</style>",
        "</head><body><h1>Texture compare</h1>",
        "<p>Made from your own game files: keep this folder private.</p>",
        "<table><tr><th>Upscaler</th><th>s/Mtexel</th><th>Repeats equal</th>"
        "<th>Scope</th><th>Licence</th><th>Note</th></tr>",
    ]
    for c in doc["columns"]:
        note = c["error"] or c["flag"]
        parts.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                q(c["name"]),
                c["s_per_mtexel"] if c["s_per_mtexel"] is not None else "-",
                {True: "yes", False: "NO", None: "-"}[c["repeatable"]],
                q(c["reproducible_scope"]),
                q(c["licence"]),
                q(note),
            )
        )
    parts.append("</table>")
    for m in doc["not_run"]:
        parts.append("<p>%s: %s</p>" % (q(m["name"]), q(m["why"])))
    names = [c["name"] for c in doc["columns"]]
    for cls, g in sorted(doc["classes"].items()):
        parts.append("<h2>%s</h2><p>Columns, left to right: %s</p>" % (q(cls), q(", ".join(names))))
        parts.append("<img src='%s' alt='%s'><table>" % (q(g["grid"]), q(cls)))
        for s in g["samples"]:
            links = " ".join(
                "<a href='full/%s/%s.png'>%s</a>" % (q(n), q(s["hash"]), q(n))
                for n in names
                if s["hash"] in next(c for c in doc["columns"] if c["name"] == n)["pixels"]
            )
            parts.append(
                "<tr><td>%s</td><td>%s</td><td>%dx%d at %dx</td><td>%s</td><td>%s</td></tr>"
                % (
                    q(s["hash"]),
                    q(s["name"]),
                    s["size"][0],
                    s["size"][1],
                    s["scale"],
                    q(s["why"]),
                    links,
                )
            )
        parts.append("</table>")
    parts.append("</body></html>\n")
    return "".join(parts)


def render(per_class, ups, missing, config, out_dir, scale=None, crop=DEFAULT_CROP, repeat=2):
    """Writes the folder and returns compare.json's document. `per_class` maps a
    class to its [Sample]. Every upscaler runs once over all the samples of all
    classes, so a batch tool starts once, as it would in a build."""

    def scale_of(s):
        return scale or s.treatment["scale"]

    samples = [s for rows in per_class.values() for s in rows]
    full_dir = os.path.join(out_dir, "full")
    os.makedirs(full_dir, exist_ok=True)
    doc = {
        "schema": SCHEMA,
        "crop": crop,
        "repeat": repeat,
        "columns": [],
        "not_run": [{"name": n, "why": w} for n, w in missing],
        "classes": {},
    }
    made = {}
    for up in ups:
        row, images = _column(up, samples, scale_of, config, repeat, full_dir)
        doc["columns"].append(row)
        made[up.name] = images
    for cls, rows in sorted(per_class.items()):
        if not rows:
            continue
        cell = max(s.crop[j] * scale_of(s) for s in rows for j in (2, 3))
        grid = []
        for s in rows:
            at = scale_of(s)
            line = []
            for up in ups:
                img = made[up.name].get(s.entry["hash"])
                line.append(_crop_at(img, s.crop, at) if img else None)
            grid.append(line)
        name = "grid_%s.png" % cls
        gw, gh = grid_png(os.path.join(out_dir, name), grid, len(ups), cell)
        doc["classes"][cls] = {
            "grid": name,
            "grid_size": [gw, gh],
            "cell": cell,
            "samples": [
                {
                    "hash": s.entry["hash"],
                    "name": s.name(),
                    "size": [s.image[0], s.image[1]],
                    "scale": scale_of(s),
                    "crop": list(s.crop),
                    "why": s.why,
                }
                for s in rows
            ],
        }
    with open(os.path.join(out_dir, "compare.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_index(doc))
    return doc


def save_timings(doc, logs):
    """The rates the next estimate reads: externals with their start-up."""
    for c in doc["columns"]:
        if c["s_per_mtexel"]:
            estimate.save_rate(logs, c["name"], c["s_per_mtexel"])


def treated_rows(entries, config, cfg):
    return estimate.treated(entries, "all", config, cfg)


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")
