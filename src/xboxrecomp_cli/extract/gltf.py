"""One stage to glTF 2.0: <name>.gltf, <name>.bin and textures/<tex>.png.

Each Ninja object is a node (translation, rotation, scale); each model a
mesh with one primitive per material, its vertices de-duplicated by
(vertex, u, v). A material is a texture and its sampler (the tiny chunk's
flip and clamp bits as glTF wraps), the diffuse colour, the strip's
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

    def __init__(self, name, names, source, up="y"):
        self.name = name
        self.names = names
        self._tlkey = tuple(n.lower() for n in names)
        self.source = source
        self.up = up
        self.doc = _Doc()
        self.summary = Summary(name)
        self._mats, self._samplers, self._meshes = {}, {}, {}
        self._by_name, self._alpha = {}, {}
        self._pngs = {}
        # texture index: [glTF wraps, umin, umax, vmin, vmax] over every
        # primitive that samples it; write() picks the sampler from it.
        self._tex_uv = {}
        self._mat_tex = {}

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
        key = (nm and nm.lower(), s.tflags if textured else 0, s.flags, s.diffuse, s.blend)
        if key in self._mats:
            return self._mats[key]
        b, g, r, a = s.diffuse or b"\xff\xff\xff\xff"
        tname = nm or "untextured"
        mat = {
            "name": "m%d_%s" % (len(self.doc.j["materials"]), tname),
            "pbrMetallicRoughness": {
                "baseColorFactor": [r / 255, g / 255, b / 255, a / 255],
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
        }
        if nm is not None and self.source is not None:
            img = self._image(nm)
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
                        self.summary.approximated[
                            "translucent texture blended without use-alpha"
                        ] += 1
                elif kind == "mask" or (kind == "blend" and not use_alpha):
                    mat["alphaMode"] = "MASK"
                elif kind in ("blend", "near-opaque") and use_alpha:
                    mat["alphaMode"] = "BLEND"
        if a < 255 and s.flags & ninja.STRIP_FLAG_USE_ALPHA:
            mat["alphaMode"] = "BLEND"
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

    def add_tree(self, va, roots, names=None):
        """Adds one tree, drawn with names (default: the stage's texlist)."""
        if names is not None:
            self.names, self._tlkey = names, tuple(n.lower() for n in names)
        top = {"name": "%s_%08x" % (self.name, va), "children": [self._node(n) for n in roots]}
        rot = UP_ROTATION[self.up]
        if rot:
            top["rotation"] = rot
        self.doc.j["scenes"][0]["nodes"].append(self.doc.add("nodes", top))

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
