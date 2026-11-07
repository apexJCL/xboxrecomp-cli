"""xbr enhance textures build: the pack itself (D4, D5, D7, D8).

Each entry goes through one pipeline, whichever upscaler it has: its texels,
the pad, the alpha handling, the upscale, down to the class's scale if the
upscaler only does another, the crop, the chain, the encode. A built-in does
one texture per task in a pool (the bytes do not depend on the job count); a
command takes batches, then the pool does the rest.

The pack is made from the owner's own game files and stays with the owner.
"""

import collections
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import time

from .. import buildstate, host, resources
from ..host import CliError
from . import alpha, bc1, bc3, classes, dds, edges, estimate, mips, pack, xpr
from .sources import SourceError
from .upscalers import UpscalerError, by_name, get
from .upscalers.builtin import resample

TOML_NAME = "textures-pack.toml"
PACK_NAME = "textures.pack"
KIND = {
    "ui": pack.KIND_ART,
    "font": pack.KIND_FONT,
    "text": pack.KIND_TEXT,
    "world": pack.KIND_WORLD,
}
# Labels of the rows in build-logs/timings.json, beside the upscalers' own.
STEPS = ("pad", "alpha split", "down-to-scale", "mip chain", "bc1", "bc3")
TIMED = time.perf_counter


class Job:
    """One texture between its source texels and the upscaled, cropped image.
    `images()` is what goes to the upscaler at `native`; `finish(outs)` makes
    the texture of those images, whichever process they were upscaled in.
    Cells (D2) make a job of each cell's padded images, in the same one list."""

    def __init__(self, w, h, rgba, scale, native, alpha_mode, mode_u, mode_v, cell=None, pad=None):
        self.w, self.h, self.rgba = w, h, rgba
        self.scale, self.native = scale, native
        self.mode_u, self.mode_v = mode_u, mode_v
        self.cell = cell
        self.opaque = alpha.opaque(rgba)
        self.alpha_mode = "native" if self.opaque else alpha_mode
        self.cover = alpha.coverage(rgba) if self.alpha_mode == "binary" else None
        self.pad = edges.PAD if pad is None else pad
        self.seconds = dict.fromkeys(STEPS, 0.0)

    def _tiles(self):
        """(x, y, w, h) of what is padded alone: the image, or each cell."""
        if self.mode_u != "cells":
            return [(0, 0, self.w, self.h)]
        cw, ch = self.cell
        return [
            (x, y, min(cw, self.w - x), min(ch, self.h - y))
            for y in range(0, self.h, ch)
            for x in range(0, self.w, cw)
        ]

    def images(self):
        out = []
        for x, y, w, h in self._tiles():
            t = TIMED()
            tile = edges.crop(self.w, self.h, self.rgba, x, y, w, h)
            if self.alpha_mode != "native":
                # By the cell: the colour under a clear texel comes from its own
                # cell, or a kernel carries the neighbour's across the border.
                tile = alpha.extend_colour(w, h, tile)
            mode_u = "clamp" if self.mode_u == "cells" else self.mode_u
            mode_v = "clamp" if self.mode_v == "cells" else self.mode_v
            if self.pad:
                pw, ph, padded = edges.pad(w, h, tile, mode_u, mode_v, self.pad)
            else:
                pw, ph, padded = w, h, tile
            self.seconds["pad"] += TIMED() - t
            t = TIMED()
            if self.alpha_mode == "native":
                out.append((pw, ph, padded))
            else:
                out.append((pw, ph, alpha.colour_image(padded)))
                out.append((pw, ph, alpha.alpha_image(padded)))
            self.seconds["alpha split"] += TIMED() - t
        return out

    def _down(self, img, w, h):
        """`img` at the target scale of a padded (w, h); premultiplied, then the
        colour under transparent texels is put back for the mips and the sampler."""
        if self.native == self.scale:
            return img[2]
        t = TIMED()
        out = resample(img[0], img[1], img[2], w * self.scale, h * self.scale)
        if not alpha.opaque(out):
            out = alpha.extend_colour(w * self.scale, h * self.scale, out)
        self.seconds["down-to-scale"] += TIMED() - t
        return out

    def finish(self, outs):
        """(w, h, rgba) at the target scale, past the crop."""
        s = self.scale
        uw, uh = self.w * s, self.h * s
        full = bytearray(uw * uh * 4)
        outs = iter(outs)
        for x, y, w, h in self._tiles():
            pw, ph = w + 2 * self.pad, h + 2 * self.pad
            n = self.pad * s
            colour = self._down(next(outs), pw, ph)
            rgba = colour
            t = TIMED()
            if self.alpha_mode != "native":
                grey = self._down(next(outs), pw, ph)
                rgba = alpha.recombine(colour, grey)
            self.seconds["alpha split"] += TIMED() - t
            t = TIMED()
            cell = edges.crop(pw * s, ph * s, rgba, n, n, w * s, h * s) if n else rgba
            edges.blit(uw, full, x * s, y * s, w * s, h * s, cell)
            self.seconds["pad"] += TIMED() - t
        rgba = bytes(full)
        if self.cover is not None:
            rgba = alpha.binarize(rgba, self.cover)
        return uw, uh, rgba


def needs_pad(up, mode_u, mode_v, native, scale):
    """No pad where the kernel's own edge is the pad's: each built-in repeats the
    edge texel past the image, within 8 texels of its reach, so a clamp pad costs
    56% more texels on a 64x64 for the same result."""
    if up.kind == "builtin" and native == scale and mode_u == mode_v == "clamp":
        return 0
    return edges.PAD


def make_job(up, w, h, rgba, t, entry):
    native = up.native_for(t["scale"])
    if native is None:
        raise CliError(
            "%s cannot reach %dx: its scales are %s"
            % (up.name, t["scale"], ", ".join(str(s) for s in up.scales))
        )
    mode = t["alpha"]
    if mode == "native" and up.alpha != "rgba":
        # A model that drops alpha would drop the texture's: the split is what
        # keeps it. Said once, by notes_of.
        mode = "split"
    mode_u, mode_v = edges.resolve(t["edges"], entry)
    return Job(
        w,
        h,
        rgba,
        t["scale"],
        native,
        mode,
        mode_u,
        mode_v,
        t.get("cell"),
        needs_pad(up, mode_u, mode_v, native, t["scale"]),
    )


def resolve_encode(e, t, rgba):
    """(encode, note). `auto` trusts the texels over the inventory's fact, which
    a file with no dump only guessed from its format: BC1 of a texture that
    does use alpha would lose it."""
    enc = t["encode"]
    if enc != "auto":
        # bc1.py writes the four-colour form only: the owner asked, the owner is told.
        lost = enc == "bc1" and not alpha.opaque(rgba)
        return enc, "encode bc1 drops the alpha it uses" if lost else ""
    enc = classes.encode_of(e, "auto")
    used = not alpha.opaque(rgba)
    fmt = e.get("format", "").removeprefix("LIN_")
    if enc == "bc1" and used and fmt not in classes.OPAQUE_FORMATS:
        return "bc3", "its texels use alpha, its format says no: BC3"
    return enc, ""


def encode_chain(uw, uh, rgba, e, t, seconds):
    """(levels, payload, bc3, bc1, notes) of an upscaled base."""
    enc, note = resolve_encode(e, t, rgba)
    notes = [note] if note else []
    if enc in ("bc1", "bc3") and (uw % 4 or uh % 4):
        # The runtime takes block formats in whole blocks only.
        notes.append("%dx%d is not whole blocks, kept raw" % (uw, uh))
        enc = "raw"
    tt = TIMED()
    levels = mips.chain(uw, uh, rgba, t["mips"], e.get("levels", 1))
    seconds["mip chain"] = seconds.get("mip chain", 0.0) + TIMED() - tt
    tt = TIMED()
    if enc == "bc3":
        payload = b"".join(bc3.encode(*lv) for lv in levels)
    elif enc == "bc1":
        payload = b"".join(bc1.encode(*lv) for lv in levels)
    else:
        payload = b"".join(pack.rgba_to_argb(lv[2]) for lv in levels)
    if enc != "raw":
        seconds[enc] = seconds.get(enc, 0.0) + TIMED() - tt
    return len(levels), payload, enc == "bc3", enc == "bc1", notes


def decode_source(src):
    """(w, h, rgba) of a task's source: DXT blocks or a dump's BGRA, decoded in
    the worker, since a game's are a third of the parent's time otherwise."""
    kind = src[0]
    if kind == "dxt":
        _, fourcc, w, h, data = src
        return w, h, dds.decode_level(fourcc, w, h, data)
    _, w, h, bgra = src
    return w, h, pack.rgba_to_argb(bgra)


def _work(task):
    """One entry in a worker: everything from source texels to the payload."""
    e, t, src, name, params = task
    t0 = TIMED()
    w, h, rgba = decode_source(src)
    up = get(name)
    job = make_job(up, w, h, rgba, t, e)
    imgs = job.images()
    t1 = TIMED()
    outs = up.upscale(imgs, job.native, params)
    upscale_s = TIMED() - t1
    uw, uh, out = job.finish(outs)
    done = encode_chain(uw, uh, out, e, t, job.seconds)
    return (
        e["hash"],
        w,
        h,
        uw,
        uh,
        done,
        job.seconds,
        upscale_s,
        TIMED() - t0,
        resources.self_peak(),
    )


def _finish_task(task):
    """A batch backend's result, on through the crop and the encode."""
    job, outs, e, t = task
    uw, uh, out = job.finish(outs)
    done = encode_chain(uw, uh, out, e, t, job.seconds)
    return e["hash"], job.w, job.h, uw, uh, done, job.seconds, 0.0, 0.0, resources.self_peak()


def name_rows(data, path):
    """[(name, Dds)] the file holds that the build knows how to decode; the same
    names the inventory gave them."""
    if xpr.is_mix(data):
        rows = xpr.parse_mix(data, raw=True)
        return [(path + "/" + sub.lower() if sub else path, d) for sub, d, _ in rows if d]
    if data[:4] == xpr.XPR_MAGIC:
        rows = [("#%d" % i,) + r for i, r in enumerate(xpr.parse_xpr(data, 0, raw=True))]
        return [(path + "/" + sub, d) for sub, d, _ in rows if d]
    if data[:4] == dds.MAGIC:
        return [(path, dds.parse(data))]
    return []


def sources_of(picked, feeders, dumps):
    """Yields (entry, treatment, source) per picked entry, and then
    (entry, treatment, None) for what nothing can decode. DXT from the feeder
    first, the very blocks the hash is over, as `enhance hud` reads them; a
    format with no decoder here, or an entry only a dump holds, from the dump."""
    todo = dict(picked)
    by_origin = collections.defaultdict(list)
    for h, (e, _t) in picked.items():
        for o in e.get("origins", ()):
            by_origin[o["source"], o["name"]].append(h)
    tops = {src for src, _ in by_origin}
    for f in feeders:
        if f.name not in tops:
            continue
        for path, data in f.items():
            try:
                rows = name_rows(data, path)
            except (dds.DdsError, xpr.XprError, ValueError):
                continue
            for name, d in rows:
                for h in by_origin.get((f.name, name), ()):
                    pair = todo.get(h)
                    if pair is None or not d.dxt:
                        continue
                    del todo[h]
                    n = dds.BLOCK_BYTES[d.fourcc] * ((d.width + 3) // 4) * ((d.height + 3) // 4)
                    yield (*pair, ("dxt", d.fourcc, d.width, d.height, d.data[:n]))
    for h in sorted(todo):
        e, t = todo[h]
        tex = dump_of(h, dumps)
        if tex is None:
            yield e, t, None
            continue
        try:
            w, ht, bgra = tex.read().levels_bgra()[0]
        except SourceError as ex:
            host.say("%s: %s" % (h, ex))
            yield e, t, None
            continue
        yield e, t, ("bgra", w, ht, bgra)


def params_of(config, up):
    """The config's [upscaler.NAME] keys a built-in has: the others (`preset`,
    `model`, `tile`) are for command backends."""
    table = config.get("upscaler", {}).get(up.name, {})
    names = {p.name for p in up.params}
    return {k: v for k, v in table.items() if k in names}


def filter_only(picked, only):
    """--only: a name pattern, or a hash."""
    if not only:
        return picked
    pats = [p.lower().replace("\\", "/") for p in only]

    def hit(e):
        if classes.matches(e, pats):
            return True
        return any(fnmatch.fnmatchcase(e["hash"].lower(), p) for p in pats)

    return [(e, t) for e, t in picked if hit(e)]


def listing(entries, preset, config, cfg):
    """--list: every entry of the preset with its class, its rules, what it gets
    or why it does not."""
    rows = []
    use = {e["hash"]: t for e, t in estimate.treated(entries, preset, config, cfg)}
    for e in entries:
        if not classes.in_preset(e, preset, cfg):
            continue
        t = use.get(e["hash"]) or classes.treatment(e, config, preset, cfg)
        name = e["origins"][0]["name"] if e.get("origins") else ""
        rules = ",".join(t["rules"]) or "-"
        if e["hash"] in use:
            what = "%dx %s alpha=%s edges=%s mips=%s encode=%s" % (
                t["scale"],
                t["upscaler"],
                t["alpha"],
                t["edges"],
                t["mips"],
                t["encode"],
            )
        else:
            what = "skipped: %s" % classes.why(t["skip"] or classes.TRACE_SKIP)
        rows.append("%s %-5s rule=%s %s %s" % (e["hash"], e["class"], rules, what, name))
    return rows


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    # An escaped JSON string is a valid TOML basic string, as in hud-pack.toml.
    return json.dumps(str(v), ensure_ascii=True)


def scope_of(ups, font=None):
    """`any` when each upscaler gives one result on any machine, `host` when on
    this one, `none` otherwise: the weakest one sets it. An atlas drawn from a
    font of this machine's is at best `host`, whatever drew the rest."""
    order = ["any", "host", "none"]
    worst = max((order.index(u.reproducible_scope) for u in ups.values()), default=0)
    return order[max(worst, order.index("host") if font else 0)]


def upscalers_of(picked, config):
    """{name: upscaler} of what the entries use, each known to run and to reach
    its scales, before a texture is read: a missing tool or a plugin of another
    contract costs nothing this way. `atlas` is the kernel the fit starts from."""
    ups = {}
    tables = config.get("upscaler", {})
    for _, t in picked:
        name = "xbr" if t["upscaler"] == "atlas" else t["upscaler"]
        up = ups.get(name)
        try:
            if up is None:
                up = ups[name] = by_name(name, tables)
                up.identity()
        except UpscalerError as ex:
            raise CliError(str(ex)) from ex
        if up.native_for(t["scale"]) is None:
            raise CliError(
                "%s cannot reach %dx: its scales are %s"
                % (up.name, t["scale"], ", ".join(str(s) for s in up.scales))
            )
    return ups


def notes_of(picked, ups):
    """What the treatments change of what was asked, each said once."""
    lines = []
    capped = sum(1 for _, t in picked if "clamped_from" in t)
    if capped:
        lines.append("%d textures at a lower scale than asked: max_side" % capped)
    split = collections.Counter(
        t["upscaler"]
        for _, t in picked
        if t["alpha"] == "native"
        and ups["xbr" if t["upscaler"] == "atlas" else t["upscaler"]].alpha != "rgba"
    )
    for name, n in sorted(split.items()):
        lines.append("%d textures: %s drops alpha, so split and not native" % (n, name))
    for up in ups.values():
        if getattr(up, "warning", ""):
            lines.append(up.warning)
    return lines


def wrap_defaulted(picked):
    """How many entries have a pad mode no dump recorded (edges.UNRECORDED)."""
    return sum(1 for e, t in picked if edges.defaulted(e, t["edges"]))


def dump_of(h, dumps):
    """The texture of the first dump with this hash's texels, or None."""
    return next(
        (d.textures[h] for d in dumps if h in d.textures and d.textures[h].path),
        None,
    )


# How often a run saw an entry: for `estimate` and the owner's reading, read by
# nothing that makes a pack, and raised by every scene added. Keep a fact out of
# this list unless the build never reads it, or a pack goes stale in silence.
COUNTS_ONLY = ("scenes", "uses", "draws")


def input_key(picked, dumps, ups, font, cfg):
    """{name: digest} of what the pack's bytes follow beyond the game files, the
    config's text and the CLI: each entry with its resolved treatment and every
    fact of it but COUNTS_ONLY, the dump file its texels would come from, the
    game's HUD table, the font's bytes, the tools' and models' bytes. A moved
    class pattern or a new dump lands here, through what it did to an entry;
    one that changed no entry costs no rebuild."""
    rows = []
    for e, t in sorted(picked, key=lambda p: p[0]["hash"]):
        tex = dump_of(e["hash"], dumps)
        facts = {k: v for k, v in e.items() if k not in COUNTS_ONLY}
        rows.append([facts, t, buildstate.sha256_file(tex.path) if tex else ""])
    atlas = cfg["hud"] if any(t["upscaler"] == "atlas" for _, t in picked) else {}
    return {
        "entries": buildstate.digest(rows),
        "hud": buildstate.digest(atlas),
        "font": buildstate.sha256_file(font[0]) if font else "",
        "tools": buildstate.digest(
            {k: u.input_key() for k, u in ups.items() if u.kind != "builtin"}
        ),
    }


def record_text(entries, ctx):
    """textures-pack.toml: the owner's record of the pack; the runtime never
    reads it. Names, hashes and identities, no texel, path or URL."""
    lines = [
        "# Texture pack record, written by xbr enhance textures build. The pack is made",
        "# from your own game files: keep it private.",
        "schema = 1",
        "tool = %s" % _toml(ctx["tool"]),
        "scale = %d" % ctx["scale"],
        "text_scale = %d" % ctx["text_scale"],
        "preset = %s" % _toml(ctx["preset"]),
        "xbe_sha256 = %s" % _toml(ctx["xbe_sha256"]),
        "pack_sha256 = %s" % _toml(ctx["pack_sha256"]),
        "config_path = %s" % _toml(ctx["config_path"]),
        "config_sha256 = %s" % _toml(ctx["config_sha256"]),
        "config = %s" % _toml(ctx["config_sha256"]),
        "reproducible = %s" % _toml(ctx["scope"] == "any"),
        "reproducible_scope = %s" % _toml(ctx["scope"]),
        "languages = [%s]" % ", ".join(_toml(x) for x in ctx["languages"]),
        "only = [%s]" % ", ".join(_toml(x) for x in ctx["only"]),
        "wrap_defaulted = %d" % ctx.get("wrap_defaulted", 0),
        "entries = %d" % len(entries),
    ]
    font = ctx.get("font")
    if font:
        lines.append("font = %s" % _toml(font["name"]))
        if font.get("face"):
            lines.append("font_face = %s" % _toml(font["face"]))
        lines.append("font_sha256 = %s" % _toml(font["sha256"]))
    lines.append("")
    stats = pack.kind_stats(entries)
    for k in pack.ALL_KINDS:
        if k not in stats:
            continue
        n, bc, size = stats[k]
        blocks = sum(1 for e in entries if pack.ALL_KINDS[e.kind] == k and (e.bc1 or e.bc3))
        lines += [
            "[classes.%s]" % ("ui" if k == "art" else k),
            "entries = %d" % n,
            "block_entries = %d" % blocks,
            "bytes = %d" % size,
            "",
        ]
        del bc
    for name, up in sorted(ctx["upscalers"].items()):
        lines.append("[upscaler.%s]" % name)
        lines += ["%s = %s" % (k, _toml(v)) for k, v in sorted(up.identity().items())]
        lines.append("")
    for e in sorted(entries, key=lambda x: x.hash):
        lines += [
            "[[entry]]",
            'hash = "%016X"' % e.hash,
            "name = %s" % _toml(e.name.replace("\\", "/")),
            "source = %s" % _toml(e.source),
            "guest = [%d, %d]" % e.guest,
            "size = [%d, %d]" % (e.width, e.height),
            "levels = %d" % e.levels,
            'kind = "%s"' % pack.ALL_KINDS[e.kind],
            'encode = "%s"' % ("bc3" if e.bc3 else "bc1" if e.bc1 else "raw"),
            'texels_sha256 = "%s"' % e.texels_sha256,
            "",
        ]
    return "\n".join(lines)


def _pool(items, fn, jobs, on_peak):
    """fn over items in a process pool, with at most two tasks a worker in
    flight: the parent holds a window, never a game's textures. `items` may
    itself add entries as the parent reads the sources."""
    if jobs == 1:
        for it in items:
            yield fn(it)
        return
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
        pending = collections.deque()
        it = iter(items)
        more = True
        while more or pending:
            while more and len(pending) < 2 * jobs:
                try:
                    pending.append(ex.submit(fn, next(it)))
                except StopIteration:
                    more = False
            if pending:
                r = pending.popleft().result()
                on_peak(r[-1])
                yield r


class Build:
    """One build's state. `run` puts each entry's payload in the spill file, in
    the order they finish; pack.write puts them in hash order, so the bytes do
    not depend on the job count."""

    def __init__(self, picked, config, cfg, feeders, dumps, spill, jobs, font=None, ups=None):
        self.config = config
        self.cfg = cfg
        self.feeders = feeders
        self.dumps = dumps
        self.spill = spill
        self.jobs = jobs
        self.font = font  # (path, face) when a font was found
        self.entries = []
        self.notes = collections.Counter()
        self.upscalers = dict(ups or {})
        self.steps = collections.Counter()
        self.upscale_seconds = collections.Counter()
        self.texels = collections.Counter()
        self.left_out = []
        self.font_info = None
        self.peaks = []
        self.by_hash = {e["hash"]: (e, t) for e, t in picked}

    def _up(self, name):
        u = self.upscalers.get(name)
        if u is None:
            try:
                u = by_name(name, self.config.get("upscaler", {}))
            except UpscalerError as ex:
                raise CliError(str(ex)) from ex
            self.upscalers[name] = u
        return u

    def _add(self, r, source):
        h, w, ht, uw, uh, done, seconds, upscale_s, _, _ = r
        levels, payload, is_bc3, is_bc1, notes = done
        e, t = self.by_hash[h]
        label = e["origins"][0]["name"] if e.get("origins") else h
        for n in notes:
            self.notes[n.split(":", 1)[0] if "whole" not in n else "not whole blocks"] += 1
            host.say("%s: %s" % (label, n))
        entry = pack.PackEntry(
            int(h, 16),
            uw,
            uh,
            None,
            levels=levels,
            name=label,
            source=source,
            guest=(w, ht),
            kind=KIND[t.get("kind_as", e["class"])],
            bc3=is_bc3,
            bc1=is_bc1,
        )
        self.spill.add(entry, payload)
        self.entries.append(entry)
        for k, v in seconds.items():
            self.steps[k] += v
        self.upscale_seconds[source] += upscale_s
        self.texels[source] += w * ht

    def _font_entry(self, e, t, src):
        """In the parent: the atlas fit reads the upscale's result as a whole."""
        from . import hud

        w, h, rgba = decode_source(src)
        up = self._up("xbr")
        job = make_job(up, w, h, rgba, t, e)
        uw, uh, out = job.finish(up.upscale(job.images(), job.native, {}))

        class It:
            pass

        it = It()
        it.rgba, it.entry = (w, h, rgba), It()
        it.entry.path = e["origins"][0]["name"] if e.get("origins") else e["hash"]
        fit, n, self.font_info = hud.render_font(
            it, out, self.cfg["hud"], self.font[0], self.font[1], t["scale"]
        )
        host.say(
            "%s: %d glyphs rendered from %s"
            % (it.entry.path, n, self.font_info.get("face") or self.font_info["name"])
        )
        done = encode_chain(uw, uh, fit, e, t, job.seconds)
        return (e["hash"], w, h, uw, uh, done, job.seconds, 0.0, 0.0, 0), "font (%d glyphs)" % n

    def _flush(self, up, pending):
        """Run a command's batch, one call per native scale, then the rest of
        each job in the pool. The batch's seconds are the upscaler's own, with
        its start-up, which is what an estimate pays."""
        queues = {}
        t0 = TIMED()
        table = self.config.get("upscaler", {}).get(up.name, {})
        try:
            for nat in sorted({job.native for job, _, _, _ in pending}):
                group = [i for job, _, _, imgs in pending if job.native == nat for i in imgs]
                queues[nat] = iter(up.upscale(group, nat, table))
        except UpscalerError as ex:
            raise CliError(str(ex)) from ex
        self.upscale_seconds[up.name] += TIMED() - t0
        tasks = [
            (job, [next(queues[job.native]) for _ in imgs], e, t) for job, e, t, imgs in pending
        ]
        for r in _pool(tasks, _finish_task, self.jobs, self.peaks.append):
            self._add(r, up.name)

    def _tasks(self, queues):
        """What goes to the pool; a font fit or a command's texture is taken
        aside. A command's batch runs here, between the pool's windows."""
        for e, t, src in sources_of(self.by_hash, self.feeders, self.dumps):
            if src is None:
                self.left_out.append(e)
                continue
            name = t["upscaler"]
            if name == "atlas":
                if self.font:
                    r, source = self._font_entry(e, t, src)
                    self._add(r, source)
                    continue
                name = "xbr"
            up = self._up(name)
            if up.kind == "builtin":
                yield (e, t, src, name, params_of(self.config, up))
                continue
            w, h, rgba = decode_source(src)
            job = make_job(up, w, h, rgba, t, e)
            queues[name].append((job, e, t, job.images()))
            if sum(len(x[3]) for x in queues[name]) >= up.batch:
                self._flush(up, queues[name])
                queues[name] = []

    def run(self):
        queues = collections.defaultdict(list)
        for r in _pool(self._tasks(queues), _work, self.jobs, self.peaks.append):
            _, t = self.by_hash[r[0]]
            self._add(r, "xbr" if t["upscaler"] == "atlas" else t["upscaler"])
        for name, pending in queues.items():
            if pending:
                self._flush(self._up(name), pending)


def save_timings(b, logs):
    """Rows for the next estimate (D6): a step's seconds per megatexel of the
    guest texels that went through it."""
    total = sum(b.texels.values())
    for name, s in b.upscale_seconds.items():
        if s and b.texels[name]:
            estimate.save_rate(logs, name, s / (b.texels[name] / 1e6))
    if total:
        for k, s in b.steps.items():
            if s:
                estimate.save_rate(logs, k, s / (total / 1e6))


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def header_scales(preset, picked, cfg):
    """(scale, text scale) of the header, which the runtime only logs: a text
    scale is said when it differs from the rest's."""
    if preset == "hud":
        scale = cfg["hud"].get("scale", 4)
    else:
        scale = max((t["scale"] for e, t in picked if e["class"] != "text"), default=0)
    text = max((t["scale"] for e, t in picked if e["class"] == "text"), default=0)
    return scale or text, text if text and text != scale else 0
