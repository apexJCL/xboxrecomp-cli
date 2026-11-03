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

Flat colour hides corners, so each face is shaded by its direction: a
brightness from its normal against one fixed light (LIGHT, from above
at an angle, AMBIENT the floor), baked into COLOR_0 and multiplied with
the class colour. The material stays unlit, so the shading is the same
under any scene light. Godot 4.7.2's glTF import keeps COLOR_0 but does
not use it as albedo on a material the file defines, so collision.gd
turns vertex_color_use_as_albedo on for the class meshes.

Each class node also gets a child `<label>_edges`: opaque, darker lines
(glTF mode LINES) along its creases, where two faces meet at more than
CREASE_DEG, and its open edges. Corners within WELD of each other are
one vertex, and a triangle two of whose corners weld is left out. A
seam between coplanar faces that share both corners draws no line, but
a T-junction (a corner of one face on the side of another) shows as
open edges, and so does a class border, in both classes. The winding is
not trusted, so a face's two sides count as one plane: a fold sharper
than 180 - CREASE_DEG degrees (160; two faces nearly back to back) draws no
line either. The child is a plain mesh (no hint suffix, and Godot
builds no shape from lines). In the scene C hides the faces and E the
lines, each without the other.

A second child `<label>_flat` holds the same faces coloured by how far
each is from exactly flat or exactly vertical, in the scene's frame
(after the up rotation): under FLAT_BANDS[0] degrees grey, then yellow,
orange, and red from FLAT_BANDS[2] on (a real slope); each band starts
at its edge. A sliver (two corners within WELD, a face the edges leave
out) has no trustworthy normal: it is dark grey and counted apart, and
keeps its shape. A face a few tenths of a degree off flat is one the
game may treat as a slope, so the view finds them without maths in the
scene. collision.json counts each class's faces per band. F swaps the
class colours for these (off by default), and O makes the faces opaque
(off by default; still unlit); both work with C, E and the editor's eye.
The scene draws the lines a hair toward the camera (EDGE_PULL), so the
opaque faces they lie on do not hide them.
"""

import json
import math
import os
from collections import Counter

from . import gltf, ninja

# How opaque the debug mesh is.
DEBUG_ALPHA = 0.35
SUFFIX = "-col"
EDGES = "_edges"
# The shading light, in Godot's frame (+Y up): from above, off to one
# side, so walls facing X and Z get different shades. AMBIENT keeps a face
# edge-on to it from going black.
LIGHT = (0.5, 1.0, 0.25)
AMBIENT = 0.3
# Faces meeting at more than this many degrees get an edge line.
CREASE_DEG = 20.0
# Corners closer than this (game units) are one vertex for the edges.
WELD = 1e-3
# The edge lines' colour: the class colour this much darker.
EDGE_DARKEN = 0.3
LINES = 1  # glTF primitive mode
FLAT = "_flat"
# Flatness band edges, in degrees off flat or vertical: a face below the
# first is grey, then yellow, orange and, from the last on, red.
FLAT_BANDS = (0.1, 2.0, 10.0)
# The last band is the slivers: faces with two corners within WELD.
FLAT_NAMES = ("flat", "near", "tilted", "slope", "sliver")
FLAT_COLOURS = (0x9A9A9A, 0xF0D020, 0xF08020, 0xE02020, 0x4A4A4A)
SLIVER = len(FLAT_BANDS) + 1
# The edge lines' vertices are pulled this fraction of their distance
# toward the camera, so they stay in front of the faces they lie on when
# O makes those write depth, and still hide behind anything else.
EDGE_PULL = 0.002


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


def _unit(v):
    ln = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return (v[0] / ln, v[1] / ln, v[2] / ln)


def light_in(up):
    """LIGHT in the tree's own frame: the top node's up rotation undone."""
    q = gltf.UP_ROTATION[up]
    lt = _unit(LIGHT)
    return lt if not q else gltf._qrot((-q[0], -q[1], -q[2], q[3]), lt)


def shade(n, light):
    """A face's brightness, AMBIENT to 1. Both sides of a face get the same
    shade (the mesh is double-sided and the game's winding is not relied
    on)."""
    d = abs(n[0] * light[0] + n[1] * light[1] + n[2] * light[2])
    return AMBIENT + (1.0 - AMBIENT) * d


def flatness_deg(n):
    """How many degrees the face with normal n (+Y up) is off exactly flat
    or exactly vertical, whichever is nearer. atan2 keeps tenths of a
    degree exact where acos of a dot near 1 would not."""
    off_flat = math.degrees(math.atan2(math.hypot(n[0], n[2]), abs(n[1])))
    return min(off_flat, 90.0 - off_flat)


def _sliver(t, weld=WELD):
    """Whether two of t's corners are within weld of each other (on every
    axis), as crease_edges welds them."""
    return any(
        all(abs(a - b) <= weld for a, b in zip(t[i], t[j])) for i, j in ((0, 1), (1, 2), (2, 0))
    )


def flat_band(deg, edges=FLAT_BANDS):
    """The band index of a face deg off flat or vertical: 0 below
    edges[0], len(edges) from edges[-1] on."""
    return sum(deg >= e for e in edges)


def crease_edges(tris, deg=CREASE_DEG, weld=WELD):
    """(points, pairs): the edges of tris (corner triples) where two faces
    meet at more than deg degrees, and the open ones (one face); an edge
    shared by more faces counts as a crease unless they are all coplanar.
    Corners within weld of each other (on every axis) are one vertex, and
    a triangle with two welded corners is left out. A face's two sides are
    one plane, so a seam between faces wound either way is coplanar, and
    so is a fold sharper than 180 - deg degrees."""
    cos_max = math.cos(math.radians(deg))
    cells, points = {}, []  # weld cell -> ids of the points in it

    def vid(p):
        k = tuple(round(c / weld) for c in p)
        # Two corners within weld can round into neighbouring cells.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for i in cells.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                        if all(abs(a - b) <= weld for a, b in zip(points[i], p)):
                            return i
        cells.setdefault(k, []).append(len(points))
        points.append(p)
        return len(points) - 1

    faces = {}  # (a, b) with a < b -> normals of the faces on it
    for t in tris:
        n = _normal(t)
        if n is None:
            continue
        v = [vid(p) for p in t]
        if len(set(v)) < 3:
            continue
        for a, b in ((v[0], v[1]), (v[1], v[2]), (v[2], v[0])):
            faces.setdefault((min(a, b), max(a, b)), []).append(n)
    pairs = []
    for e, ns in faces.items():
        n0 = ns[0]
        if len(ns) == 1 or any(
            abs(n0[0] * m[0] + n0[1] * m[1] + n0[2] * m[2]) < cos_max for m in ns[1:]
        ):
            pairs.append(e)
    # Only the points the lines use, renumbered.
    used = sorted({i for e in pairs for i in e})
    remap = {old: new for new, old in enumerate(used)}
    return [points[i] for i in used], [(remap[a], remap[b]) for a, b in pairs]


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
        self.edges = Counter()  # class -> edge lines
        self.light = light_in(up)
        self.bands = {}  # class -> Counter of flatness band index
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
        pos, nrm, col, flat = [], [], [], []
        q = gltf.UP_ROTATION[self.up]
        bands = self.bands.setdefault(c, Counter())
        for t in tris:
            n = _normal(t)
            if n is None:
                self.stats["degenerate triangles"] += 1
                continue
            pos.extend(t)
            nrm.extend((n, n, n))
            # COLOR_0 is linear: store the shade so it shows at that
            # brightness on screen.
            s = shade(n, self.light)
            g = _srgb_to_linear(255.0 * s)
            col.extend(((g, g, g),) * 3)
            # The band is measured in the scene's frame, the up rotation
            # applied. Both ups in use ("y", and "-y", a half turn about
            # X) leave every band as it is, but a turn that tilts the
            # scene would not, so the rotation stays. A sliver's normal is
            # noise, so it gets a band of its own (it keeps its shape).
            # The colour keeps the shade so corners still read.
            if _sliver(t):
                b = SLIVER
            else:
                b = flat_band(flatness_deg(n if not q else gltf._qrot(q, n)))
            bands[b] += 1
            fc = FLAT_COLOURS[b]
            fl = tuple(_srgb_to_linear(((fc >> k) & 0xFF) * s) for k in (16, 8, 0))
            flat.extend((fl,) * 3)
        if not pos:
            return None
        self.tris[c] += len(pos) // 3
        label = self._label(c)
        row = class_row(c, self.classes)
        colour = c if row is None or row.get("colour") is None else row["colour"]
        srgb = [(colour >> s) & 0xFF for s in (16, 8, 0)]
        rgb = [_srgb_to_linear(v) for v in srgb]
        mat = self._material(label, rgb + [DEBUG_ALPHA], blend=True)
        pa = self.doc.accessor("f", 3, pos, "VEC3", gltf.ARRAY_BUFFER, minmax=True)
        na = self.doc.accessor("f", 3, nrm, "VEC3", gltf.ARRAY_BUFFER)
        ca = self.doc.accessor("f", 3, col, "VEC3", gltf.ARRAY_BUFFER)
        attrs = {"POSITION": pa, "NORMAL": na, "COLOR_0": ca}
        mesh = self.doc.add(
            "meshes", {"name": label, "primitives": [{"attributes": attrs, "material": mat}]}
        )
        name = label + ("" if c in self.skip else SUFFIX)
        node = {"name": name, "mesh": mesh}
        at = self.doc.add("nodes", node)
        # The flatness view: the same faces (shared positions and
        # normals) in band colours, on a white material.
        fa = self.doc.accessor("f", 3, flat, "VEC3", gltf.ARRAY_BUFFER)
        fmat = self._material(label + FLAT, [1.0, 1.0, 1.0, DEBUG_ALPHA], blend=True)
        fattrs = {"POSITION": pa, "NORMAL": na, "COLOR_0": fa}
        fmesh = self.doc.add(
            "meshes",
            {"name": label + FLAT, "primitives": [{"attributes": fattrs, "material": fmat}]},
        )
        kids = [self.doc.add("nodes", {"name": label + FLAT, "mesh": fmesh})]
        edges = self._edges_node(c, label, tris, [_srgb_to_linear(v * EDGE_DARKEN) for v in srgb])
        if edges is not None:
            kids.append(edges)
        node["children"] = kids
        return at

    def _material(self, label, rgba, blend):
        m = {
            "name": label,
            "pbrMetallicRoughness": {
                "baseColorFactor": rgba,
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
            "doubleSided": True,
            "extensions": {gltf.UNLIT: {}},
        }
        if blend:
            m["alphaMode"] = "BLEND"
        if gltf.UNLIT not in self.doc.j["extensionsUsed"]:
            self.doc.j["extensionsUsed"].append(gltf.UNLIT)
        return self.doc.add("materials", m)

    def _edges_node(self, c, label, tris, rgb):
        """The class's crease and open edges as a LINES mesh node, or None."""
        points, pairs = crease_edges(tris)
        if not pairs:
            return None
        self.edges[c] += len(pairs)
        name = label + EDGES
        mat = self._material(name, rgb + [1.0], blend=False)
        pa = self.doc.accessor("f", 3, points, "VEC3", gltf.ARRAY_BUFFER, minmax=True)
        idx = [(i,) for e in pairs for i in e]
        ia = self.doc.accessor("I", 1, idx, "SCALAR", gltf.ELEMENT_ARRAY_BUFFER)
        prim = {"attributes": {"POSITION": pa}, "indices": ia, "mode": LINES, "material": mat}
        mesh = self.doc.add("meshes", {"name": name, "primitives": [prim]})
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
        e = {"triangles": n, "shape": c not in self.skip, "edges": self.edges[c]}
        b = self.bands.get(c, Counter())
        e["flatness"] = {k: b[i] for i, k in enumerate(FLAT_NAMES)}
        row = class_row(c, self.classes)
        if row is not None:
            e["name"] = row["name"]
        return e
