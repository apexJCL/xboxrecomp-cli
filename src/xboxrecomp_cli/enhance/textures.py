"""xbr enhance textures: what a game's textures are, and what a pack would cost.

  inventory  every texture the sources name or the dump holds, joined on the
             runtime's hash, into dist/enhance/inventory.json
  estimate   the bytes, VRAM and minutes of a preset, from that inventory
  export     a dump's textures as <hash>.png, to edit and bring back with
             enhance hud --import
  build      the pack, from your own game files; it stays with you
  compare    every upscaler on a few of the game's textures, side by side
  pick       a class's upscaler and scale, written into the config
  backend    the local upscalers this CLI knows of, and the torch plugin's install line

The sources are game.toml's [enhance.textures] sources; the dump is the
directory a run wrote with RECOMP_DEBUG=tex_dump=<dir>. The dump alone is a
full inventory: a game with no archive reader needs nothing else.
"""

import argparse
import hashlib
import json
import os
import sys
import time

from .. import __version__, buildstate, host, resources
from ..host import CliError
from ..package.progress import load_timings
from . import (
    build,
    classes,
    compare,
    estimate,
    imported,
    inventory,
    pack,
    textures_cfg,
    traced,
)
from .sources import SourceError
from .upscalers import command

LIST_CAP = 20
DEFAULT_CONFIG = os.path.join("config", "enhance-textures.toml")


def configure(p):
    sub = p.add_subparsers(dest="verb", metavar="verb")
    sub.required = True
    i = sub.add_parser(
        "inventory",
        description="every texture of the game's sources and dumps, joined on the runtime's "
        "hash: dist/enhance/inventory.json (names, hashes, classes; no texels)",
    )
    i.add_argument(
        "--dump",
        action="append",
        default=[],
        metavar="DIR",
        help="a directory a run wrote with RECOMP_DEBUG=tex_dump=DIR, one scene (repeatable)",
    )
    i.add_argument(
        "--source",
        action="append",
        default=[],
        choices=classes.SOURCES,
        help="read only this source (repeatable; default: game.toml's [enhance.textures] sources)",
    )
    i.add_argument("--out", help="the inventory file (default dist/enhance/inventory.json)")
    i.add_argument("--json", action="store_true", help="print the inventory as JSON")
    i.add_argument(
        "--list", action="store_true", help="print every entry, and why a skipped one is skipped"
    )
    i.add_argument(
        "--propose",
        type=int,
        metavar="R",
        help="the trace's proposal per entry at render scale R, and the bytes it saves",
    )
    i.add_argument(
        "--untraced",
        choices=("class", "skip"),
        help="with --propose: what an entry the trace never saw gets (default: its class's scale)",
    )
    i.add_argument(
        "--write-rules",
        metavar="FILE",
        help="with --propose: write the proposals into this config as rules",
    )
    e = sub.add_parser(
        "estimate",
        description="pack bytes, resident VRAM and upscale minutes of a preset, from the inventory",
    )
    e.add_argument(
        "--preset",
        help="hud, ui, world, all, traced, custom or the game's own "
        "(default: the config's preset, else all)",
    )
    e.add_argument(
        "--config", help="the config to apply (default config/enhance-textures.toml, if there)"
    )
    e.add_argument("--inventory", help="the inventory file (default dist/enhance/inventory.json)")
    e.add_argument("--json", action="store_true", help="print the estimate as JSON")
    resources.add_jobs(e, "the job count to divide by (default: the cores)")
    x = sub.add_parser(
        "export",
        description="a dump's textures as <hash>.png (level 0) and an index.json, to edit "
        "and bring back with enhance hud --import; a PNG already there is left as it is",
    )
    x.add_argument(
        "--dump",
        action="append",
        required=True,
        metavar="DIR",
        help="a directory a run wrote with RECOMP_DEBUG=tex_dump=DIR (repeatable)",
    )
    x.add_argument("--out", help="the folder (default dist/enhance/export/)")
    x.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="HASH",
        help="only this hash; wildcards work (repeatable)",
    )
    x.add_argument(
        "--misses",
        action="append",
        default=[],
        metavar="LOG",
        help="only the textures this RECOMP_TRACE=tex_pack log missed (repeatable)",
    )
    b = sub.add_parser(
        "build",
        description="the texture pack, from your own game files and dumps: "
        "dist/enhance/textures/textures.pack; never share it",
    )
    b.add_argument("--preset", help="hud, ui, world, all, traced, custom or the game's own")
    b.add_argument(
        "--config", help="the config to apply (default config/enhance-textures.toml, if there)"
    )
    b.add_argument("--inventory", help="the inventory file (default dist/enhance/inventory.json)")
    b.add_argument(
        "--dump",
        action="append",
        default=[],
        metavar="DIR",
        help="where entries only a dump holds are read (default: the dumps the inventory saw)",
    )
    b.add_argument("--out", help="the output folder (default dist/enhance/textures/)")
    b.add_argument("--only", action="append", metavar="PATTERN", help="only entries matching this")
    b.add_argument(
        "--language", action="append", default=[], help="text of this language (repeatable)"
    )
    b.add_argument(
        "--font", help="a font file or an installed font's full name, for the font class's atlas"
    )
    b.add_argument(
        "--list",
        action="store_true",
        help="print each entry's class, rule and reason; build nothing",
    )
    resources.add_jobs(b, "textures at once (default: as many as the free memory and cores allow)")
    resources.add_flag(b)
    c = sub.add_parser(
        "compare",
        description="every upscaler on a few of the game's textures, side by side: "
        "dist/enhance/compare/<stamp>/ (a grid per class, full outputs, index.html, "
        "compare.json); from your own game files, keep it private",
    )
    c.add_argument(
        "--class",
        dest="classes",
        action="append",
        default=[],
        choices=classes.CLASSES,
        help="only this class (repeatable; default: every class with entries)",
    )
    c.add_argument(
        "--upscaler",
        action="append",
        default=[],
        metavar="NAME",
        help="only this upscaler (repeatable; default: the built-ins, the config's and "
        "every installed preset)",
    )
    c.add_argument("--scale", type=int, choices=(2, 3, 4), help="default: each class's scale")
    c.add_argument(
        "--samples", type=int, default=compare.DEFAULT_SAMPLES, help="textures per class"
    )
    c.add_argument(
        "--pick",
        action="append",
        default=[],
        metavar="NAME",
        help="this texture too, by name pattern or hash (repeatable)",
    )
    c.add_argument(
        "--crop", type=int, default=compare.DEFAULT_CROP, help="the grid's crop, in source texels"
    )
    c.add_argument("--repeat", type=int, default=2, help="runs per upscaler, to check they agree")
    c.add_argument("--out", help="the output folder (default dist/enhance/compare/<stamp>/)")
    c.add_argument(
        "--config", help="the config to apply (default config/enhance-textures.toml, if there)"
    )
    c.add_argument("--inventory", help="the inventory file (default dist/enhance/inventory.json)")
    c.add_argument(
        "--dump", action="append", default=[], metavar="DIR", help="where to read dumped texels"
    )
    k = sub.add_parser(
        "pick", description="a class's upscaler and scale, written into the tracked config"
    )
    k.add_argument("cls", metavar="CLASS", choices=classes.CLASSES)
    k.add_argument("upscaler", metavar="UPSCALER")
    k.add_argument("--scale", type=int, choices=(2, 3, 4))
    k.add_argument("--config", help="the config to write (default config/enhance-textures.toml)")
    w = sub.add_parser(
        "backend",
        description="the local upscalers this CLI knows of; it ships and downloads none",
    )
    w.add_argument("action", nargs="?", choices=("list", "install"), default="list")
    w.add_argument("what", nargs="?", choices=("torch",), help="with install: the plugin")


def _inventory_path(a):
    G = host.g()
    given = getattr(a, "out", None) or getattr(a, "inventory", None)
    return os.path.abspath(given) if given else os.path.join(G.dist, "enhance", inventory.FILE_NAME)


def _one_line_about_sources(kinds):
    if not {"archive", "hook"} & set(kinds):
        if "loose" in kinds:
            return "no archive reader here: entries stay unnamed unless a loose file names them"
        return "no archive reader here: every entry stays unnamed"
    return ""


def _name(e):
    return e["origins"][0]["name"] if e["origins"] else ""


def _lines_capped(rows, fmt, full):
    shown = rows if full else rows[:LIST_CAP]
    out = [fmt(r) for r in shown]
    if len(rows) > len(shown):
        out.append("  ... and %d more (--list prints them all)" % (len(rows) - len(shown)))
    return out


def _unhashed_lines(rows, full):
    """Each with its reason under --list, else the reasons with a count: a
    game's unreadable files are mostly one kind, by the hundred."""
    if full:
        return ["  %s: %s" % (u["name"], u["why"]) for u in rows]
    per = {}
    for u in rows:
        why = u["why"].split(": ", 1)[-1] if "power of two" in u["why"] else u["why"]
        per[why] = per.get(why, 0) + 1
    return ["  %d %s" % (per[k], k) for k in sorted(per, key=lambda k: -per[k])]


def _summary(doc, path, full):
    per, skipped = inventory.counts(doc)
    rows = doc["entries"]
    seen = inventory.not_seen(doc)
    none = inventory.unnamed(doc)
    out = [
        "inventory: %d entries: %d joined, %d not seen, %d unnamed"
        % (len(rows), len(rows) - len(seen) - len(none), len(seen), len(none))
    ]
    for d in doc["dumps"]:
        out.append(
            "  dump %s: %d textures, %d volatile addresses"
            % (d["name"], d["textures"], d["volatile_addresses"])
        )
        out += ["    problem: %s" % p for p in d["problems"]]
    out.append("  %-6s %8s %9s %12s" % ("class", "entries", "packable", "texels"))
    for c in classes.CLASSES:
        r = per[c]
        out.append("  %-6s %8d %9d %12d" % (c, r["entries"], r["packable"], r["texels"]))
    if skipped:
        out.append("  skipped: " + ", ".join("%s %d" % (k, skipped[k]) for k in sorted(skipped)))
    variants = [e for e in rows if e.get("palette_variants", 0) > 1]
    if variants:
        images = round(sum(1 / e["palette_variants"] for e in variants))
        out.append("  palettized: %d entries are variants of %d images" % (len(variants), images))
    if seen:
        out.append("not seen (in the sources, no dump holds their hash): %d" % len(seen))
        out += _lines_capped(seen, lambda e: "  %s %s" % (e["hash"], _name(e)), full)
    if none:
        out.append("unnamed (in a dump, no source names them): %d" % len(none))
        out += _lines_capped(
            none,
            lambda e: "  %s %dx%d %s" % (e["hash"], e["width"], e["height"], e.get("format", "")),
            full,
        )
    if doc["unhashed"]:
        out.append(
            "no hash (the feeder cannot reproduce the guest's bytes): %d" % len(doc["unhashed"])
        )
        out += _lines_capped(_unhashed_lines(doc["unhashed"], full), str, True)
    if full:
        out.append("entries:")
        for e in rows:
            why = "  skipped: %s" % classes.why(e["skip"]) if e["skip"] else ""
            out.append(
                "  %s %-5s %dx%d %s [%s] %s%s"
                % (
                    e["hash"],
                    e["class"],
                    e["width"],
                    e["height"],
                    e.get("format", ""),
                    " ".join(e["facts"]),
                    _name(e),
                    why,
                )
            )
    out.append("wrote %s" % path)
    return out


def _check_propose(a):
    """Refused before the sources are read, rather than ignored after."""
    if a.propose is None:
        if a.write_rules or a.untraced:
            raise CliError("--write-rules and --untraced go with --propose R")
        return
    if a.propose < 1:
        raise CliError("--propose: a render scale of 1 or more")
    if a.json:
        raise CliError("--json prints the inventory, --propose a table: run them apart")


def _proposal(doc, a):
    r = a.propose
    untraced = a.untraced or "class"
    packable = [e for e in doc["entries"] if not e["skip"]]
    rows = traced.table(packable, r, untraced)
    traced_rows = [x for x in rows if x["mag_scale1"] is not None]
    out = ["proposal at render scale %d: %d traced, %d untraced (%s)"]
    out[0] = out[0] % (r, len(traced_rows), len(rows) - len(traced_rows), untraced)
    if not traced_rows:
        out.append("  no sidecar has a magnification: dump a run with RECOMP_TRACE=tex_mag too")
    out.append(
        "  %-16s %-5s %6s %8s %12s  %s" % ("hash", "class", "mag", "proposal", "saved", "name")
    )
    for x in sorted(rows, key=lambda x: -x["saved"]):
        mag = "-" if x["mag_scale1"] is None else "%.2f" % x["mag_scale1"]
        prop = "%dx" % x["proposal"] if x["proposal"] else "skip"
        line = "  %-16s %-5s %6s %8s %12d  %s" % (
            x["hash"],
            x["class"],
            mag,
            prop,
            x["saved"],
            x["name"],
        )
        out.append(line + ("  (%s)" % x["note"] if x["note"] else ""))
    out.append(
        "bytes saved against the class defaults: %d of %d"
        % (sum(x["saved"] for x in rows), sum(x["default_bytes"] for x in rows))
    )
    if a.write_rules:
        _write_rules(a.write_rules, traced.choices_from(rows))
        out.append("wrote the rules into %s" % a.write_rules)
    return out


def _write_rules(path, choices):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = "schema = %d\n" % classes.CONFIG_SCHEMA
    tmp = path + ".tmp"
    try:
        new = traced.merge_rules(text, choices)
        # Text that would not read back leaves the file, and the folder, alone.
        classes.parse_config(new)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise CliError("%s: %s" % (path, e)) from e


def run_inventory(a):
    _check_propose(a)
    G = host.g()
    cfg = classes.game_config(G.m)
    kinds = a.source or cfg["sources"]
    dump_dirs = a.dump
    if "dump" in kinds and not dump_dirs and kinds == ["dump"]:
        raise CliError(
            "the dump is the only source and no --dump names one: run the game with "
            "RECOMP_DEBUG=tex_dump=DIR, then pass --dump DIR"
        )
    if dump_dirs and "dump" not in kinds:
        kinds = list(kinds) + ["dump"]
    if {"archive", "hook"} & set(kinds) and not cfg["archives"]:
        raise CliError("game.toml names no archives: [enhance.textures] archives")
    try:
        feeders, dumps = inventory.open_sources(cfg, kinds, dump_dirs, G.game_files, G.root)
        doc = inventory.build(cfg, feeders, dumps)
    except (OSError, SourceError) as e:
        raise CliError(str(e)) from e
    path = _inventory_path(a)
    inventory.write(doc, path)
    _write_dump_paths(path, dump_dirs)
    note = _one_line_about_sources(kinds)
    if a.json:
        json.dump(doc, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    if note:
        host.say(note)
    for line in _summary(doc, path, a.list):
        host.say(line)
    if a.propose is not None:
        for line in _proposal(doc, a):
            host.say(line)
    return 0


def _load_config(a):
    G = host.g()
    path = a.config or os.path.join(G.root, DEFAULT_CONFIG)
    if not a.config and not os.path.exists(path):
        return {}
    try:
        return classes.load_config(path)
    except (OSError, ValueError) as e:
        raise CliError("%s: %s" % (path, e)) from e


def run_estimate(a):
    G = host.g()
    cfg = classes.game_config(G.m)
    config = _load_config(a)
    preset = a.preset or config.get("preset", "all")
    known = set(classes.PRESETS) | set(cfg["presets"]) | {classes.CUSTOM}
    if preset not in known:
        raise CliError("preset %s: one of %s" % (preset, ", ".join(sorted(known))))
    if preset == "hud" and not cfg["hud"]:
        raise CliError("preset hud: game.toml has no [enhance.hud]")
    path = _inventory_path(a)
    try:
        doc = inventory.load(path)
    except FileNotFoundError as e:
        raise CliError("%s: no inventory yet: run enhance textures inventory first" % path) from e
    except (OSError, ValueError) as e:
        raise CliError("%s: %s" % (path, e)) from e
    jobs = a.jobs or resources.cores() or 1
    est = estimate.estimate(doc["entries"], preset, config, cfg, load_timings(G.logs), jobs)
    if a.json:
        json.dump(est, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    for line in estimate.report(est):
        host.say(line)
    langs = sorted({e["language"] for e in doc["entries"] if e.get("language")})
    if preset != "hud" and len(langs) > 1 and not config.get("coverage", {}).get("languages"):
        host.say(
            "  text counts %d languages (%s): a config's [coverage] languages picks"
            % (len(langs), ", ".join(langs))
        )
    if not doc["dumps"]:
        host.say("from the archives only: a dump adds what the game composes, and the VRAM")
    return 0


def _dump_paths_file(inv_path):
    return inv_path + ".dumps"


def _write_dump_paths(inv_path, dump_dirs):
    """Where the build finds a dump's texels, beside the inventory and not in
    it: the inventory holds names only, a path holds a user name."""
    with open(_dump_paths_file(inv_path), "w", encoding="utf-8", newline="\n") as f:
        json.dump([os.path.abspath(d) for d in dump_dirs], f)


def _read_dump_paths(a):
    try:
        with open(_dump_paths_file(_inventory_path(a)), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _load_build_config(a):
    G = host.g()
    path = a.config or os.path.join(G.root, DEFAULT_CONFIG)
    if not a.config and not os.path.exists(path):
        return textures_cfg.Config({})
    try:
        return textures_cfg.load(path)
    except textures_cfg.ConfigError as e:
        raise CliError(str(e)) from e


def _known_preset(preset, cfg):
    known = set(classes.PRESETS) | set(cfg["presets"]) | {classes.CUSTOM}
    if preset not in known:
        raise CliError("preset %s: one of %s" % (preset, ", ".join(sorted(known))))
    if preset == "hud" and not cfg["hud"]:
        raise CliError("preset hud: game.toml has no [enhance.hud]")


def _inv_only(a):
    # --out is the build's folder here, never the inventory's.
    return argparse.Namespace(out=None, inventory=a.inventory)


def _inventory_for_build(a, cfg, dump_dirs):
    """The file if there is one, else one made here from the same sources."""
    G = host.g()
    path = _inventory_path(_inv_only(a))
    if os.path.exists(path):
        try:
            return inventory.load(path)
        except (OSError, ValueError) as e:
            raise CliError("%s: %s" % (path, e)) from e
    if a.inventory:
        raise CliError("%s: no such inventory" % path)
    feeders, dumps = _open(cfg, dump_dirs, G)
    return inventory.build(cfg, feeders, dumps)


def _open(cfg, dump_dirs, G):
    kinds = [k for k in cfg["sources"] if k != "dump"]
    if {"archive", "hook"} & set(kinds) and not cfg["archives"]:
        kinds = [k for k in kinds if k not in ("archive", "hook")]
    try:
        return inventory.open_sources(cfg, kinds + ["dump"], dump_dirs, G.game_files, G.root)
    except (OSError, SourceError) as e:
        raise CliError(str(e)) from e


def _font_of(a, config, cfg, picked):
    """(path, face) when an entry wants the atlas fit and a face is found. No
    face leaves the atlas to xbr, said once, as `enhance hud` says it."""
    if not any(t["upscaler"] == "atlas" for _, t in picked):
        return None
    hud_cfg = cfg["hud"]
    if not hud_cfg.get("font_rows"):
        return None
    from . import hud

    face = config.get("font", {}).get("face", "") or hud_cfg.get("font", "")
    path, name = hud.resolve_font(a.font, face)
    if path is None:
        host.say(hud.font_hint(name))
        return None
    return path, name


def _record_path(path, root):
    """The config's path for the record: under the game's root, or a file's name
    alone, since a path out of it holds the machine's folders."""
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # another drive
        rel = os.pardir
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return os.path.basename(path)
    return rel.replace(os.sep, "/")


def check_preset(preset):
    """Refused while the plan is made, before a build of anything else."""
    _known_preset(preset, classes.game_config(host.g().m))


def _build_record(G, inputs_extra):
    from ..package import incr, plib

    lib = plib()
    st = buildstate.State(G.root)
    inputs = dict(inputs_extra, cli=incr.tree_input(incr.cli_dir.source_dir(), lib))
    up = {"import": incr.record_import(st, lib)}
    return st, buildstate.stage_key(inputs, up), inputs, up


def run_build(a):
    G = host.g()
    cfg = classes.game_config(G.m)
    conf = _load_build_config(a)
    config = dict(conf.doc)
    if a.language:
        config["coverage"] = dict(config.get("coverage", {}), languages=list(a.language))
    preset = a.preset or config.get("preset", "all")
    _known_preset(preset, cfg)
    doc = _inventory_for_build(a, cfg, a.dump)
    if a.list:
        for line in build.listing(doc["entries"], preset, config, cfg):
            host.say(line)
        return 0
    host.require(G.xbe, "enhance textures build")
    picked = build.filter_only(estimate.treated(doc["entries"], preset, config, cfg), a.only or [])
    if not picked:
        raise CliError("preset %s: no entry to build" % preset)
    is_hud = preset == "hud"
    sub = "hud" if is_hud else "textures"
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(G.dist, "enhance", sub)
    pack_name = "hud.pack" if is_hud else build.PACK_NAME
    toml_name = "hud-pack.toml" if is_hud else build.TOML_NAME
    path = os.path.join(out_dir, pack_name)
    dump_dirs = a.dump or _read_dump_paths(_inv_only(a))
    font = _font_of(a, config, cfg, picked)
    ups = build.upscalers_of(picked, config)
    feeders, dumps = _open(cfg, dump_dirs, G)
    with open(G.xbe, "rb") as f:
        xbe_sha = hashlib.sha256(f.read()).hexdigest()
    languages = sorted({e["language"] for e, _ in picked if e.get("language")})
    st, key, inputs, up = _build_record(
        G,
        {
            "preset": preset,
            "config": conf.sha256,
            "only": list(a.only or ()),
            "languages": languages,
            "font": font[1] if font else "",
            **build.input_key(picked, dumps, ups, font, cfg),
            "hud_scale": cfg["hud"].get("scale", 0) if is_hud else 0,
            "xbe": xbe_sha,
            "out": buildstate.State(G.root)._store_path(out_dir),
            "inventory": buildstate.digest([e["hash"] for e, _ in picked]),
            "upscalers": buildstate.digest(
                {k: command.stage_table(v) for k, v in config.get("upscaler", {}).items()}
            ),
        },
    )
    stage = "textures:" + preset
    if not st.check(stage, key, inputs, up):
        host.say("%s is current (same game files, settings and CLI): kept" % G.rel(path))
        return 0
    want = sum(classes.pack_bytes(e, t["scale"], t["encode"], t["mips"]) for e, t in picked)
    # The spill and the pack it is copied to are on disk together.
    resources.check_disk(
        [resources.Need("enhance textures", out_dir, 2 * want + (1 << 20), "the texture pack")],
        a.ignore_disk_space,
    )
    st.begin(stage)
    jobs = a.jobs or resources.hud_jobs(len(picked))
    os.makedirs(out_dir, exist_ok=True)
    spill = pack.Spill(path + ".payload.tmp")
    t0 = time.time()
    host.say(
        "building %d textures, about %.1f MB, %d jobs: the pack is made from your game files"
        % (len(picked), want / 1e6, jobs)
    )
    for line in build.notes_of(picked, ups):
        host.say("  " + line)
    made = build.Build(picked, config, cfg, feeders, dumps, spill, jobs, font, ups)
    try:
        made.run()
        spill.close()
        if not made.entries:
            raise CliError("no entry could be read: a dump or the game files hold their texels")
        scale, text_scale = build.header_scales(preset, picked, cfg)
        sha = pack.write(
            path,
            made.entries,
            scale,
            text_scale,
            spill=spill.path,
            version=pack.VERSION_CHAINS,
        )
    finally:
        spill.discard()
    pack.read_index(path)
    defaulted = build.wrap_defaulted(picked)
    ctx = {
        "tool": "xboxrecomp-cli " + __version__,
        "scale": scale,
        "text_scale": text_scale,
        "preset": preset,
        "xbe_sha256": xbe_sha,
        "pack_sha256": sha,
        "config_path": _record_path(conf.path, G.root),
        "config_sha256": conf.sha256,
        "scope": build.scope_of(made.upscalers, made.font_info),
        "wrap_defaulted": defaulted,
        "languages": languages,
        "only": list(a.only or ()),
        "upscalers": made.upscalers,
        "font": made.font_info,
    }
    toml_path = os.path.join(out_dir, toml_name)
    with open(toml_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(build.record_text(made.entries, ctx))
    for e in made.left_out:
        host.say(
            "left out %s %s: no decoder for %s here and %s"
            % (
                e["hash"],
                e["origins"][0]["name"] if e.get("origins") else "",
                e.get("format", ""),
                "no dump holds it" if dumps else "no dump was given (--dump)",
            )
        )
    build.save_timings(made, G.logs)
    if made.peaks:
        resources.record("textures:worker", max(p for p in made.peaks if p) or 0, jobs)
    stats = pack.kind_stats(made.entries)
    host.say(
        "wrote %s: %d entries (%s), %d left out, %.1f MB, sha256 %s, %.0f s"
        % (
            G.rel(path),
            len(made.entries),
            ", ".join("%d %s" % (stats[k][0], k) for k in stats if stats[k][0]),
            len(made.left_out),
            os.path.getsize(path) / 1e6,
            sha[:12],
            time.time() - t0,
        )
    )
    for n, c in sorted(made.notes.items()):
        host.say("  %d: %s" % (c, n))
    if defaulted:
        host.say("  %d: world textures no dump recorded a mode for, padded as wrap" % defaulted)
    budget = config.get("budget", {}).get("pack_mb", 0)
    if budget and os.path.getsize(path) > budget * 1e6:
        host.say("  over budget.pack_mb %d: rules or lower scales bring it down" % budget)
    host.say(
        "the pack is made from your game files: keep it private (see %s beside it)" % toml_name
    )
    st.record(stage, key, inputs, [path, toml_path], upstream=up)
    return 0


def run_compare(a):
    G = host.g()
    cfg = classes.game_config(G.m)
    conf = _load_build_config(a)
    config = dict(conf.doc)
    if a.samples < 1 or a.crop < 1 or a.repeat < 1:
        raise CliError("--samples, --crop and --repeat: 1 or more")
    doc = _inventory_for_build(a, cfg, a.dump)
    rows = compare.treated_rows(doc["entries"], config, cfg)
    wanted = a.classes or [c for c in classes.CLASSES if any(e["class"] == c for e, _ in rows)]
    ups, missing = compare.columns(a.upscaler, config)
    if a.upscaler and missing:
        raise CliError(missing[0][1])
    feeders, dumps = _open(cfg, a.dump or _read_dump_paths(_inv_only(a)), G)
    per_class = {}
    for c in wanted:
        per_class[c] = compare.pick_samples(
            rows, c, a.samples, a.pick, cfg["preview"], a.crop, feeders, dumps
        )
    if not any(per_class.values()):
        raise CliError(
            "no sample's texels could be read: a dump or the game files hold them "
            "(enhance textures inventory --dump DIR)"
        )
    out = os.path.abspath(a.out or os.path.join(G.dist, "enhance", "compare", compare.stamp()))
    result = compare.render(per_class, ups, missing, config, out, a.scale, a.crop, a.repeat)
    compare.save_timings(result, G.logs)
    for r in result["columns"]:
        rate = "%.2f s/Mtexel" % r["s_per_mtexel"] if r["s_per_mtexel"] else "no timing"
        line = "  %-24s %s, scope %s" % (r["name"], rate, r["reproducible_scope"])
        if r["repeatable"] is False:
            line += ", its repeats differ"
        if r["flag"]:
            line += " (%s)" % r["flag"]
        if r["error"]:
            line = "  %-24s failed: %s" % (r["name"], r["error"])
        host.say(line)
    host.say("wrote %s: open index.html" % G.rel(out))
    host.say("made from your game files: keep it private")
    return 0


def run_pick(a):
    G = host.g()
    path = a.config or os.path.join(G.root, DEFAULT_CONFIG)
    try:
        textures_cfg.write_class_row(path, a.cls, a.upscaler, a.scale)
    except (OSError, ValueError, textures_cfg.ConfigError) as e:
        raise CliError("%s: %s" % (path, e)) from e
    host.say(
        "%s: class %s uses %s%s"
        % (G.rel(path), a.cls, a.upscaler, " at %dx" % a.scale if a.scale else "")
    )
    return 0


def run_backend(a):
    from .upscalers import BUILTINS

    if a.action == "install":
        if a.what != "torch":
            raise CliError("backend install: torch is the one plugin")
        host.say(
            "The torch plugin is optional, and yours to install. This prints; it runs nothing."
        )
        host.say("It ships and fetches no model: the model files, and their licences, are yours.")
        for where, line in command.plugin_install_lines(__version__):
            host.say("  %s:" % where)
            host.say("    %s" % line)
        return 0
    host.say("built in: " + ", ".join(u.name for u in BUILTINS))
    host.say("presets, over tools you install (never downloaded here):")
    for r in command.available():
        state = "found" if r["found"] else "not installed"
        extra = ", %s" % r["flag"] if r["flag"] else ""
        host.say("  %-26s %-14s %s, %s%s" % (r["name"], state, r["licence"], r["homepage"], extra))
    host.say("models the presets know of (your files, your licence check):")
    for m in command.model_rows():
        flag = m.get("flag") or command.model_flag(m.get("licence", ""))
        host.say("  %s: %s%s" % (m["name"], m.get("licence", ""), " (%s)" % flag if flag else ""))
    return 0


def run(a):
    verbs = {
        "inventory": run_inventory,
        "export": imported.run_export,
        "build": run_build,
        "compare": run_compare,
        "pick": run_pick,
        "backend": run_backend,
    }
    return verbs.get(a.verb, run_estimate)(a)
