"""Mod hook slots (mod-connectivity-layer D3).

game.toml's [mods] hook_functions names the functions a mod may hook, by VA
or by a [mods.signatures] row. analyze resolves them against the pinned XBE
and the disassembly into config/hook_functions.json:
- a signature must match exactly once in its section;
- the VA must be a function start, and the game's manual file must not
  define it by hand (one function, one owner);
- ret_pop, the bytes the function's `ret imm16` pops past the return address
  (0 for cdecl), so a skipped body leaves the stack as the body would;
- x87, whether it returns on the x87 stack, so a skip can push the result
  (hooks_x87.py; [mods] st0 / no_st0 settles what it cannot).

recomp then wraps each listed function: the toolkit's recomp emits the body
as sub_X_gen when the manual file it scans declares that name (the
`--exclude-manual` wrap), and recomp_hook_slots.c in gen/ defines sub_X as
the slot, which runs the pre callbacks, the body or a skip, then the post
callbacks. The scan file is the manual file plus the slots' declarations, so
the game's own file is never touched. With an empty list nothing here runs:
the argv, the gen key and gen/ are those of a game without [mods].
"""

import hashlib
import json
import os
import re
import subprocess

from .hooks_x87 import Listing, Shape, x87_return
from .host import CliError

JSON_VERSION = 1
SLOTS_C = "recomp_hook_slots.c"
# The runtime's slot interface (toolkit).
RUNTIME_HEADER = "recomp_hook.h"
# Where the toolkit keeps it; a toolkit without it has no hook runtime.
RUNTIME_HEADER_PATH = ("src", "kernel", RUNTIME_HEADER)
TABLE = "s_hook_slots_gen"


# ── entries and signatures ───────────────────────────────────────────────


def pattern_regex(pattern):
    """A signature's bytes ("55 8B EC ?? ?? 83") as a bytes regex; ValueError
    names the bad token. Each token is two hex digits or ?? (any byte)."""
    toks = pattern.split()
    if not toks:
        raise ValueError("no bytes")
    out = []
    for t in toks:
        if t in ("??", "?"):
            out.append(b".")
        elif re.fullmatch(r"[0-9A-Fa-f]{2}", t):
            out.append(re.escape(bytes([int(t, 16)])))
        else:
            raise ValueError("%r is not a byte (two hex digits) or ??" % t)
    if all(x == b"." for x in out):
        raise ValueError("only wildcards")
    # A lookahead, so overlapping matches are counted: two of them still
    # mean the signature is not specific enough.
    return re.compile(b"(?=(" + b"".join(out) + b"))", re.DOTALL)


def resolve_signature(data, secs, name, row):
    """The one VA where [mods.signatures] row `name` matches in its section
    of the XBE (data: the whole file; secs: xbe.sections)."""
    sec = [s for s in secs if s.name == row["section"]]
    if not sec:
        raise CliError(
            "mods.signatures.%s: the XBE has no section %r (it has %s)"
            % (name, row["section"], ", ".join(s.name for s in secs))
        )
    s = sec[0]
    code = data[s.raw_off : s.raw_off + s.raw_size]
    hits = [m.start() for m in pattern_regex(row["pattern"]).finditer(code)]
    if len(hits) != 1:
        where = ", ".join("0x%08X" % (s.va + h) for h in hits[:8])
        raise CliError(
            "mods.signatures.%s: %d matches in %s%s; a signature must match exactly once"
            % (name, len(hits), s.name, (" (" + where + ")") if hits else "")
        )
    return s.va + hits[0]


# ── the manual file ──────────────────────────────────────────────────────

# Kept in step with the toolkit's tools/recomp/manual_scan.py, which decides
# what recomp leaves out: a definition is `void sub_XXXXXXXX(void)` followed
# by a body, outside `#if 0` blocks.
_SUB_DEF_RE = re.compile(r"^void sub_([0-9A-Fa-f]{8})\(void\)\s*(?:\{|$)", re.M)
# A wrap: the manual file calls the generated body as sub_X_gen.
_WRAP_RE = re.compile(r"^extern void sub_([0-9A-Fa-f]{8})_gen\(void\);", re.M)


def _strip_disabled(src):
    out, depth = [], 0
    for line in src.splitlines(keepends=True):
        s = line.strip()
        if re.match(r"^#if\s+0\b", s):
            depth += 1
        elif depth and re.match(r"^#if(def|ndef)?\b", s):
            depth += 1
        elif depth and s.startswith("#endif"):
            depth -= 1
        elif depth == 0:
            out.append(line)
    return "".join(out)


def _live(path):
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return _strip_disabled(f.read())


def manual_defined(path):
    """VAs the manual file defines by hand (overrides and _gen wrappers)."""
    return {int(m.group(1), 16) for m in _SUB_DEF_RE.finditer(_live(path))}


def manual_wrapped(path):
    """VAs whose generated body the manual file calls as sub_X_gen."""
    return {int(m.group(1), 16) for m in _WRAP_RE.finditer(_live(path))}


def entries(m):
    return list(m["mods"]["hook_functions"])


def mods_key(m):
    """What of [mods] the slots depend on, when the list is not empty: the
    list, the signature rows it names and the st0 overrides. analyze
    records it in hook_functions.json, and the generation key hashes it, so
    an edit stales both until analyze runs again."""
    mods = m["mods"]
    sigs = {e[4:] for e in mods["hook_functions"] if e.startswith("sig:")}
    return {
        "hook_functions": list(mods["hook_functions"]),
        "signatures": {k: dict(v) for k, v in sorted(mods["signatures"].items()) if k in sigs},
        "st0": list(mods["st0"]),
        "no_st0": list(mods["no_st0"]),
    }


def mods_digest(m):
    return hashlib.sha256(json.dumps(mods_key(m), sort_keys=True).encode()).hexdigest()


def resolve(G, xbe_data=None):
    """[{entry, va, name, signature, ret_pop, x87, x87_from}] for the
    manifest's hook_functions, in its order (the slot index); CliError on
    any entry that cannot be a slot. x87_from is "walk", or "override" when
    [mods] st0 / no_st0 names the entry."""
    from . import xbe as xbe_mod

    mods = G.m["mods"]
    listing = Listing(os.path.join(G.out, "disasm"))
    shape = Shape(listing)
    manual = manual_defined(G.recomp_manual)
    wrapped = manual_wrapped(G.recomp_manual)
    override = {e: True for e in mods["st0"]}
    override.update({e: False for e in mods["no_st0"]})
    data = secs = None
    out, errors, seen = [], [], {}
    for e in entries(G.m):
        sig = ""
        if e.startswith("sig:"):
            sig = e[4:]
            row = mods["signatures"].get(sig)
            if row is None:
                errors.append("%s: no [mods.signatures] row named %r" % (e, sig))
                continue
            if data is None:
                if xbe_data is None:
                    with open(G.xbe, "rb") as f:
                        xbe_data = f.read()
                data, secs = xbe_data, xbe_mod.sections(xbe_data)
            try:
                va = resolve_signature(data, secs, sig, row)
            except CliError as err:
                errors.append(str(err))
                continue
        else:
            va = int(e, 16)
        what = "%s (0x%08X)" % (e, va) if sig else e
        if va in seen:
            errors.append("%s: the same function as %s" % (what, seen[va]))
            continue
        seen[va] = e
        if va in manual:
            errors.append(
                "%s: %s defines sub_%08X by hand, and a function has one owner: "
                "drop it from hook_functions or from that file" % (what, G.rel(G.recomp_manual), va)
            )
            continue
        if va in wrapped:
            errors.append(
                "%s: %s already wraps sub_%08X (it calls sub_%08X_gen), and a function "
                "has one owner: drop it from hook_functions or from that file"
                % (what, G.rel(G.recomp_manual), va, va)
            )
            continue
        if va not in listing.funcs:
            errors.append(
                "%s: no function starts there in the disassembly (seed it in a "
                "pipeline.seeds file, then analyze)" % what
            )
            continue
        outer = listing.containing(va)
        if outer:
            # The outer function reaches this code by falling or jumping
            # into it, not by a call, so those runs would miss the slot.
            errors.append(
                "%s: lies inside %s, which can run it without calling it, so a hook "
                "would miss those runs" % (what, ", ".join("sub_%08X" % o for o in outer))
            )
            continue
        w = shape.of(va)
        if len(w.pops) != 1:
            errors.append(
                "%s: %s"
                % (
                    what,
                    "no ret found"
                    if not w.pops
                    else "its rets pop different byte counts (%s)"
                    % ", ".join(str(p) for p in sorted(w.pops)),
                )
            )
            continue
        if e in override:
            x87, why, source = override[e], "", "override"
        else:
            (x87, why), source = x87_return(shape, va, w), "walk"
        if why:
            errors.append("%s: %s" % (what, why))
            continue
        out.append(
            {
                "entry": e,
                "va": "0x%08X" % va,
                "name": "sub_%08X" % va,
                "signature": sig,
                "ret_pop": next(iter(w.pops)),
                "x87": x87,
                "x87_from": source,
            }
        )
    if errors:
        raise CliError("[mods] hook_functions: " + "; ".join(errors))
    return out


def write_json(path, slots, m):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="\n") as f:
        doc = {"version": JSON_VERSION, "mods": mods_key(m), "functions": slots}
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def untracked(root, path):
    """True when root is a git checkout that does not track path."""

    def git(*args):
        return subprocess.run(
            ["git", "-C", root, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode

    if git("rev-parse", "--is-inside-work-tree"):
        return False
    return git("ls-files", "--error-unmatch", "--", path) != 0


def load_json(G):
    """The slots analyze wrote, checked against the manifest's [mods]:
    CliError when analyze has not run since the list, a signature it uses
    or an override changed."""
    path = G.hook_functions
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise CliError("no %s: run analyze (game.toml lists hook_functions)" % G.rel(path)) from e
    if doc.get("version") != JSON_VERSION:
        raise CliError(
            "%s: format %r, not %d; run analyze" % (G.rel(path), doc.get("version"), JSON_VERSION)
        )
    slots = doc.get("functions") or []
    if doc.get("mods") != mods_key(G.m) or [s.get("entry") for s in slots] != entries(G.m):
        raise CliError("%s does not match game.toml's [mods]: run analyze" % G.rel(path))
    return slots


# ── recomp ───────────────────────────────────────────────────────────────


def scan_path(G):
    """The file recomp's --exclude-manual reads when there are slots."""
    return os.path.join(G.out, "hooks", "exclude_manual.c")


def scan_text(manual_text, slots):
    """The manual file, then each slot's `extern void sub_X_gen(void);` and
    `void sub_X(void) {}`: the wrap recomp's scan recognises."""
    lines = [
        "",
        "/* xbr: the [mods] hook slots (gen/%s), for recomp's scan only. */" % SLOTS_C,
    ]
    for s in slots:
        lines += ["extern void %s_gen(void);" % s["name"], "void %s(void) {}" % s["name"]]
    return manual_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def write_scan(G, slots):
    text = ""
    if G.recomp_manual:
        try:
            with open(G.recomp_manual, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
    path = scan_path(G)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(scan_text(text, slots))


def slots_c(slots):
    """gen/recomp_hook_slots.c: each listed sub_X as a slot around its
    generated body (sub_X_gen), per the runtime's recomp_hook.h: the slot
    table registers itself before main, and the dispatch costs one load and
    a predicted branch while no mod hooks the function."""
    out = [
        "/* Generated by xbr recomp from config/hook_functions.json: the mod hook",
        " * slots ([mods] hook_functions; mod-connectivity-layer D3). Do not edit. */",
        "",
        '#include "%s"' % RUNTIME_HEADER,
        "",
        # One table, in this one file. On MSVC the registration's pointer is
        # a global named after the table, so a second slot file would need a
        # name of its own.
        "static struct recomp_hook_slot %s[] = {" % TABLE,
    ]
    for s in slots:
        flags = "RECOMP_HOOK_SLOT_ST0" if s["x87"] else "0"
        out.append(
            "    { %su, %du, %s, 0, NULL }, /* %s */" % (s["va"], s["ret_pop"], flags, s["entry"])
        )
    out += ["};", "RECOMP_HOOK_SLOTS(%s, %d)" % (TABLE, len(slots)), ""]
    for i, s in enumerate(slots):
        out += [
            "extern void %s_gen(void);" % s["name"],
            "void %s(void)" % s["name"],
            "{",
            "    RECOMP_HOOK_CALL(&%s[%d], %s_gen);" % (TABLE, i, s["name"]),
            "}",
            "",
        ]
    return "\n".join(out)
