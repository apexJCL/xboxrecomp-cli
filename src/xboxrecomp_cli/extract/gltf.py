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
  smears or tiles on both (M34's walls: V clamped, U tiled to 7, drawn
  as horizontal bands). When the two wraps differ and both axes leave
  [0, 1], the clamped axis is cut into the mesh: a triangle whose UVs
  pass an edge texel's centre on it is cut there, and the pieces outside
  take that centre, which is what clamp-to-edge samples past it. That
  axis then stays between the two centres, where repeat and clamp sample
  alike at mip 0; smaller mips still blend in the opposite edge, which
  clamp would not. Otherwise an axis whose UVs stay inside [0, 1] takes
  the other axis's wrap (both inside: clamp); wraps that agree are kept,
  since repeat bleeds the opposite edge under bilinear filtering where
  clamp does not. Only a repeat turned into clamp inside [0, 1] and a
  cut axis are counted here: a mirrored or clamped axis turned into
  repeat is doubled (below), which is exact, and with mirrors doubled
  and clamps cut the two wraps never still differ.
- a texture whose alpha never reaches 0 but dips below 240 is glass and
  blends even without the strip's use-alpha flag.
- a texture named in the stage's texture_alpha blends at that opacity:
  the game covers such a surface with a pass of its own (a planar
  reflection drawn into a render target and blended over the floor),
  which a static scene stands in for with a mirrored copy of the room
  below a see-through floor.

Godot's 3D materials cannot mirror, and a material that repeats on one
axis repeats on both. Both are met with a copy of the texture twice as
long on an axis, the image then its mirror image (_mirrored), and the
UVs halved on that axis, written as repeat (_settle_wraps), counted
under `supported`:
- a mirrored axis whose UVs leave [0, 1]: one repeat of the copy is one
  mirrored pair, so repeat samples it as the game's mirror does;
- a mirrored or clamped axis whose UVs stay inside [0, 1] while the
  material repeats: halved to [0, 0.5], each edge texel meets its own
  mirror image under bilinear filtering, as with clamp-to-edge, where
  repeat would blend in the opposite edge.
Bilinear seams and power-of-two mips match the game's. The copies are
images of their own; a texture no material samples plain any more is
neither written nor counted, so the summary's texture count is the
images the stage's glTF names, copies included.

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
# The summary's `supported` line per wrap a doubled axis stands in for.
DOUBLED = {
    MIRROR: "mirrored wrap as a doubled texture (Godot cannot mirror)",
    CLAMP: "clamped axis inside [0, 1] as a doubled texture (Godot repeats both axes)",
}
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


# A UV this close to a cut line is on it: snapped there before the cut, so
# a corner on the line makes no sliver or near-duplicate corner.
CUT_SNAP = 1e-4


def _lerp_corner(a, b, axis, at):
    """The point where the edge a-b crosses UV axis = at. Interpolated from
    the lower end (by position, then UV), so the two triangles sharing an
    edge cut it at the very same point and no crack opens."""
    for c in (a, b):
        if c[3][axis] == at:
            return c
    if (b[1], b[3]) < (a[1], a[3]):
        a, b = b, a
    t = (at - a[3][axis]) / (b[3][axis] - a[3][axis])

    def mix(p, q):
        return tuple(x + (y - x) * t for x, y in zip(p, q))

    uv = list(mix(a[3], b[3]))
    uv[axis] = at
    n = mix(a[2], b[2])
    # Opposed normals cancel; glTF wants unit normals, so take an end's.
    n = _normalize(n) if sum(x * x for x in n) > 1e-12 else a[2]
    p, uv = mix(a[1], b[1]), tuple(uv)
    return (("cut",) + p + uv, p, n, uv)


def _clip(poly, axis, at, below, origin):
    """Sutherland-Hodgman: the part of a convex polygon (corner list) on
    one side of UV axis = at, its winding kept. origin maps a cut
    corner's key to the triangle edge it lies on: an edge from it to that
    edge's end is cut on the whole edge, the same as the piece across the
    line cuts it, so the two meet."""
    out = []
    for i, c in enumerate(poly):
        d = poly[i - 1]
        cin = c[3][axis] <= at if below else c[3][axis] >= at
        din = d[3][axis] <= at if below else d[3][axis] >= at
        if cin != din:
            e0, e1 = d, c
            for x, y in ((d, c), (c, d)):
                edge = origin.get(x[0])
                if edge and y in edge:
                    e0, e1 = edge
            cut = _lerp_corner(e0, e1, axis, at)
            if cut[0][0] == "cut":
                origin.setdefault(cut[0], (e0, e1))
            out.append(cut)
        if cin:
            out.append(c)
    # A corner on the line comes back once more as its own cut point.
    out = [c for i, c in enumerate(out) if (c[1], c[3]) != (out[i - 1][1], out[i - 1][3])]
    return out if len(out) >= 3 else []


def _bake(corners, axis, lo, hi):
    """A triangle's pieces with clamp-to-edge applied on one UV axis: cut
    at the edge texel centres lo and hi, the pieces outside pinned there,
    which is what clamp-to-edge samples past them. The piece inside keeps
    its UVs, so the material's repeat never reaches the half texel where
    it would blend in the opposite edge. Each piece is a convex polygon
    of corners (key, position, normal, uv)."""

    def pin(poly, at):
        out = []
        for k, p, n, uv in poly:
            uv = tuple(at if i == axis else x for i, x in enumerate(uv))
            out.append((("pin",) + tuple(k) + uv, p, n, uv))
        return out

    snapped = []
    for k, p, n, uv in corners:
        x = uv[axis]
        at = lo if abs(x - lo) <= CUT_SNAP else hi if abs(x - hi) <= CUT_SNAP else x
        if at != x:
            uv = tuple(at if i == axis else y for i, y in enumerate(uv))
            k = ("snap",) + tuple(k) + uv
        snapped.append((k, p, n, uv))
    if hi <= lo:
        # One texel across: clamp samples it everywhere.
        return [pin(snapped, lo)]
    origin = {}
    inside = _clip(_clip(snapped, axis, lo, False, origin), axis, hi, True, origin)
    return [
        piece
        for piece in (
            pin(_clip(snapped, axis, lo, True, origin), lo),
            inside,
            pin(_clip(snapped, axis, hi, False, origin), hi),
        )
        if piece
    ]


def _needs_bake(corners, axis, lo, hi):
    return any(not lo - CUT_SNAP <= c[3][axis] <= hi + CUT_SNAP for c in corners)


def mirror_rgba(w, h, rgba, axes):
    """(width, height, RGBA) of the image followed by its mirror image on
    each of axes (0: across, 1: down): mirrored repeat with period 2 as
    plain repeat with period 1."""
    rows = [rgba[y * w * 4 : (y + 1) * w * 4] for y in range(h)]
    if 0 in axes:
        rows = [r + b"".join(r[x * 4 : x * 4 + 4] for x in reversed(range(w))) for r in rows]
        w *= 2
    if 1 in axes:
        rows = rows + rows[::-1]
        h *= 2
    return w, h, b"".join(rows)


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
        self._by_name, self._alpha, self._size = {}, {}, {}
        # material index: (axis, edge texel centres) of a clamped axis
        # the mesh carries (see _bake).
        self._bake = {}
        # material index: [[umin, umax], [vmin, vmax]] of its UVs as read.
        self._raw_uv = {}
        # Buffer writes in the order the meshes were added (write()).
        self._pending = []
        self._pngs = {}
        # texture index: the game's (hardware) wraps; write() picks the
        # sampler from them and the UVs (_settle_wraps).
        self._tex_wraps = {}
        self._mat_tex = {}
        # (texture name, axes) to its mirrored copy's image index, and the
        # images some texture read before its copy.
        self._mirror_img = {}
        self._replaced = set()
        # material index: the UV axes its texture's copy doubles
        # (_settle_wraps), halved on those axes.
        self._mirror = {}
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
                self._size[key] = (w, h)
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
            self._tex_wraps[tex] = wraps(s.tflags)
            self._mat_tex[len(self.doc.j["materials"])] = tex
            ws = wraps(s.tflags)
            if ws[0] != ws[1] and CLAMP in ws:
                # The game clamps this axis alone; Godot's one wrap per
                # material cannot, so the mesh does (_bake).
                axis = ws.index(CLAMP)
                size = self._size[nm.lower()][axis]
                self._bake[len(self.doc.j["materials"])] = (axis, 0.5 / size, 1 - 0.5 / size)
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
        plan = []
        for mi, strips in groups.items():
            tris = []
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
                        corners = []
                        for vi, u, v in tri:
                            n = _normalize(model.normals.get(vi, (0.0, 1.0, 0.0)))
                            if env:
                                u, v = _env_uv(n)
                            corners.append(((vi, u, v), model.verts[vi], n, (u, v)))
                        tris.append(corners)
            if not tris:
                continue
            r = self._raw_uv.setdefault(mi, [[], []])
            for axis in (0, 1):
                xs = [c[3][axis] for t in tris for c in t]
                r[axis] = [min(xs + r[axis][:1]), max(xs + r[axis][1:])]
            plan.append((mi, tris))
        mesh = None
        if plan:
            # The buffers wait for write(): whether a clamped axis is cut
            # into the mesh depends on every primitive of its material.
            m = {"name": "mdl_%08x" % model.va, "primitives": []}
            mesh = self.doc.add("meshes", m)
            self.summary.meshes += 1
            self._pending.append(lambda: self._primitives(m, plan))
        self._meshes[mkey] = mesh
        return mesh

    def _primitives(self, mesh, plan):
        for mi, tris in plan:
            remap, pos, nrm, uvs, idx = {}, [], [], [], []
            bake = self._bake.get(mi)
            mirror = self._mirror.get(mi)
            for corners in tris:
                pieces = [corners]
                if bake and _needs_bake(corners, *bake):
                    pieces = _bake(corners, *bake)
                    self.summary.approximated[
                        "clamped axis cut into the mesh (Godot keeps one wrap)"
                    ] += 1
                for poly in pieces:
                    for i in range(1, len(poly) - 1):
                        for k, p, n, uv in (poly[0], poly[i], poly[i + 1]):
                            if k not in remap:
                                remap[k] = len(pos)
                                pos.append(p)
                                nrm.append(n)
                                if mirror:
                                    uv = tuple(
                                        x * 0.5 if a in mirror else x for a, x in enumerate(uv)
                                    )
                                uvs.append(uv)
                            idx.append((remap[k],))
            d = self.doc
            mesh["primitives"].append(
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
            self.summary.triangles += len(idx) // 3

    def _settle_bakes(self):
        """Keeps the cut only for materials that need it: a clamped axis
        that leaves [0, 1] while the other axis does too. With the other
        axis inside, one clamp for both already samples like the game
        (_settle_wraps), and an axis inside has nothing to cut."""
        for mi, (axis, _lo, _hi) in list(self._bake.items()):
            r = self._raw_uv.get(mi)
            if r is None or _in_unit(*r[axis]):
                del self._bake[mi]
                continue
            other = r[1 - axis]
            if self._tex_wraps[self._mat_tex[mi]][1 - axis] == MIRROR:
                # A mirror is doubled and halved (_settle_wraps): inside
                # [0, 1] once halved, one clamp for both samples like the
                # game too.
                other = (other[0] * 0.5, other[1] * 0.5)
            if _in_unit(*other):
                del self._bake[mi]

    def _settle_wraps(self):
        """Picks each texture's sampler, before the buffers are filled:
        Godot keeps one wrap per material and cannot mirror. From the UVs
        as read, with a cut axis (_settle_bakes) counted as inside, since
        the cut keeps it between its edge texel centres.

        A mirrored axis that leaves [0, 1] is doubled (_mirrored) and
        repeats. Wraps that still differ need a choice: an axis inside
        [0, 1] takes the other axis's wrap (both inside: clamp). Wraps
        that agree are kept: repeat would bleed the opposite edge into a
        sky's horizon or a sign's border, where clamp does not; two
        mirrors inside [0, 1] sample as two clamps. Last, an axis the game
        mirrors or clamps that stays inside [0, 1] under a repeating
        material is doubled too: its edge texels then meet their own
        mirror image, as with clamp-to-edge."""
        for mi, tex in self._mat_tex.items():
            k = self._tex_wraps[tex]
            r = self._raw_uv.get(mi)
            if r is None:
                self.doc.j["textures"][tex]["sampler"] = self._sampler(k)
                continue
            cut = self._bake[mi][0] if mi in self._bake else None
            w = list(k)
            inside = [a == cut or _in_unit(*r[a]) for a in (0, 1)]
            double = []
            for a in (0, 1):
                if w[a] == MIRROR and not inside[a]:
                    double.append(a)
                    w[a] = REPEAT
                    inside[a] = _in_unit(r[a][0] * 0.5, r[a][1] * 0.5)
            if w[0] != w[1]:
                if inside[0] and inside[1]:
                    w = [CLAMP, CLAMP]
                elif inside[0]:
                    w[0] = w[1]
                elif inside[1]:
                    w[1] = w[0]
            elif w[0] == MIRROR:
                w = [CLAMP, CLAMP]
            if w[0] != w[1]:
                self.summary.unsupported["texture wraps differ per axis (Godot uses one)"] += 1
            elif w[0] == REPEAT:
                for a in (0, 1):
                    if k[a] in (MIRROR, CLAMP) and inside[a] and a != cut and a not in double:
                        double.append(a)
            # Repeat turned into clamp inside [0, 1] loses the bleed the game
            # has at the edges; a cut axis repeats where the game clamps,
            # which smaller mips still show.
            if any(
                (k[a] == REPEAT and w[a] == CLAMP) or (a == cut and w[a] == REPEAT) for a in (0, 1)
            ):
                self.summary.approximated[
                    "texture wrap kept inside [0, 1] (written as the other axis's)"
                ] += 1
            if double:
                double = tuple(sorted(double))
                img = self.doc.j["textures"][tex]["source"]
                self._replaced.add(img)
                self.doc.j["textures"][tex]["source"] = self._mirrored(img, double)
                self._mirror[mi] = double
                for label in {DOUBLED[k[a]] for a in double}:
                    self.summary.supported[label] += 1
            self.doc.j["textures"][tex]["sampler"] = self._sampler(tuple(w))

    def _drop_unused_images(self):
        """Drops the images replaced by doubled copies (_settle_wraps) that
        no texture samples any more, and their PNGs, so they are neither
        written nor counted."""
        j = self.doc.j
        drop = self._replaced - {t["source"] for t in j["textures"]}
        if not drop:
            return
        for i in drop:
            self._pngs.pop(j["images"][i]["name"] + ".png", None)
        keep = [i for i in range(len(j["images"])) if i not in drop]
        new = {old: i for i, old in enumerate(keep)}
        j["images"] = [j["images"][i] for i in keep]
        for t in j["textures"]:
            t["source"] = new[t["source"]]
        self._by_name = {}
        self._mirror_img = {}

    def _mirrored(self, img, axes):
        """The image index of a copy of image img twice as long on each of
        axes (0: U, 1: V): the image, then its mirror image. Shared by
        every material that mirrors the same axes of it."""
        nm = self.doc.j["images"][img]["name"]
        key = (nm.lower(), axes)
        if key not in self._mirror_img:
            # Decoded again: only these few textures need their pixels kept.
            w, h, rgba, _ = textures.decode(self.source.read(nm))
            w, h, rgba = mirror_rgba(w, h, rgba, axes)
            fn = "%s.mirror-%s" % (nm, "".join("uv"[a] for a in axes))
            self._pngs[fn + ".png"] = textures.png(w, h, rgba)
            uri = "textures/%s.png" % urllib.parse.quote(fn)
            self._mirror_img[key] = self.doc.add("images", {"uri": uri, "name": fn})
        return self._mirror_img[key]

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
        name = "%s_%08x" % (self.name, va)
        self._tree_names[name] += 1
        if self._tree_names[name] > 1:
            name += "_%d" % self._tree_names[name]
        kids = self.add_nodes(roots, names)
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

    def add_nodes(self, roots, names=None):
        """The nodes of one tree, drawn with names (default: the last
        texlist given), outside any scene and without the up turn: the
        caller parents them (placements.py puts a model under each of its
        placements)."""
        if names is not None:
            self.names, self._tlkey = names, tuple(n.lower() for n in names)
        return [self._node(n) for n in roots]

    def marker(self, name, half, rgb):
        """An unlit box mesh (half its edge on each axis, a 0xRRGGBB
        colour), made once per name: a stand-in where no model is drawn."""
        key = ("marker", name)
        if key in self._meshes:
            return self._meshes[key]
        pos, nrm, idx = [], [], []
        for b in range(3):
            u, v = (b + 1) % 3, (b + 2) % 3
            for side in (-1.0, 1.0):
                base = len(pos)
                for cu, cv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    p = [0.0, 0.0, 0.0]
                    p[b], p[u], p[v] = side * half, cu * half, cv * half
                    pos.append(tuple(p))
                    n = [0.0, 0.0, 0.0]
                    n[b] = side
                    nrm.append(tuple(n))
                # e_u x e_v = e_b: counter-clockwise faces +b, outward on
                # the high face; the low face winds the other way.
                quad = (0, 1, 2, 0, 2, 3) if side > 0 else (0, 2, 1, 0, 3, 2)
                idx += [(base + i,) for i in quad]
        c = [((rgb >> s) & 0xFF) / 255.0 for s in (16, 8, 0)]
        mat = self.doc.add(
            "materials",
            {
                "name": "m%d_%s" % (len(self.doc.j["materials"]), name),
                "pbrMetallicRoughness": {
                    "baseColorFactor": c + [1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "extensions": {UNLIT: {}},
            },
        )
        if UNLIT not in self.doc.j["extensionsUsed"]:
            self.doc.j["extensionsUsed"].append(UNLIT)
        d = self.doc
        m = {"name": name, "primitives": []}

        def prim():
            m["primitives"].append(
                {
                    "attributes": {
                        "POSITION": d.accessor("f", 3, pos, "VEC3", ARRAY_BUFFER, minmax=True),
                        "NORMAL": d.accessor("f", 3, nrm, "VEC3", ARRAY_BUFFER),
                    },
                    "indices": d.accessor("I", 1, idx, "SCALAR", ELEMENT_ARRAY_BUFFER),
                    "material": mat,
                }
            )

        self._pending.append(prim)
        mesh = d.add("meshes", m)
        self.summary.meshes += 1
        self.summary.triangles += len(idx) // 3
        self._meshes[key] = mesh
        return mesh

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
        m = {"name": name + "_backdrop", "primitives": []}

        def prim():
            m["primitives"].append(
                {
                    "attributes": {
                        "POSITION": d.accessor("f", 3, pos, "VEC3", ARRAY_BUFFER, minmax=True),
                        "NORMAL": d.accessor("f", 3, nrm, "VEC3", ARRAY_BUFFER),
                    },
                    "indices": d.accessor("I", 1, idx, "SCALAR", ELEMENT_ARRAY_BUFFER),
                    "material": mat,
                }
            )

        self._pending.append(prim)
        mesh = d.add("meshes", m)
        self.summary.meshes += 1
        self.summary.triangles += len(idx) // 3
        self.summary.approximated["render target clear colour as a backdrop box"] += 1
        return d.add("nodes", {"name": name + "_backdrop", "mesh": mesh})

    def unmatched_texture_alpha(self):
        """texture_alpha names no strip of the stage drew with."""
        return sorted(v for k, v in self._ta_names.items() if k not in self._ta_matched)

    def write(self, out_dir):
        """Writes the files; returns the Summary."""
        self._settle_bakes()
        self._settle_wraps()
        self._drop_unused_images()
        for fill in self._pending:
            fill()
        self._pending = []
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
        with open(os.path.join(out_dir, self.name + ".gltf"), "w", encoding="utf-8") as f:
            json.dump(j, f, indent=1)
            f.write("\n")
        self.summary.materials = len(self.doc.j["materials"])
        self.summary.textures = len(self.doc.j["images"])
        return self.summary
