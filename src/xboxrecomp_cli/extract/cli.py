"""xbr extract: the [extract] stages of game.toml to a Godot project.

  xbr extract [--stage NAME ...] [--out DIR] [--import] [--list]
  xbr extract --scan SECTION      (maintainers: candidate roots, texlists)

Writes <out>/stages/<name>/<name>.gltf (+ .bin, textures/), one
<name>.tscn per stage and project.godot; the default out is [extract]
out, under the game's gitignored dist/. Each stage also gets
stages/<name>/report.json (its counts, what was left out and what was
approximated), and <out>/report.txt gathers every stage's report in the
words the run prints. --import then imports the
project headless with the pinned Godot and fails on any Godot error.
"""

import json
import os
import shutil
from collections import Counter

from .. import host
from ..host import CliError
from . import gltf, godot, ninja, scan, textures
from .image import Image, ImageError


def configure(p):
    p.add_argument(
        "--stage",
        action="append",
        default=[],
        metavar="NAME",
        help="only this stage (repeatable; default every [[extract.stage]])",
    )
    p.add_argument("--out", help="output directory (default [extract] out, dist/extract/)")
    p.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="import the project headless with the pinned Godot and check for errors",
    )
    p.add_argument("--list", action="store_true", help="list the stages game.toml names")
    p.add_argument(
        "--scan",
        metavar="SECTION",
        help="maintainers: candidate stage roots and texlists in an XBE section",
    )


def config(m):
    cfg = m.get("extract")
    if not cfg:
        raise CliError(
            "game.toml has no [extract] table: this game names no stages to extract "
            "(docs/manifest.md, [extract])"
        )
    return cfg


def fmt_of(cfg):
    return ninja.Format(cfg["vertex_header"], cfg["angles"], cfg["packed_normals"])


def extract_stage(image, cfg, stage, source, out_dir):
    """Writes one stage under out_dir; returns its gltf.Summary."""
    reader = ninja.Reader(image, fmt_of(cfg))
    w = gltf.Writer(stage["name"], [], source, cfg["up"])
    lists = {}
    for va, tl in zip(stage["root"], stage["texlist"]):
        if tl not in lists:
            lists[tl] = textures.texlist(image, tl, cfg["texname_size"])
        w.add_tree(va, reader.tree(va), lists[tl])
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    s = w.write(out_dir)
    s.unsupported.update(reader.unsupported)
    _write_report(s, stage, out_dir)
    return s


def _write_report(s, stage, out_dir):
    rep = {
        "stage": s.name,
        "trees": [
            {"root": "0x%08X" % r, "texlist": "0x%08X" % t}
            for r, t in zip(stage["root"], stage["texlist"])
        ],
        "meshes": s.meshes,
        "triangles": s.triangles,
        "materials": s.materials,
        "textures": s.textures,
        "missing": sorted(set(s.missing)),
        "unsupported": dict(sorted(s.unsupported.items())),
        "approximated": dict(sorted(s.approximated.items())),
    }
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(rep, f, indent=1)
        f.write("\n")


def _report_lines(rep):
    s = "%s: %d meshes, %d triangles, %d materials, %d textures" % (
        rep["stage"],
        rep["meshes"],
        rep["triangles"],
        rep["materials"],
        rep["textures"],
    )
    if rep["missing"]:
        s += ", %d missing" % len(rep["missing"])
    lines = [s]
    for k, n in rep["unsupported"].items():
        lines.append("    unsupported: %s (%d)" % (k, n))
    for k, n in rep["approximated"].items():
        lines.append("    approximated: %s (%d)" % (k, n))
    if rep["missing"]:
        lines.append("    missing textures: %s" % ", ".join(rep["missing"][:10]))
    return lines


def _summary(out_dir):
    with open(os.path.join(out_dir, "report.json")) as f:
        for line in _report_lines(json.load(f)):
            host.say(line)


def write_report_txt(out, names):
    """<out>/report.txt: every stage extracted under out, so a run over one
    stage keeps the others' lines. A stage written before reports existed
    says so rather than vanishing from the list."""
    lines = []
    for n in names:
        sdir = os.path.join(out, "stages", n)
        p = os.path.join(sdir, "report.json")
        if os.path.isfile(p):
            with open(p) as f:
                lines += _report_lines(json.load(f))
        elif os.path.isdir(sdir):
            lines.append("%s: no report (re-extract)" % n)
    with open(os.path.join(out, "report.txt"), "w") as f:
        f.write("".join(line + "\n" for line in lines))


def _scan(G, cfg, section):
    image = Image.load(G.xbe)
    fmt = fmt_of(cfg)
    host.say("roots in %s (objects, models):" % section)
    for va, objs, models in scan.roots(image, section, fmt):
        host.say("  0x%08X  %5d objects  %5d models" % (va, objs, models))
    host.say("texlists in %s (names, first):" % section)
    for va, n, first in scan.texlists(image, section, cfg["texname_size"]):
        host.say("  0x%08X  %5d  %s" % (va, n, first))
    return 0


def run(a):
    G = host.g()
    cfg = config(G.m)
    if a.scan:
        try:
            return _scan(G, cfg, a.scan)
        except ImageError as e:
            raise CliError(str(e)) from e
    stages = cfg["stage"]
    if a.list:
        for st in stages:
            parts = ", ".join("0x%08X/0x%08X" % p for p in zip(st["root"], st["texlist"]))
            host.say("%-10s %-8s root/texlist %s" % (st["name"], st["section"] or "-", parts))
        return 0
    # Stage names match whatever case the player types (map11, MAP11).
    known = {st["name"].casefold() for st in stages}
    want = {n.casefold() for n in a.stage}
    for n in a.stage:
        if n.casefold() not in known:
            raise CliError(
                "no stage %r in [[extract.stage]] (%s extract --list)" % (n, host.prog())
            )
    picked = [st for st in stages if not want or st["name"].casefold() in want]
    if not picked:
        raise CliError("[extract] names no stage ([[extract.stage]])")
    out = os.path.abspath(a.out) if a.out else G.path(cfg["out"])
    if not os.path.isfile(G.xbe):
        raise CliError("no XBE at %s: import your dump first" % G.xbe)
    image = Image.load(G.xbe)
    try:
        source = textures.Source(G.game_files, cfg["textures"])
    except OSError as e:
        raise CliError("[extract] textures: %s" % e) from e
    host.step("extract %d stage(s) to %s" % (len(picked), out))
    totals = Counter()
    for st in picked:
        sdir = os.path.join(out, "stages", st["name"])
        try:
            s = extract_stage(image, cfg, st, source, sdir)
        except (ImageError, textures.TextureError) as e:
            raise CliError("%s: %s" % (st["name"], e)) from e
        _summary(sdir)
        totals.update(meshes=s.meshes, triangles=s.triangles, stages=1)
    godot.write_project(
        out,
        "%s (extracted)" % G.name,
        [st["name"] for st in stages if os.path.isdir(os.path.join(out, "stages", st["name"]))],
    )
    write_report_txt(out, [st["name"] for st in stages])
    host.say(
        "%d stage(s), %d meshes, %d triangles: %s"
        % (totals["stages"], totals["meshes"], totals["triangles"], out)
    )
    host.say("what each stage leaves out or approximates: %s" % os.path.join(out, "report.txt"))
    host.say("game data from your own dump: never commit or share it")
    if a.do_import:
        host.step("Godot %s import (headless)" % godot.VERSION)
        binary = godot.ensure(G.third_party)
        godot.import_project(binary, out, os.path.join(out, "import.log"))
        host.say("imported with no errors (log %s)" % os.path.join(out, "import.log"))
    return 0
