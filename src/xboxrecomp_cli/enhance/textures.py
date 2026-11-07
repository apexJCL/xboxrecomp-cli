"""xbr enhance textures: what a game's textures are, and what a pack would cost.

  inventory  every texture the sources name or the dump holds, joined on the
             runtime's hash, into dist/enhance/inventory.json
  estimate   the bytes, VRAM and minutes of a preset, from that inventory
  export     a dump's textures as <hash>.png, to edit and bring back with
             enhance hud --import

The sources are game.toml's [enhance.textures] sources; the dump is the
directory a run wrote with RECOMP_DEBUG=tex_dump=<dir>. The dump alone is a
full inventory: a game with no archive reader needs nothing else.
"""

import json
import os
import sys

from .. import host, resources
from ..host import CliError
from ..package.progress import load_timings
from . import classes, estimate, imported, inventory, traced
from .sources import SourceError

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


def run(a):
    if a.verb == "inventory":
        return run_inventory(a)
    if a.verb == "export":
        return imported.run_export(a)
    return run_estimate(a)
