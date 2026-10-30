"""What a hook slot needs to know about its function's returns, from disasm's
listing: the bytes its `ret n` pops, and whether it returns a float on the
x87 stack (st0), so that a mod that skips the body can leave the guest's
stack and FPU as the body would.

The x87 answer comes from walking the function's branches with the x87
stack depth carried along, direct callees followed. It is a guess about
code nobody annotated, so it answers only when it is sure: a path that
returns right after a call it cannot follow (an indirect call, a callee it
could not walk, one whose paths disagree), or that leaves the function
through a jump it cannot follow, makes the walk unsure, and analyze then
refuses the entry unless a caller proves a float return or game.toml's
[mods] st0 / no_st0 says which it is.
"""

import collections
import json
import os
import re

from .host import CliError

# disasm's listing line: "  0x0003B180  83ec2c   sub      esp, 0x2c   ; ->".
_INSN_RE = re.compile(r"^  0x([0-9A-Fa-f]{8})  \S+\s+(\S+)[ \t]*([^;\n]*?)\s*(?:;.*)?$")
_START_RE = re.compile(rb"^; Start: 0x([0-9A-Fa-f]{8})  End: 0x([0-9A-Fa-f]{8})", re.M)
_PREFIXES = {"rep", "repe", "repz", "repne", "repnz", "lock"}
_HEX_TARGET = re.compile(r"^0x[0-9A-Fa-f]+$")


def _lines_from(data, at):
    while at < len(data):
        nl = data.find(b"\n", at)
        nl = len(data) if nl < 0 else nl
        yield data[at:nl]
        at = nl + 1


class Listing:
    """disasm's per-section listings (<out>/disasm/asm/<section>.asm) and
    functions.json: the instructions of any function, read on demand."""

    def __init__(self, disasm_dir):
        self.dir = disasm_dir
        with open(os.path.join(disasm_dir, "functions.json")) as f:
            funcs = json.load(f)
        self.funcs = {int(x["start"], 16): x for x in funcs}
        self._files = {}

    def _file(self, section):
        if section not in self._files:
            safe = section.replace("$", "").replace(".", "")
            path = os.path.join(self.dir, "asm", safe + ".asm")
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                raise CliError("no disasm listing %s: run analyze" % path) from e
            index = {int(m.group(1), 16): m.start() for m in _START_RE.finditer(data)}
            self._files[section] = (data, index)
        return self._files[section]

    def insns(self, va):
        """[(addr, mnemonic, operands)] of the function at va, in address
        order; None when va is not a function start."""
        f = self.funcs.get(va)
        if f is None:
            return None
        end = int(f["end"], 16)
        data, index = self._file(f.get("section") or ".text")
        at = index.get(va)
        if at is None:
            return None
        # Up to the function's end, not the next header: a function the
        # disassembler found inside another (a tail-jump alias) starts its
        # own block within the outer one's range.
        out = []
        for raw in _lines_from(data, at):
            m = _INSN_RE.match(raw.decode("ascii", errors="replace"))
            if not m:
                continue
            addr = int(m.group(1), 16)
            if addr >= end:
                break
            mn, ops = m.group(2).lower(), m.group(3).strip()
            if mn in _PREFIXES:
                mn, _, ops = (mn + " " + ops).partition(" ")[2].partition(" ")
            out.append((addr, mn, ops))
        return out

    def containing(self, va):
        """Starts of the other functions in va's section whose range holds
        va (va itself a function start)."""
        sec = self.funcs[va].get("section")
        return sorted(
            s
            for s, f in self.funcs.items()
            if s < va < int(f["end"], 16) and f.get("section") == sec
        )


# x87 stack effect per mnemonic (pushes positive). Everything else is 0:
# fldcw and fldenv load control words, not values.
_FP_DELTA = {
    **dict.fromkeys(
        "fld fild fbld fld1 fldz fldpi fldl2e fldl2t fldlg2 fldln2 fsincos fptan fxtract".split(),
        1,
    ),
    **dict.fromkeys(
        "fstp fistp fisttp fbstp faddp fsubp fsubrp fmulp fdivp fdivrp fcomp ficomp "
        "fucomp fcomip fucomip ffreep fpatan fyl2x fyl2xp1".split(),
        -1,
    ),
    "fcompp": -2,
    "fucompp": -2,
}
_STOP = {"int3", "hlt", "ud2"}
# Instructions read after a call site for the first x87 one.
SITE_SCAN = 40
# x87 instructions that touch neither st0 nor the stack depth.
_FP_CONTROL = {
    "fnstcw",
    "fstcw",
    "fldcw",
    "fnclex",
    "fclex",
    "fwait",
    "fninit",
    "finit",
    "fnstsw",
    "fstsw",
}


_ST_RE = re.compile(r"\bst\((\d)\)")
# Ops with no st operand that read st0 and st1.
_READS_TWO = {
    "fcompp",
    "fucompp",
    "faddp",
    "fsubp",
    "fsubrp",
    "fmulp",
    "fdivp",
    "fdivrp",
    "fxch",
    "fpatan",
    "fyl2x",
    "fyl2xp1",
    "fprem",
    "fprem1",
    "fscale",
}


def _need(mn, ops):
    """How many x87 registers the x87 instruction reads."""
    regs = [int(x) for x in _ST_RE.findall(ops)]
    if regs:
        return max(regs) + 1
    if _FP_DELTA.get(mn) == 1:
        return 0  # a load from memory or a constant
    return 2 if mn in _READS_TWO else 1


def _reads_missing(insns, depth):
    """True when the straight-line insns, starting at x87 depth `depth`,
    read a register that is not there."""
    for _, mn, ops in insns:
        if mn in ("call", "ret") or mn.startswith("j"):
            return False
        if not mn.startswith("f") or mn in _FP_CONTROL:
            continue
        if _need(mn, ops) > depth:
            return True
        depth += _FP_DELTA.get(mn, 0)
    return False


def _is_x87(mn):
    return mn.startswith("f") and mn not in _FP_CONTROL


# One function's walk: the x87 depths and ret_pops seen at its returns,
# whether it (or what it tail-jumps to) runs any x87 instruction, the depth
# before each instruction it reached, by address, whether that depth never
# went below empty (a walk that did started in the middle of something, such
# as a fragment the disassembler split off), and whether some path left the
# function in a way the walk cannot vouch for (see Shape._walk).
Walk = collections.namedtuple("Walk", "deltas pops touches depths sound unsure")
_NO_WALK = Walk(frozenset(), frozenset(), False, {}, False, True)


class Shape:
    """Per function: its x87 stack effect at return and its ret_pop, from
    the listing, with direct callees (and tail jumps) followed."""

    def __init__(self, listing):
        self.listing = listing
        self._memo = {}
        self._insns = {}

    def insns(self, va):
        if va not in self._insns:
            self._insns[va] = self.listing.insns(va)
        return self._insns[va]

    def of(self, va):
        """va's Walk, or _NO_WALK when va is no function start."""
        if va not in self._memo:
            if self.insns(va) is None:
                return _NO_WALK
            self._summarise(va)
        return self._memo[va]

    def _edges(self, va):
        """The listed functions va calls or jumps to."""
        own = {a for a, _, _ in self.insns(va)}
        out = []
        for _, mn, ops in self.insns(va):
            if (mn == "call" or mn.startswith("j")) and _HEX_TARGET.match(ops):
                t = int(ops, 16)
                if t not in own and t not in out and self.insns(t) is not None:
                    out.append(t)
        return out

    def _summarise(self, root):
        """Walk every function root reaches, callees first: Tarjan's
        strongly connected components of the call graph, each emitted after
        the ones it calls into. A call between functions of one component
        (recursion) is one the walk cannot follow, so every walk depends
        only on the graph, never on which function was asked first, and
        each is memoised."""
        index, low, stack, on = {root: 0}, {root: 0}, [root], {root}
        work = [(root, iter(self._edges(root)))]
        while work:
            v, it = work[-1]
            for w in it:
                if w in self._memo:
                    continue
                if w not in index:
                    index[w] = low[w] = len(index)
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(self._edges(w))))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    u = work[-1][0]
                    low[u] = min(low[u], low[v])
                if low[v] == index[v]:
                    comp = set()
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.add(w)
                        if w == v:
                            break
                    walks = {w: self._walk(w, comp) for w in comp}
                    self._memo.update(walks)

    def _callee(self, target, comp):
        """target's Walk, as a call from inside comp sees it."""
        if target in comp:
            return _NO_WALK
        return self.of(target)

    def _after_call(self, target, d, comp):
        """(x87 depth, sure) after `call target` made at depth d. MSVC calls
        a function that uses the x87 stack with the stack empty, except its
        x87 helpers (_CIfmod, _CIpow, _ftol ...), which take their operands
        there and leave one result, or none (_ftol). They are hand-written
        and reach their work through jump tables the walk cannot follow, so
        at d > 0 a callee that touches the x87 stack and does not grow it
        is taken to leave one result; sure only when the callee's own walk
        is sure and its returns agree. A callee with no x87 instruction leaves d as it is: the
        caller may keep a value there across it. A callee the walk cannot
        follow, or whose returns disagree, leaves d unknown."""
        w = self._callee(target, comp)
        delta = next(iter(w.deltas)) if len(w.deltas) == 1 else 0
        if d > 0 and delta >= 0 and w.touches:
            return 1, not w.unsure and len(w.deltas) == 1
        return d + delta, len(w.deltas) == 1 and not w.unsure

    def callers_read_st0(self, va):
        """Whether some caller of va reads, right after the call, an x87
        register that would not be there had va left the stack as it found
        it: proof that va returns in st0. Straight-line code after each call
        site only, from the caller's depth at the call. There is no proof
        the other way: silence after a call proves nothing.

        A caller whose walk goes below empty is used only when it does so
        right after a call to va, which is what a missed float return
        looks like; anywhere else its depths are off (a fragment the
        disassembler split from its function) and prove nothing."""
        for c in self.listing.funcs[va].get("called_by") or ():
            cva = int(c, 16)
            insns = self.insns(cva) or []
            depths = self.of(cva).depths
            sites = [
                i
                for i, (addr, mn, ops) in enumerate(insns)
                if mn == "call" and _HEX_TARGET.match(ops) and int(ops, 16) == va and addr in depths
            ]
            after = set()
            for i in sites:
                for addr, mn, _ in insns[i + 1 : i + 1 + SITE_SCAN]:
                    after.add(addr)
                    if mn in ("call", "ret") or mn.startswith("j"):
                        break
            if any(d < 0 and a not in after for a, d in depths.items()):
                continue
            for i in sites:
                if _reads_missing(insns[i + 1 : i + 1 + SITE_SCAN], depths[insns[i][0]]):
                    return True
        return False

    def _walk(self, va, comp):
        """A depth-first walk over the function's branches, the x87 depth
        carried along. A block reached only through a jump table (an
        indirect jmp with a scaled index) is entered at the depth of the
        last such jump: every case block of every table gets that one
        depth, right for MSVC's switches, which run with the x87 stack as
        their function found it.

        Each path carries whether its depth is in doubt: after a call the
        walk cannot vouch for, until the next x87 instruction that reads
        only registers the walk knows are there (MSVC's code stores or uses
        a float result at once, so such an instruction shows the result
        was accounted for). An x87 instruction that reads below the walked
        depth, after a call on its path, shows a push the walk missed (a
        hidden result being consumed, `call [ecx+8]; fadd dword [eax]`),
        and makes the walk unsure, in doubt or not. With no call before it
        on the path it is an operand the caller left (_ftol reads st0 at
        its depth 0), and taking that as unsure would make _ftol, and every
        function that ends on a call to it, unsure. The walk is unsure when a path returns, or leaves the
        function, in doubt; also when it leaves
        through a jump the walk cannot follow (an indirect jmp that is no
        jump table, a conditional jump into another function)."""
        insns = self.insns(va)
        at = {a: i for i, (a, _, _) in enumerate(insns)}
        seen = {}
        deltas, pops = set(), set()
        touches = unsure = False
        table_depth = []
        work = [(0, 0, False, False)]
        while True:
            while work:
                i, d, doubt, called = work.pop()
                while i < len(insns) and i not in seen:
                    seen[i] = d
                    _, mn, ops = insns[i]
                    target = int(ops, 16) if _HEX_TARGET.match(ops) else None
                    if mn == "ret":
                        deltas.add(d)
                        pops.add(int(ops, 0) if ops else 0)
                        unsure = unsure or doubt
                        break
                    if mn in _STOP:
                        break
                    if mn == "jmp":
                        if target is None:
                            if "*" in ops:
                                table_depth.append((d, called))
                            else:
                                unsure = True  # a tail call through a pointer
                        elif target in at:
                            work.append((at[target], d, doubt, called))
                        else:
                            # A tail call: the target returns for us.
                            t = self._callee(target, comp)
                            deltas |= {d + x for x in t.deltas}
                            pops |= t.pops
                            touches = touches or t.touches
                            unsure = unsure or doubt or t.unsure or not t.deltas
                        break
                    if mn.startswith("j") or mn.startswith("loop"):
                        if target in at:
                            work.append((at[target], d, doubt, called))
                        elif target is not None:
                            # A conditional tail call: taken, another
                            # function returns for this one.
                            t = self._callee(target, comp)
                            deltas |= {d + x for x in t.deltas}
                            pops |= t.pops
                            touches = touches or t.touches
                            unsure = True
                    elif mn == "call":
                        called = True
                        if target is None:
                            doubt = True
                        else:
                            d, sure = self._after_call(target, d, comp)
                            doubt = doubt or not sure
                    elif _is_x87(mn):
                        touches = True
                        if _need(mn, ops) > d:
                            if called:
                                unsure = True
                        else:
                            doubt = False
                        d += _FP_DELTA.get(mn, 0)
                    i += 1
            rest = [i for i in range(len(insns)) if i not in seen]
            if not rest or not table_depth:
                break
            d, called = table_depth[-1]
            work.append((rest[0], d, False, called))
        depths = {insns[i][0]: d for i, d in seen.items()}
        sound = min(depths.values(), default=0) >= 0 and min(deltas, default=0) >= 0
        return Walk(frozenset(deltas), frozenset(pops), touches, depths, sound, unsure)


def x87_return(shape, va, w):
    """(returns in st0, why not decided). A sure walk decides when every
    ret agrees; a caller that reads st0 after the call proves a float
    return the walk missed, and contradicts a walk that found none. Callers
    are looked at only when the walk alone does not decide."""
    if not w.unsure and w.deltas == {1}:
        return True, ""
    if not w.unsure and w.deltas == {0}:
        if shape.callers_read_st0(va):
            return False, (
                "its rets leave the x87 stack as they found it, but a caller reads st0 "
                "after calling it; cannot tell whether it returns a float"
            )
        return False, ""
    if w.deltas and w.deltas <= {0, 1} and w.sound and shape.callers_read_st0(va):
        return True, ""
    depths = ", ".join("%+d" % x for x in sorted(w.deltas)) or "none"
    why = "cannot tell whether it returns a float (x87 depth at its rets: %s" % depths
    if w.unsure:
        why += "; a path returns through a call or jump the walk cannot follow"
    return False, why + "); say which with [mods] st0 or no_st0"
