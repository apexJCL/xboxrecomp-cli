"""The texture inventory (D1): what the feeders read and what the runtime
dumped, joined on the hash the runtime's pack looks up.

An archive entry gives a name and the bytes; the dump gives the authoritative
hash, the runtime's facts and the texels. Both: one `joined` entry. Archive
only: `not seen`, unused in the runs made or a hash the feeder got wrong, both
are listed. Dump only: `unnamed`, and still packable from the dumped texels,
which is all a game with no reader has.

The feeder reproduces the guest's bytes: DXT as the file's blocks, an
uncompressed DDS through the swizzle port (the game's D3DX swizzles it on
load), XPR0 data as it is (already in GPU layout). No format change and no
generated levels: a file with one level stays one level. The runtime hashes
only the levels it binds (it clamps to MAX_LOD + 1, and a no-mips run drops the
chain), so a mipped file has one hash per level prefix and the inventory keeps
those the dump confirms. A palettized texture folds the palette the frame
bound into its hash, so only the dump can name it.

Where the load changes the bytes, the file's hash is a guess: `not seen` proves
nothing and an entry built on it would never match. A DXT file whose sides are
not powers of two is one case, on every game: the GPU takes none, so the load
must resize it. A game's [enhance.textures] reencodes_small_levels names the
other, a DDS chain with a level under 4x4: its load re-encodes those levels
(measured on one game, where the decoded levels from 4x4 up agree with the file
within the decoders' rounding, those below differ by up to 6, and no such file's
bytes ever hash to a dumped texture; that game has no one-level file with a side
under 4, so none is flagged). A dump still names such a file, by comparing one
small decoded level and then level 0 (`by: content`), and the dump's hash is
then the real one.

inventory.json holds names, hashes, sizes, formats, classes, facts and use
counts, never a texel.
"""

import json
import os
import struct

from . import classes, dds, edges, swizzle, texhash, traced, xpr
from .sources import DumpSource, HookArchive, IpkSource, LooseSource, SourceError
from .sources.dump import hash_text

SCHEMA = 1
JOINED, NOT_SEEN, UNNAMED = "joined", "not seen", "unnamed"
FILE_NAME = "inventory.json"
# The facts a sidecar hands to the entry as they are. Dumps older than the
# draw facts have no `bump`, `rt` or `drawn_2d`, which reads as unknown.
SIDECAR_KEYS = (
    "format",
    "layout",
    "pitch",
    "wrap_u",
    "wrap_v",
    "min_filter",
    "mag_filter",
    "lod_min",
    "lod_max",
    "cube",
    "palette",
    "palette_len",
    "rt",
    "bump",
    "drawn_2d",
)

DDPF_ALPHA = 0x2
DDPF_PALETTE = 0x20
DDPF_LUMINANCE = 0x20000
DDPF_BUMPDUDV = 0x80000
DDSCAPS2_CUBEMAP = 0x200
# The level compared by content first: the largest with at most this many
# texels, cheap to decode in pure Python and enough to rule most textures out.
PROBE_TEXELS = 256
# Two DXT decoders agree within this per channel: rounding of the interpolants.
PROBE_TOLERANCE = 1

# (bits, r, g, b, a): the masks of the uncompressed formats a Direct3D 8
# title's DDS holds, and the guest's name for each.
DDS_MASKS = {
    (32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000): "A8R8G8B8",
    (32, 0xFF0000, 0xFF00, 0xFF, 0): "X8R8G8B8",
    (16, 0xF800, 0x7E0, 0x1F, 0): "R5G6B5",
    (16, 0x7C00, 0x3E0, 0x1F, 0x8000): "A1R5G5B5",
    (16, 0x7C00, 0x3E0, 0x1F, 0): "X1R5G5B5",
    (16, 0xF00, 0xF0, 0xF, 0xF000): "A4R4G4B4",
    (8, 0xFF, 0, 0, 0): "L8",
    (8, 0, 0, 0, 0xFF): "A8",
    (16, 0xFF, 0, 0, 0xFF00): "A8L8",
    (16, 0xFF, 0xFF00, 0, 0): "G8B8",
}


class Candidate:
    """One texture a feeder found: its name and, per level prefix, the hash
    the guest's bytes would have. `hashes` is [(levels, hash text)], empty with
    `note` saying why when this feeder cannot reproduce the guest's bytes."""

    __slots__ = ("source", "name", "entry", "width", "height", "levels", "format", "layout")
    __slots__ += ("hashes", "note", "guess", "probe", "top")

    def __init__(self, source, name, entry=""):
        self.source = source
        self.name = name
        self.entry = entry
        self.width = self.height = 0
        self.levels = 1
        self.format = ""
        self.layout = ""
        self.hashes = []
        self.note = ""
        # The load re-encodes a level, so `hashes` are not the guest's;
        # `probe` is (level, its block bytes) to name it by content instead,
        # and `top` level 0's blocks, which have the last word.
        self.guess = False
        self.probe = None
        self.top = None


def _prefix_hashes(data, extents):
    """[(levels, hash text)], one per prefix of `extents` (bytes per level)."""
    lengths = []
    n = 0
    for x in extents:
        n += x
        lengths.append(n)
    hs = texhash.tex_hash_prefixes(data, lengths)
    return [(i + 1, hash_text(h)) for i, h in enumerate(hs)]


def _level_sizes(w, h, levels):
    out = []
    for _ in range(levels):
        out.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return out


def dds_format(data):
    """(format name, bytes a texel), a DXT's bytes a texel 0, or (None, why)."""
    pf_flags, fourcc, bits = struct.unpack_from("<I4sI", data, 80)
    if pf_flags & 0x4:
        if fourcc in dds.BLOCK_BYTES:
            return fourcc.decode(), 0
        return None, "format %s" % fourcc.decode("latin-1").strip("\0 ")
    if pf_flags & DDPF_PALETTE:
        return None, "P8: the hash folds a palette only a dump knows"
    r, g, b, a = struct.unpack_from("<IIII", data, 92)
    if not pf_flags & DDPF_ALPHA and not pf_flags & 0x1:
        a = 0
    name = DDS_MASKS.get((bits, r, g, b, a))
    if name is None and pf_flags & DDPF_BUMPDUDV and bits == 16:
        name = "V8U8"
    if name is None:
        return None, "format of %d bits, masks %08X %08X %08X %08X" % (bits, r, g, b, a)
    return name, bits // 8


def from_dds(c, data, reencodes=False):
    """Fill a candidate from a DDS, by the game's D3DX load rules only.
    `reencodes`: the game's load re-encodes a chain's levels under 4x4."""
    d = dds.parse(data)
    c.width, c.height, c.levels = d.width, d.height, d.levels
    if struct.unpack_from("<I", data, 112)[0] & DDSCAPS2_CUBEMAP:
        c.note = "a cube map"
        return
    name, bpp = dds_format(data)
    if name is None:
        c.note = bpp
        return
    c.format = name
    if d.dxt:
        c.layout = "dxt"
        sizes = _level_sizes(d.width, d.height, d.levels)
        bb = dds.BLOCK_BYTES[d.fourcc]
        ext = [((w + 3) // 4) * ((h + 3) // 4) * bb for w, h in sizes]
        c.hashes = _prefix_hashes(d.data, ext)
        npot = not swizzle.is_pow2(d.width) or not swizzle.is_pow2(d.height)
        small = d.levels > 1 and any(min(w, h) < 4 for w, h in sizes)
        if npot or (reencodes and small):
            c.guess = True
            c.probe = _probe(d.data, sizes, ext)
            c.top = d.data[: ext[0]]
        return
    c.layout = "swizzled"
    if not swizzle.is_pow2(d.width) or not swizzle.is_pow2(d.height):
        c.note = "%dx%d is not a power of two: the game resizes it on load" % (d.width, d.height)
        return
    sizes = _level_sizes(d.width, d.height, d.levels)
    guest = []
    off = 0
    for w, h in sizes:
        n = w * h * bpp
        if off + n > len(d.data):
            c.note = "%s data truncated" % name
            return
        guest.append(swizzle.swizzle(d.data[off : off + n], w, h, bpp))
        off += n
    c.hashes = _prefix_hashes(b"".join(guest), [len(g) for g in guest])


def _probe(data, sizes, ext):
    """(level, its bytes): the level compared with a dumped one."""
    off = 0
    for i, (w, h) in enumerate(sizes):
        if w * h <= PROBE_TEXELS or i == len(sizes) - 1:
            return i, data[off : off + ext[i]]
        off += ext[i]
    return 0, b""


def _same_level(rgba, bgra):
    """Whether a decoded level is the dumped one, within the decoders' rounding.
    A fully transparent texel's color means nothing."""
    if len(rgba) != len(bgra):
        return False
    tol = PROBE_TOLERANCE
    for i in range(0, len(rgba), 4):
        if abs(rgba[i + 3] - bgra[i + 3]) > tol:
            return False
        if rgba[i + 3] == 0:
            continue
        if (
            abs(rgba[i] - bgra[i + 2]) > tol
            or abs(rgba[i + 1] - bgra[i + 1]) > tol
            or abs(rgba[i + 2] - bgra[i]) > tol
        ):
            return False
    return True


def _name_by_content(guesses, entries, texture_of):
    """Hand the guessed candidates the dumped textures their content matches.
    Returns the candidates left with no match."""
    wanted = {}
    for h, e in entries.items():
        if e["status"] == UNNAMED and e.get("layout") == "dxt" and h in texture_of:
            key = (e["width"], e["height"], e.get("format"), e["levels"])
            wanted.setdefault(key, []).append(e)
    level_of = {}
    left = []
    for c in guesses:
        rows = wanted.get((c.width, c.height, c.format, c.levels), ())
        hit = False
        if rows and c.probe:
            fmt = c.format.encode()
            level, blocks = c.probe
            w, h = _level_sizes(c.width, c.height, c.levels)[level]
            rgba = dds.decode_level(fmt, w, h, blocks)
            top = None
            for e in rows:
                key = (e["hash"], level)
                if key not in level_of:
                    level_of[key] = _dump_level(texture_of[e["hash"]], level)
                if not _same_level(rgba, level_of[key]):
                    continue
                # A small level can be one color or one shared detail; two
                # textures may agree there and nowhere else.
                if level:
                    if top is None:
                        top = dds.decode_level(fmt, c.width, c.height, c.top)
                    if not _same_level(top, _dump_level(texture_of[e["hash"]], 0)):
                        continue
                e["origins"].append(dict(_origin(c), by="content"))
                hit = True
        if not hit:
            left.append(c)
    for rows in wanted.values():
        for e in rows:
            if e["origins"]:
                e["status"] = JOINED
    return left


def _dump_level(tex, level):
    try:
        return tex.read().levels_bgra()[level][2]
    except (SourceError, IndexError):
        return b""


def _from_xpr_dds(c, t):
    """A texture out of an XPR0: in GPU layout already, hashed as it is."""
    c.width, c.height, c.levels = t.width, t.height, t.levels
    c.format = t.fourcc.decode()
    sizes = _level_sizes(t.width, t.height, t.levels)
    if t.dxt:
        c.layout = "dxt"
        bb = dds.BLOCK_BYTES[t.fourcc]
        ext = [((w + 3) // 4) * ((h + 3) // 4) * bb for w, h in sizes]
    else:
        c.layout = "swizzled"
        bpp = next(b for n, b in xpr.RAW_FORMATS.values() if n == t.fourcc)
        ext = [w * h * bpp for w, h in sizes]
    c.hashes = _prefix_hashes(t.data, ext)


def candidates(source, name, data, reencodes=False):
    """The textures in one file, by what its bytes are; [] when it holds none."""
    try:
        if xpr.is_mix(data):
            return _from_container(source, name, xpr.parse_mix(data, raw=True))
        if data[:4] == xpr.XPR_MAGIC:
            rows = [("#%d" % i,) + t for i, t in enumerate(xpr.parse_xpr(data, 0, raw=True))]
            return _from_container(source, name, rows)
        if data[:4] == dds.MAGIC:
            c = Candidate(source, name)
            from_dds(c, data, reencodes)
            return [c]
        if name.endswith(".tga"):
            c = Candidate(source, name)
            c.note = "a TGA: the format it loads as is the game's choice"
            if len(data) >= 17:
                c.width, c.height = struct.unpack_from("<HH", data, 12)
            return [c]
    except (dds.DdsError, xpr.XprError, struct.error, ValueError) as e:
        c = Candidate(source, name)
        c.note = str(e)
        return [c]
    return []


def _from_container(source, name, rows):
    out = []
    for sub, t, note in rows:
        c = Candidate(source, name + "/" + sub.lower() if sub else name, name)
        if t is None:
            c.note = note.removeprefix("skipped: ")
        else:
            _from_xpr_dds(c, t)
        out.append(c)
    return out


def _origin(c):
    o = {"source": c.source, "name": c.name}
    if c.entry:
        o["entry"] = c.entry
    if c.levels > 1:
        # The file's chain, which the dump's bound levels may stop short of.
        o["levels"] = c.levels
    return o


def _chain_texels(w, h, levels):
    return sum(a * b for a, b in _level_sizes(w, h, levels))


def _texels_of(meta):
    """Texels in a dumped texture's levels: a linear one has only level 0,
    and its pitch-based hash is of no use for anything else."""
    return _chain_texels(meta["width"], meta["height"], meta.get("levels") or 1)


def _dump_entry(h, tex, name, volatile):
    meta = tex.meta
    e = {
        "hash": h,
        "status": UNNAMED,
        "origins": [],
        "width": meta["width"],
        "height": meta["height"],
        "levels": meta.get("levels") or 1,
        "texels": _texels_of(meta),
        "scenes": {name: tex.uses},
        "uses": tex.uses,
        "has_texels": bool(tex.path),
        "volatile": h in volatile,
    }
    for k in SIDECAR_KEYS:
        if k in meta:
            e[k] = meta[k]
    # Traced runs only; written by RECOMP_TRACE=tex_mag beside the dump.
    mag = traced.sane_mag(meta.get("mag_scale1"))
    if mag is not None:
        e["mag_scale1"] = mag
        e["draws"] = meta.get("mag_draws", 0)
    return e


def _merge_dump(e, other, name, tex, volatile):
    """Another scene's sidecar of a hash already in: the figures add up."""
    e["scenes"][name] = e["scenes"].get(name, 0) + tex.uses
    e["uses"] += tex.uses
    e["has_texels"] = e["has_texels"] or bool(tex.path)
    e["volatile"] = e["volatile"] or other in volatile
    mag = traced.sane_mag(tex.meta.get("mag_scale1"))
    if mag is not None:
        e["mag_scale1"] = max(e.get("mag_scale1", 0), mag)
        e["draws"] = e.get("draws", 0) + tex.meta.get("mag_draws", 0)
    # 2D only if every scene that drew it says so: no `drawn_2d` is a scene
    # that only loaded the texture, which has no say. One read as slopes or
    # rendered to anywhere is that everywhere.
    if "drawn_2d" in tex.meta:
        e["drawn_2d"] = e.get("drawn_2d", True) and tex.meta["drawn_2d"]
    for k in ("bump", "rt"):
        if tex.meta.get(k):
            e[k] = True
    # It decides the build's padding, so not whichever scene came first.
    for k in ("wrap_u", "wrap_v"):
        if tex.meta.get(k):
            e[k] = edges.merged_address(e.get(k), tex.meta[k])


def _alpha_used(tex):
    """Whether the dumped texels use their alpha; None when they cannot say."""
    if not tex.path:
        return None
    try:
        return tex.read().alpha_used()
    except SourceError:
        return None


def _palette_variants(entries, dumps):
    """How many hashes each palettized image has: one address and size, one
    hash per palette the game bound there."""
    groups = {}
    for d in dumps:
        for h, t in d.textures.items():
            if t.meta.get("palette") or t.meta.get("format") == "P8":
                key = (t.meta.get("offset"), t.meta["width"], t.meta["height"])
                groups.setdefault(key, set()).add(h)
    for hs in groups.values():
        for h in hs:
            if h in entries:
                entries[h]["palette_variants"] = len(hs)


def join(cands, dumps):
    """(entries by hash, the candidates no hash could be made for)."""
    entries = {}
    volatile = set()
    for d in dumps:
        volatile |= d.volatile
    for d in dumps:
        for h, t in d.textures.items():
            if h in entries:
                _merge_dump(entries[h], h, d.name, t, volatile)
                continue
            entries[h] = _dump_entry(h, t, d.name, volatile)
            used = _alpha_used(t)
            if used is not None:
                entries[h]["alpha_used"] = used
    _palette_variants(entries, dumps)
    dumped = set(entries)
    unhashed = []
    texture_of = {}
    for d in dumps:
        for h, t in d.textures.items():
            if t.path:
                texture_of.setdefault(h, t)
    rest = []
    guesses = []
    for c in cands:
        if not c.hashes:
            unhashed.append(c)
            continue
        hit = [(lv, h) for lv, h in c.hashes if h in dumped]
        for _, h in hit:
            e = entries[h]
            e["status"] = JOINED
            e["origins"].append(_origin(c))
        if hit:
            continue
        (guesses if c.guess else rest).append(c)
    named = set(id(c) for c in _name_by_content(guesses, entries, texture_of))
    rest += [c for c in guesses if id(c) in named]
    for c in rest:
        # Not seen: keyed by the whole chain, the prefixes kept so a later
        # dump, or whoever reads the list, can tell a wrong hash from an
        # unused texture.
        levels, h = c.hashes[-1]
        e = entries.get(h)
        if e is None:
            e = entries[h] = {
                "hash": h,
                "status": NOT_SEEN,
                "origins": [],
                "width": c.width,
                "height": c.height,
                "levels": levels,
                "format": c.format,
                "layout": c.layout,
                "texels": _chain_texels(c.width, c.height, levels),
                "scenes": {},
                "uses": 0,
                "has_texels": False,
            }
            if len(c.hashes) > 1:
                e["prefixes"] = [{"levels": lv, "hash": p} for lv, p in c.hashes]
            if c.guess:
                e["hash_guess"] = True
        e["origins"].append(_origin(c))
    return entries, unhashed


def open_sources(cfg, kinds, dump_dirs, game_files, repo):
    """(feeders, dumps) of the kinds in order; SourceError or OSError when one
    cannot be opened."""
    feeders, dumps = [], []
    for kind in kinds:
        if kind == "dump":
            dumps += [DumpSource(d) for d in dump_dirs]
        elif kind == "archive":
            feeders += [IpkSource(game_files, a) for a in cfg["archives"]]
        elif kind == "hook":
            feeders += [HookArchive(game_files, a, cfg["hook"], repo) for a in cfg["archives"]]
        elif kind == "loose":
            feeders.append(LooseSource(game_files, cfg["loose"]))
    return feeders, dumps


def scan(feeders, dumps=(), reencodes=False):
    """Every candidate of the feeders, in their order. A guess keeps its blocks
    only if a dump holds that shape, the one case they are compared: a game has
    thousands, and every file's level 0 would stay in memory."""
    shapes = set()
    for d in dumps:
        for t in d.textures.values():
            if t.path:
                m = t.meta
                shapes.add((m["width"], m["height"], m.get("format"), m.get("levels") or 1))
    out = []
    for src in feeders:
        for path, data in src.items():
            for c in candidates(src.name, path, data, reencodes):
                if c.guess and (c.width, c.height, c.format, c.levels) not in shapes:
                    c.probe = c.top = None
                out.append(c)
    return out


def build(cfg, feeders, dumps):
    """The inventory document."""
    entries, unhashed = join(scan(feeders, dumps, cfg["reencodes_small_levels"]), dumps)
    rows = [classes.classify(entries[h], cfg) for h in sorted(entries)]
    return {
        "schema": SCHEMA,
        "sources": [s.name for s in feeders],
        "dumps": [
            {
                "name": d.name,
                "textures": len(d.textures),
                "volatile_addresses": d.volatile_addresses,
                "bad_facts": d.bad_facts,
                "problems": d.problems,
            }
            for d in dumps
        ],
        "entries": rows,
        "unhashed": [
            {
                "source": c.source,
                "name": c.name,
                "width": c.width,
                "height": c.height,
                "why": c.note,
            }
            for c in unhashed
        ],
    }


def not_seen(doc):
    return [e for e in doc["entries"] if e["status"] == NOT_SEEN]


def unnamed(doc):
    return [e for e in doc["entries"] if e["status"] == UNNAMED]


def counts(doc):
    """{class: {entries, packable, texels}} and {skip reason: entries}."""
    per = {c: {"entries": 0, "packable": 0, "texels": 0} for c in classes.CLASSES}
    skipped = {}
    for e in doc["entries"]:
        r = per[e["class"]]
        r["entries"] += 1
        r["texels"] += e["texels"]
        if e["skip"]:
            skipped[e["skip"]] = skipped.get(e["skip"], 0) + 1
        else:
            r["packable"] += 1
    return per, skipped


def write(doc, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not an inventory of schema %d" % SCHEMA)
    return doc
