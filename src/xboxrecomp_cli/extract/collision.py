"""A stage's collision trees to glTF: <name>_col.gltf and <name>_col.bin.

Some Ninja engines keep the stage's collision as ordinary object trees
that are never drawn, whose strips carry the surface class in their
material diffuse. Every strip with a diffuse is baked, textured or not. At load the game walks each tree, applies every node's
transform whatever its eval flags say (a collision tree's root is often
hidden), and bakes the triangles into world space for its own spatial
index. This module does the same walk and writes the world triangles
grouped by class, one node per class, named `col_0x<rrggbb>-col` so
that Godot's scene importer gives each node a StaticBody3D with a
concave (trimesh) shape and keeps its mesh. A class listed in [extract]
collision_skip (one the game never tests, such as a trigger volume)
keeps its mesh and gets no shape: its node is named `col_0x<rrggbb>`.

A game's [[extract.collision_class]] rows name the classes: the first
row whose value matches the class under its mask names the node
`col_<name>_0x<rrggbb>` (the colour stays, so classes that share a name
still get a node each), and its colour, when given, replaces the class
colour in the debug mesh. A class no row matches keeps `col_0x<rrggbb>`.

The `0x` is there for Godot: before it matches an import hint it strips
a name's trailing digits and '_', so `col_rigid_000000` would import as
a RigidBody3D and `col_a_col_000000` as a StaticBody3D. No hint ends in
`x`, so with it no name of ours strips into one.

The meshes are a debug view: each class draws in its own diffuse colour,
see-through, unlit and double-sided, so walls stay readable from either
side. The Godot scene toggles them (godot.py's COLLISION_GD); the shapes
stay. Nothing here reads textures.
"""

import json
import math
import os
from collections import Counter

from . import gltf, ninja

# How opaque the debug mesh is.
DEBUG_ALPHA = 0.35
SUFFIX = "-col"


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _apply(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))


def _local(n, flags):
    """The node's matrix: translate, then rotate, then scale, as Ninja's
    matrix stack applies them.

    The rotation always uses the default order (X first, then Y, then Z)
    and ignores EVAL_ZXY: the game's collision bake calls RotZ, RotY and
    RotX in that fixed order for every node, unlike its draw walker. So
    the baked triangles match what the game tests, even where the drawn
    mesh is rotated another way."""
    x, y, z, w = ninja.quat(n.ang, flags & ~ninja.EVAL_ZXY)
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    s = n.scl
    return [
        [r[0][0] * s[0], r[0][1] * s[1], r[0][2] * s[2], n.pos[0]],
        [r[1][0] * s[0], r[1][1] * s[1], r[1][2] * s[2], n.pos[1]],
        [r[2][0] * s[0], r[2][1] * s[1], r[2][2] * s[2], n.pos[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


IDENTITY = [[float(i == j) for j in range(4)] for i in range(4)]


def class_of(strip):
    """The surface class: the diffuse as 0xRRGGBB (the material chunk
    stores it B, G, R, A), or None for a strip with no material colour."""
    d = strip.diffuse
    if d is None:
        return None
    return d[2] << 16 | d[1] << 8 | d[0]


def world_triangles(roots, stats=None):
    """{class: [(p0, p1, p2), ...]} in the tree's world space, every
    transform applied and every model kept, hidden or not. stats (a
    Counter) counts strips with no class."""
    out = {}

    def walk(nodes, parent):
        for n in nodes:
            m = _mat_mul(parent, _local(n, n.flags))
            if n.model:
                for s in n.model.strips:
                    c = class_of(s)
                    if c is None:
                        if stats is not None:
                            stats["strips without a class colour"] += 1
                        continue
                    tl = out.setdefault(c, [])
                    for tri in s.tris:
                        if any(k[0] not in n.model.verts for k in tri):
                            if stats is not None:
                                stats["triangles with a missing vertex"] += 1
                            continue
                        tl.append(tuple(_apply(m, n.model.verts[k[0]]) for k in tri))
            walk(n.children, m)

    walk(roots, IDENTITY)
    return out


def _normal(t):
    a, b, c = t
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    return None if ln == 0 else (n[0] / ln, n[1] / ln, n[2] / ln)


def _srgb_to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def class_row(c, rows):
    """The first [[extract.collision_class]] row that matches class c
    (c & mask == value), or None."""
    for r in rows:
        if c & r.get("mask", 0xFFFFFF) == r["value"]:
            return r
    return None


class Writer:
    def __init__(self, name, up="y", skip=(), classes=()):
        """classes: the [[extract.collision_class]] rows, in order."""
        self.name = name + "_col"
        self.up = up
        self.skip = set(skip)
        self.classes = list(classes)
        self.doc = gltf._Doc()
        self.tris = Counter()  # class -> triangles
        self.stats = Counter()

    def add_trees(self, trees):
        """trees: the root lists of every collision tree of the stage."""
        by_class = {}
        for roots in trees:
            for c, tl in world_triangles(roots, self.stats).items():
                by_class.setdefault(c, []).extend(tl)
        kids = []
        for c in sorted(by_class):
            node = self._class_node(c, by_class[c])
            if node is not None:
                kids.append(node)
        top = {"name": self.name, "children": kids}
        rot = gltf.UP_ROTATION[self.up]
        if rot:
            top["rotation"] = rot
        self.doc.j["scenes"][0]["nodes"].append(self.doc.add("nodes", top))

    def _class_node(self, c, tris):
        pos, nrm = [], []
        for t in tris:
            n = _normal(t)
            if n is None:
                self.stats["degenerate triangles"] += 1
                continue
            pos.extend(t)
            nrm.extend((n, n, n))
        if not pos:
            return None
        self.tris[c] += len(pos) // 3
        label = self._label(c)
        row = class_row(c, self.classes)
        colour = c if row is None or row.get("colour") is None else row["colour"]
        rgb = [_srgb_to_linear((colour >> s) & 0xFF) for s in (16, 8, 0)]
        mat = self.doc.add(
            "materials",
            {
                "name": label,
                "pbrMetallicRoughness": {
                    "baseColorFactor": rgb + [DEBUG_ALPHA],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "alphaMode": "BLEND",
                "doubleSided": True,
                "extensions": {gltf.UNLIT: {}},
            },
        )
        if gltf.UNLIT not in self.doc.j["extensionsUsed"]:
            self.doc.j["extensionsUsed"].append(gltf.UNLIT)
        pa = self.doc.accessor("f", 3, pos, "VEC3", gltf.ARRAY_BUFFER, minmax=True)
        na = self.doc.accessor("f", 3, nrm, "VEC3", gltf.ARRAY_BUFFER)
        mesh = self.doc.add(
            "meshes",
            {
                "name": label,
                "primitives": [{"attributes": {"POSITION": pa, "NORMAL": na}, "material": mat}],
            },
        )
        name = label + ("" if c in self.skip else SUFFIX)
        return self.doc.add("nodes", {"name": name, "mesh": mesh})

    def _label(self, c):
        row = class_row(c, self.classes)
        return "col_0x%06x" % c if row is None else "col_%s_0x%06x" % (row["name"], c)

    def write(self, out_dir):
        """Writes <name>_col.gltf/.bin and collision.json; returns the
        report dict."""
        j = dict(self.doc.j)
        if self.doc.bin:
            j["buffers"] = [{"uri": self.name + ".bin", "byteLength": len(self.doc.bin)}]
            with open(os.path.join(out_dir, self.name + ".bin"), "wb") as f:
                f.write(self.doc.bin)
        for k in list(j):
            if isinstance(j[k], list) and not j[k]:
                del j[k]
        with open(os.path.join(out_dir, self.name + ".gltf"), "w", encoding="utf-8") as f:
            json.dump(j, f, indent=1)
            f.write("\n")
        rep = {
            "triangles": sum(self.tris.values()),
            "classes": {"%06X" % c: self._entry(c, n) for c, n in sorted(self.tris.items())},
            "left_out": dict(sorted(self.stats.items())),
        }
        with open(os.path.join(out_dir, "collision.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, sort_keys=True)
            f.write("\n")
        return rep

    def _entry(self, c, n):
        e = {"triangles": n, "shape": c not in self.skip}
        row = class_row(c, self.classes)
        if row is not None:
            e["name"] = row["name"]
        return e
