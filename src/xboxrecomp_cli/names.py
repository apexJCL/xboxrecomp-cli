"""Port names ([pipeline] names; docs/names.md): the addresses and
values a port's hand-written side uses, named once and resolved per
release of the game.

A port is written against one dump, the reference ([xbe] sha256): its
manual wraps, data addresses, seeds and the fence are that XBE's. Another
release (another region or version) has its own addresses, so
`config/names.toml` names each of them with the reference's address, and
one `config/releases/<id>.toml` per other release gives the same names its
addresses, as `match` placed them and a person checked them. The CLI picks
the map for the dump in game_files by its fingerprint, writes
gen/recomp_names.h (XBR_FN, XBR_GEN, XBR_VA, XBR_VAL, XBR_HAVE) and hands
recomp the same names (--names), so one source builds for every release.

Unlike the symbol map (symbols.py) nothing here renames or comments gen:
the code recomp generates for the reference is the same with or without
this file.
"""

import json
import math
import os
import re
import tomllib
from collections import namedtuple

from . import fingerprint, host, symbols, xbe
from .host import CliError

KINDS = ("function", "global", "site", "value")
VERDICTS = ("sure", "unsure", "reviewed")
HEADER = "recomp_names.h"
REFERENCE = "reference"

# One name as the selected release has it: addr (an int) for every kind but
# value, value (an int or float) for a value; both None when unplaced.
Placed = namedtuple("Placed", "name kind addr value verdict how note")
# The selected release: its id, the file it came from ("" for the
# reference) and {name: Placed}, in names.toml's order.
Selection = namedtuple("Selection", "id path names")


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _va(v):
    return _int(v) and 0 <= v < 1 << 32


def _value(v):
    if isinstance(v, float):
        return math.isfinite(v)
    return _int(v)


def ref_of(r):
    """A names.toml row's reference address, or its value for a value."""
    return r["value"] if r["kind"] == "value" else r["addr"]


def _read_toml(path, what):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise CliError("%s %s: %s" % (what, path, e.strerror or e)) from e
    except tomllib.TOMLDecodeError as e:
        raise CliError("%s %s: %s" % (what, path, e)) from e


def load(path):
    """names.toml's rows, checked: [{name, kind, addr | value, note}]."""
    data = _read_toml(path, "port names")
    rows = data.get("name", [])
    if not isinstance(rows, list):
        raise CliError("port names %s: 'name' must be [[name]] tables" % path)
    errors, seen = [], {}
    for i, e in enumerate(rows):
        where = "[[name]] #%d" % (i + 1)
        name = e.get("name")
        if not isinstance(name, str) or not symbols.IDENT.match(name):
            errors.append("%s: name %r is not a C identifier" % (where, name))
            continue
        where = "%s (%s)" % (where, name)
        why = symbols.reserved(name)
        if why:
            errors.append("%s: %s is reserved: %s" % (where, name, why))
        if name in seen:
            errors.append("%s: the name is also %s" % (where, seen[name]))
        seen[name] = where
        kind = e.get("kind")
        if kind not in KINDS:
            errors.append("%s: kind %r is none of %s" % (where, kind, ", ".join(KINDS)))
        elif kind == "value":
            if not _value(e.get("value")):
                errors.append("%s: a value needs value = an integer or a finite float" % where)
            if "addr" in e:
                errors.append("%s: a value has no addr" % where)
        elif not _va(e.get("addr")):
            errors.append("%s: addr %r is not a 32-bit VA" % (where, e.get("addr")))
        if not isinstance(e.get("note", ""), str):
            errors.append("%s: note is not a string" % where)
    if errors:
        raise CliError("port names %s:\n  %s" % (path, "\n  ".join(errors)))
    return rows


def load_release(path, rows):
    """A release file, checked against names.toml's rows: ({release
    table}, {name: row}). A name the file does not list is unplaced; one
    names.toml lacks is an error (a renamed name would silently vanish)."""
    data = _read_toml(path, "release map")
    rel = data.get("release")
    errors = []
    if not isinstance(rel, dict):
        raise CliError("release map %s: no [release] table" % path)
    if not isinstance(rel.get("id"), str) or not rel["id"]:
        errors.append("[release] id is not a string")
    sha = rel.get("sha256", [])
    if not isinstance(sha, list) or not all(
        isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s) for s in sha
    ):
        errors.append("[release] sha256 must be a list of lowercase sha256 strings")
    kinds = {r["name"]: r["kind"] for r in rows}
    out = {}
    for i, e in enumerate(data.get("name", [])):
        name = e.get("name")
        where = "[[name]] #%d (%s)" % (i + 1, name)
        if name not in kinds:
            errors.append("%s: not a name in names.toml" % where)
            continue
        if name in out:
            errors.append("%s: listed twice" % where)
        out[name] = e
        verdict = e.get("verdict")
        if verdict is not None and verdict not in VERDICTS:
            errors.append("%s: verdict %r is none of %s" % (where, verdict, ", ".join(VERDICTS)))
        if "ref" in e and not _value(e["ref"]):
            errors.append("%s: ref %r is not a number" % (where, e["ref"]))
        if kinds[name] == "value":
            if "addr" in e:
                errors.append("%s: a value has no addr" % where)
            if "value" in e and not _value(e["value"]):
                errors.append("%s: value %r is not a number" % (where, e["value"]))
        else:
            if "value" in e:
                errors.append("%s: a %s has no value" % (where, kinds[name]))
            if "addr" in e and not _va(e["addr"]):
                errors.append("%s: addr %r is not a 32-bit VA" % (where, e["addr"]))
    if errors:
        raise CliError("release map %s:\n  %s" % (path, "\n  ".join(errors)))
    return rel, out


def release_id(region, version):
    """'pal-v3' from the certificate's region flags and version."""
    parts = []
    if region is not None:
        parts.append(xbe.region_text(region).lower().replace("/", "-"))
    if version is not None:
        parts.append("v%d" % version)
    return "-".join(parts) or REFERENCE


def release_files(G):
    """[(path, release table, {name: row})] of every release file."""
    if not os.path.isdir(G.releases):
        return []
    rows = load(G.names)
    ref_sha = set(G.m["xbe"].get("sha256") or [])
    out, seen, errors = [], {}, []
    for n in sorted(os.listdir(G.releases)):
        if n.endswith(".toml"):
            p = os.path.join(G.releases, n)
            rel, placed_rows = load_release(p, rows)
            for sha in rel.get("sha256", []):
                if sha in ref_sha:
                    errors.append(
                        "%s lists %s, the reference's own sha256 ([xbe] sha256)"
                        % (G.rel(p), sha[:12])
                    )
                elif sha in seen:
                    errors.append("%s and %s both list %s" % (seen[sha], G.rel(p), sha[:12]))
                seen.setdefault(sha, G.rel(p))
            out.append((p, rel, placed_rows))
    if errors:
        raise CliError("release maps in %s:\n  %s" % (G.rel(G.releases), "\n  ".join(errors)))
    return out


def reference(G):
    """The reference's Selection: names.toml as it is."""
    rows = load(G.names)
    m = G.m["xbe"]
    rid = release_id(m.get("region"), m.get("version"))
    return Selection(
        rid,
        "",
        {
            r["name"]: Placed(
                r["name"], r["kind"], r.get("addr"), r.get("value"), "sure", "", r.get("note", "")
            )
            for r in rows
        },
    )


def _from_release(G, path, rel, placed):
    rows = load(G.names)
    ref_sha = G.m["xbe"].get("sha256") or []
    if rel.get("reference") and ref_sha and rel["reference"] not in ref_sha:
        host.say(
            "WARNING: %s was matched against %s, which is not in [xbe] sha256: run '%s match' "
            "again on the reference" % (G.rel(path), rel["reference"][:12], host.prog())
        )
    moved = [
        r["name"]
        for r in rows
        if "ref" in placed.get(r["name"], {}) and placed[r["name"]]["ref"] != ref_of(r)
    ]
    if moved:
        raise CliError(
            "%s was made for other reference addresses of %s than %s has now: run '%s match' "
            "again and review those rows"
            % (G.rel(path), ", ".join(moved), G.rel(G.names), host.prog())
        )
    unpinned = [r["name"] for r in rows if r["name"] in placed and "ref" not in placed[r["name"]]]
    if unpinned:
        host.say(
            "WARNING: %s does not record the reference address of %s (ref = ...), so a change "
            "to names.toml cannot be caught: run '%s match' again"
            % (G.rel(path), ", ".join(unpinned), host.prog())
        )
    out = {}
    for r in rows:
        e = placed.get(r["name"], {})
        out[r["name"]] = Placed(
            r["name"],
            r["kind"],
            e.get("addr"),
            e.get("value"),
            e.get("verdict", "unsure"),
            e.get("how", ""),
            r.get("note", ""),
        )
    return Selection(rel["id"], path, out)


def select(G, v="current"):
    """The Selection for the dump in game_files (v: its fingerprint
    Verdict), or None for a game without [pipeline] names.

    - a dump [xbe] sha256 lists: the reference (names.toml);
    - a dump a release map lists by sha256: that map, whatever else the
      fingerprint says;
    - the reference's code (header or data: a region or title patch, a data
      mod): the reference;
    - another release (the certificate says so) with other code, or
      unknown: stop, naming the command that makes a map;
    - otherwise (a code mod of the reference, or an unknown dump that claims
      no other release): the reference, as before names existed; the
      fingerprint has warned already."""
    if not G.names:
        return None
    if not os.path.isfile(G.names):
        raise CliError("[pipeline] names names %s, which is not there" % G.rel(G.names))
    if v == "current":
        v = fingerprint.current()
    if v is None or v.verdict == fingerprint.KNOWN:
        return reference(G)
    for path, rel, placed_rows in release_files(G):
        if v.sha in rel.get("sha256", []):
            return _from_release(G, path, rel, placed_rows)
    if v.verdict in (fingerprint.HEADER, fingerprint.DATA) or not v.other:
        return reference(G)
    raise CliError(
        "another release (%s) than this port's (%s), and no release map in %s lists its "
        "sha256 %s: run '%s match %s' to make one, then review it (docs/names.md)"
        % (
            fingerprint.release_words(v) or "unknown",
            v.ref or "unrecorded",
            G.rel(G.releases),
            v.sha[:12],
            host.prog(),
            G.rel(G.game_files),
        )
    )


def placed(sel):
    """The names that have an address or value in this release."""
    return [p for p in sel.names.values() if (p.value if p.kind == "value" else p.addr) is not None]


def missing(sel):
    return [p.name for p in sel.names.values() if p not in placed(sel)]


def unsure(sel):
    """Placed names of a release map whose verdict no one has checked:
    (name, verdict), "unsure" or "sure" (the matcher was sure; no person
    has looked). The reference's own names need no review."""
    if not sel.path:
        return []
    return [(p.name, p.verdict) for p in placed(sel) if p.verdict != "reviewed"]


def addresses(sel):
    """{name: va} for recomp --names: every placed address name."""
    return {p.name: p.addr for p in placed(sel) if p.kind != "value"}


def names_json_text(sel):
    """analysis/names.json, recomp --names: the addresses recomp resolves,
    and every name the release has (values too), which is what keeps an
    #if XBR_HAVE(n) block of the manual file in recomp's view of it."""
    return (
        json.dumps(
            {
                "addresses": {n: "0x%08X" % va for n, va in addresses(sel).items()},
                "have": [p.name for p in placed(sel)],
            },
            indent=1,
        )
        + "\n"
    )


def c_value(v):
    """C and TOML at once: a float as Python writes it, a negative integer
    in signed decimal (0x-5 is neither), else hex."""
    if isinstance(v, float):
        return repr(v)
    return "%d" % v if v < 0 else "0x%X" % v


def header_text(sel, G):
    """gen/recomp_names.h for sel."""
    src = G.rel(sel.path) if sel.path else G.rel(G.names)
    lines = [
        "/* %s: the port's names for release %s, from %s." % (HEADER, sel.id, src),
        " * Written by the CLI before recomp (docs/names.md); never edit it. */",
        "#ifndef RECOMP_NAMES_H",
        "#define RECOMP_NAMES_H",
        "",
        '#define XBR_RELEASE "%s"' % sel.id,
        "#define XBR_FN(n)   XBR_FN_##n",
        "#define XBR_GEN(n)  XBR_GEN_##n",
        "#define XBR_VA(n)   XBR_VA_##n",
        "#define XBR_VAL(n)  XBR_VAL_##n",
        "#define XBR_HAVE(n) XBR_HAVE_##n",
    ]
    have = {p.name for p in placed(sel)}
    for p in sel.names.values():
        lines.append("")
        if p.name not in have:
            lines.append("/* %s: %s, not in this release */" % (p.name, p.kind))
            lines.append("#define XBR_HAVE_%s 0" % p.name)
            continue
        how = p.verdict + (" (%s)" % p.how if p.how else "")
        lines.append("/* %s: %s, %s */" % (p.name, p.kind, how))
        lines.append("#define XBR_HAVE_%s 1" % p.name)
        if p.kind == "value":
            lines.append("#define XBR_VAL_%s %s" % (p.name, c_value(p.value)))
            continue
        lines.append("#define XBR_VA_%s 0x%08Xu" % (p.name, p.addr))
        if p.kind == "function":
            lines.append("#define XBR_FN_%s sub_%08X" % (p.name, p.addr))
            lines.append("#define XBR_GEN_%s sub_%08X_gen" % (p.name, p.addr))
    lines += ["", "#endif", ""]
    return "\n".join(lines)


# ── the game's sources: uses of names this release lacks ────────────────

_FORM_RE = re.compile(r"\bXBR_(FN|GEN|VA|VAL)\(\s*(\w+)\s*\)")
# Any XBR_HAVE use: only the plain "#if XBR_HAVE(n)" line is understood
# (by this check and by recomp's manual scan), so every other one is refused.
_HAVE_ANY_RE = re.compile(r"\bXBR_HAVE\b(?:\(\s*(\w+)\s*\))?")
_HAVE_IF_RE = re.compile(r"^#\s*if\s+XBR_HAVE\(\s*(\w+)\s*\)\s*(?:/[*/].*)?$")
_COND_OPEN_RE = re.compile(r"^#\s*if(?:def|ndef)?\b")
_COND_ELSE_RE = re.compile(r"^#\s*(else|elif)\b")
_COND_END_RE = re.compile(r"^#\s*endif\b")
_COMMENT_OR_STRING_RE = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S
)


def _blank(m):
    # Keep the newlines, so line numbers still hold.
    return re.sub(r"[^\n]", " ", m.group(0))


def uses(text):
    """[(line, form, name, guards)] of the name forms in C text, outside
    comments and strings; guards is the set of names whose XBR_HAVE block
    the use sits in (on its #if side). A use on an #else side has that
    name in no guard: it is built when the name is missing."""
    return _scan(text)[0]


def have_uses(text):
    """[(line, name, plain)] of the XBR_HAVE uses in C text, outside
    comments and strings: plain when the line is "#if XBR_HAVE(n)"."""
    return _scan(text)[1]


def _scan(text):
    code = _COMMENT_OR_STRING_RE.sub(_blank, text)
    out, haves = [], []
    stack = []  # [name, on_if_side] per XBR_HAVE block, None for others
    for n, line in enumerate(code.splitlines(), 1):
        s = line.strip()
        m = _HAVE_IF_RE.match(s)
        if m:
            haves.append((n, m.group(1), True))
            stack.append([m.group(1), True])
            continue
        haves += [(n, h.group(1), False) for h in _HAVE_ANY_RE.finditer(line)]
        if _COND_OPEN_RE.match(s):
            stack.append(None)
        elif _COND_ELSE_RE.match(s) and stack and stack[-1] is not None:
            stack[-1][1] = False
        elif _COND_END_RE.match(s) and stack:
            stack.pop()
        guards = {f[0] for f in stack if f is not None and f[1]}
        for u in _FORM_RE.finditer(line):
            out.append((n, u.group(1), u.group(2), guards))
    return out, haves


def sources(G):
    """The game's own C sources: src/ without gen/."""
    gen = os.path.realpath(G.gen)
    out = []
    for d, dirs, files in os.walk(G.path("src")):
        dirs[:] = sorted(x for x in dirs if os.path.realpath(os.path.join(d, x)) != gen)
        out += [os.path.join(d, f) for f in sorted(files) if f.endswith((".c", ".h"))]
    return out


def check_sources(G, sel):
    """Stop, naming each, on a use the release cannot build: a name it
    lacks outside an XBR_HAVE block of that name, a name names.toml does
    not have (in a form or in XBR_HAVE: a typo there would drop the block
    from every release), a form that does not fit the name's kind, or an
    XBR_HAVE that is not a plain "#if XBR_HAVE(n)" line."""
    have = {p.name for p in placed(sel)}
    bad = []
    for path in sources(G):
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        found, haves = _scan(text)
        for line, name, plain in haves:
            where = "%s:%d: XBR_HAVE(%s)" % (G.rel(path), line, name or "")
            if not plain:
                bad.append(
                    "%s: only a plain '#if XBR_HAVE(name)' line is understood (no !, &&, "
                    "#elif or macro around it); nest the blocks instead" % where
                )
            elif name not in sel.names:
                bad.append("%s: not a name in %s" % (where, G.rel(G.names)))
        for line, form, name, guards in found:
            where = "%s:%d: XBR_%s(%s)" % (G.rel(path), line, form, name)
            p = sel.names.get(name)
            if p is None:
                bad.append("%s: not a name in %s" % (where, G.rel(G.names)))
            elif form in ("FN", "GEN") and p.kind != "function":
                bad.append("%s: %s is a %s, not a function" % (where, name, p.kind))
            elif form == "VAL" and p.kind != "value":
                bad.append("%s: %s is a %s, not a value (XBR_VA)" % (where, name, p.kind))
            elif form == "VA" and p.kind == "value":
                bad.append("%s: %s is a value (XBR_VAL)" % (where, name))
            elif name not in have and name not in guards:
                bad.append(
                    "%s: release %s has no %s; put the code under #if XBR_HAVE(%s) or place it in %s"
                    % (where, sel.id, name, name, G.rel(sel.path) if sel.path else G.rel(G.names))
                )
    if bad:
        raise CliError(
            "the game's code uses %d name(s) release %s cannot build:\n  %s"
            % (len(bad), sel.id, "\n  ".join(bad))
        )


# ── seeds by name ────────────────────────────────────────────────────────


def seeds_dir(G):
    return os.path.join(G.out, "seeds")


def _named_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(rows, list) and any(isinstance(r, dict) and "name" in r for r in rows):
        return rows
    return None


def seed_path(G, path):
    """The seed file disasm and recomp read for path: the resolved copy in
    analysis/seeds/ when it has rows by name, else path itself."""
    if not G.names or _named_rows(path) is None:
        return path
    # Keyed by the path under the root, so two seed files of one basename
    # in different directories do not share a resolved copy.
    rel = G.rel(path)
    if rel.startswith("../") or os.path.isabs(rel):
        rel = os.path.abspath(path).strip("/\\").replace(":", "")
    return os.path.join(seeds_dir(G), *rel.split("/"))


def write_seeds(G, sel):
    """Resolve every seed file with rows by name into analysis/seeds/: a
    row gets the release's start; one whose name the release lacks is
    left out with a warning (a seed only helps analysis)."""
    for path in G.seeds:
        rows = _named_rows(path)
        if rows is None:
            continue
        out, dropped = [], []
        for r in rows:
            if isinstance(r, dict) and "name" in r:
                if "start" in r:
                    raise CliError(
                        "%s: seed %r has both name and start; a start is the reference's "
                        "address only, so give the name alone" % (G.rel(path), r["name"])
                    )
                p = sel.names.get(r["name"])
                if p is None:
                    raise CliError(
                        "%s: seed %r is not a name in %s" % (G.rel(path), r["name"], G.rel(G.names))
                    )
                if p.kind != "function":
                    raise CliError(
                        "%s: seed %r is a %s, not a function" % (G.rel(path), r["name"], p.kind)
                    )
                if p.addr is None:
                    dropped.append(r["name"])
                    continue
                r = dict(r, start="0x%08X" % p.addr)
            out.append(r)
        if dropped:
            host.say(
                "WARNING: release %s has no %s: those seeds are left out"
                % (sel.id, ", ".join(dropped))
            )
        dest = seed_path(G, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _write_if_changed(dest, json.dumps(out, indent=2) + "\n")


# ── what the stages read and write ───────────────────────────────────────


def names_json(G):
    return os.path.join(G.out, "names.json")


def _write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def warn_unsure(sel):
    u = unsure(sel)
    if u:
        host.say(
            "WARNING: release %s: %d name(s) placed but not reviewed (%s): check each against "
            'the code and set verdict = "reviewed" in %s'
            % (sel.id, len(u), ", ".join("%s, %s" % nv for nv in u), os.path.basename(sel.path))
        )


def prepare(G, sel):
    """Before disasm: the source check and the resolved seeds."""
    check_sources(G, sel)
    write_seeds(G, sel)
    warn_unsure(sel)


def write_recomp_inputs(G, sel):
    """Before recomp: the source check, the seeds, analysis/names.json
    (recomp --names) and gen/recomp_names.h."""
    check_sources(G, sel)
    write_seeds(G, sel)
    os.makedirs(G.out, exist_ok=True)
    _write_if_changed(names_json(G), names_json_text(sel))
    os.makedirs(G.gen, exist_ok=True)
    _write_if_changed(os.path.join(G.gen, HEADER), header_text(sel, G))
    gone = missing(sel)
    if gone:
        host.say("release %s: built without %s" % (sel.id, ", ".join(gone)))
    warn_unsure(sel)


# ── the release file match writes ────────────────────────────────────────


def _toml_str(s):
    return json.dumps(s, ensure_ascii=False)


def release_text(G, rel, rows, placed_rows):
    """A release file: the [release] table, then one [[name]] per row of
    names.toml in its order (placed_rows: {name: row dict})."""
    out = [
        "# %s: the port's names (%s) in %s."
        % (rel["id"], G.rel(G.names), rel.get("words", rel["id"])),
        "# Written by `%s match`, then checked by hand: a row whose" % host.prog(),
        '# verdict is "reviewed" is kept as it is when match runs again; set it once',
        "# the address (or value) is checked against the code. A row with no addr or",
        "# value is unplaced: the code under #if XBR_HAVE(name) is left out of this",
        "# release, and any other use stops the build (docs/names.md).",
        "",
        "[release]",
        "id = %s" % _toml_str(rel["id"]),
        "sha256 = [%s]" % ", ".join(_toml_str(s) for s in rel["sha256"]),
    ]
    if rel.get("region") is not None:
        out.append("region = 0x%X" % rel["region"])
    if rel.get("version") is not None:
        out.append("version = %d" % rel["version"])
    if rel.get("reference"):
        out.append("reference = %s" % _toml_str(rel["reference"]))
    for r in rows:
        e = placed_rows.get(r["name"], {})
        out += ["", "[[name]]", "name = %s" % _toml_str(r["name"])]
        # The reference's address (value) the row was matched from: a row
        # whose ref no longer agrees with names.toml is stale.
        if r["kind"] == "value":
            out.append("ref = %s" % c_value(r["value"]))
        else:
            out.append("ref = 0x%08X" % r["addr"])
        if r["kind"] == "value":
            if "value" in e:
                out.append("value = %s" % c_value(e["value"]))
            else:
                out.append(
                    "# unplaced: a value, which match does not place (the reference has %s);"
                    % c_value(r["value"])
                )
                out.append('# set value = ... and verdict = "reviewed" by hand')
        elif "addr" in e:
            out.append("addr = 0x%08X" % e["addr"])
        else:
            out.append(
                "# unplaced: %s (the reference has 0x%08X)" % (e.get("why", "not found"), r["addr"])
            )
        if "verdict" in e:
            out.append("verdict = %s" % _toml_str(e["verdict"]))
        if e.get("how"):
            out.append("how = %s" % _toml_str(e["how"]))
        if e.get("check"):
            out.append("check = %s" % _toml_str(e["check"]))
    return "\n".join(out) + "\n"
