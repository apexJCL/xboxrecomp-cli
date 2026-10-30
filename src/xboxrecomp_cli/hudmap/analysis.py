"""What hud-map reads from the analysis (<out>/disasm/): the functions'
ranges, and the return address of every direct call to a given function.

A call's return address is the call's address plus its length. xrefs.json
names each call (from, to); the length comes from the listing's bytes
column (disasm/asm/<section>.asm), read only for the calls asked about.
"""

import bisect
import json
import os
import re

from ..host import CliError

_LINE_RE = re.compile(rb"^  0x([0-9A-Fa-f]{8})  ([0-9a-fA-F]+)\s")


class Analysis:
    def __init__(self, disasm_dir):
        self.dir = disasm_dir
        path = os.path.join(disasm_dir, "functions.json")
        try:
            with open(path, encoding="utf-8") as f:
                funcs = json.load(f)
        except (OSError, ValueError) as e:
            raise CliError("no analysis at %s: run analyze" % path) from e
        self.funcs = sorted(
            (int(x["start"], 16), int(x["end"], 16), x.get("name") or "", x.get("section"))
            for x in funcs
        )
        self._starts = [f[0] for f in self.funcs]
        self._calls = None

    @classmethod
    def of(cls, G):
        return cls(os.path.join(G.out, "disasm"))

    def function(self, va):
        """(start, end, name) of the innermost function holding va, or
        None. Innermost: a function found inside another's range (a
        tail-jump alias) is the nearer one."""
        i = bisect.bisect_right(self._starts, va)
        while i > 0:
            i -= 1
            s, e, name, _ = self.funcs[i]
            if s <= va < e:
                return s, e, name
            if va - s > 0x100000:
                break
        return None

    def body(self, va):
        """(start, end) of the function starting at va, or None."""
        i = bisect.bisect_left(self._starts, va)
        if i < len(self.funcs) and self.funcs[i][0] == va:
            return self.funcs[i][0], self.funcs[i][1]
        return None

    def calls(self):
        """{from: to} of every direct call."""
        if self._calls is None:
            path = os.path.join(self.dir, "xrefs.json")
            try:
                with open(path, encoding="utf-8") as f:
                    xrefs = json.load(f)
            except (OSError, ValueError) as e:
                raise CliError("no analysis at %s: run analyze" % path) from e
            self._calls = {}
            for x in xrefs:
                if x.get("type") == "call":
                    try:
                        self._calls[int(x["from"], 16)] = int(x["to"], 16)
                    except (KeyError, TypeError, ValueError):
                        continue
        return self._calls

    def lengths(self, addrs):
        """{addr: instruction length} for the instructions at addrs, from
        the listings."""
        want = set(addrs)
        out = {}
        asm = os.path.join(self.dir, "asm")
        try:
            names = sorted(n for n in os.listdir(asm) if n.endswith(".asm"))
        except OSError as e:
            raise CliError("no disasm listing in %s: run analyze" % asm) from e
        for n in names:
            with open(os.path.join(asm, n), "rb") as f:
                for line in f:
                    m = _LINE_RE.match(line)
                    if m:
                        a = int(m.group(1), 16)
                        if a in want:
                            out[a] = len(m.group(2)) // 2
            if len(out) == len(want):
                break
        return out

    def returns_to(self, targets):
        """{return address: callee} of every direct call to one of targets."""
        targets = set(targets)
        froms = {a: t for a, t in self.calls().items() if t in targets}
        lens = self.lengths(froms)
        return {a + lens[a]: t for a, t in froms.items() if a in lens}
