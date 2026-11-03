"""A stage's placements (xbr extract --placements): what the game spawns
into a stage at load, from the per-stage tables [[extract.placement_set]]
names, as named nodes of <name>_placements.gltf plus placements.json.

Each set is a table of pointers indexed by the stage's number (the
[[extract.stage]] placements key) plus each of the set's layers, since a
game may load more than one entry for a stage (an area's own list beside
the stage's). A stage's placement_layers replaces a set's layers on that
stage alone: a negative offset reads another stage's entry, [] none. An
entry points to a u32 count and a u32 pointer to the records. A record
holds a class dword (low byte the class, the next byte its variant, the
top byte flags), a position and a rotation (three f32 radians, X applied
first, as Ninja objects turn); the rest of it stays raw in
placements.json for whoever reads the class's own fields. That class
dword is the one record shape read here.

A record whose position or rotation is not finite keeps its bytes but no
transform: it is unreadable and gets a marker at its set's origin. A
table entry or record list that cannot be read is an error of its set and
layer in placements.json; the other sets and layers are still exported.

Positions are in the game's world. [extract] placement_world says how it
sits against the stage trees' data: "y", the same axes, or "-y", the
data turned a half turn about X (a game that pushes that turn before it
draws its stage trees). Each set is a top node carrying the turn back
into the data's axes, then the stage's own up turn. A model under a
placement is authored in the data's axes like the stage trees, so with
"-y" the game pushes the half turn after the position and before the
record's rotation (T * Rx(pi) * R), and the placement node carries
Rx(pi) * R. Put after R instead, the turn would mirror the Y and Z
angles.

A class [[extract.placement_model]] names is drawn with its model: a
variant table (one record per variant, a flags-set table optional) or a
fixed object and texlist. A weighted (skinned) model is not drawn: its
vertices are weighted between nodes and its polygon lists cached and
drawn at another node, which a rigid glTF node cannot carry. It gets the
set's marker box like a class without a model, and the node's extras
name the model so a later skinned export can pick it up.
"""

import json
import math
import os
import struct

from . import gltf, ninja, textures
from .gltf import UP_ROTATION
from .image import ImageError

# Marker colours by set order (0xRRGGBB): only a viewing aid.
PALETTE = (0xE04040, 0x40C040, 0x4080F0, 0xF0C020, 0xC040C0, 0x40C0C0, 0xF08030, 0x808080)
# A count past this is not a placement list (a wrong table or stage).
MAX_RECORDS = 4096


class Placement:
    def __init__(self, set_name, layer, index, raw, at):
        self.set = set_name
        self.layer = layer
        self.index = index
        self.raw = raw
        cls = struct.unpack_from("<I", raw, at["class_at"])[0]
        self.cls, self.variant, self.flags = cls & 0xFF, (cls >> 8) & 0xFF, cls >> 24
        self.word = cls
        self.pos = struct.unpack_from("<3f", raw, at["pos_at"])
        self.rot = struct.unpack_from("<3f", raw, at["rot_at"])
        self.model = self.texlist = 0
        self.skinned = self.drawn = self.variant_out_of_range = False
        # Garbage or an uninitialised record: no transform to give it.
        self.bad_transform = not all(math.isfinite(v) for v in self.pos + self.rot)
        self.unreadable = self.bad_transform

    @property
    def name(self):
        return "%s_l%d_%03d_cls%x" % (self.set, self.layer, self.index, self.word & 0xFFFF)


def _read_layer(image, s, stage_index, layer):
    entry = s["table"] + 4 * (stage_index + layer)
    p = image.unpack("<I", entry)[0]
    if not p:
        return []
    n, recs = image.unpack("<II", p)
    if n > MAX_RECORDS:
        raise ImageError("%d records at 0x%08X, not a placement list" % (n, p))
    return [
        Placement(s["name"], layer, i, image.read(recs + s["stride"] * i, s["stride"]), s)
        for i in range(n)
    ]


def stage_layers(sets, stage):
    """{set name: the offsets the stage reads}: the set's layers unless
    the stage's placement_layers names the set."""
    over = stage.get("placement_layers") or {}
    return {s["name"]: list(over.get(s["name"], s["layers"])) for s in sets}


def read(image, sets, stage_index, layers=None):
    """Every placement of the stage, set by set and layer by layer, and
    the (set, layer, message) of each layer that could not be read.
    layers ({set name: offsets}) replaces the sets' own."""
    out, errors = [], []
    for s in sets:
        for layer in (layers or {}).get(s["name"], s["layers"]):
            try:
                out += _read_layer(image, s, stage_index, layer)
            except (ImageError, struct.error) as e:
                errors.append((s["name"], layer, str(e)))
    return out, errors


def resolve(image, models, placements):
    """Fills each placement's model and texlist VAs from the
    [[extract.placement_model]] entries; returns the classes with none."""
    by = {(m["set"], m["class"]): m for m in models}
    missing = set()
    for p in placements:
        m = by.get((p.set, p.cls))
        if m is None:
            missing.add((p.set, p.cls))
            continue
        if "table" in m:
            if "variants" in m and p.variant >= m["variants"]:
                p.variant_out_of_range = True
                continue
            base = m["table_flagged"] if p.flags and "table_flagged" in m else m["table"]
            rec = base + m["stride"] * p.variant
            try:
                p.model = image.unpack("<I", rec + m["object_at"])[0]
                p.texlist = image.unpack("<I", rec + m["texlist_at"])[0]
            except ImageError:
                p.model = p.texlist = 0
        else:
            p.model, p.texlist = m["object"], m["texlist"]
        if p.model and not image.contains(p.model, 0x34):
            p.model = 0
    return sorted(missing)


def skinned(reader):
    """A tree read with weighted vertices (the *_NF chunks) or cached
    polygon lists: what Ninja's weighted models are made of."""
    return reader.unsupported["cached polygon list"] > 0 or any(
        k.startswith("vertex chunk ") and k.endswith("NF") and n for k, n in reader.stats.items()
    )


def _rotation(rad):
    # Record angles may run many turns (a spinning prop's accumulator).
    return ninja.quat([math.remainder(a, 2 * math.pi) for a in rad], 0)


def _floats(v):
    return [float(a) if math.isfinite(a) else None for a in v]


def _turns(cfg):
    """The rotations of a set's top node and the turn each placement
    applies after its own rotation (None: none)."""
    half = UP_ROTATION["-y"]
    world = cfg["placement_world"] == "-y"
    up = UP_ROTATION[cfg["up"]] is not None
    # Two half turns about X cancel.
    return (half if world != up else None), (half if world else None)


def _mul(p, r):
    """The quaternion (x, y, z, w) p * r: r applied first."""
    px, py, pz, pw = p
    qx, qy, qz, qw = r
    return [
        pw * qx + px * qw + py * qz - pz * qy,
        pw * qy - px * qz + py * qw + pz * qx,
        pw * qz + px * qy - py * qx + pz * qw,
        pw * qw - px * qx - py * qy - pz * qz,
    ]


def add(writer, image, fmt, cfg, sets, placements):
    """Adds the placement nodes to writer (a gltf.Writer of its own) and
    returns placements.json's content."""
    colour = {s["name"]: s.get("colour", PALETTE[i % len(PALETTE)]) for i, s in enumerate(sets)}
    size = {s["name"]: s["size"] for s in sets}
    groups, rows, trees, lists = {}, [], {}, {}
    top_turn, turn = _turns(cfg)
    for p in placements:
        nd = {"name": p.name}
        if not p.bad_transform:
            if any(p.pos):
                nd["translation"] = [float(v) for v in p.pos]
            rot = _rotation(p.rot) if any(p.rot) else None
            if turn:
                # The game's T * Rx(pi) * R: the turn outside the rotation.
                rot = _mul(turn, rot) if rot else turn
            if rot:
                nd["rotation"] = rot
        kids = []
        if p.model and not p.bad_transform:
            if p.model not in trees:
                r = ninja.Reader(image, fmt)
                try:
                    roots = r.tree(p.model)
                except (ImageError, struct.error):
                    roots = None
                trees[p.model] = (roots, r)
            roots, r = trees[p.model]
            # A weighted chunk the reader does not know can derail it
            # before the end of the tree; what it read still tells.
            p.skinned = skinned(r)
            p.unreadable = roots is None and not p.skinned
            if roots and not p.skinned:
                if p.texlist not in lists:
                    try:
                        lists[p.texlist] = textures.texlist(image, p.texlist, cfg["texname_size"])
                    except (ImageError, textures.TextureError):
                        lists[p.texlist] = []
                m = {"name": p.name + "_model"}
                m["children"] = writer.add_nodes(roots, lists[p.texlist])
                kids.append(writer.doc.add("nodes", m))
                p.drawn = True
        if not kids:
            mesh = writer.marker("marker_" + p.set, size[p.set], colour[p.set])
            kids.append(writer.doc.add("nodes", {"name": p.name + "_marker", "mesh": mesh}))
        nd["children"] = kids
        x = {
            "set": p.set,
            "layer": p.layer,
            "index": p.index,
            "class": p.cls,
            "variant": p.variant,
            "flags": p.flags,
            # NaN and infinities are not JSON: null.
            "rotation_radians": _floats(p.rot),
        }
        if p.model:
            x.update(model="0x%08X" % p.model, texlist="0x%08X" % p.texlist, skinned=p.skinned)
        if p.unreadable:
            x["unreadable"] = True
        if p.variant_out_of_range:
            x["variant_out_of_range"] = True
        nd["extras"] = x
        groups.setdefault(p.set, []).append(writer.doc.add("nodes", nd))
        row = dict(x, name=p.name, position=_floats(p.pos), raw=p.raw.hex())
        rows.append(row)
    for s in sets:
        if s["name"] in groups:
            top = {"name": s["name"], "children": groups[s["name"]]}
            if top_turn:
                top["rotation"] = top_turn
            top = writer.doc.add("nodes", top)
            writer.doc.j["scenes"][0]["nodes"].append(top)
    return rows


def extract(image, fmt, cfg, stage, source, out_dir):
    """Writes <name>_placements.gltf (+ .bin, textures/) and
    placements.json under out_dir; returns placements.json's summary."""
    sets = cfg["placement_set"]
    layers = stage_layers(sets, stage)
    placements, errors = read(image, sets, stage["placements"], layers)
    missing = resolve(image, cfg["placement_model"], placements)
    w = gltf.Writer(stage["name"] + "_placements", [], source, cfg["up"], cfg["textured_colour"])
    rows = add(w, image, fmt, cfg, sets, placements)
    s = w.write(out_dir)
    counts = {}

    def count(name):
        return counts.setdefault(
            name, {"records": 0, "drawn": 0, "skinned": 0, "unreadable": 0, "marker": 0}
        )

    for name, layer, why in errors:
        count(name).setdefault("errors", []).append("layer %d: %s" % (layer, why))
    for p in placements:
        c = count(p.set)
        c["records"] += 1
        if p.drawn:
            c["drawn"] += 1
        elif p.skinned:
            c["skinned"] += 1
        elif p.unreadable:
            c["unreadable"] += 1
        else:
            c["marker"] += 1
    rep = {
        "stage": stage["name"],
        "stage_index": stage["placements"],
        "layers": layers,
        "sets": counts,
        "classes_without_model": ["%s 0x%02X" % (k, c) for k, c in missing],
        "meshes": s.meshes,
        "triangles": s.triangles,
        "missing_textures": sorted(set(s.missing)),
        "placements": rows,
    }
    with open(os.path.join(out_dir, "placements.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, allow_nan=False)
        f.write("\n")
    return rep
