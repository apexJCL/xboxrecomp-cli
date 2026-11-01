"""xbr extract: the [extract] stages of game.toml to a Godot project.

  xbr extract [--stage NAME ...] [--out DIR] [--collision] [--placements]
              [--import] [--list]
  xbr extract --scan SECTION      (maintainers: candidate roots, texlists)

Writes <out>/stages/<name>/<name>.gltf (+ .bin, textures/), one
<name>.tscn per stage and project.godot; the default out is [extract]
out, under the game's gitignored dist/. Each stage also gets
stages/<name>/report.json (its counts, what was left out, what was
approximated and what goes beyond plain glTF), and <out>/report.txt
gathers every stage's report in the words the run prints. --collision
adds, for each stage that names collision trees, stages/<name>/<name>_col.gltf
(collision.py: Godot static bodies plus a see-through debug mesh by
surface class, C toggles it) and stages/<name>/collision.json.
--placements adds, for each stage with a placements index,
stages/<name>/<name>_placements.gltf (placements.py: one named node per
record of the [[extract.placement_set]] sets, its model where one is named
and rigid, a marker box otherwise) and stages/<name>/placements.json.
--import
then imports the project headless with the pinned Godot and fails on any
Godot error.
"""

import json
import os
import shutil
from collections import Counter

from .. import host
from ..host import CliError
from . import collision, gltf, godot, ninja, placements, scan, textures
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
    p.add_argument(
        "--collision",
        action="store_true",
        help="also write each stage's collision trees as Godot static bodies with a debug mesh",
    )
    p.add_argument(
        "--placements",
        action="store_true",
        help="also write each stage's placements (enemies, items, props) as named nodes",
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


def extract_stage(image, cfg, stage, source, out_dir, with_collision=False, with_placements=False):
    """Writes one stage under out_dir; returns its gltf.Summary. With
    with_collision, a stage that names collision trees also gets them
    (extract_collision); with with_placements, a stage with a placements
    index gets its placements (placements.extract)."""
    reader = ninja.Reader(image, fmt_of(cfg))
    w = gltf.Writer(
        stage["name"],
        [],
        source,
        cfg["up"],
        cfg["textured_colour"],
        stage.get("texture_alpha"),
    )
    lists = {}
    for va, tl, xf in zip(stage["root"], stage["texlist"], transforms(stage)):
        if tl not in lists:
            lists[tl] = textures.texlist(image, tl, cfg["texname_size"])
        w.add_tree(va, reader.tree(va), lists[tl], xf)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    s = w.write(out_dir)
    s.unsupported.update(reader.unsupported)
    s.supported.update(reader.supported)
    _write_report(s, stage, out_dir, w.unmatched_texture_alpha())
    if with_collision and stage.get("collision"):
        extract_collision(image, cfg, stage, out_dir)
    if with_placements and "placements" in stage:
        placements.extract(image, fmt_of(cfg), cfg, stage, source, out_dir)
    return s


def extract_collision(image, cfg, stage, out_dir):
    """Writes <name>_col.gltf and collision.json under out_dir; returns
    the collision report."""
    reader = ninja.Reader(image, fmt_of(cfg))
    w = collision.Writer(stage["name"], cfg["up"], cfg["collision_skip"])
    w.add_trees([reader.tree(va) for va in stage["collision"]])
    return w.write(out_dir)


def transforms(stage):
    """The stage's per-root transforms ({} where it names none)."""
    return stage.get("transform") or [{}] * len(stage["root"])


def _xf_text(xf):
    return " ".join(
        "%s(%s)" % (k, ", ".join("%g" % v for v in xf[k]))
        for k in ("scale", "translation", "backdrop")
        if k in xf
    )


def _write_report(s, stage, out_dir, unmatched=()):
    trees = []
    for r, t, xf in zip(stage["root"], stage["texlist"], transforms(stage)):
        tree = {"root": "0x%08X" % r, "texlist": "0x%08X" % t}
        if xf:
            tree["transform"] = xf
        trees.append(tree)
    rep = {
        "stage": s.name,
        "trees": trees,
        "meshes": s.meshes,
        "triangles": s.triangles,
        "materials": s.materials,
        "textures": s.textures,
        "missing": sorted(set(s.missing)),
        "unsupported": dict(sorted(s.unsupported.items())),
        "approximated": dict(sorted(s.approximated.items())),
        "supported": dict(sorted(s.supported.items())),
    }
    if stage.get("texture_alpha"):
        rep["texture_alpha"] = dict(sorted(stage["texture_alpha"].items()))
    if unmatched:
        rep["texture_alpha_unmatched"] = list(unmatched)
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
    # Reports written before the key existed have none.
    for k, n in rep.get("supported", {}).items():
        lines.append("    supported: %s (%d)" % (k, n))
    if rep["missing"]:
        lines.append("    missing textures: %s" % ", ".join(rep["missing"][:10]))
    # A name no strip drew with changes nothing: a typo, or a texture the
    # stage's texlists do not hold.
    if rep.get("texture_alpha_unmatched"):
        lines.append(
            "    warning: texture_alpha matched no texture: %s"
            % ", ".join(rep["texture_alpha_unmatched"])
        )
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
            parts = ", ".join(
                "0x%08X/0x%08X" % (r, t) + (" " + _xf_text(xf) if xf else "")
                for r, t, xf in zip(st["root"], st["texlist"], transforms(st))
            )
            if st.get("collision"):
                parts += "; collision " + ", ".join("0x%08X" % c for c in st["collision"])
            if "placements" in st:
                parts += "; placements %d" % st["placements"]
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
            s = extract_stage(image, cfg, st, source, sdir, a.collision, a.placements)
        except (ImageError, textures.TextureError) as e:
            raise CliError("%s: %s" % (st["name"], e)) from e
        _summary(sdir)
        totals.update(meshes=s.meshes, triangles=s.triangles, stages=1)
        if a.collision:
            cj = os.path.join(sdir, "collision.json")
            if os.path.isfile(cj):
                with open(cj) as f:
                    rep = json.load(f)
                host.say(
                    "  %s collision: %d triangles in %d classes"
                    % (st["name"], rep["triangles"], len(rep["classes"]))
                )
            else:
                host.say("  %s collision: no collision trees in game.toml" % st["name"])
        if a.placements:
            pj = os.path.join(sdir, "placements.json")
            if os.path.isfile(pj):
                with open(pj) as f:
                    rep = json.load(f)
                host.say(
                    "  %s placements: %s"
                    % (
                        st["name"],
                        ", ".join(
                            "%s %d (%d drawn, %d skinned, %d unreadable, %d markers)"
                            % (
                                k,
                                c["records"],
                                c["drawn"],
                                c["skinned"],
                                c["unreadable"],
                                c["marker"],
                            )
                            for k, c in rep["sets"].items()
                        )
                        or "none",
                    )
                )
                for k, c in rep["sets"].items():
                    for why in c.get("errors", ()):
                        host.say("    warning: %s %s, skipped" % (k, why))
            else:
                host.say("  %s placements: no placements index in game.toml" % st["name"])
    written = [
        st["name"] for st in stages if os.path.isdir(os.path.join(out, "stages", st["name"]))
    ]
    godot.write_project(
        out,
        "%s (extracted)" % G.name,
        written,
        {n for n in written if os.path.isfile(os.path.join(out, "stages", n, n + "_col.gltf"))},
        {
            n
            for n in written
            if os.path.isfile(os.path.join(out, "stages", n, n + "_placements.gltf"))
        },
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
