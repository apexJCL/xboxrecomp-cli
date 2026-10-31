"""One stage to glTF 2.0: <name>.gltf, <name>.bin and textures/<tex>.png.

Each Ninja object is a node (translation, rotation, scale); each model a
mesh with one primitive per material, its vertices de-duplicated by
(vertex, u, v). A material is a texture and its sampler (the tiny chunk's
flip and clamp bits as glTF wraps), a colour (the diffuse, or white for a
strip with UVs or an environment map and a texture under [extract]
textured_colour = "white"), the strip's
double-sided and ignore-light flags (KHR_materials_unlit) and an alpha
mode from the texture's alpha and the strip's use-alpha flag. A hidden
object keeps its node and loses its mesh.

Approximations, counted in the summary's `approximated`:
- an environment-mapped strip carries no UVs; the game looks its texture
  up from the view-space normal each frame. A static scene has no view,
  so its UVs come from the model-space normal (a sphere map frozen at
  the identity view).
- Godot's glTF importer keeps one repeat flag per material, taken from
  wrapT, so a texture clamped on one axis and repeated on the other
  smears or tiles on both. When the two wraps differ, an axis whose UVs
  stay inside [0, 1] takes the other axis's wrap (both inside: clamp);
  wraps that agree are kept, since repeat bleeds the opposite edge under
  bilinear filtering where clamp does not. The wraps that still differ
  per axis are counted as unsupported. Godot cannot mirror either, so a
  mirrored axis that leaves [0, 1] repeats and is counted.
- a texture whose alpha never reaches 0 but dips below 240 is glass and
  blends even without the strip's use-alpha flag.
- a texture named in the stage's texture_alpha blends at that opacity:
  the game covers such a surface with a pass of its own (a planar
  reflection drawn into a render target and blended over the floor),
  which a static scene stands in for with a mirrored copy of the room
  below a see-through floor.

glTF has no additive blend. A strip drawn with Ninja blend 0x21 (source
alpha, one: the lamp glows) is written as BLEND with the material extra
`"xbr_blend": "add"`, which the Godot stage scene turns into its
material's additive blend mode (godot.py's STAGE_GD). Other glTF readers
show it as plain alpha blending.
"""

import json
import os
import struct
import urllib.parse
from collections import Counter

from . import ninja, textures

FLOAT, UINT = 5126, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
REPEAT, CLAMP, MIRROR = 10497, 33071, 33648
LINEAR, LINEAR_MIPMAP_LINEAR = 9729, 9987
UNLIT = "KHR_materials_unlit"
# The material extra the Godot stage scene reads (godot.py STAGE_GD).
EXTRA_BLEND = "xbr_blend"
# Tiny chunk flag bits (the byte above the texture id word).
TINY_FLIP_U, TINY_FLIP_V, TINY_CLAMP_U, TINY_CLAMP_V = 0x80, 0x40, 0x20, 0x10
# up = "-y": the game's world is Y-down (its floors face -Y in the data);
# a half turn about X makes it glTF's Y-up and keeps it right-handed. A
# flip the game applies at draw time (its view matrix) is not that: such a
# world is already Y-up.
UP_ROTATION = {"y": None, "-y": [1.0, 0.0, 0.0, 0.0]}


class Summary:
    def __init__(self, name):
        self.name = name
        self.meshes = 0
        self.triangles = 0
        self.materials = 0
        self.textures = 0
        self.missing = []
        self.unsupported = Counter()
        self.approximated = Counter()
        self.supported = Counter()


class _Doc:
    def __init__(self):
        self.j = {
            "asset": {"version": "2.0", "generator": "xbr extract"},
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
            "meshes": [],
            "materials": [],
            "textures": [],
            "images": [],
            "samplers": [],
            "accessors": [],
            "bufferViews": [],
            "buffers": [],
            "extensionsUsed": [],
        }
        self.bin = bytearray()

    def add(self, kind, obj):
        self.j[kind].append(obj)
        return len(self.j[kind]) - 1

    def accessor(self, fmt, comps, rows, typ, target, minmax=False):
        data = b"".join(struct.pack("<" + fmt * comps, *r) for r in rows)
        while len(self.bin) % 4:
            self.bin.append(0)
        view = self.add(
            "bufferViews",
            {
                "buffer": 0,
                "byteOffset": len(self.bin),
                "byteLength": len(data),
                "target": target,
            },
        )
        self.bin += data
        a = {
            "bufferView": view,
            "componentType": FLOAT if fmt == "f" else UINT,
            "count": len(rows),
            "type": typ,
        }
        if minmax:
            # From the packed values: a float64 row (a computed box corner)
            # rounds when packed, and glTF wants the bounds of what's stored.
            rows = list(struct.iter_unpack("<" + fmt * comps, data))
            a["min"] = [min(r[i] for r in rows) for i in range(comps)]
            a["max"] = [max(r[i] for r in rows) for i in range(comps)]
        return self.add("accessors", a)


def _normalize(n):
    ln = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5 or 1.0
    return (n[0] / ln, n[1] / ln, n[2] / ln)


def _env_uv(n):
    """A sphere-map lookup at the identity view: the normal's X and Y
    across the texture, centre at (0.5, 0.5)."""
    return 0.5 + 0.5 * n[0], 0.5 - 0.5 * n[1]


def _qrot(q, p):
    """p rotated by the unit quaternion q (x, y, z, w)."""
    x, y, z, w = q
    tx = 2 * (y * p[2] - z * p[1])
    ty = 2 * (z * p[0] - x * p[2])
    tz = 2 * (x * p[1] - y * p[0])
    return (
        p[0] + w * tx + y * tz - z * ty,
        p[1] + w * ty + z * tx - x * tz,
        p[2] + w * tz + x * ty - y * tx,
    )


def _tree_bounds(roots):
    """The bounds (lo, hi) of the drawn vertices of a tree in its root's
    space, nodes placed as _node writes them; (None, None) if none."""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3

    def walk(n, place):
        def here(p, n=n):
            p = tuple(p[i] * n.scl[i] for i in range(3))
            p = _qrot(ninja.quat(n.ang, n.flags), p)
            return place(tuple(p[i] + n.pos[i] for i in range(3)))

        if n.model and not n.flags & ninja.EVAL_HIDE:
            for v in n.model.verts.values():
                w = here(v)
                for i in range(3):
                    lo[i], hi[i] = min(lo[i], w[i]), max(hi[i], w[i])
        for c in n.children:
            walk(c, here)

    for n in roots:
        walk(n, lambda p: p)
    if lo[0] == float("inf"):
        return None, None
    return lo, hi


def _in_unit(lo, hi):
    return lo >= -1e-4 and hi <= 1.0 + 1e-4


def wraps(tflags):
    hf = tflags >> 16
    s = MIRROR if hf & TINY_FLIP_U else (CLAMP if hf & TINY_CLAMP_U else REPEAT)
    t = MIRROR if hf & TINY_FLIP_V else (CLAMP if hf & TINY_CLAMP_V else REPEAT)
    return s, t


class Writer:
    """Writes one stage. names: the texlist (add_tree may give each tree
    its own); source: textures.Source (or None for no textures).
    Textures are shared by name, so two texlists naming one file make
    one image."""

    def __init__(self, name, names, source, up="y", textured_colour="diffuse", texture_alpha=None):
        self.name = name
        self.names = names
        self._tlkey = tuple(n.lower() for n in names)
        self.source = source
        self.up = up
        self.textured_colour = textured_colour
        # Texture name (lower case) to the opacity a blend pass of the
        # game's own gives its surfaces ([[extract.stage]] texture_alpha).
        self.texture_alpha = {k.lower(): v for k, v in (texture_alpha or {}).items()}
        # The names as the stage spells them, for the report of the ones no
        # strip drew with (a typo, or a texture of another texlist).
        self._ta_names = {k.lower(): k for k in texture_alpha or {}}
        self._ta_matched = set()
        self.doc = _Doc()
        self.summary = Summary(name)
        self._mats, self._samplers, self._meshes = {}, {}, {}
        self._by_name, self._alpha = {}, {}
        self._pngs = {}
        # texture index: [glTF wraps, umin, umax, vmin, vmax] over every
        # primitive that samples it; write() picks the sampler from it.
        self._tex_uv = {}
        self._mat_tex = {}
        self._tree_names = Counter()

    def _tex_name(self, ti):
        """The texlist's name for texture ti, or None past its end."""
        if ti is not None and ti < len(self.names):
            return self.names[ti]
        if ti is not None and self.source is not None:
            self.summary.unsupported["texture id past the texlist"] += 1
        return None

    def _image(self, nm):
        key = nm.lower()
        if key in self._by_name:
            return self._by_name[key]
        img = None
        data = self.source.read(nm) if textures.file_name(nm) else None
        if not textures.file_name(nm):
            self.summary.unsupported["texture name %r: not a file name" % nm] += 1
        elif data is None:
            self.summary.missing.append(nm)
        else:
            try:
                w, h, rgba, _ = textures.decode(data)
            except (ValueError, struct.error) as e:
                self.summary.unsupported["texture %s: %s" % (nm, e)] += 1
            else:
                self._pngs[nm + ".png"] = textures.png(w, h, rgba)
                self._alpha[key] = textures.alpha_kind(rgba)
                uri = "textures/%s.png" % urllib.parse.quote(nm)
                img = self.doc.add("images", {"uri": uri, "name": nm})
        self._by_name[key] = img
        return img

    def _sampler(self, k):
        if k not in self._samplers:
            self._samplers[k] = self.doc.add(
                "samplers",
                {
                    "magFilter": LINEAR,
                    "minFilter": LINEAR_MIPMAP_LINEAR,
                    "wrapS": k[0],
                    "wrapT": k[1],
                },
            )
        return self._samplers[k]

    def _material(self, s):
        textured = s.uv or s.flags & ninja.STRIP_FLAG_ENV_MAP
        nm = self._tex_name(s.tex) if textured else None
        img = self._image(nm) if nm is not None and self.source is not None else None
        diffuse = s.diffuse
        # Stock Ninja modulates the texture by the diffuse. BLiNX 2's drawer
        # gives a UV or environment-mapped strip the engine's constant
        # colour (white unless a fade sets it, alpha included) and keeps the
        # diffuse for the rest: M121's materials are all black diffuse and
        # draw fully textured. The drawer only knows kinds 64 and 65, so
        # other kinds follow whichever of the two has UVs like them.
        if self.textured_colour == "white":
            if s.kind not in (64, 65):
                self.summary.approximated[
                    "strip kind other than 64 and 65 coloured like its UV twin"
                ] += 1
            if img is not None:
                diffuse = None
        key = (nm and nm.lower(), s.tflags if textured else 0, s.flags, diffuse, s.blend)
        if key in self._mats:
            return self._mats[key]
        b, g, r, a = diffuse or b"\xff\xff\xff\xff"
        tname = nm or "untextured"
        mat = {
            "name": "m%d_%s" % (len(self.doc.j["materials"]), tname),
            "pbrMetallicRoughness": {
                "baseColorFactor": [r / 255, g / 255, b / 255, a / 255],
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
        }
        if img is not None:
            tex = self.doc.add("textures", {"source": img})
            self._tex_uv[tex] = [wraps(s.tflags), None, None, None, None]
            self._mat_tex[len(self.doc.j["materials"])] = tex
            mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": tex}
            kind = self._alpha[nm.lower()]
            use_alpha = s.flags & ninja.STRIP_FLAG_USE_ALPHA
            # Alpha that never reaches 0 and dips well below opaque is
            # see-through glass, not a cut-out: an alpha test would
            # punch holes in it, so it blends whatever the use-alpha
            # flag says. Near-opaque alpha passes any alpha test, so
            # without the flag it is opaque.
            if kind == "translucent":
                mat["alphaMode"] = "BLEND"
                if not use_alpha:
                    self.summary.approximated["translucent texture blended without use-alpha"] += 1
            elif kind == "mask" or (kind == "blend" and not use_alpha):
                mat["alphaMode"] = "MASK"
            elif kind in ("blend", "near-opaque") and use_alpha:
                mat["alphaMode"] = "BLEND"
        if a < 255 and s.flags & ninja.STRIP_FLAG_USE_ALPHA:
            mat["alphaMode"] = "BLEND"
        if nm is not None and nm.lower() in self.texture_alpha:
            # A surface the game covers with a blend pass of its own (a
            # floor under its planar reflection) that the static scene
            # stands in for by letting what lies below show through. An
            # opacity of 1 hides nothing, so the surface keeps its own
            # alpha mode: BLEND would only cost it depth sorting.
            self._ta_matched.add(nm.lower())
            op = self.texture_alpha[nm.lower()]
            if op < 1:
                mat["alphaMode"] = "BLEND"
                mat["pbrMetallicRoughness"]["baseColorFactor"][3] *= op
                self.summary.approximated[
                    "blend pass as a translucent texture (texture_alpha)"
                ] += 1
        if s.blend == ninja.BLEND_ADD:
            # Additive light never cuts out or hides what is behind it.
            mat["alphaMode"] = "BLEND"
            mat["extras"] = {EXTRA_BLEND: "add"}
        if s.flags & ninja.STRIP_FLAG_DOUBLE_SIDE:
            mat["doubleSided"] = True
        if s.flags & ninja.STRIP_FLAG_IGNORE_LIGHT:
            mat["extensions"] = {UNLIT: {}}
            if UNLIT not in self.doc.j["extensionsUsed"]:
                self.doc.j["extensionsUsed"].append(UNLIT)
        self._mats[key] = self.doc.add("materials", mat)
        return self._mats[key]

    def _mesh(self, model):
        # A model's materials depend on the texlist it is drawn with.
        mkey = (model.va, self._tlkey)
        if mkey in self._meshes:
            return self._meshes[mkey]
        groups = {}
        for s in model.strips:
            groups.setdefault(self._material(s), []).append(s)
        prims = []
        tris = 0
        for mi, strips in groups.items():
            remap, pos, nrm, uvs, idx = {}, [], [], [], []
            for s in strips:
                # The game looks an environment map up from the view-space
                # normal; a strip with UVs of its own keeps them here.
                env = s.flags & ninja.STRIP_FLAG_ENV_MAP and not s.uv
                if env:
                    self.summary.approximated["environment map (UVs from model-space normals)"] += 1
                elif s.flags & ninja.STRIP_FLAG_ENV_MAP:
                    self.summary.approximated["environment map (the strip's own UVs)"] += 1
                for tri in s.tris:
                    for vi, _u, _v in tri:
                        if vi not in model.verts:
                            self.summary.unsupported["strip index without a vertex"] += 1
                            break
                    else:
                        for vi, u, v in tri:
                            n = _normalize(model.normals.get(vi, (0.0, 1.0, 0.0)))
                            if env:
                                u, v = _env_uv(n)
                            k = (vi, u, v)
                            if k not in remap:
                                remap[k] = len(pos)
                                pos.append(model.verts[vi])
                                nrm.append(n)
                                uvs.append((u, v))
                            idx.append((remap[k],))
            if not idx:
                continue
            if mi in self._mat_tex:
                r = self._tex_uv[self._mat_tex[mi]]
                us, vs = [t[0] for t in uvs], [t[1] for t in uvs]
                lo = (min(us), max(us), min(vs), max(vs))
                r[1] = lo[0] if r[1] is None else min(r[1], lo[0])
                r[2] = lo[1] if r[2] is None else max(r[2], lo[1])
                r[3] = lo[2] if r[3] is None else min(r[3], lo[2])
                r[4] = lo[3] if r[4] is None else max(r[4], lo[3])
            d = self.doc
            prims.append(
                {
                    "attributes": {
                        "POSITION": d.accessor("f", 3, pos, "VEC3", ARRAY_BUFFER, minmax=True),
                        "NORMAL": d.accessor("f", 3, nrm, "VEC3", ARRAY_BUFFER),
                        "TEXCOORD_0": d.accessor("f", 2, uvs, "VEC2", ARRAY_BUFFER),
                    },
                    "indices": d.accessor("I", 1, idx, "SCALAR", ELEMENT_ARRAY_BUFFER),
                    "material": mi,
                }
            )
            tris += len(idx) // 3
        mesh = None
        if prims:
            mesh = self.doc.add("meshes", {"name": "mdl_%08x" % model.va, "primitives": prims})
            self.summary.meshes += 1
            self.summary.triangles += tris
        self._meshes[mkey] = mesh
        return mesh

    def _node(self, n):
        nd = {"name": "obj_%08x" % n.va}
        if any(n.pos):
            nd["translation"] = [float(v) for v in n.pos]
        if any(n.ang):
            nd["rotation"] = ninja.quat(n.ang, n.flags)
        if tuple(n.scl) != (1.0, 1.0, 1.0):
            nd["scale"] = [float(v) for v in n.scl]
        if n.model and not n.flags & ninja.EVAL_HIDE:
            mi = self._mesh(n.model)
            if mi is not None:
                nd["mesh"] = mi
        me = self.doc.add("nodes", nd)
        kids = [self._node(c) for c in n.children]
        if kids:
            self.doc.j["nodes"][me]["children"] = kids
        return me

    def add_tree(self, va, roots, names=None, transform=None):
        """Adds one tree, drawn with names (default: the stage's texlist).
        transform: the scale and translation the game's draw code puts
        around the tree, in the game's axes ({"scale": [x, y, z],
        "translation": [x, y, z]}, both optional): the tree goes under a
        node carrying them, inside the up turn. A root drawn twice (the
        second time mirrored) is added twice, so the node names count."""
        if names is not None:
            self.names, self._tlkey = names, tuple(n.lower() for n in names)
        name = "%s_%08x" % (self.name, va)
        self._tree_names[name] += 1
        if self._tree_names[name] > 1:
            name += "_%d" % self._tree_names[name]
        kids = [self._node(n) for n in roots]
        xf = {}
        if transform and transform.get("translation") and any(transform["translation"]):
            xf["translation"] = [float(v) for v in transform["translation"]]
        if transform and transform.get("scale") and list(transform["scale"]) != [1, 1, 1]:
            xf["scale"] = [float(v) for v in transform["scale"]]
        if xf:
            xf["name"] = name + "_transform"
            xf["children"] = kids
            kids = [self.doc.add("nodes", xf)]
        if transform and transform.get("backdrop") is not None:
            box = self._backdrop(name, roots, transform)
            if box is not None:
                kids.append(box)
        top = {"name": name, "children": kids}
        rot = UP_ROTATION[self.up]
        if rot:
            top["rotation"] = rot
        self.doc.j["scenes"][0]["nodes"].append(self.doc.add("nodes", top))

    def _backdrop(self, name, roots, transform):
        """The render target's clear colour behind a mirrored tree: the
        game draws the reflection into a target cleared to that colour, so
        wherever the mirrored room has no surface (its open top, once
        mirrored below the floor) the floor shows the colour, not the sky
        or whatever lies further down. An unlit box, open on the mirror
        plane's side, faces inward around the tree's mirrored bounds."""
        lo, hi = _tree_bounds(roots)
        if lo is None:
            return None
        scale = [float(v) for v in transform.get("scale", (1, 1, 1))]
        move = [float(v) for v in transform.get("translation", (0, 0, 0))]
        a = next(i for i in range(3) if scale[i] < 0)
        ends = [sorted((lo[i] * scale[i] + move[i], hi[i] * scale[i] + move[i])) for i in range(3)]
        # The plane the mirror keeps in place: x = s*x + t.
        plane = move[a] / (1 - scale[a])
        # A margin keeps the box off the tree's own outermost faces (the
        # mirrored ceiling would z-fight with its bottom).
        pad = 0.01 * max(e[1] - e[0] for e in ends) or 1.0
        box = [[e[0] - pad, e[1] + pad] for e in ends]
        # On the mirror's axis the box runs from the plane to the tree's
        # far side, so the gap between the plane and the tree is covered.
        far = 0 if plane > (ends[a][0] + ends[a][1]) / 2 else 1
        box[a] = sorted((box[a][far], plane))
        open_side = box[a].index(plane)
        pos, nrm, idx = [], [], []
        for b in range(3):
            u, v = (b + 1) % 3, (b + 2) % 3
            for side in (0, 1):
                if b == a and side == open_side:
                    continue
                n = [0.0, 0.0, 0.0]
                n[b] = 1.0 if side == 0 else -1.0
                base = len(pos)
                for cu, cv in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    p = [0.0, 0.0, 0.0]
                    p[b], p[u], p[v] = box[b][side], box[u][cu], box[v][cv]
                    pos.append(tuple(p))
                    nrm.append(tuple(n))
                # e_u x e_v = e_b: counter-clockwise faces +b, the low
                # face's inside; the high face winds the other way.
                quad = (0, 1, 2, 0, 2, 3) if side == 0 else (0, 2, 1, 0, 3, 2)
                idx += [(base + i,) for i in quad]
        r, g, b_ = (float(c) for c in transform["backdrop"])
        mat = self.doc.add(
            "materials",
            {
                "name": "m%d_backdrop" % len(self.doc.j["materials"]),
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r, g, b_, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "extensions": {UNLIT: {}},
            },
        )
        if UNLIT not in self.doc.j["extensionsUsed"]:
            self.doc.j["extensionsUsed"].append(UNLIT)
        d = self.doc
        prim = {
            "attributes": {
                "POSITION": d.accessor("f", 3, pos, "VEC3", ARRAY_BUFFER, minmax=True),
                "NORMAL": d.accessor("f", 3, nrm, "VEC3", ARRAY_BUFFER),
            },
            "indices": d.accessor("I", 1, idx, "SCALAR", ELEMENT_ARRAY_BUFFER),
            "material": mat,
        }
        mesh = d.add("meshes", {"name": name + "_backdrop", "primitives": [prim]})
        self.summary.meshes += 1
        self.summary.triangles += len(idx) // 3
        self.summary.approximated["render target clear colour as a backdrop box"] += 1
        return d.add("nodes", {"name": name + "_backdrop", "mesh": mesh})

    def unmatched_texture_alpha(self):
        """texture_alpha names no strip of the stage drew with."""
        return sorted(v for k, v in self._ta_names.items() if k not in self._ta_matched)

    def _samplers_by_range(self):
        for tex, (k, u0, u1, v0, v1) in self._tex_uv.items():
            s, t = k
            u_in = u0 is not None and _in_unit(u0, u1)
            v_in = v0 is not None and _in_unit(v0, v1)
            # Godot keeps one wrap per material. Only wraps that differ per
            # axis need a choice: an axis whose UVs stay inside [0, 1] takes
            # the other axis's wrap, which tiles the same there. A
            # clamp is never turned into repeat when both axes agree: under
            # bilinear and mips, repeat bleeds the opposite edge into a
            # sky's horizon or a sign's border.
            if s != t:
                if u_in and v_in:
                    s = t = CLAMP
                elif u_in:
                    s = t
                elif v_in:
                    t = s
                if s != t:
                    self.summary.unsupported["texture wraps differ per axis (Godot uses one)"] += 1
                else:
                    self.summary.approximated[
                        "texture wrap kept inside [0, 1] (written as the other axis's)"
                    ] += 1
            # Godot's 3D materials cannot mirror: a mirrored axis that leaves
            # [0, 1] repeats instead.
            if (s == MIRROR and not u_in) or (t == MIRROR and not v_in):
                self.summary.approximated["mirrored texture wrap (Godot repeats it)"] += 1
            self.doc.j["textures"][tex]["sampler"] = self._sampler((s, t))

    def write(self, out_dir):
        """Writes the files; returns the Summary."""
        self._samplers_by_range()
        j = dict(self.doc.j)
        # glTF refuses empty top-level arrays and zero-length buffers, so a
        # stage with nothing drawn keeps only its nodes and scene.
        if self.doc.bin:
            j["buffers"] = [{"uri": self.name + ".bin", "byteLength": len(self.doc.bin)}]
        for k in list(j):
            if isinstance(j[k], list) and not j[k]:
                del j[k]
        if self._pngs:
            os.makedirs(os.path.join(out_dir, "textures"), exist_ok=True)
        for fn, data in self._pngs.items():
            with open(os.path.join(out_dir, "textures", fn), "wb") as f:
                f.write(data)
        if self.doc.bin:
            with open(os.path.join(out_dir, self.name + ".bin"), "wb") as f:
                f.write(self.doc.bin)
        with open(os.path.join(out_dir, self.name + ".gltf"), "w") as f:
            json.dump(j, f, indent=1)
            f.write("\n")
        self.summary.materials = len(self.doc.j["materials"])
        self.summary.textures = len(self.doc.j["images"])
        return self.summary
