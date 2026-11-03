"""A game's symbol map ([pipeline] symbols; docs/symbols.md): names it has
learnt for guest addresses, carried into gen/.

The names stage writes each function's name into functions.json, the field
recomp makes C identifiers from (the path Ghidra's names take), and after
recomp the notes, globals, fields and labels become comments. Hooks, seeds
and every runtime table stay keyed by address; a name recomp's
--exclude-manual scan pins back to sub_ (the manual file mentions it) keeps
its note in the doc block, which is found by address.
"""

import glob
import hashlib
import json
import os
import re
import textwrap
import tomllib

from .host import CliError

KINDS = ("function", "global", "field", "label")
# Bumped when the comments annotate_gen writes change, so gen.key.json
# stales a gen/ annotated the old way.
ANNOTATION_VERSION = 2

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Names the pipeline makes itself: a map name shaped like one would be
# mistaken for, or collide with, a generated function or label.
GENERATED = re.compile(r"^(sub|loc)_[0-9A-Fa-f]{8}(_|$)")
C_KEYWORDS = frozenset(
    """
auto break case char const continue default do double else enum extern float
for goto if inline int long register restrict return short signed sizeof static
struct switch typedef union unsigned void volatile while _Alignas _Alignof
_Atomic _Bool _Complex _Generic _Imaginary _Noreturn _Static_assert
_Thread_local bool true false nullptr alignas alignof static_assert
thread_local typeof
""".split()
)

# Names the lifter's own output already uses in every gen file: the x86
# registers it keeps as locals and globals, its temporaries (all with a
# leading underscore: _t, _i, _fa, _flags, _jt ...), the runtime's g_*
# globals and the recomp_* helpers recomp_types.h defines static inline.
# A map name equal to one would shadow it or fail the compile.
X86_REGISTERS = frozenset(
    """
eax ebx ecx edx esi edi ebp esp eip eflags ax bx cx dx si di bp sp ip
al ah bl bh cl ch dl dh cs ds es fs gs ss cr0 cr2 cr3 cr4 dr0 dr1 dr2 dr3
dr6 dr7 st st0 st1 st2 st3 st4 st5 st6 st7 mm0 mm1 mm2 mm3 mm4 mm5 mm6 mm7
xmm0 xmm1 xmm2 xmm3 xmm4 xmm5 xmm6 xmm7 mxcsr
""".split()
)
RESERVED_PREFIXES = ("_", "g_", "recomp_")


def reserved(name):
    """Why name cannot be a gen identifier, or None."""
    if name in C_KEYWORDS:
        return "a C keyword"
    if name.lower() in X86_REGISTERS:
        return "an x86 register name the lifter uses"
    if name.startswith(RESERVED_PREFIXES):
        return "under a prefix the lifter and runtime use (%s)" % ", ".join(RESERVED_PREFIXES)
    if GENERATED.match(name):
        return "shaped like a generated name (sub_, loc_)"
    return None


# The marker apply_names leaves on a renamed functions.json entry, so a
# rerun of names alone can put sub_ back first.
MARK = "symbol_name"


def load(path):
    """The map's entries, checked: [{addr, name, kind, note, ...}]. Keys the
    CLI does not use are kept (a game's own: source, conflict, ...)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise CliError("symbol map %s: %s" % (path, e.strerror or e)) from e
    except tomllib.TOMLDecodeError as e:
        raise CliError("symbol map %s: %s" % (path, e)) from e
    rows = data.get("symbol", [])
    if not isinstance(rows, list):
        raise CliError("symbol map %s: 'symbol' must be [[symbol]] tables" % path)
    errors, names, addrs = [], {}, {}
    for i, e in enumerate(rows):
        where = "[[symbol]] #%d" % (i + 1)
        name = e.get("name")
        if not isinstance(name, str) or not IDENT.match(name):
            errors.append("%s: name %r is not a C identifier" % (where, name))
            continue
        where = "%s (%s)" % (where, name)
        why = reserved(name)
        if why:
            errors.append("%s: %s is reserved: %s" % (where, name, why))
        if name in names:
            errors.append("%s: the name is also %s" % (where, names[name]))
        names[name] = where
        kind = e.get("kind")
        if kind not in KINDS:
            errors.append("%s: kind %r is none of %s" % (where, kind, ", ".join(KINDS)))
        addr = e.get("addr")
        if addr is None:
            # Only a field of a heap struct has no fixed address.
            if kind != "field":
                errors.append("%s: no addr" % where)
        elif not isinstance(addr, int) or isinstance(addr, bool) or not 0 <= addr < 1 << 32:
            errors.append("%s: addr %r is not a 32-bit VA" % (where, addr))
        elif kind in ("function", "label"):
            if addr in addrs:
                errors.append("%s: 0x%08X is also %s" % (where, addr, addrs[addr]))
            addrs[addr] = where
        note = e.get("note", "")
        if not isinstance(note, str):
            errors.append("%s: note is not a string" % where)
        if "in_gen" in e and not isinstance(e["in_gen"], bool):
            errors.append("%s: in_gen is not true or false" % where)
    if errors:
        raise CliError("symbol map %s:\n  %s" % (path, "\n  ".join(errors)))
    return rows


def functions(entries):
    """{va: entry} of the functions recomp may name (in_gen not false)."""
    return {
        e["addr"]: e
        for e in entries
        if e["kind"] == "function" and e.get("in_gen", True) is not False
    }


def _va(entry):
    s = entry.get("start")
    return int(s, 16) if isinstance(s, str) else int(s)


def apply_names(functions_json, entries):
    """Write the map's function names into functions.json (D1). Returns
    (renamed, kept, absent): VAs renamed, VAs left with a name disasm or
    Ghidra gave them, and VAs that are no function start.

    A rerun first resets every entry an earlier run renamed whose name it
    still holds, so a name dropped from the map leaves gen/ too."""
    with open(functions_json, encoding="utf-8") as f:
        data = json.load(f)
    for e in data:
        mark = e.pop(MARK, None)
        if mark is not None and e.get("name") == mark:
            e["name"] = "sub_%08X" % _va(e)
    want = functions(entries)
    taken = {e.get("name"): _va(e) for e in data}
    clash = sorted(
        "%s (0x%08X is already called that)" % (want[va]["name"], taken[want[va]["name"]])
        for va in want
        if taken.get(want[va]["name"], va) != va
    )
    if clash:
        raise CliError("symbol map names another function's name: " + ", ".join(clash))
    renamed, kept, seen = [], [], set()
    for e in data:
        va = _va(e)
        if va not in want:
            continue
        seen.add(va)
        if e.get("name") == "sub_%08X" % va:
            e["name"] = e[MARK] = want[va]["name"]
            renamed.append(va)
        elif e.get("name") != want[va]["name"]:
            kept.append(va)
    tmp = functions_json + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, functions_json)
    return renamed, kept, sorted(set(want) - seen)


# ── comments in gen/ (D3) ───────────────────────────────────────────────

_DOC_ORIGINAL = re.compile(r"^ \* Original: 0x([0-9A-F]{8}) - ", re.M)
_HEX = re.compile(r"\b0x([0-9A-Fa-f]+)")
_LABEL = re.compile(r"^loc_([0-9A-Fa-f]{8}):")
_COMMENT = re.compile(r"/\*.*?\*/")


def _note(text):
    """One comment-safe line: no comment terminator, whitespace folded."""
    return " ".join(text.replace("*/", "* /").split())


def doc_lines(entry):
    """The doc block lines for one mapped function."""
    head = "%s (sub_%08X)" % (entry["name"], entry["addr"])
    note = _note(entry.get("note", ""))
    if not note:
        return [" * " + head]
    return [
        " * " + line
        for line in textwrap.wrap(
            "%s: %s" % (head, note),
            width=76,
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    ]


def comment_tables(entries):
    """({va: doc lines}, {va: [names]} of globals and fixed fields,
    {va: name} of labels)."""
    docs = {e["addr"]: doc_lines(e) for e in entries if e["kind"] == "function"}
    data = {}
    for e in entries:
        if e["kind"] in ("global", "field") and e.get("addr") is not None:
            data.setdefault(e["addr"], []).append(e["name"])
    labels = {e["addr"]: e["name"] for e in entries if e["kind"] == "label"}
    return docs, data, labels


def _code_line(line):
    """A line a trailing comment can go on: code, no string, nothing open."""
    s = line.lstrip()
    if not s or s.startswith(("*", "/*", "//", "#")) or '"' in line or line.endswith("\\"):
        return False
    return line.rfind("/*") <= line.rfind("*/")


def _trailing(line, names):
    """line with names in a trailing comment, before any CR it ends with
    (recomp writes CRLF on a Windows host)."""
    body, cr = (line[:-1], "\r") if line.endswith("\r") else (line, "")
    if not _code_line(body):
        return line
    return "%s /* %s */%s" % (body, ", ".join(names), cr)


def annotate_text(text, tables):
    docs, data, labels = tables
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        # recomp's doc block: "/**", " * <name>", " * Original: 0x<VA> - ...".
        if line.startswith("/**") and i + 1 < len(lines):
            m = _DOC_ORIGINAL.match(lines[i + 1])
            if m and lines[i].startswith(" * ") and int(m.group(1), 16) in docs:
                cr = "\r" if lines[i].endswith("\r") else ""
                out.append(lines[i])
                out.extend(d + cr for d in docs[int(m.group(1), 16)])
                i += 1
            elif lines[i].startswith(" * "):
                # Already annotated: the name line, then this map's lines.
                j = i + 1
                while j < len(lines) and not lines[j].startswith((" * Original:", " */")):
                    j += 1
                m = _DOC_ORIGINAL.match(lines[j]) if j < len(lines) else None
                va = int(m.group(1), 16) if m else None
                cr = "\r" if lines[i].endswith("\r") else ""
                if va in docs and lines[i + 1 : j] == [d + cr for d in docs[va]]:
                    out.extend(lines[i:j])
                    i = j
            continue
        names = []
        if labels:
            m = _LABEL.match(line)
            if m and int(m.group(1), 16) in labels:
                names.append(labels[int(m.group(1), 16)])
        if data and "0x" in line:
            # recomp's own comments (the instruction it lifted) repeat the
            # literal; only the code gets a name.
            for h in _HEX.findall(_COMMENT.sub(" ", line)):
                names += [n for n in data.get(int(h, 16), ()) if n not in names]
        if names and not line.rstrip("\r").endswith(" /* %s */" % ", ".join(names)):
            out[-1] = _trailing(line, names)
    return "\n".join(out)


def snapshot(gen_dir):
    """{path: (sha256, mtime_ns)} of gen/'s C files, before recomp."""
    out = {}
    for p in glob.glob(os.path.join(gen_dir, "*.c")):
        with open(p, "rb") as f:
            out[p] = (hashlib.sha256(f.read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


def annotate_gen(gen_dir, entries, before):
    """Write the comments into gen/'s C files. A file whose bytes end up as
    they were before recomp gets its old mtime back: recomp rewrote it
    (it compares against its own, unannotated text), but nothing in it
    changed, so it must not rebuild. Returns the files changed."""
    tables = comment_tables(entries)
    changed = 0
    for p in sorted(glob.glob(os.path.join(gen_dir, "*.c"))):
        with open(p, encoding="utf-8", newline="") as f:
            text = f.read()
        new = annotate_text(text, tables)
        if new != text:
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(new)
        digest = hashlib.sha256(new.encode("utf-8")).hexdigest()
        old = before.get(p)
        if old and old[0] == digest:
            st = os.stat(p)
            if st.st_mtime_ns != old[1]:
                os.utime(p, ns=(st.st_atime_ns, old[1]))
        else:
            changed += 1
    return changed
