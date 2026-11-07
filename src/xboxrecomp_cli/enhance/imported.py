"""A pack from a run's dumped textures, for a game with no archive reader:

  enhance textures export  a dump's textures as <hash>.png, level 0, to edit
  enhance hud --import     a folder of <hash>.png at any size into a pack
  enhance hud --from-dumps the dumped textures upscaled into a pack, all of
                           them or those a RECOMP_TRACE=tex_pack log missed

An entry the inventory or export's index classes as world is kind world, so the
game's world switch (textures.world) governs it, and that takes pack version 3.
Every other entry is kind art, which the runtime draws whatever the texture is,
and a pack with no world entry stays version 2: an older toolkit still loads it.

The guest's size comes from export's index.json beside the PNGs or from a dump
(--from-dumps with --import). The runtime compares an entry's aspect exactly
(nv2a_tex_pack_find) and uses the guest's texels on a mismatch, so one that
would be refused stops the build here instead of going unused in the pack.

A guest with a mip chain gets a new one from the entry's level 0: the guest's
own levels hold its old colours, and a recolour would show them at a distance.
"""

import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import time

from .. import host, resources
from ..host import CliError
from . import classes, inventory, mips, pack, png, scaler
from .sources import SourceError
from .sources.dump import HASH_NAME, Xtd

INDEX = "index.json"
INDEX_SCHEMA = 1
MAX_DIM = 16384  # NV2A_TEX_PACK_MAX_DIM
MISS_LINE = re.compile(r"\[TEXPACK\] miss ([0-9A-Fa-f]{16}) \d+x\d+ \S+ x\d+ [^(\n]*(\(.*\))?\s*$")
# Entries named in a message before the rest are only counted.
LIST_CAP = 20
IMPORT_KERNEL = "import"
# What the runtime never looks up. A volatile texture is looked up, under a
# hash that keeps changing, so an entry for it can still hit: said, not refused.
# An `rt` one was hashed and uploaded like any other, so likewise.
NOT_LOOKED_UP = ("cube",)
# Looked up, but drawn at guest texel positions: docs/formats/texture-pack.md
# keeps a linear texture out of a pack.
KEPT_OUT = NOT_LOOKED_UP + ("linear",)
SHA256 = re.compile(r"[0-9a-f]{64}$")
# export saves its index this often, and when it stops: a kill loses at most
# these rows, and the next export finds those PNG files again.
INDEX_EVERY = 32


# ---- what is known about a guest texture ------------------------------------


def _facts_of_dumps(dirs):
    """({hash: inventory entry}, {hash: its .xtd}) of the dumps, first dump
    first. Each dump's own problems are said: its other textures stay usable."""
    cfg = classes.game_config(host.g().m)
    try:
        _feeders, dumps = inventory.open_sources(cfg, ["dump"], dirs, "", "")
        doc = inventory.build(cfg, [], dumps)
    except (OSError, SourceError) as e:
        raise CliError(str(e)) from e
    for d in dumps:
        for p in d.problems:
            host.say("dump %s: %s" % (d.name, p))
    xtd = {}
    for d in dumps:
        for h, t in d.textures.items():
            if t.path:
                xtd.setdefault(h, t.path)
    return {e["hash"]: e for e in doc["entries"]}, xtd


def _read_index(directory):
    path = os.path.join(directory, INDEX)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise CliError("%s: %s" % (path, e)) from e
    rows = doc.get("textures") if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        raise CliError("%s: not an index export wrote" % path)
    out = {str(k).upper(): _clean_row(v) for k, v in rows.items()}
    bad = sum(1 for k, v in rows.items() if out[str(k).upper()] != v)
    if bad:
        host.say("%s: %d rows have a bad field, which is ignored" % (path, bad))
    return out


def _positive(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _clean_row(row):
    """A row's fields that are what export writes. The file can be edited by
    hand, and a size that is no number is no size to check against."""
    if not isinstance(row, dict):
        return {}
    out = {}
    if _positive(row.get("width")) and _positive(row.get("height")):
        out.update(width=row["width"], height=row["height"])
    if _positive(row.get("levels")):
        out["levels"] = row["levels"]
    for k in ("format", "class"):
        if isinstance(row.get(k), str):
            out[k] = row[k]
    if isinstance(row.get("alpha"), bool):
        out["alpha"] = row["alpha"]
    if isinstance(row.get("sha256"), str) and SHA256.match(row["sha256"]):
        out["sha256"] = row["sha256"]
    return out


def _guest(h, index, entries, xtd):
    """What the build knows of the guest texture h, or None: size, levels,
    whether its texels use alpha, what the runtime will never do with it, and
    the level 0 the PNG started from (a sha256, or the dump's .xtd to read)."""
    e = entries.get(h)
    row = index.get(h)
    if e:
        return {
            "width": e["width"],
            "height": e["height"],
            "levels": e.get("levels") or 1,
            "class": e.get("class", ""),
            "alpha": "alpha" in e["facts"],
            "never": next((f for f in KEPT_OUT if f in e["facts"]), ""),
            "sha256": (row or {}).get("sha256", ""),
            "xtd": xtd.get(h, ""),
        }
    if row and row.get("width") and row.get("height"):
        return {
            "width": row["width"],
            "height": row["height"],
            "levels": row.get("levels") or 1,
            "class": row.get("class", ""),
            "alpha": bool(row.get("alpha")),
            "never": "linear" if row.get("format", "").startswith("LIN_") else "",
            "sha256": row.get("sha256", ""),
            "xtd": "",
        }
    return None


def _no_record(guest):
    """Whether nothing says what a PNG of this texture looked like as exported."""
    return not guest or not (guest["sha256"] or guest["xtd"])


# ---- the trace ---------------------------------------------------------------


def read_misses(paths):
    """(hashes the pack lacks, hashes it holds and did not use) from the
    [TEXPACK] miss lines of these logs; a hash is lacking if any line says so."""
    lacking, held = set(), set()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = MISS_LINE.search(line)
                    if m:
                        # The tag "(text off)" and the like: the pack has it.
                        (held if m.group(2) else lacking).add(m.group(1).upper())
        except OSError as e:
            raise CliError("%s: %s" % (path, e)) from e
    if not lacking and not held:
        raise CliError(
            "%s: no [TEXPACK] miss line: run with RECOMP_TRACE=tex_pack and a pack loaded "
            "(with no pack the runtime records no misses)" % ", ".join(paths)
        )
    return lacking, held - lacking


def _narrowed(hashes, misses, only):
    """The hashes that --misses and --only leave, and what the log adds."""
    out = sorted(hashes)
    if misses:
        lacking, held = read_misses(misses)
        out = [h for h in out if h in lacking]
        away = sorted(lacking - set(hashes))
        host.say("misses: %d in the log, %d of them dumped" % (len(lacking), len(out)))
        if away:
            host.say(
                "  %d with no dumped texels: dump a run of the scene that drew them "
                "(a linear texture is never dumped with texels): %s" % (len(away), _few(away))
            )
        if held:
            host.say("  %d the pack holds, not used (switched off or refused)" % len(held))
    if only:
        pats = [p.upper() for p in only]
        out = [h for h in out if any(fnmatch.fnmatchcase(h, p) for p in pats)]
    return out


def _few(names):
    shown = ", ".join(names[:LIST_CAP])
    return shown + (" and %d more" % (len(names) - LIST_CAP) if len(names) > LIST_CAP else "")


# ---- level 0 to a pack payload ------------------------------------------------


def chain_length(w, h):
    """Levels a full chain has: the most the runtime's reader allows."""
    n = 1
    m = max(w, h)
    while m > 1:
        m >>= 1
        n += 1
    return n


def levels_for(guest, w, h, mips):
    """The entry's level count. Auto gives a chain to a guest that has one, to
    a guest nothing is known about, and to a one-level guest drawn in the world
    at a larger size: the samplers turn mips on for such an entry (nv2a_stage_lod),
    and with no chain it would shimmer where it is minified. A one-level guest at
    its own size stays one level (the sampler is mip-off, a chain is unused bytes),
    and so does a HUD piece, which the game draws without mips (design D4)."""
    if mips == "none":
        return 1
    if mips == "full" or not guest or guest["levels"] > 1:
        return chain_length(w, h)
    if w > guest["width"] and guest.get("class", "world") == "world":
        return chain_length(w, h)
    return 1


def _kind(guest):
    return pack.KIND_WORLD if guest and guest.get("class") == "world" else pack.KIND_ART


def payload(w, h, rgba, levels):
    """Every level's texels, the pack's byte order, level 0 first."""
    parts = [pack.rgba_to_argb(rgba)]
    for _ in range(levels - 1):
        w, h, rgba = mips.down2(w, h, rgba)
        parts.append(pack.rgba_to_argb(rgba))
    return b"".join(parts)


def _level0(xtd_path):
    """(w, h, rgba) of level 0 of a dumped texture. OSError or SourceError."""
    with open(xtd_path, "rb") as f:
        x = Xtd(f.read())
    w, h, bgra = x.levels_bgra()[0]
    # The swap is its own inverse: B, G, R, A to R, G, B, A too.
    return w, h, pack.rgba_to_argb(bgra)


def _aspect_problem(name, w, h, guest):
    gw, gh = guest["width"], guest["height"]
    if w * gh == h * gw:
        return ""
    return (
        "%s is %dx%d, its texture %dx%d: the runtime compares the aspect exactly and "
        "would ignore this entry (%dx%d or %dx%d would do)"
        % (name, w, h, gw, gh, gw, gh, gw * 2, gh * 2)
    )


def _unedited(guest, w, h, rgba):
    """Whether these are the texels the PNG was exported with."""
    if not guest or (w, h) != (guest["width"], guest["height"]):
        return False
    if guest["sha256"]:
        return hashlib.sha256(rgba).hexdigest() == guest["sha256"]
    if guest["xtd"]:
        try:
            return _level0(guest["xtd"])[2] == rgba
        except (OSError, SourceError):
            return False
    return False


def _import_one(task):
    """A PNG to a result: (texels or None, w, h, levels, why left out, problem,
    note). In a worker process, so a failure comes back as text."""
    name, path, guest, mips = task
    try:
        # Before decoding: the file's own limit counts texels, so a very long
        # thin image passes it, and decoding costs far more than the file does.
        w, h = png.size(path)
        if w > MAX_DIM or h > MAX_DIM:
            why = "%s is %dx%d, over the runtime's %d a side" % (name, w, h, MAX_DIM)
            return None, w, h, 0, "", why, ""
        w, h, rgba = png.read(path)
    except (OSError, png.PngError) as e:
        return None, 0, 0, 0, "", "%s: %s" % (name, e), ""
    if guest:
        why = _aspect_problem(name, w, h, guest)
        if why:
            return None, w, h, 0, "", why, ""
        if _unedited(guest, w, h, rgba):
            return None, w, h, 0, "unedited", "", ""
    note = ""
    if guest and guest["alpha"] and not png.has_alpha(rgba):
        note = "%s has no alpha left where its texture uses it: it draws solid" % name
    levels = levels_for(guest, w, h, mips)
    return payload(w, h, rgba, levels), w, h, levels, "", "", note


def _dump_one(task):
    """A dumped texture upscaled, as _import_one's result."""
    name, path, guest, mips, s = task
    try:
        w, h, rgba = _level0(path)
    except (OSError, SourceError) as e:
        return None, 0, 0, 0, "", "%s: %s" % (name, e), ""
    w, h, rgba = w * s, h * s, scaler.scale(w, h, rgba, s)
    levels = levels_for(guest, w, h, mips)
    return payload(w, h, rgba, levels), w, h, levels, "", "", ""


def _results(fn, tasks, jobs):
    """fn over the tasks, in task order whatever the job count."""
    n = len(tasks)
    if not jobs and n > 1:
        jobs = resources.hud_jobs(n)
    if jobs == 1 or n <= 1:
        yield from map(fn, tasks)
        return
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
        yield from ex.map(fn, tasks)


# ---- enhance hud --import and --from-dumps -------------------------------------


def wanted(a):
    """Whether this run is one of these. An option of theirs alone counts, so
    its refusal is said rather than a plain HUD pack made without it."""
    return bool(
        getattr(a, "import_dir", None)
        or getattr(a, "from_dumps", None)
        or getattr(a, "misses", None)
        or getattr(a, "mips", "auto") != "auto"
    )


def _check_options(a):
    """Refused rather than ignored: each of these would change nothing here."""
    if not a.import_dir and not a.from_dumps:
        raise CliError("--misses and --mips go with --import or --from-dumps")
    if a.font or a.language:
        raise CliError("--font and --language make a game's HUD set, not this pack")
    if a.import_dir:
        if a.scale:
            raise CliError("--scale with --import: each PNG keeps its own size")
        if a.misses:
            raise CliError("--misses narrows --from-dumps; with --import the folder is the set")
        if not os.path.isdir(a.import_dir):
            raise CliError("--import %s: no such folder" % a.import_dir)


def _png_files(directory):
    """({hash: file name}, names that are no hash)."""
    files, odd = {}, []
    for f in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(f)
        if ext.lower() != ".png":
            continue
        if not HASH_NAME.match(stem):
            odd.append(f)
            continue
        h = stem.upper()
        if h in files:
            raise CliError("%s: %s and %s are the same hash" % (directory, files[h], f))
        files[h] = f
    return files, odd


def _import_tasks(a, entries, xtd):
    files, odd = _png_files(a.import_dir)
    if odd:
        host.say(
            "%d PNG files with no 16-digit hash for a name, left out: %s" % (len(odd), _few(odd))
        )
    hashes = _narrowed(files, [], a.only)
    if not hashes:
        raise CliError("%s: no <hash>.png to import" % a.import_dir)
    index = _read_index(a.import_dir)
    problems, unknown, moving, targets, no_record = [], [], [], [], []
    tasks = []
    for h in hashes:
        guest = _guest(h, index, entries, xtd)
        if _no_record(guest):
            no_record.append(h)
        if guest is None:
            unknown.append(h)
        elif guest["never"]:
            problems.append(
                "%s: %s (%s), %s"
                % (
                    files[h],
                    classes.why(guest["never"]),
                    guest["never"],
                    "so a pack should not hold it"
                    if guest["never"] == "linear"
                    else "so the runtime never uses an entry for it",
                )
            )
        elif h in entries and "volatile" in entries[h]["facts"]:
            moving.append(h)
        elif h in entries and "rt" in entries[h]["facts"]:
            targets.append(h)
        tasks.append((files[h], os.path.join(a.import_dir, files[h]), guest, a.mips))
    if unknown:
        host.say(
            "%d with no size to check them against (no %s entry, no dump holds them): an "
            "entry of another aspect would go unused. %s" % (len(unknown), INDEX, _few(unknown))
        )
    if no_record:
        # Said, not refused: a folder of PNG files made by hand has no export
        # behind it and is a fair thing to import.
        host.say(
            "%d have no record of what export wrote, so each counts as an edit and goes in the "
            "pack, changed or not: %s. To leave out the unchanged, pass --from-dumps DUMP or run "
            "export again" % (len(no_record), _few(no_record))
        )
    if moving:
        host.say(
            "%d change under the game's hands, so their hash does too, and an entry "
            "rarely applies: %s" % (len(moving), _few(moving))
        )
    if targets:
        host.say(
            "%d were uploaded at an address and size also sampled as a render target, so "
            "their texels may be the game's own rendering: %s" % (len(targets), _few(targets))
        )
    if problems:
        raise CliError("\n  ".join(["these cannot go in a pack:"] + problems))
    return hashes, tasks


def _dump_tasks(a, entries, xtd):
    usable = [h for h, e in entries.items() if h in xtd]
    hashes = []
    skipped = {}
    for h in _narrowed(usable, a.misses, a.only):
        why = entries[h]["skip"]
        if why:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            hashes.append(h)
    if skipped:
        host.say("left out: " + ", ".join("%d %s" % (skipped[k], k) for k in sorted(skipped)))
    if not hashes:
        raise CliError("nothing in the dumps to pack: every texture is left out, or none matched")
    s = a.scale or 2
    tasks = [(h, xtd[h], _guest(h, {}, entries, xtd), a.mips, s) for h in hashes]
    return hashes, tasks


def run(a):
    from .. import __version__

    _check_options(a)
    G = host.g()
    t0 = time.time()
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(G.dist, "enhance", "import")
    entries, xtd = _facts_of_dumps(a.from_dumps) if a.from_dumps else ({}, {})
    if a.import_dir:
        hashes, tasks = _import_tasks(a, entries, xtd)
        fn, kernel, source = _import_one, IMPORT_KERNEL, "png"
    else:
        hashes, tasks = _dump_tasks(a, entries, xtd)
        fn, kernel, source = _dump_one, scaler.KERNEL, scaler.KERNEL
    if a.list:
        for h, t in zip(hashes, tasks):
            g = t[2]
            guest = "%dx%d x%d" % (g["width"], g["height"], g["levels"]) if g else "not known"
            host.say("%s  guest %s" % (h, guest))
        host.say("%d textures" % len(hashes))
        return 0
    resources.check_disk([resources.hud_need(out_dir)], a.ignore_disk_space)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "hud.pack")
    spill = pack.Spill(path + ".payload.tmp")
    built, problems, notes = [], [], []
    unedited = 0
    scale = 1
    try:
        for h, t, r in zip(hashes, tasks, _results(fn, tasks, a.jobs)):
            texels, w, ht, levels, left, problem, note = r
            if problem:
                problems.append(problem)
            if note:
                notes.append(note)
            if left:
                unedited += 1
            if texels is None:
                continue
            guest = t[2]
            e = pack.PackEntry(
                int(h, 16),
                w,
                ht,
                None,
                levels=levels,
                name=t[0],
                source=source,
                guest=(guest["width"], guest["height"]) if guest else (0, 0),
                kind=_kind(guest),
            )
            spill.add(e, texels)
            built.append(e)
            if guest:
                scale = max(scale, w // guest["width"])
        if problems:
            raise CliError(
                "\n  ".join(["%d textures cannot go in the pack:" % len(problems)] + problems)
            )
        if not built:
            raise CliError(
                "every PNG in %s is as it was exported: edit some, then import" % a.import_dir
            )
        spill.close()
        worlds = sum(1 for e in built if e.kind == pack.KIND_WORLD)
        version = pack.VERSION_CHAINS if worlds else pack.VERSION
        sha = pack.write(path, built, scale, spill=spill.path, version=version)
    finally:
        spill.discard()
    xbe_sha = host.sha256_path(G.xbe) if os.path.exists(G.xbe) else ""
    with open(os.path.join(out_dir, "hud-pack.toml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(pack.manifest_text(built, scale, kernel, "xboxrecomp-cli " + __version__, xbe_sha))
    for note in notes:
        host.say(note)
    if unedited:
        host.say(
            "%d PNG files are as they were exported: left out, the game draws its own" % unedited
        )
    if a.import_dir:
        blind = sum(1 for t in tasks if _no_record(t[2]))
        if blind:
            host.say("%d of the entries had nothing to compare them with, as said above" % blind)
    chained = sum(1 for e in built if e.levels > 1)
    host.say(
        "wrote %s: %d entries (%d with a new mip chain), %d world, %d art, %.1f MB, "
        "sha256 %s, %.0f s"
        % (
            G.rel(path),
            len(built),
            chained,
            worlds,
            len(built) - worlds,
            os.path.getsize(path) / 1e6,
            sha[:12],
            time.time() - t0,
        )
    )
    host.say(
        'to use it: textures.pack = "%s" in config.toml, or package --hud-pack %s'
        % (path, G.rel(out_dir))
    )
    host.say("the pack is made from your game files: keep it private")
    return 0


# ---- enhance textures export ---------------------------------------------------


def _index_row(e, sha):
    """sha: of the texels as exported. "" leaves it out: size facts alone, so
    import can check the size and knows it has no baseline."""
    row = {
        "width": e["width"],
        "height": e["height"],
        "levels": e.get("levels") or 1,
        "format": e.get("format", ""),
        "class": e.get("class", ""),
        "alpha": "alpha" in e["facts"],
    }
    if sha:
        row["sha256"] = sha
    return row


def _save_index(out_dir, index):
    tmp = os.path.join(out_dir, INDEX + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema": INDEX_SCHEMA, "textures": index}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, os.path.join(out_dir, INDEX))


def _kept_sha(path, row, xtd_path):
    """The baseline of a PNG already in the folder: the one recorded, or, with
    its row lost, its own sha256 if its texels still equal the dump's. Otherwise
    "": what an owner changed must not become what counts as unchanged."""
    if row.get("sha256"):
        return row["sha256"]
    try:
        w, h, rgba = png.read(path)
        if (w, h, rgba) == _level0(xtd_path):
            return hashlib.sha256(rgba).hexdigest()
    except (OSError, png.PngError, SourceError):
        pass
    return ""


def run_export(a):
    G = host.g()
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(G.dist, "enhance", "export")
    entries, xtd = _facts_of_dumps(a.dump)
    moving = KEPT_OUT + ("volatile", "rt")
    never = sum(1 for e in entries.values() if any(f in moving for f in e["facts"]))
    usable = [
        h for h, e in entries.items() if h in xtd and not any(f in moving for f in e["facts"])
    ]
    hashes = _narrowed(usable, a.misses, a.only)
    if not hashes:
        raise CliError("nothing to export: no texture in the dump matched")
    index = _read_index(out_dir)
    wrote = kept = empty = 0
    try:
        os.makedirs(out_dir, exist_ok=True)
        try:
            for h in hashes:
                p = os.path.join(out_dir, h + ".png")
                # A second export must never undo the owner's edits.
                if os.path.exists(p):
                    kept += 1
                    index[h] = _index_row(entries[h], _kept_sha(p, index.get(h, {}), xtd[h]))
                    continue
                try:
                    w, ht, rgba = _level0(xtd[h])
                except (OSError, SourceError) as e:
                    host.say("%s: %s" % (h, e))
                    continue
                png.write(p, w, ht, rgba)
                wrote += 1
                if not any(rgba[3::4]):
                    empty += 1
                index[h] = _index_row(entries[h], hashlib.sha256(rgba).hexdigest())
                if wrote % INDEX_EVERY == 0:
                    _save_index(out_dir, index)
        finally:
            _save_index(out_dir, index)
    except OSError as e:
        raise CliError("%s: %s" % (G.rel(out_dir), e)) from e
    host.say("wrote %d PNG files to %s (level 0 of each)" % (wrote, G.rel(out_dir)))
    if kept:
        host.say("%d were there already and are left as they are" % kept)
    if never:
        host.say(
            "%d left out: a render target, a cube map, a linear texture, or changing every frame"
            % never
        )
    if empty:
        host.say(
            "%d have alpha 0 everywhere: an editor shows them empty, and may lose their colour "
            "on saving" % empty
        )
    host.say(
        "edit the ones you want, at any size of the same aspect, then: enhance hud --import %s"
        % G.rel(out_dir)
    )
    host.say("they are your game's textures: keep them private")
    return 0
