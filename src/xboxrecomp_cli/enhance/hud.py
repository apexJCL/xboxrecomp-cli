"""xbr enhance hud: the HUD texture pack from the owner's own dump.

Reads game.toml's [enhance.hud] (manifest.py: the archives, the entry
name patterns, the default scale and the font atlas's rows), decodes the
selected DXT textures, hashes each data block as the runtime hashes the
guest texture, upscales them with the integer xBR kernel in parallel
processes (one texture per task, so the result is the same whatever the
job count), re-renders the Latin atlas from a font (--font, else the
font game.toml names when this host has it installed), and
writes <out>/hud.pack and <out>/hud-pack.toml. The dump is read, never
written; the output goes under dist/ by default, which is gitignored.
"""

import concurrent.futures
import fnmatch
import hashlib
import os
import struct
import time

from .. import host
from ..host import CliError
from . import atlas, dds, font, ipk, pack, scaler, texhash, xpr


def font_hint(name):
    if name:
        return (
            "no font: the atlas is upscaled like every other texture. For real\n"
            "  glyphs, install %s (game.toml [enhance.hud] font; this host's font\n"
            "  folders were searched) or point --font at its .ttf or .ttc file" % name
        )
    return (
        "no font: the atlas is upscaled like every other texture. For real\n"
        "  glyphs, point --font at a TrueType file or an installed font's full name"
    )


def resolve_font(arg, name):
    """(path, face name) of the font to draw: --font as a file, else --font
    or game.toml's font as an installed font's full name; (None, name)."""
    if arg and os.path.isfile(arg):
        return arg, name
    want = arg or name
    if not want:
        return None, ""
    path = font.find(want)
    if path is None and arg:
        raise CliError("--font %s: no such file, and no installed font has that full name" % arg)
    return path, want


def configure(p):
    p.add_argument(
        "--scale", type=int, choices=(2, 4), help="upscale factor (default: game.toml's)"
    )
    p.add_argument("--out", help="output directory (default dist/enhance/hud/)")
    p.add_argument(
        "--font",
        help="a TrueType file (.ttf, .ttc) or an installed font's full name for the "
        "Latin atlas (default: game.toml's [enhance.hud] font, if installed)",
    )
    p.add_argument(
        "--jobs", type=int, default=0, help="processes for the upscale (default: the CPUs)"
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PATTERN",
        help="narrow the set to entries matching this pattern (repeatable)",
    )
    p.add_argument(
        "--list", action="store_true", help="print the set and its hashes, write nothing"
    )


def hud_config():
    G = host.g()
    cfg = G.m.get("enhance", {}).get("hud")
    if not cfg or not cfg.get("archives") or not cfg.get("textures"):
        raise CliError(
            "game.toml has no [enhance.hud] table with archives and textures: this game "
            "names no HUD set (docs/manifest.md)"
        )
    return cfg


def _match(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def select(game_files, cfg, only=()):
    """[(archive, entry)] of the set, in archive and index order."""
    patterns = [p.lower().replace("\\", "/") for p in cfg["textures"]]
    only = [p.lower().replace("\\", "/") for p in only]
    out = []
    for name in cfg["archives"]:
        try:
            arc = ipk.Archive(game_files, name)
        except (OSError, ipk.ArchiveError) as e:
            raise CliError("%s: %s" % (os.path.join(game_files, name + ".iph"), e)) from e
        for e in arc.entries:
            if not _match(e.path, patterns):
                continue
            if only and not (_match(e.path, only) or _match(os.path.basename(e.path), only)):
                continue
            out.append((arc, e))
    return out


class Item:
    __slots__ = ("archive", "entry", "sub", "dds", "hash", "rgba", "note")

    def __init__(self, archive, entry, sub=""):
        self.archive = archive
        self.entry = entry
        self.sub = sub  # the texture's name inside a .mix, else ""
        self.dds = None
        self.hash = 0
        self.rgba = None
        self.note = ""

    @property
    def label(self):
        # entry.path is lower-cased for matching; the label follows it.
        return self.entry.path + ("/" + self.sub.lower() if self.sub else "")

    @property
    def name(self):
        return "%s:%s" % (self.archive.name, self.entry.name) + ("/" + self.sub if self.sub else "")


def _check(it, d, max_texels):
    """Set it.dds and it.hash, or it.note with why the texture is left out."""
    if not d.dxt:
        it.note = "skipped: not DXT (%s)" % (d.fourcc.decode("latin-1") or "uncompressed")
    elif d.levels != 1:
        it.note = "skipped: %d mip levels" % d.levels
    elif max_texels and d.width * d.height > max_texels:
        it.note = "skipped: %dx%d is over max_texels %d" % (d.width, d.height, max_texels)
    else:
        it.dds = d
        it.hash = texhash.tex_hash(d.data)


def load_items(selected, max_texels=0):
    items = []
    for arc, e in selected:
        try:
            data = arc.read(e)
            if xpr.is_mix(data):
                # Pre-rendered text: one texture per name in the MIX.
                for sub, d, note in xpr.parse_mix(data):
                    it = Item(arc, e, sub)
                    if d is None:
                        it.note = note
                    else:
                        _check(it, d, max_texels)
                    items.append(it)
                continue
            d = dds.parse(data)
        except (ipk.ArchiveError, dds.DdsError, xpr.XprError) as ex:
            it = Item(arc, e)
            it.note = "skipped: %s" % ex
            items.append(it)
            continue
        it = Item(arc, e)
        _check(it, d, max_texels)
        items.append(it)
    return items


def dedupe(usable, font_atlas=""):
    """One item per hash: the pack's index holds each hash once. The same
    texture in two archives is kept once (the font atlas, if it is one of
    them) and said; two different textures with one hash are refused."""
    kept = {}
    for it in usable:
        prev = kept.get(it.hash)
        if prev is None:
            kept[it.hash] = it
            continue
        a, b = prev.dds, it.dds
        if (a.width, a.height, a.data) != (b.width, b.height, b.data):
            raise CliError(
                "%s and %s hash alike (%016X) but differ; a pack holds one texture per hash"
                % (prev.label, it.label, it.hash)
            )
        keep, drop = prev, it
        if font_atlas and it.entry.path == font_atlas:
            keep, drop = it, prev
            kept[it.hash] = it
        host.say("%s: same texels as %s, one copy kept" % (drop.label, keep.label))
    return list(kept.values())


def _scale_task(args):
    w, h, rgba, s = args
    return scaler.scale(w, h, rgba, s)


def upscale_all(items, s, jobs):
    """Each item's rgba at s x, in item order whatever the job count."""
    work = []
    for it in items:
        w, h, rgba = dds.decode(it.dds)
        it.rgba = (w, h, rgba)
        work.append((w, h, rgba, s))
    if jobs == 1 or len(work) <= 1:
        return [_scale_task(a) for a in work]
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs or None) as ex:
        return list(ex.map(_scale_task, work, chunksize=1))


def render_font(it, up, cfg, font_path, face, s):
    """The atlas item's rgba with the font's glyphs; (rgba, glyphs, info)."""
    try:
        f = font.load(font_path, face)
    except (OSError, font.FontError, struct.error) as e:
        raise CliError("font %s: %s" % (font_path, e)) from e
    try:
        rgba, n, notes = atlas.render(it.rgba, cfg["font_rows"], f, s, up)
    except atlas.AtlasError as e:
        raise CliError("%s: %s (game.toml [enhance.hud] font_rows)" % (it.entry.path, e)) from e
    for note in notes:
        host.say("  %s" % note)
    with open(font_path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    info = {"name": os.path.basename(font_path), "sha256": sha}
    if f.name:
        info["face"] = f.name
    return rgba, n, info


def run(a):
    from .. import __version__

    G = host.g()
    cfg = hud_config()
    s = a.scale or cfg["scale"]
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(G.dist, "enhance", "hud")
    host.require(G.xbe, "enhance hud")
    t0 = time.time()
    items = load_items(select(G.game_files, cfg, a.only), cfg["max_texels"])
    usable = [it for it in items if it.dds]
    if a.list:
        for it in items:
            if it.dds:
                host.say(
                    "%016X  %-40s %s %dx%d"
                    % (it.hash, it.label, it.dds.fourcc.decode(), it.dds.width, it.dds.height)
                )
            else:
                host.say("%16s  %-40s %s" % ("-", it.label, it.note))
        host.say("%d entries, %d usable" % (len(items), len(usable)))
        return 0
    if not usable:
        raise CliError("no DXT texture matched [enhance.hud] textures in %s" % G.game_files)
    for it in items:
        if it.note:
            host.say("%s: %s" % (it.label, it.note))
    font_atlas = cfg.get("font_atlas", "").lower().replace("\\", "/")
    usable = dedupe(usable, font_atlas)
    font_info = None
    font_path, face = resolve_font(a.font, cfg.get("font", ""))
    if font_path is None:
        host.say(font_hint(face))
    elif not a.font or not os.path.isfile(a.font):
        host.say("font: %s, %s" % (face, font_path))
    host.say("upscaling %d textures %dx (%s)..." % (len(usable), s, scaler.KERNEL))
    ups = upscale_all(usable, s, a.jobs)
    entries = []
    for it, up in zip(usable, ups):
        w, h, _ = it.rgba
        rgba = up
        source = "xbr"
        if font_path and font_atlas and it.entry.path == font_atlas:
            rgba, n, font_info = render_font(it, up, cfg, font_path, face, s)
            source = "font (%d glyphs)" % n
            host.say(
                "%s: %d glyphs rendered from %s"
                % (it.entry.path, n, font_info.get("face") or font_info["name"])
            )
        entries.append(
            pack.PackEntry(
                it.hash,
                w * s,
                h * s,
                pack.rgba_to_argb(rgba),
                name=it.name,
                source=source,
                guest=(w, h),
            )
        )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "hud.pack")
    sha = pack.write(path, entries, s)
    with open(G.xbe, "rb") as f:
        xbe_sha = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(out_dir, "hud-pack.toml"), "w", newline="\n") as f:
        f.write(
            pack.manifest_text(
                entries, s, scaler.KERNEL, "xboxrecomp-cli " + __version__, xbe_sha, font_info
            )
        )
    host.say(
        "wrote %s: %d entries at %dx, sha256 %s, %.0f s"
        % (G.rel(path), len(entries), s, sha[:12], time.time() - t0)
    )
    host.say("the pack is made from your game files: keep it private (see hud-pack.toml beside it)")
    return 0
