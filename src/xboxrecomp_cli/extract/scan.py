"""Maintainer aid: candidate stage roots and texlists in one XBE section,
found by structure alone, for filling in [[extract.stage]].

A root candidate is an NJS_OBJECT that passes the shape checks (small
eval flags, sane floats, links inside the image) and that no other
candidate in the section names as its child or sibling; it is listed
with the objects and models its tree holds, largest first. A texlist
candidate is (names*, count) whose names are printable and NUL-padded.
"""

import math
import struct


def _finite(v, limit):
    return math.isfinite(v) and abs(v) < limit


def is_object(image, va, fmt):
    try:
        f = image.unpack(fmt.object, va)
    except ValueError:
        return False
    flags, model, child, sib = f[0], f[1], f[11], f[12]
    if flags > 0x3FF:
        return False
    for p in (model, child, sib):
        if p and (p & 3 or not image.contains(p, 0x18)):
            return False
    # An empty node that only links to its sibling is a real object (a
    # group's placeholder); one that links nowhere is not told from zeros.
    if not (model or child or sib):
        return False
    if not all(_finite(v, 1e6) for v in f[2:5]):
        return False
    if not all(_finite(v, 1e3) and 1e-3 < abs(v) for v in f[8:11]):
        return False
    if fmt.angles == "radians":
        if not all(_finite(v, 100.0) for v in f[5:8]):
            return False
    elif not all(abs(v) < 0x20000 for v in f[5:8]):
        return False
    if model:
        try:
            vl, pl = image.unpack("<II", model)
        except ValueError:
            return False
        for p in (vl, pl):
            if p and (not image.contains(p, 8)):
                return False
        if not vl and not pl:
            return False
    return True


def roots(image, section, fmt, limit=20):
    """[(va, objects, models)] for the root candidates of a section."""
    s = image.section(section)
    cands = set()
    for va in range(s.va, s.va + s.vsize - 0x34, 4):
        if is_object(image, va, fmt):
            cands.add(va)
    linked = set()
    for va in cands:
        f = image.unpack(fmt.object, va)
        linked.update((f[11], f[12]))
    out = []
    for va in sorted(cands - linked):
        objs, models, seen, stack = 0, set(), set(), [va]
        ok = True
        while stack and ok:
            v = stack.pop()
            if v in seen:
                continue
            seen.add(v)
            if v not in cands:
                ok = False
                break
            f = image.unpack(fmt.object, v)
            objs += 1
            if f[1]:
                models.add(f[1])
            stack += [p for p in (f[11], f[12]) if p]
        if ok:
            out.append((va, objs, len(models)))
    out.sort(key=lambda r: (-r[2], -r[1], r[0]))
    return out[:limit]


def texlists(image, section, name_size, limit=20):
    """[(va, count, first name)] for texlist candidates in a section."""
    s = image.section(section)
    out = []
    for va in range(s.va, s.va + s.vsize - 8, 4):
        p, n = image.unpack("<II", va)
        if not (0 < n <= 4096) or p & 3 or not image.contains(p, n * name_size):
            continue
        names = []
        for i in range(min(n, 4)):
            raw = image.read(p + i * name_size, name_size)
            nm, _, pad = raw.partition(b"\0")
            if not nm or any(c < 0x20 or c > 0x7E for c in nm) or pad.strip(b"\0"):
                break
            names.append(nm.decode("ascii"))
        if len(names) == min(n, 4):
            out.append((va, n, names[0]))
    out.sort(key=lambda r: -r[1])
    return out[:limit]


def model_pointers(image, root, fmt):
    """The model VAs of the tree at root (to match against a draw log)."""
    out = []
    stack = [root]
    seen = set()
    while stack:
        v = stack.pop()
        if not v or v in seen:
            continue
        seen.add(v)
        f = struct.unpack(fmt.object, image.read(v, struct.calcsize(fmt.object)))
        if f[1]:
            out.append(f[1])
        stack += [f[11], f[12]]
    return out
