"""[mods] hook slots (mod-connectivity-layer D3): the manifest table, what
analyze resolves into config/hook_functions.json from a synthetic
disassembly, and what recomp emits. An empty list must leave the argv, the
generation key and gen/ exactly as a game without [mods] has them.

  uv run pytest tests/test_hooks.py
"""

import importlib.util
import json
import os
import re
import subprocess

import pytest
from test_cli import fake_tree, write
from test_manifest import MINIMAL

from xboxrecomp_cli import hooks, hooks_x87, host, manifest, pipeline, xbe
from xboxrecomp_cli.host import CliError

# ── the manifest ─────────────────────────────────────────────────────────


def mods(text):
    return manifest.parse_text(MINIMAL + "[mods]\n" + text + "\n")["mods"]


def test_manifest_mods_default_and_full():
    assert manifest.parse_text(MINIMAL)["mods"] == {
        "hook_functions": [],
        "signatures": {},
        "st0": [],
        "no_st0": [],
    }
    m = mods(
        'hook_functions = ["0x0003B180", "sig:hud.draw"]\n'
        "[mods.signatures.'hud.draw']\n"
        'pattern = "55 8B ?? EC"\nsection = ".text"'
    )
    assert m["hook_functions"] == ["0x0003B180", "sig:hud.draw"]
    assert m["signatures"] == {"hud.draw": {"pattern": "55 8B ?? EC", "section": ".text"}}


@pytest.mark.parametrize(
    "text, error",
    [
        ('hook_functions = ["3B180"]', "neither a VA"),
        ('hook_functions = ["0x10", "0x0010"]', "'0x0010' is listed twice"),
        ('hook_functions = ["sig:x"]', "has no [mods.signatures.x]"),
        ('hook_functions = ["0x10"]\nst0 = ["0x20"]', "mods.st0: '0x20' is not in hook_functions"),
        ('hook_functions = ["0x10"]\nst0 = ["0x10"]\nno_st0 = ["0x10"]', "in both st0 and no_st0"),
        ('hook_functions = []\n[mods.signatures.x]\npattern = "5Z"\nsection = ".text"', "'5Z'"),
        ('hook_functions = []\n[mods.signatures.x]\npattern = "?? ??"\nsection = "T"', "wildcards"),
        ('hook_functions = []\n[mods.signatures.x]\npattern = "55"', "x.section: required"),
        (
            'hook_functions = []\n[mods.signatures.x]\npattern = "55"\nsection = "T"\nva = "1"',
            "x.va",
        ),
    ],
)
def test_manifest_mods_errors(text, error):
    with pytest.raises(manifest.ManifestError, match=re.escape(error)):
        mods(text)


# ── signatures ───────────────────────────────────────────────────────────


def test_pattern_regex():
    r = hooks.pattern_regex("55 8b ?? EC")
    assert [m.start() for m in r.finditer(b"\x00\x55\x8b\x01\xec\x55\x8b\xff\xec")] == [1, 5]
    # Overlapping matches count, so "AA AA" in AAA is two matches.
    assert len(list(hooks.pattern_regex("AA AA").finditer(b"\xaa\xaa\xaa"))) == 2
    # A wildcard matches a newline byte too.
    assert hooks.pattern_regex("55 ?").search(b"\x55\x0a")
    for bad in ("", "5", "GG", "55 8B?", "?? ?"):
        with pytest.raises(ValueError):
            hooks.pattern_regex(bad)


def test_resolve_signature():
    data = b"\0" * 16 + b"\x55\x8b\xec\x90" + b"\x55\x8b\xec\x91" + b"\x55\x8b\xec\x90" * 2
    secs = [
        xbe.Section(".text", 6, 0x11000, 12, 16, 12),
        xbe.Section("D3D", 6, 0x20000, 4, 28, 4),
    ]
    row = {"pattern": "55 8B EC 91", "section": ".text"}
    assert hooks.resolve_signature(data, secs, "s", row) == 0x11004
    with pytest.raises(CliError, match=r"2 matches in \.text \(0x00011000, 0x00011008\)"):
        hooks.resolve_signature(data, secs, "s", dict(row, pattern="55 8B EC 90"))
    with pytest.raises(CliError, match="0 matches in .text; a signature must match exactly once"):
        hooks.resolve_signature(data, secs, "s", dict(row, pattern="55 8B EC 92"))
    # The section bounds the search: the last copy lies in D3D.
    assert hooks.resolve_signature(
        data, secs, "s", dict(row, section="D3D", pattern="55 ?? EC")
    ) == (0x20000)
    with pytest.raises(CliError, match="no section '.data' \\(it has .text, D3D\\)"):
        hooks.resolve_signature(data, secs, "s", dict(row, section=".data"))


# ── the manual file ──────────────────────────────────────────────────────


def test_manual_defined(d):
    p = os.path.join(d, "manual.c")
    write(
        p,
        "void sub_00011000(void)\n{\n}\n"
        "void sub_00011010(void) { esp += 4; return; }\n"
        "void sub_00011020(void);\n"
        "#if 0\nvoid sub_00011030(void) {}\n#endif\n"
        "static void sub_00011040(void) {}\n",
    )
    assert hooks.manual_defined(p) == {0x11000, 0x11010}
    # As in the toolkit's scan, an #if 0 the file never closes hides the rest.
    write(p, "void sub_00011000(void) {}\n#if 0\nvoid sub_00011010(void) {}\n")
    assert hooks.manual_defined(p) == {0x11000}
    write(p, "extern void sub_00011000_gen(void);\n#if 0\nextern void sub_00011010_gen(void);\n")
    assert hooks.manual_wrapped(p) == {0x11000}
    assert hooks.manual_defined(os.path.join(d, "none.c")) == set()
    assert hooks.manual_defined("") == set()


# ── the disassembly ──────────────────────────────────────────────────────

# Synthetic functions: (name, [(mnemonic, operands)]), laid out 0x10 bytes
# apart from 0x11000, each instruction 1 byte. A jump target is written as
# "@<index>" and becomes that instruction's address in the same function;
# "&name" is another function's start.
BASE = 0x11000


def listing(d, funcs, callers=None):
    """Write <d>/disasm/{functions.json, asm/text.asm} in disasm's format;
    {name: va}."""
    at, vas = BASE, {}
    for name, insns in funcs:
        vas[name] = at
        at += (len(insns) + 0x10) & ~0xF
    lines, table = ["; Section: .text", ""], []
    for name, insns in funcs:
        va = vas[name]
        end = va + len(insns)
        table.append(
            {
                "start": "0x%08X" % va,
                "end": "0x%08X" % end,
                "name": name,
                "section": ".text",
                "called_by": ["0x%08X" % vas[c] for c in (callers or {}).get(name, ())],
            }
        )
        lines += [
            "; " + "=" * 60,
            "; Function: " + name,
            "; Start: 0x%08X  End: 0x%08X  Size: %d bytes" % (va, end, end - va),
            "; " + "=" * 60,
            name + ":",
        ]
        for i, (mn, ops) in enumerate(insns):
            if ops.startswith("@"):
                ops = hex(va + int(ops[1:]))
            elif ops.startswith("&"):
                ops = hex(vas[ops[1:]])
            lines.append(
                "  0x%08X  %-22s  %-8s %-30s %s" % (va + i, "90", mn, ops, "; -> x" if ops else "")
            )
        lines += ["; end of function", "  0x%08X  cc                      int3" % end, ""]
    write(os.path.join(d, "disasm", "functions.json"), json.dumps(table))
    write(os.path.join(d, "disasm", "asm", "text.asm"), "\n".join(lines) + "\n")
    return vas


FUNCS = [
    ("cdecl", [("push", "ebp"), ("mov", "eax, 1"), ("pop", "ebp"), ("ret", "")]),
    ("stdcall", [("test", "eax, eax"), ("je", "@3"), ("ret", "0xc"), ("ret", "0xc")]),
    ("flt", [("fld", "dword ptr [ecx]"), ("fmul", "st(0)"), ("ret", "")]),
    # Stores what flt returned: no float of its own.
    ("calls_flt", [("call", "&flt"), ("fstp", "dword ptr [eax]"), ("ret", "4")]),
    # Returns what flt returns, through a tail jump.
    ("tail_flt", [("mov", "ecx, eax"), ("jmp", "&flt")]),
    # A rep prefix and a loop; returns st0 on both paths.
    (
        "loop_flt",
        [
            ("rep movsd", "dword ptr es:[edi], dword ptr [esi]"),
            ("fldz", ""),
            ("dec", "ecx"),
            ("jne", "@2"),
            ("ret", ""),
        ],
    ),
    # An x87 helper (MSVC's _CIfmod): operands on the x87 stack, the work
    # behind a jump table the walk cannot follow.
    ("helper", [("fxch", "st(1)"), ("jmp", "dword ptr [eax*4 + 0x1000]")]),
    (
        "uses_helper",
        [
            ("fld", "dword ptr [eax]"),
            ("fld", "dword ptr [ecx]"),
            ("call", "&helper"),
            ("fstp", "dword ptr [edx]"),
            ("ret", ""),
        ],
    ),
    ("mixed_pop", [("je", "@2"), ("ret", "4"), ("ret", "8")]),
    ("mixed_fp", [("fldz", ""), ("je", "@3"), ("ret", ""), ("fstp", "st(0)"), ("ret", "")]),
    ("no_ret", [("push", "0"), ("call", "&cdecl"), ("int3", "")]),
    # Returns what a pointer call returns: the walk cannot tell, but its
    # caller reads st0 after the call, which proves a float.
    ("hidden_flt", [("call", "dword ptr [eax]"), ("ret", "")]),
    (
        "reads_hidden",
        [("call", "&hidden_flt"), ("fld", "dword ptr [eax]"), ("fucompp", ""), ("ret", "")],
    ),
    # A sure walk that finds no float, and a caller that reads one.
    ("plain", [("mov", "eax, 1"), ("ret", "")]),
    ("reads_plain", [("call", "&plain"), ("fld", "dword ptr [eax]"), ("fucompp", ""), ("ret", "")]),
]
CALLERS = {
    "hidden_flt": ["reads_hidden"],
    "flt": ["calls_flt"],
    "cdecl": ["no_ret"],
    "plain": ["reads_plain"],
}


def test_listing_and_shape(d):
    vas = listing(d, FUNCS, CALLERS)
    lst = hooks_x87.Listing(os.path.join(d, "disasm"))
    assert lst.insns(vas["loop_flt"])[0] == (
        vas["loop_flt"],
        "movsd",
        "dword ptr es:[edi], dword ptr [esi]",
    )
    assert lst.insns(vas["cdecl"] + 1) is None
    shape = hooks_x87.Shape(lst)

    def got(name):
        w = shape.of(vas[name])
        return set(w.deltas), set(w.pops), w.unsure

    assert got("cdecl") == ({0}, {0}, False)
    assert got("stdcall") == ({0}, {12}, False)
    assert got("flt") == ({1}, {0}, False)
    assert got("calls_flt") == ({0}, {4}, False)
    assert got("tail_flt") == ({1}, {0}, False)
    assert got("loop_flt") == ({1}, {0}, False)
    assert got("uses_helper") == ({0}, {0}, False)
    assert got("mixed_pop") == ({0}, {4, 8}, False)
    assert got("mixed_fp") == ({0, 1}, {0}, False)
    assert got("no_ret") == (set(), set(), False)
    assert got("hidden_flt") == ({0}, {0}, True)
    assert shape.callers_read_st0(vas["hidden_flt"])
    # calls_flt stores what flt left, which agrees with flt's walk.
    assert shape.callers_read_st0(vas["flt"])
    assert not shape.callers_read_st0(vas["cdecl"])


def decide(d, funcs, callers=None, name=None):
    """x87_return for one function of a small listing."""
    vas = listing(d, funcs, callers)
    shape = hooks_x87.Shape(hooks_x87.Listing(os.path.join(d, "disasm")))
    va = vas[name or funcs[0][0]]
    return hooks_x87.x87_return(shape, va, shape.of(va))


UNSURE = "a path returns through a call or jump the walk cannot follow"


@pytest.mark.parametrize(
    "funcs, callers",
    [
        # (a) a virtual call in return position: it may return a float.
        ([("virt", [("call", "dword ptr [ecx+8]"), ("ret", "")])], {}),
        # (b) a callee whose returns disagree.
        (
            [
                ("outer", [("call", "&amb"), ("ret", "")]),
                ("amb", [("fldz", ""), ("je", "@3"), ("ret", ""), ("fstp", "st(0)"), ("ret", "")]),
            ],
            {},
        ),
        # (c) a callee the disassembly does not list (_ftol would pop st0).
        ([("toint", [("fld", "dword ptr [eax]"), ("call", "0x00099990"), ("ret", "")])], {}),
        # (e) a conditional tail call, a tail call through a pointer, an
        # import thunk's jump.
        (
            [
                ("cond", [("test", "eax, eax"), ("je", "&plain"), ("ret", "")]),
                ("plain", [("mov", "eax, 1"), ("ret", "")]),
            ],
            {},
        ),
        ([("tail_ptr", [("mov", "eax, ecx"), ("jmp", "eax")])], {}),
        ([("thunk", [("jmp", "dword ptr [0x00012000]")])], {}),
        # An x87 instruction that consumes a result the walk did not see
        # pushed: it accounts for nothing, and the float is still there.
        (
            [
                (
                    "hid_add",
                    [("call", "dword ptr [ecx+8]"), ("fadd", "dword ptr [eax]"), ("ret", "")],
                )
            ],
            {},
        ),
        (
            [
                (
                    "hid_addp",
                    [
                        ("call", "dword ptr [ecx+8]"),
                        ("fld", "dword ptr [eax]"),
                        ("faddp", "st(1)"),
                        ("ret", ""),
                    ],
                )
            ],
            {},
        ),
        # A callee called with a value on the x87 stack, as MSVC's helpers
        # are, but one the walk cannot follow: it may consume the value.
        (
            [
                ("to_helper", [("fld", "dword ptr [eax]"), ("call", "&eats"), ("ret", "")]),
                ("eats", [("fstp", "dword ptr [eax]"), ("jmp", "dword ptr [eax*4 + 0x1000]")]),
            ],
            {},
        ),
    ],
    ids=[
        "indirect-call",
        "ambiguous-callee",
        "unlisted-callee",
        "cond-tail",
        "jmp-reg",
        "thunk",
        "hidden-fadd",
        "hidden-faddp",
        "unwalkable-helper",
    ],
)
def test_unsure_walks_refuse(d, funcs, callers):
    x87, why = decide(d, funcs, callers)
    assert x87 is False and UNSURE in why and "[mods] st0 or no_st0" in why


def test_unsure_cleared_by_x87_and_proved_by_callers(d):
    # An x87 instruction after the pointer call accounts for the stack.
    assert decide(d, [("f", [("call", "dword ptr [eax]"), ("fldz", ""), ("ret", "")])]) == (
        True,
        "",
    )
    # A caller that compares st0 right after the call proves a float.
    funcs = [
        ("virt", [("call", "dword ptr [ecx+8]"), ("ret", "")]),
        ("reader", [("call", "&virt"), ("fld", "dword ptr [eax]"), ("fucompp", ""), ("ret", "")]),
    ]
    assert decide(d, funcs, {"virt": ["reader"]}) == (True, "")


def test_caller_proof_needs_a_plausible_walk(d):
    # (d) two values left: a caller that reads st0 proves nothing about it.
    funcs = [
        ("two", [("fld", "dword ptr [eax]"), ("fld", "dword ptr [ecx]"), ("ret", "")]),
        ("caller", [("call", "&two"), ("fstp", "dword ptr [eax]"), ("ret", "")]),
    ]
    x87, why = decide(d, funcs, {"two": ["caller"]})
    assert x87 is False and "x87 depth at its rets: +2" in why


def test_recursion_is_unsure_and_order_free(d):
    """A cycle the walk cuts is not memoised, so each function's answer is
    the same whichever is asked first."""
    funcs = [
        ("a", [("test", "eax, eax"), ("je", "@3"), ("call", "&b"), ("ret", "")]),
        ("b", [("fldz", ""), ("fstp", "dword ptr [eax]"), ("call", "&a"), ("ret", "")]),
    ]
    vas = listing(d, funcs)
    lst = hooks_x87.Listing(os.path.join(d, "disasm"))
    ab, ba = hooks_x87.Shape(lst), hooks_x87.Shape(lst)
    first = {n: ab.of(vas[n]) for n in ("a", "b")}
    second = {n: ba.of(vas[n]) for n in ("b", "a")}
    assert first == second
    assert first["a"].unsure and first["b"].unsure


def hooked(d, entries, manual="", signatures=None, xbe_data=None, st0=(), no_st0=(), more=()):
    vas = listing(os.path.join(d, "out"), FUNCS, CALLERS)
    if more:
        p = os.path.join(d, "out", "disasm", "functions.json")
        with open(p) as f:
            table = json.load(f)
        write(p, json.dumps(table + list(more)))

    class G:
        m = {
            "mods": {
                "hook_functions": entries,
                "signatures": signatures or {},
                "st0": list(st0),
                "no_st0": list(no_st0),
            }
        }
        out = os.path.join(d, "out")
        recomp_manual = os.path.join(d, "manual.c")
        xbe = os.path.join(d, "default.xbe")

        @staticmethod
        def rel(p):
            return os.path.relpath(p, d)

    write(G.recomp_manual, manual)
    return vas, hooks.resolve(G, xbe_data)


def slot(va, ret_pop, x87, entry=None, sig="", source="walk"):
    return {
        "entry": entry or va,
        "va": va,
        "name": "sub_" + va[2:],
        "signature": sig,
        "ret_pop": ret_pop,
        "x87": x87,
        "x87_from": source,
    }


def test_resolve(d):
    _, slots = hooked(d, ["0x00011020", "0x11010", "0x00011000"])
    assert slots == [
        slot("0x00011020", 0, True),
        slot("0x00011010", 12, False, entry="0x11010"),
        slot("0x00011000", 0, False),
    ]


def test_resolve_overrides(d):
    vas = {n: "0x%08X" % v for n, v in listing(os.path.join(d, "x"), FUNCS, CALLERS).items()}
    mixed, flt = vas["mixed_fp"], vas["flt"]
    _, slots = hooked(d, [mixed, flt], st0=[mixed], no_st0=[flt])
    assert slots == [
        slot(mixed, 0, True, source="override"),
        # An override wins even where the walk is sure.
        slot(flt, 0, False, source="override"),
    ]


def test_resolve_signature_entry(d):
    code = bytes(range(0x40))
    data = b"\0" * 8 + code
    sec = xbe.Section(".text", 6, BASE, len(code), 8, len(code))

    def secs(_data):
        return [sec]

    orig = xbe.sections
    xbe.sections = secs
    try:
        sig = {"flt": {"pattern": "20 21 ?? 23", "section": ".text"}}
        _, slots = hooked(d, ["sig:flt"], signatures=sig, xbe_data=data)
        assert slots == [slot("0x00011020", 0, True, entry="sig:flt", sig="flt")]
        # A signature and a VA naming one function.
        with pytest.raises(
            CliError, match=r"sig:flt \(0x00011020\): the same function as 0x00011020"
        ):
            hooked(d, ["0x00011020", "sig:flt"], signatures=sig, xbe_data=data)
    finally:
        xbe.sections = orig


def test_resolve_errors(d):
    vas = {n: "0x%08X" % v for n, v in listing(os.path.join(d, "x"), FUNCS, CALLERS).items()}
    inner = {"start": "0x00011002", "end": "0x00011004", "name": "x", "section": ".text"}
    with pytest.raises(CliError) as e:
        hooked(
            d,
            [vas["cdecl"], vas["stdcall"], vas["mixed_pop"], vas["mixed_fp"], vas["no_ret"],
             vas["plain"], "0x00011001", "0x00011002"],
            manual="void sub_%s(void)\n{\n}\nextern void sub_%s_gen(void);\n"
            % (vas["cdecl"][2:], vas["stdcall"][2:]),
            more=[inner],
        )  # fmt: skip
    msg = str(e.value)
    assert "manual.c defines sub_00011000 by hand" in msg
    assert "manual.c already wraps sub_00011010 (it calls sub_00011010_gen)" in msg
    assert "%s: its rets pop different byte counts (4, 8)" % vas["mixed_pop"] in msg
    assert (
        "%s: cannot tell whether it returns a float (x87 depth at its rets: +0, +1); "
        "say which with [mods] st0 or no_st0" % vas["mixed_fp"]
    ) in msg
    assert "%s: no ret found" % vas["no_ret"] in msg
    assert (
        "%s: its rets leave the x87 stack as they found it, but a caller reads st0" % vas["plain"]
    ) in msg
    assert "0x00011001: no function starts there" in msg
    assert "0x00011002: lies inside sub_00011000, which can run it without calling it" in msg


def toolkit_manual_scan():
    """The toolkit's real manual_scan module, when a checkout is beside this
    repo or named by XBOXRECOMP_DIR; None otherwise."""
    here = os.path.dirname(os.path.abspath(__file__))
    for tk in (os.environ.get("XBOXRECOMP_DIR"), os.path.join(here, "..", "..", "xboxrecomp")):
        path = os.path.join(tk or "", "tools", "recomp", "manual_scan.py")
        if tk and os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("tk_manual_scan", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def test_scan_text_through_the_toolkit(d):
    ms = toolkit_manual_scan()
    if ms is None or not hasattr(ms, "scan"):
        pytest.skip("no toolkit checkout with tools/recomp/manual_scan.py beside this repo")
    p = os.path.join(d, "scan.c")
    slots = [slot("0x00011020", 0, True), slot("0x00011010", 12, False)]
    write(p, hooks.scan_text("void sub_00099000(void) { }\n", slots))
    skip, wrap, _ = ms.scan(p)[:3]
    names = lambda xs: {x if isinstance(x, str) else "sub_%08X" % x for x in xs}  # noqa: E731
    # recomp emits the wrapped bodies as sub_X_gen and declares only what
    # is defined by hand and not wrapped (skip - wrap): the slots are wraps.
    assert {"sub_00011020", "sub_00011010"} <= names(wrap)
    assert names(set(skip) - set(wrap)) == {"sub_00099000"}


def test_untracked(d):
    def git(*args):
        subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)

    p = os.path.join(d, "config", "hook_functions.json")
    write(p, "{}")
    assert not hooks.untracked(d, p)  # not a checkout: nothing to say
    git("init", "-q")
    assert hooks.untracked(d, p)
    git("add", p)
    assert not hooks.untracked(d, p)


# ── the pipeline ─────────────────────────────────────────────────────────


def use_mods(root, text):
    """Append text to the tree's game.toml and make it the current game."""
    with open(os.path.join(root, "game.toml"), "a") as f:
        f.write("\n" + text + "\n")
    G = manifest.load(root)
    manifest.use(G)
    return G


def edit_toml(root, old, new):
    p = os.path.join(root, "game.toml")
    with open(p) as f:
        text = f.read()
    assert old in text
    with open(p, "w") as f:
        f.write(text.replace(old, new))
    return use_mods(root, "")


def fake_recomp(cmds):
    """recomp's output as a function of its argv alone: gen/ is the same
    exactly when the command is."""
    for cmd in cmds:
        args = cmd[2]
        gen = args[args.index("--gen-dir") + 1]
        argv = pipeline._placeholders(json.dumps([a for a in args if a != "-v"]))
        write(os.path.join(gen, "recomp_0000.c"), "/* %s */\n" % argv)
        write(os.path.join(gen, "recomp_funcs.h"), "")


def gen_tree(G):
    out = {}
    for name in sorted(os.listdir(G.gen)):
        with open(os.path.join(G.gen, name), "rb") as f:
            out[name] = f.read()
    return out


def recomp(G, monkeypatch):
    write(os.path.join(G.out, "abi", "abi_functions.json"), "{}")
    monkeypatch.setattr(pipeline, "run_cmds", fake_recomp)
    try:
        pipeline.stage_recomp()
    finally:
        monkeypatch.undo()
        if os.path.exists(G.regen_marker):
            os.remove(G.regen_marker)
    with open(G.gen_key) as f:
        return gen_tree(G), f.read()


def runtime_header(tk):
    write(os.path.join(tk, *hooks.RUNTIME_HEADER_PATH), "/* */")


def test_empty_list_is_no_mods(d, monkeypatch):
    """[mods] with an empty list: the same stages, argv, key and gen/ bytes
    as no [mods] at all."""
    with fake_tree(d) as (root, _):
        G = host.g()
        stages = [n for n, _ in pipeline.analyze_stages()]
        argv, inputs = pipeline.stage_argv(), pipeline.gen_inputs()
        before = recomp(G, monkeypatch)
        G = use_mods(
            root,
            '[mods]\nhook_functions = []\nst0 = []\n[mods.signatures.x]\npattern = "55"\nsection = ".text"',
        )
        assert [n for n, _ in pipeline.analyze_stages()] == stages and "hooks" not in stages
        assert pipeline.stage_argv() == argv and pipeline.gen_inputs() == inputs
        # A slot file a list left behind goes once the list is empty.
        write(os.path.join(G.gen, hooks.SLOTS_C), "stale")
        assert recomp(G, monkeypatch) == before
        assert pipeline.gen_stale_reasons() == []


def test_slots_through_the_pipeline(d, monkeypatch):
    with fake_tree(d) as (root, tk):
        G = host.g()
        argv = pipeline.stage_argv()
        with open(G.recomp_manual, "w") as f:
            f.write("void sub_00099000(void) { }\n")
        G = use_mods(root, '[mods]\nhook_functions = ["0x00011020", "0x00011010"]')
        assert [n for n, _ in pipeline.analyze_stages()][-1] == "hooks"
        assert pipeline.stage_argv() != argv
        # recomp before analyze resolved the list.
        with pytest.raises(CliError, match="no config/hook_functions.json: run analyze"):
            recomp(G, monkeypatch)
        listing(G.out, FUNCS, CALLERS)
        with pytest.raises(CliError, match="no extra arguments"):
            pipeline.stage_hooks(["--x"])
        pipeline.stage_hooks()
        with open(G.hook_functions) as f:
            doc = json.load(f)
        assert doc["version"] == 1 and [s["ret_pop"] for s in doc["functions"]] == [0, 12]
        assert doc["mods"] == hooks.mods_key(G.m)
        inputs = pipeline.gen_inputs()
        assert pipeline._placeholders(G.hook_functions) in inputs
        assert inputs["game.toml [mods]"] == hooks.mods_digest(G.m)

        # A toolkit without the hook runtime.
        with pytest.raises(CliError, match="has no src/kernel/recomp_hook.h"):
            recomp(G, monkeypatch)
        runtime_header(tk)
        gen, _ = recomp(G, monkeypatch)
        r = json.loads(pipeline.stage_argv())[-1][2]
        scan = r[r.index("--exclude-manual") + 1]
        assert scan == "$ROOT/analysis/hooks/exclude_manual.c"
        with open(hooks.scan_path(G)) as f:
            text = f.read()
        assert text.startswith("void sub_00099000(void) { }\n")
        assert "extern void sub_00011020_gen(void);\nvoid sub_00011020(void) {}\n" in text
        assert hooks.manual_wrapped(hooks.scan_path(G)) == {0x11020, 0x11010}

        slots = gen[hooks.SLOTS_C].decode()
        assert '#include "recomp_hook.h"\n' in slots and "recomp_types.h" not in slots
        assert "static struct recomp_hook_slot s_hook_slots_gen[] = {\n" in slots
        assert "    { 0x00011020u, 0u, RECOMP_HOOK_SLOT_ST0, 0, NULL }, /* 0x00011020 */\n" in slots
        assert "    { 0x00011010u, 12u, 0, 0, NULL }, /* 0x00011010 */\n" in slots
        assert "RECOMP_HOOK_SLOTS(s_hook_slots_gen, 2)\n" in slots
        assert (
            "extern void sub_00011010_gen(void);\nvoid sub_00011010(void)\n{\n"
            "    RECOMP_HOOK_CALL(&s_hook_slots_gen[1], sub_00011010_gen);\n}\n"
        ) in slots
        assert pipeline.gen_stale_reasons() == []


def test_mods_edits_stale_gen_before_analyze(d, monkeypatch):
    """An edit to the list, a signature it uses or an override stales gen/
    at once, though analyze has not rewritten hook_functions.json yet; an
    unused signature row does not."""
    with fake_tree(d) as (root, tk):
        runtime_header(tk)
        G = use_mods(
            root,
            '[mods]\nhook_functions = ["0x00011020"]\n'
            '[mods.signatures.unused]\npattern = "55"\nsection = ".text"',
        )
        listing(G.out, FUNCS, CALLERS)
        pipeline.stage_hooks()
        recomp(G, monkeypatch)
        assert pipeline.gen_stale_reasons() == []
        edit_toml(root, 'pattern = "55"', 'pattern = "56"')
        assert pipeline.gen_stale_reasons() == []
        G = edit_toml(root, '["0x00011020"]', '["0x00011020", "0x00011010"]')
        assert pipeline.gen_stale_reasons() == ["game.toml [mods] changed"]
        with pytest.raises(CliError, match=r"does not match game.toml's \[mods\]: run analyze"):
            hooks.load_json(G)
        G = edit_toml(root, '"0x00011010"]', '"0x00011010"]\nno_st0 = ["0x00011020"]')
        with pytest.raises(CliError, match="run analyze"):
            hooks.load_json(G)
        pipeline.stage_hooks()
        recomp(G, monkeypatch)
        assert pipeline.gen_stale_reasons() == []
        with open(G.hook_functions) as f:
            assert json.load(f)["functions"][0]["x87_from"] == "override"
