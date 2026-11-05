"""Release matcher (spike): pair one release's functions with
another release's of the same game, and its globals through them.

A port is written against one dump (the reference): its manual wraps,
seeds, hooks, symbols and data addresses are guest VAs of that XBE. Another
region or revision of the game is mostly the same code relinked at other
addresses, so most of that work carries over once each reference address
has its counterpart. This finds them from the two analyses `analyze`
already makes (the disassembler's functions.json and xrefs.json) and the
XBEs, with no disassembler of its own:

- masked bytes: a function's bytes with every field that encodes an address
  zeroed. The fields come from the xrefs (a call's rel32, a data operand's
  absolute VA), plus any aligned-or-not dword that falls in the image, for
  the immediates the disassembler did not record;
- pass 1, exact: a masked hash unique on both sides;
- pass 2, calls: a matched pair's call sites at the same offset name callee
  pairs, so a changed function is still found through an unchanged caller
  (and, through the callers' tables, the reverse);
- pass 3, order: between two matched neighbours of one section, gaps with
  the same number of functions on both sides pair in order (the linker
  keeps each object file's order), when the sizes agree;
- globals: in a pair whose masked bytes are equal, the address fields at the
  same offsets give reference -> other VA pairs, kept by vote. A VA no
  instruction names (a struct field, a table row) is placed by the nearest
  mapped global of its section below it, at the same distance.

Everything stays keyed by address; this only proposes the other release's
addresses for the reference ones. Run it as `python -m xboxrecomp_cli.match`.
"""

import argparse
import bisect
import difflib
import hashlib
import json
import struct
import sys
from collections import Counter, defaultdict, namedtuple

from . import xbe

Func = namedtuple("Func", "start end section")
# A masked field: offset in the function, length, the VA it encodes, kind.
Field = namedtuple("Field", "off size va kind")

CALL_KINDS = ("call", "jump", "cond_jump")
DATA_KINDS = ("data_read", "data_imm", "kernel_call")
# Below this many bytes a masked body is too generic to pair on its own
# (ret, xor eax,eax; ret, a jmp thunk): those pair only through calls and
# order.
MIN_EXACT = 6
# Changed pairs longer than this are not aligned byte by byte (difflib is
# quadratic at worst); their fields carry nothing.
MAX_ALIGN = 16384
# An aligned field needs this many common bytes around it.
MIN_CONTEXT = 4
# What plausible() allows a proposed pair.
SIZE_RATIO = 2.5
MIN_SIMILAR = 0.5


class Release:
    """One XBE with its analysis: the image, the functions and the fields
    that encode addresses in each."""

    def __init__(self, base, image_size, sections, functions, xrefs):
        """sections: [(name, va, bytes)], each zero-filled to its virtual
        size; functions and xrefs as the disassembler writes them."""
        self.base, self.image_size = base, image_size
        self.mem = {name: (va, raw) for name, va, raw in sections}
        self.sec_starts = sorted((va, va + len(raw), name) for name, va, raw in sections)
        self.funcs = sorted(
            (Func(int(f["start"], 16), int(f["end"], 16), f["section"]) for f in functions),
            key=lambda f: f.start,
        )
        self.by_start = {f.start: f for f in self.funcs}
        self._starts = [f.start for f in self.funcs]
        self.refs = defaultdict(list)
        for x in xrefs:
            fr = int(x["from"], 16)
            fn = self.func_at(fr)
            if fn is not None:
                self.refs[fn.start].append((fr, int(x["to"], 16), x["type"]))
        self._masked = {}
        self.callers = defaultdict(set)
        for start, rs in self.refs.items():
            for _, to, kind in rs:
                if kind in CALL_KINDS and to in self.by_start and to != start:
                    self.callers[to].add(start)

    def section_of(self, va):
        i = bisect.bisect_right(self.sec_starts, (va, 1 << 33, "")) - 1
        if i >= 0 and self.sec_starts[i][0] <= va < self.sec_starts[i][1]:
            return self.sec_starts[i][2]
        return None

    def read(self, va, n):
        name = self.section_of(va)
        if name is None:
            return b""
        sva, raw = self.mem[name]
        return raw[va - sva : va - sva + n]

    def func_at(self, va):
        i = bisect.bisect_right(self._starts, va) - 1
        if i >= 0 and self.funcs[i].start <= va < self.funcs[i].end:
            return self.funcs[i]
        return None

    def in_image(self, v):
        return self.base <= v < self.base + self.image_size

    def masked(self, fn):
        """(masked bytes, [Field]) of fn, cached."""
        got = self._masked.get(fn.start)
        if got is None:
            got = self._masked[fn.start] = _mask(self, fn)
        return got


def _mask(rel, fn):
    raw = rel.read(fn.start, fn.end - fn.start)
    # The int3 padding to the next function's alignment is the linker's,
    # not the function's: a relink pads differently.
    body = bytearray(raw.rstrip(b"\xcc") or raw[:1])
    fields = {}
    for fr, to, kind in rel.refs.get(fn.start, ()):
        off = fr - fn.start
        if off >= len(body):
            continue
        if kind in CALL_KINDS:
            if fn.start <= to < fn.end:
                # A branch inside the function is position independent.
                continue
            op = body[off] if off < len(body) else 0
            if op in (0xE8, 0xE9):
                fields[off + 1] = Field(off + 1, 4, to, kind)
            elif op == 0x0F and off + 1 < len(body) and 0x80 <= body[off + 1] <= 0x8F:
                fields[off + 2] = Field(off + 2, 4, to, kind)
            elif op == 0xEB or 0x70 <= op <= 0x7F:
                fields[off + 1] = Field(off + 1, 1, to, kind)
            continue
        want = struct.pack("<I", to)
        at = bytes(body[off : off + 15]).find(want)
        if at >= 0:
            fields[off + at] = Field(off + at, 4, to, kind)
    # Immediates the xrefs leave out (push offset, a table base in an
    # lea): any dword that falls in the image. A few constants are masked
    # by mistake; both releases mask them alike when the code is the same.
    i = 0
    covered = set()
    for f in fields.values():
        covered.update(range(f.off, f.off + f.size))
    while i + 4 <= len(body):
        if i not in covered and not covered.intersection(range(i, i + 4)):
            (v,) = struct.unpack_from("<I", body, i)
            if rel.in_image(v) and v > rel.base + 0x1000:
                fields[i] = Field(i, 4, v, "imm")
                covered.update(range(i, i + 4))
                i += 4
                continue
        i += 1
    out = sorted(fields.values())
    for f in out:
        body[f.off : f.off + f.size] = b"\0" * f.size
    return bytes(body), out


def masked_key(rel, fn):
    body, fields = rel.masked(fn)
    h = hashlib.sha1(body)
    h.update(repr([(f.off, f.size) for f in fields]).encode())
    return h.hexdigest()


class Match:
    """The pairing of two releases: fn (reference start -> other start, with
    the pass that found it) and data (reference VA -> other VA)."""

    def __init__(self, ref, other):
        self.ref, self.other = ref, other
        self.fn = {}
        self.how = {}
        self.rev = {}
        self.votes = defaultdict(Counter)
        self.data = {}
        self._aligned = {}

    def pair(self, a, b, how):
        if a in self.fn or b in self.rev:
            return False
        self.fn[a], self.rev[b], self.how[a] = b, a, how
        return True

    # ── passes ───────────────────────────────────────────────────────────

    def exact(self):
        ka, kb = defaultdict(list), defaultdict(list)
        for f in self.ref.funcs:
            if f.end - f.start >= MIN_EXACT:
                ka[masked_key(self.ref, f)].append(f.start)
        for f in self.other.funcs:
            if f.end - f.start >= MIN_EXACT:
                kb[masked_key(self.other, f)].append(f.start)
        n = 0
        for k, a in ka.items():
            b = kb.get(k)
            if b and len(a) == 1 and len(b) == 1:
                n += self.pair(a[0], b[0], "exact")
        return n

    def _same_shape(self, a, b):
        fa, fb = self.ref.by_start[a], self.other.by_start[b]
        ma, mb = self.ref.masked(fa), self.other.masked(fb)
        return ma[0] == mb[0] and [(f.off, f.size) for f in ma[1]] == [
            (f.off, f.size) for f in mb[1]
        ]

    def aligned(self, a, b):
        """[(ref Field, other Field)] at the same place in a pair: by offset
        when the masked bytes are equal, else through the blocks of masked
        bytes the two have in common (a changed function still shares most
        of its instructions), each field kept only when it lies wholly
        inside one block on both sides."""
        key = (a, b)
        got = self._aligned.get(key)
        if got is not None:
            return got
        ma = self.ref.masked(self.ref.by_start[a])
        mb = self.other.masked(self.other.by_start[b])
        if self._same_shape(a, b):
            got = list(zip(ma[1], mb[1]))
        elif len(ma[0]) > MAX_ALIGN or len(mb[0]) > MAX_ALIGN:
            got = []
        else:
            at_b = {f.off: f for f in mb[1]}
            got = []
            sm = difflib.SequenceMatcher(None, ma[0], mb[0], autojunk=False)
            blocks = sm.get_matching_blocks()
            for x in ma[1]:
                for i, j, n in blocks:
                    if n >= x.size + MIN_CONTEXT and i <= x.off and x.off + x.size <= i + n:
                        y = at_b.get(j + x.off - i)
                        if y is not None and y.size == x.size:
                            got.append((x, y))
                        break
        self._aligned[key] = got
        return got

    def plausible(self, a, b):
        """A pair the bytes do not rule out: the same section, sizes within
        a factor of SIZE_RATIO, and masked bytes at least MIN_SIMILAR alike
        (difflib's quick ratio, an upper bound of the real one). The call
        and neighbour passes propose; this keeps them from pairing a thunk
        with the body it was inlined into, or a ret with a function."""
        fa, fb = self.ref.by_start[a], self.other.by_start[b]
        if fa.section != fb.section:
            return False
        ma, mb = self.ref.masked(fa)[0], self.other.masked(fb)[0]
        la, lb = len(ma), len(mb)
        if max(la, lb) > SIZE_RATIO * min(la, lb):
            return False
        sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
        return sm.quick_ratio() >= MIN_SIMILAR

    def calls(self):
        """Callee pairs from the call fields at the same place in a pair."""
        n = 0
        changed = True
        while changed:
            changed = False
            for a, b in list(self.fn.items()):
                for x, y in self.aligned(a, b):
                    if x.kind not in ("call", "jump", "imm"):
                        continue
                    if x.va in self.fn or y.va in self.rev:
                        continue
                    if x.va in self.ref.by_start and y.va in self.other.by_start:
                        if self.plausible(x.va, y.va) and self.pair(x.va, y.va, "calls"):
                            n += 1
                            changed = True
        return n

    def _features(self, rel, start, to_other):
        """What a function touches, in the other release's addresses where
        the reference's are known: its callees and callers, and the globals
        it reads. to_other maps a reference VA, or is None for the other
        release's own."""
        out = set()
        fields = rel.masked(rel.by_start[start])[1]
        for f in fields:
            va = f.va
            if f.kind == "imm":
                # The value itself too: a constant that only falls in the
                # image (a size, a mask) is the same number on both sides,
                # a unique-constant anchor.
                out.add(("k", va))
            if to_other is not None:
                va = to_other(va)
                if va is None:
                    continue
            out.add(("c" if f.kind in CALL_KINDS else "d", va))
        for c in rel.callers.get(start, ()):
            c2 = c if to_other is None else self.fn.get(c)
            if c2 is not None:
                out.add(("r", c2))
        return out

    def neighbours(self, min_score=0.5, min_shared=3):
        """Unmatched functions paired by what they touch: the matched
        callees and callers and the mapped globals. A reference function
        pairs with the unmatched function of its own section that
        shares the most (Jaccard >= min_score, at least min_shared
        features), when no other candidate comes within 0.1 of it."""

        def to_other(va):
            if va in self.fn:
                return self.fn[va]
            if self.ref.func_at(va) is not None:
                return None
            return self.data.get(va)

        index = defaultdict(set)
        feats_b = {}
        for f in self.other.funcs:
            if f.start in self.rev:
                continue
            fb = self._features(self.other, f.start, None)
            feats_b[f.start] = fb
            for t in fb:
                index[t].add(f.start)
        props = []
        for f in self.ref.funcs:
            if f.start in self.fn:
                continue
            fa = self._features(self.ref, f.start, to_other)
            if len(fa) < min_shared:
                continue
            cand = Counter()
            for t in fa:
                for b in index.get(t, ()):
                    cand[b] += 1
            scored = []
            for b, shared in cand.most_common(8):
                if shared < min_shared:
                    break
                if self.other.by_start[b].section != f.section:
                    continue
                if not self.plausible(f.start, b):
                    continue
                scored.append((shared / len(fa | feats_b[b]), b))
            scored.sort(reverse=True)
            if scored and scored[0][0] >= min_score:
                if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1:
                    props.append((scored[0][0], f.start, scored[0][1]))
        n = 0
        # Best first, so two reference functions wanting one partner leave
        # it to the stronger.
        for _, a, b in sorted(props, reverse=True):
            n += self.pair(a, b, "neighbours")
        return n

    def order(self):
        """Gaps between matched neighbours that hold the same number of
        functions on both sides pair in order when every size is within a
        quarter (or 16 bytes) of its partner's."""
        n = 0
        by_sec_a = defaultdict(list)
        for f in self.ref.funcs:
            by_sec_a[f.section].append(f)
        for fa in by_sec_a.values():
            anchors = [i for i, f in enumerate(fa) if f.start in self.fn]
            for i0, i1 in zip(anchors, anchors[1:]):
                gap_a = fa[i0 + 1 : i1]
                if not gap_a:
                    continue
                b0, b1 = self.fn[fa[i0].start], self.fn[fa[i1].start]
                if b1 <= b0:
                    continue
                gap_b = [
                    f
                    for f in self.other.funcs[
                        bisect.bisect_right(self.other._starts, b0) : bisect.bisect_left(
                            self.other._starts, b1
                        )
                    ]
                ]
                if len(gap_a) != len(gap_b):
                    continue
                if any(f.start in self.rev for f in gap_b):
                    continue
                ok = all(
                    abs((x.end - x.start) - (y.end - y.start)) <= max(16, (x.end - x.start) // 4)
                    for x, y in zip(gap_a, gap_b)
                )
                if ok:
                    for x, y in zip(gap_a, gap_b):
                        n += self.pair(x.start, y.start, "order")
        return n

    def run(self, fuzzy=True):
        stats = Counter(exact=self.exact())
        stats["calls"] += self.calls()
        # The passes feed each other: each new pair is a new anchor.
        for _ in range(8):
            o = self.order()
            c = self.calls()
            self.globals_()
            nb = self.neighbours() if fuzzy else 0
            stats.update(order=o, calls=c, neighbours=nb)
            if not (o or c or nb):
                break
        self.globals_()
        return dict(stats)

    # ── globals ──────────────────────────────────────────────────────────

    def globals_(self):
        """Votes for each reference address an aligned field names: data
        (self.data) and code pointers (self.code: a thread routine or
        callback pushed as an immediate, which is how a seed the other
        release's analysis lacks is still found)."""
        self.votes = defaultdict(Counter)
        code_votes = defaultdict(Counter)
        for a, b in self.fn.items():
            same = self._same_shape(a, b)
            for x, y in self.aligned(a, b):
                # An immediate only the image range calls an address is
                # trusted in equal bytes alone: aligned into changed code
                # it is as often a flag word that changed with it.
                if x.kind == "imm" and not same:
                    continue
                # The disassembler's data_imm is a guess as well: a fill
                # pattern (0x02020202) that falls in a moved section and
                # did not move with it is a constant.
                if x.kind != "kernel_call" and not self._moved_alike(x.va, y.va):
                    continue
                if x.kind in DATA_KINDS or x.kind == "imm":
                    if self.ref.func_at(x.va) is None:
                        self.votes[x.va][y.va] += 1
                    else:
                        code_votes[x.va][y.va] += 1
        self.data = {}
        self.data_conflict = {}
        for va, c in self.votes.items():
            (best, n), *rest = c.most_common()
            self.data[va] = best
            if rest:
                self.data_conflict[va] = dict(c)
        self.code = {va: c.most_common(1)[0][0] for va, c in code_votes.items()}
        self._data_keys = sorted(self.data)

    def _moved_alike(self, x, y):
        """An immediate the xrefs did not name is taken for an address
        only when it lies in the same section on both sides and moved with
        it: a constant that only happens to fall in the image (a mask, a
        size) stays put while its section moves."""
        sx, sy = self.ref.section_of(x), self.other.section_of(y)
        if sx is None or sx != sy:
            return False
        moved = self.other.mem[sy][0] != self.ref.mem[sx][0]
        return x != y or not moved

    def _map_offset(self, a, b, off):
        """off inside reference function a, as an offset inside b: the same
        offset when the masked bytes are equal, else through the common
        block that holds it; None when it lies in changed bytes."""
        if self._same_shape(a, b):
            return off
        ma = self.ref.masked(self.ref.by_start[a])[0]
        mb = self.other.masked(self.other.by_start[b])[0]
        if len(ma) > MAX_ALIGN or len(mb) > MAX_ALIGN:
            return None
        sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
        for i, j, n in sm.get_matching_blocks():
            if i <= off < i + n and n >= MIN_CONTEXT:
                return j + off - i
        return None

    def place(self, va):
        """(other VA, how) for a reference VA, or (None, why). how starts
        with "sure" or "unsure": only "sure" needs no look by hand."""
        if va in self.fn:
            sure = "sure" if self.how[va] == "exact" else "unsure"
            return self.fn[va], "%s: function (%s)" % (sure, self.how[va])
        if va in self.code:
            return self.code[va], "unsure: code pointer (pushed by a matched function)"
        f = self.ref.func_at(va)
        if f is not None:
            if f.start not in self.fn:
                return None, "in unmatched function 0x%X" % f.start
            b = self.fn[f.start]
            off = self._map_offset(f.start, b, va - f.start)
            if off is None:
                return None, "in changed bytes of 0x%X" % f.start
            sure = "sure" if self._same_shape(f.start, b) else "unsure"
            return b + off, "%s: inside function 0x%X +0x%X" % (sure, f.start, va - f.start)
        if va in self.data:
            sure = "unsure" if va in self.data_conflict else "sure"
            return self.data[va], "%s: global (referenced)" % sure
        sec = self.ref.section_of(va)
        i = bisect.bisect_right(self._data_keys, va) - 1
        if i < 0 or self.ref.section_of(self._data_keys[i]) != sec:
            # A data section no code names an address in (model and map
            # data reached through tables): the section's own move, sure
            # when its size is unchanged.
            if sec in self.ref.mem and sec in self.other.mem:
                (va0, raw0), (va1, raw1) = self.ref.mem[sec], self.other.mem[sec]
                sure = "sure" if len(raw0) == len(raw1) else "unsure"
                return va + va1 - va0, "%s: section %s moved %+#x%s" % (
                    sure,
                    sec,
                    va1 - va0,
                    "" if sure == "sure" else " (its size changed)",
                )
            return None, "no mapped global below it in %s" % sec
        lo = self._data_keys[i]
        d = self.data[lo] - lo
        # The neighbour above with the same shift: nothing moved between.
        hi = self._data_keys[i + 1] if i + 1 < len(self._data_keys) else None
        same = hi is not None and self.ref.section_of(hi) == sec and self.data[hi] - hi == d
        return va + d, "%s: between globals 0x%X and %s, shift %+#x%s" % (
            "sure" if same else "unsure",
            lo,
            "0x%X" % hi if hi is not None else "-",
            d,
            "" if same else " (the next one moves by another)",
        )

    # ── checks ───────────────────────────────────────────────────────────

    def disagreements(self):
        """Pairs the map itself contradicts, the false-match signal that
        needs no ground truth: a same-shape pair whose call at some offset
        goes to a function matched elsewhere, and pairs out of their
        section's order (the linker keeps an object file's order, so an
        inversion is a suspect unless the file order changed)."""
        bad_calls = set()
        for a, b in self.fn.items():
            if not self._same_shape(a, b):
                continue
            fa = self.ref.masked(self.ref.by_start[a])[1]
            fb = self.other.masked(self.other.by_start[b])[1]
            for x, y in zip(fa, fb):
                if x.kind in ("call", "jump") and x.va in self.fn and self.fn[x.va] != y.va:
                    bad_calls.add(a)
        inversions = set()
        by_sec = defaultdict(list)
        for a in sorted(self.fn):
            by_sec[self.ref.by_start[a].section].append(a)
        for xs in by_sec.values():
            ys = [self.fn[a] for a in xs]
            # A pair out of order with both neighbours is the suspect.
            for i in range(1, len(xs) - 1):
                if not (ys[i - 1] < ys[i] < ys[i + 1]) and ys[i - 1] < ys[i + 1]:
                    inversions.add(xs[i])
        return sorted(bad_calls), sorted(inversions)

    # ── report ───────────────────────────────────────────────────────────

    def summary(self):
        per = defaultdict(lambda: [0, 0])
        for f in self.ref.funcs:
            per[f.section][1] += 1
            if f.start in self.fn:
                per[f.section][0] += 1
        bad_calls, bad_order = self.disagreements()
        return {
            "functions": {"ref": len(self.ref.funcs), "other": len(self.other.funcs)},
            "matched": len(self.fn),
            "by_pass": dict(Counter(self.how.values())),
            "by_section": {k: {"matched": v[0], "of": v[1]} for k, v in sorted(per.items())},
            "globals": len(self.votes),
            "globals_conflicting": len(self.data_conflict),
            "suspect_calls": len(bad_calls),
            "suspect_order": len(bad_order),
        }


def read_xbe(path):
    """(base, image size, [(name, va, bytes)]) of an XBE file."""
    with open(path, "rb") as f:
        data = f.read()
    base = struct.unpack_from("<I", data, 0x104)[0]
    size = struct.unpack_from("<I", data, 0x10C)[0]
    secs = []
    for s in xbe.sections(data[: xbe.header_size(data)]):
        raw = data[s.raw_off : s.raw_off + min(s.raw_size, s.vsize)]
        secs.append((s.name, s.va, raw.ljust(s.vsize, b"\0")))
    return base, size, secs


def load(xbe_path, disasm_dir):
    with open("%s/functions.json" % disasm_dir, encoding="utf-8") as f:
        functions = json.load(f)
    with open("%s/xrefs.json" % disasm_dir, encoding="utf-8") as f:
        xrefs = json.load(f)
    return Release(*read_xbe(xbe_path), functions, xrefs)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m xboxrecomp_cli.match", description=__doc__)
    p.add_argument("ref_xbe")
    p.add_argument("ref_disasm", help="the reference's analysis/disasm folder")
    p.add_argument("other_xbe")
    p.add_argument("other_disasm")
    p.add_argument("--place", nargs="*", default=[], help="reference VAs to place (hex)")
    p.add_argument("--map", help="write the function and data map here (JSON)")
    a = p.parse_args(argv)
    m = Match(load(a.ref_xbe, a.ref_disasm), load(a.other_xbe, a.other_disasm))
    stats = m.run()
    out = m.summary()
    out["passes"] = stats
    print(json.dumps(out, indent=1))
    for v in a.place:
        va = int(v, 16)
        b, how = m.place(va)
        print("0x%08X -> %s  %s" % (va, "0x%08X" % b if b is not None else "-", how))
    if a.map:
        with open(a.map, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "functions": {"0x%08X" % k: ["0x%08X" % v, m.how[k]] for k, v in m.fn.items()},
                    "data": {"0x%08X" % k: "0x%08X" % v for k, v in m.data.items()},
                    "data_conflicts": {
                        "0x%08X" % k: {"0x%08X" % x: n for x, n in c.items()}
                        for k, c in m.data_conflict.items()
                    },
                },
                f,
                indent=1,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
