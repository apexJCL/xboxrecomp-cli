"""Sega Ninja chunk models (NJS_OBJECT / NJS_CNK_MODEL) read out of an XBE.

NJS_OBJECT (0x34): u32 eval flags, model*, f32 pos[3], ang[3], f32 scl[3],
child*, sibling*. The angles are int32 BAMS (0x10000 a turn) in the
classic layout and f32 radians in some Xbox ports ([extract] angles).
NJS_CNK_MODEL: vlist*, plist*, f32 center[3], f32 radius.

Vertex chunks: u8 type, u8 flags, u16 size, u16 index offset, u16 count,
then the vertices; the list ends with type 0xFF. Xbox ports widen the
header ([extract] vertex_header) and pack float normals into
NORMPACKED3 ([extract] packed_normals) without touching `size`, so the
stride comes from the type, never from `size`.

Polygon chunks: bits (1-7, 4 and 5 cache and draw a polygon list),
tiny (8-9, the texture id), material (16-31), strips (64-75); the list
ends with 0xFF. A strip chunk takes the texture and material state in
force when it is read.

Everything the glTF writer cannot express goes into the reader's
`unsupported` counter, which the summary prints per stage, instead of
being guessed. Environment-mapped strips (flag 0x40) are read like any
other; the writer approximates their map.
"""

import math
import struct
from collections import Counter

EVAL_UNIT_POS = 0x01
EVAL_UNIT_ANG = 0x02
EVAL_UNIT_SCL = 0x04
EVAL_HIDE = 0x08
EVAL_BREAK = 0x10
EVAL_ZXY = 0x20

# Vertex chunk types: (label, bytes after xyz with float normals).
VERTEX = {
    32: ("SH", 4),
    33: ("VN_SH", 20),
    34: ("CV", 0),
    35: ("D8", 4),
    36: ("UF", 4),
    37: ("NF", 4),
    38: ("S5", 4),
    39: ("S4", 4),
    40: ("IN", 4),
    41: ("VN", 12),
    42: ("VN_D8", 16),
    43: ("VN_UF", 16),
    44: ("VN_NF", 16),
    45: ("VN_S5", 16),
    46: ("VN_S4", 16),
    47: ("VN_IN", 16),
    48: ("VNX", 4),
    49: ("VNX_D8", 8),
    50: ("VNX_UF", 8),
}
# Float normals (3 floats) the Xbox converter may pack into one u32.
FLOAT_NORMAL_TYPES = range(41, 48)
PACKED_NORMAL_TYPES = range(48, 51)

# Strip chunks: which per-vertex fields follow the index.
STRIP_UV = {65: 256.0, 66: 1024.0, 68: 256.0, 69: 1024.0, 71: 256.0, 72: 1024.0}
STRIP_UV2 = {74: 256.0, 75: 1024.0}
STRIP_VN = (67, 68, 69)
STRIP_D8 = (70, 71, 72)
STRIP_FLAG_IGNORE_LIGHT = 0x01
STRIP_FLAG_USE_ALPHA = 0x08
STRIP_FLAG_DOUBLE_SIDE = 0x10
STRIP_FLAG_ENV_MAP = 0x40
# Ninja's blend byte (src << 3 | dst): source alpha over inverse source
# alpha, the one mode glTF has; the material's default.
BLEND_ALPHA = 0x25

ANGLES = ("bams", "radians")


class Format:
    """The dialect of one game ([extract] keys)."""

    def __init__(self, vertex_header=8, angles="bams", packed_normals=False):
        if vertex_header < 8:
            raise ValueError("vertex_header: at least 8 bytes")
        if angles not in ANGLES:
            raise ValueError("angles: one of %s" % ", ".join(ANGLES))
        self.vertex_header = vertex_header
        self.angles = angles
        self.packed_normals = packed_normals
        self.object = "<II3f3%s3fII" % ("f" if angles == "radians" else "i")

    def stride(self, vtype):
        """Bytes per vertex of a vertex chunk type."""
        tail = VERTEX[vtype][1]
        if self.packed_normals and vtype in FLOAT_NORMAL_TYPES:
            tail -= 8
        return 12 + tail

    def radians(self, ang):
        if self.angles == "radians":
            return tuple(float(a) for a in ang)
        return tuple(a * 2 * math.pi / 0x10000 for a in ang)


def normpacked3(v):
    """Xbox D3DVSDT_NORMPACKED3: x 11 bits, y 11 bits, z 10 bits, signed.

    Not classic Ninja's 10/10/10 VNX: the Xbox converter packs VNX normals
    for the GPU's own format. On both BLiNX dumps every sampled VNX normal
    decodes to unit length this way, and to a median of 0.6-0.7 as
    10/10/10."""

    def s(val, bits):
        if val & (1 << (bits - 1)):
            val -= 1 << bits
        return val / ((1 << (bits - 1)) - 1)

    return (s(v & 0x7FF, 11), s((v >> 11) & 0x7FF, 11), s((v >> 22) & 0x3FF, 10))


class Node:
    __slots__ = ("va", "flags", "model", "pos", "ang", "scl", "children")


class Model:
    __slots__ = ("va", "verts", "normals", "strips", "center", "radius")


class Strip:
    """One strip chunk's triangles, each corner (vertex index, u, v), with
    the texture and material state in force."""

    __slots__ = ("kind", "flags", "tex", "tflags", "diffuse", "blend", "uv", "tris")


class Reader:
    """Reads object trees from an Image; `stats` counts what was read and
    `unsupported` what the output leaves out."""

    def __init__(self, image, fmt):
        self.x = image
        self.fmt = fmt
        self.stats = Counter()
        self.unsupported = Counter()
        self.models = {}

    def tree(self, va):
        """The sibling chain at va, children recursively."""
        return self._chain(va, set())

    def _chain(self, va, seen):
        out = []
        while va:
            if va in seen:
                self.unsupported["object cycle"] += 1
                break
            seen.add(va)
            f = self.x.unpack(self.fmt.object, va)
            n = Node()
            n.va = va
            n.flags = f[0]
            n.pos = f[2:5]
            n.ang = self.fmt.radians(f[5:8])
            n.scl = f[8:11]
            n.model = self.model(f[1]) if f[1] else None
            n.children = self._chain(f[11], seen) if f[11] else []
            self.stats["objects"] += 1
            out.append(n)
            va = f[12]
        return out

    def model(self, va):
        if va in self.models:
            return self.models[va]
        m = Model()
        m.va = va
        vl, pl, cx, cy, cz, r = self.x.unpack("<II4f", va)
        m.center, m.radius = (cx, cy, cz), r
        m.verts, m.normals = {}, {}
        if vl:
            self._vlist(vl, m)
        m.strips = self._plist(pl) if pl else []
        self.models[va] = m
        return m

    def _vlist(self, va, m):
        hdr = self.fmt.vertex_header
        while True:
            b = self.x.read(va, 8)
            t = b[0]
            if t == 0xFF:
                return
            if t not in VERTEX:
                self.unsupported["vertex chunk type %d" % t] += 1
                return
            ioff, count = struct.unpack_from("<HH", b, 4)
            label = VERTEX[t][0]
            stride = self.fmt.stride(t)
            data = self.x.read(va + hdr, stride * count)
            packed = t in PACKED_NORMAL_TYPES or (
                self.fmt.packed_normals and t in FLOAT_NORMAL_TYPES
            )
            for i in range(count):
                o = i * stride
                m.verts[ioff + i] = struct.unpack_from("<3f", data, o)
                if t == 33:  # VN_SH: xyzw then the normal's xyzw
                    m.normals[ioff + i] = struct.unpack_from("<3f", data, o + 16)
                elif packed:
                    m.normals[ioff + i] = normpacked3(struct.unpack_from("<I", data, o + 12)[0])
                elif t in FLOAT_NORMAL_TYPES:
                    m.normals[ioff + i] = struct.unpack_from("<3f", data, o + 12)
            self.stats["vertex chunk %s" % label] += 1
            if label.endswith(("D8", "S5", "S4", "IN")):
                # Per-vertex colours or baked lighting: not written yet.
                self.unsupported["vertex %s colours" % label] += 1
            va += hdr + stride * count

    def _plist(self, va):
        strips = []
        state = {"tex": None, "tflags": 0, "diffuse": None, "blend": BLEND_ALPHA}
        while True:
            t, fl = self.x.read(va, 2)
            if t == 0xFF:
                return strips
            if t == 0:
                va += 2
            elif t in (4, 5):
                # Cache and draw a polygon list: weighted models share
                # lists across nodes; a stage's static tree should not.
                self.unsupported["cached polygon list"] += 1
                va += 2
            elif t < 8:
                self.stats["bits chunk %d" % t] += 1
                va += 2
            elif t < 16:
                w = struct.unpack("<H", self.x.read(va + 2, 2))[0]
                state["tex"] = w & 0x1FFF
                state["tflags"] = (fl << 16) | (w & 0xE000)
                va += 4
            else:
                size = struct.unpack("<H", self.x.read(va + 2, 2))[0] * 2
                if 64 <= t <= 75:
                    # The drawer walks a strip chunk by its counts. Some
                    # chunks carry a size that undercounts them (as if
                    # each point were 4 bytes), and the next chunk starts
                    # where the counts end, so the counts win.
                    need = self._strip_len(t, va + 4)
                    if need > size:
                        self.stats["strip chunk longer than its size"] += 1
                        size = need
                    strips.append(self._strip(t, fl, self.x.read(va + 4, size), state))
                elif t < 32:
                    self._material(t, fl, self.x.read(va + 4, size), state)
                else:
                    self.unsupported["polygon chunk type %d" % t] += 1
                va += 4 + size

    def _material(self, t, fl, body, state):
        state["blend"] = fl
        o = 0
        if t & 1:
            state["diffuse"] = body[o : o + 4]
            o += 4
        if t & 2:
            o += 4  # ambient: the engine's lighting, not the material's
        if t & 4:
            self.unsupported["specular colour"] += 1
        if t & 8:
            self.unsupported["material type %d" % t] += 1
        if fl != BLEND_ALPHA:
            self.unsupported["blend mode 0x%02X" % fl] += 1

    def _strip_len(self, t, va):
        hdr = struct.unpack("<H", self.x.read(va, 2))[0]
        nstrips, user = hdr & 0x3FFF, hdr >> 14
        point = 2 + 4 * (t in STRIP_UV or t in STRIP_UV2) + 6 * (t in STRIP_VN)
        point += 4 * (t in STRIP_D8) + 4 * (t in STRIP_UV2)
        o = 2
        for _ in range(nstrips):
            n = abs(struct.unpack("<h", self.x.read(va + o, 2))[0])
            o += 2 + point * n + 2 * user * max(n - 2, 0)
        return o

    def _strip(self, t, fl, body, state):
        s = Strip()
        s.kind, s.flags = t, fl
        s.tex, s.tflags = state["tex"], state["tflags"]
        s.diffuse, s.blend = state["diffuse"], state["blend"]
        uv_div = STRIP_UV.get(t) or STRIP_UV2.get(t)
        s.uv = uv_div is not None
        hdr = struct.unpack_from("<H", body, 0)[0]
        nstrips, user = hdr & 0x3FFF, hdr >> 14
        tris = []
        o = 2
        for _ in range(nstrips):
            n = struct.unpack_from("<h", body, o)[0]
            o += 2
            flip = n < 0
            n = abs(n)
            pts = []
            for k in range(n):
                idx = struct.unpack_from("<H", body, o)[0]
                o += 2
                u = v = 0.0
                if uv_div:
                    u, v = struct.unpack_from("<hh", body, o)
                    u, v = u / uv_div, v / uv_div
                    o += 4
                if t in STRIP_VN:
                    o += 6
                if t in STRIP_D8:
                    o += 4
                if t in STRIP_UV2:
                    o += 4
                if k >= 2:
                    o += 2 * user
                pts.append((idx, u, v))
            for k in range(n - 2):
                a, b, c = pts[k], pts[k + 1], pts[k + 2]
                if (k % 2 == 1) != flip:
                    a, b = b, a
                if a[0] == b[0] or b[0] == c[0] or a[0] == c[0]:
                    continue
                tris.append((a, b, c))
        s.tris = tris
        if t in STRIP_VN or t in STRIP_D8 or t in STRIP_UV2:
            self.unsupported["strip type %d extras" % t] += 1
        self.stats["strip chunk %d" % t] += 1
        return s


def quat(rad, flags):
    """A quaternion (x, y, z, w) for Ninja's R = Rz*Ry*Rx (X applied first),
    or Ry*Rx*Rz with EVAL_ZXY."""

    def q(axis, a):
        s, c = math.sin(a / 2), math.cos(a / 2)
        return [(s if i == axis else 0.0) for i in range(3)] + [c]

    def mul(p, r):
        px, py, pz, pw = p
        qx, qy, qz, qw = r
        return [
            pw * qx + px * qw + py * qz - pz * qy,
            pw * qy - px * qz + py * qw + pz * qx,
            pw * qz + px * qy - py * qx + pz * qw,
            pw * qw - px * qx - py * qy - pz * qz,
        ]

    qx, qy, qz = q(0, rad[0]), q(1, rad[1]), q(2, rad[2])
    if flags & EVAL_ZXY:
        return mul(mul(qy, qx), qz)
    return mul(mul(qz, qy), qx)
