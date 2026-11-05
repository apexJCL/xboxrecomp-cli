"""`match`: a release map for another release of the game (docs/names.md).

Maintainers run it with the reference dump in game_files, analysed, and
the other release's XBE (or its folder) as PATH. It parses and
disassembles the other dump into analysis/releases/<id>/ with the port's
own disasm options, pairs the two releases' functions and globals
(match.py, the release matcher), places every address of names.toml and
writes config/releases/<id>.toml. A value (a struct offset, a timing
constant) is not an address the matcher sees: it is written unplaced, for
a person to fill in. Rows already marked "reviewed" are kept as they are.
"""

import os
from collections import namedtuple

from . import fingerprint, host, names, pipeline, symbols, xbe
from .host import CliError


def other_xbe(path):
    """The XBE file at path: the file itself, or default.xbe in a folder."""
    if os.path.isdir(path):
        p = os.path.join(path, "default.xbe")
        if not os.path.isfile(p):
            raise CliError("%s: no default.xbe in that folder" % path)
        return p
    if not os.path.isfile(path):
        raise CliError("%s: not found" % path)
    with open(path, "rb") as f:
        if f.read(4) != b"XBEH":
            raise CliError(
                "%s: not an XBE; give match the XBE or the folder that holds it "
                "(a disc image: import it into another folder first)" % path
            )
    return path


def _reference_disasm(G):
    """analysis/disasm, when it is the reference dump's."""
    d = os.path.join(G.out, "disasm")
    summary = os.path.join(d, "summary.json")
    if not os.path.isfile(os.path.join(d, "functions.json")) or not os.path.isfile(summary):
        raise CliError(
            "match needs the reference dump analysed: run '%s analyze' first" % host.prog()
        )
    import json

    with open(summary, encoding="utf-8") as f:
        binary = json.load(f).get("binary") or ""
    if binary and os.path.realpath(binary) != os.path.realpath(G.xbe):
        raise CliError(
            "%s is the analysis of %s, not of %s: run '%s analyze' on the reference first"
            % (G.rel(d), binary, G.rel(G.xbe), host.prog())
        )
    return d


def _analyse(G, path, out):
    """Parse and disassemble the other dump into out, with the port's
    disasm options and no seeds (the port's seeds are the reference's)."""
    os.makedirs(out, exist_ok=True)
    d = G.m["pipeline"]["disasm"]
    opts = (["--text-only"] if d["text_only"] else []) + (
        ["--extra-sections", ",".join(d["extra_sections"])] if d["extra_sections"] else []
    )
    analysis = os.path.join(out, "analysis.json")
    pipeline.run_cmds(
        [
            pipeline.tool_cmd("xbe_parser", path, "--json", analysis),
            pipeline.tool_cmd(
                "disasm",
                path,
                "--analysis-json",
                analysis,
                "-o",
                os.path.join(out, "disasm"),
                *opts,
            ),
        ]
    )
    return os.path.join(out, "disasm")


def place_rows(m, rows):
    """{name: row} placed through Match m: addr, verdict and how, or why
    it could not be placed. Values are left out (unplaced)."""
    out = {}
    for r in rows:
        if r["kind"] == "value":
            continue
        va, how = m.place(r["addr"])
        if va is None:
            out[r["name"]] = {"why": how}
            continue
        verdict, _, rest = how.partition(": ")
        if verdict not in ("sure", "unsure"):
            verdict, rest = "unsure", how
        row = {"addr": va, "verdict": verdict, "how": rest}
        # A function name must land on a function start of the other
        # release: inside one, the wrap or seed would split it.
        if r["kind"] == "function" and va not in m.other.by_start and "code pointer" not in how:
            row["verdict"] = "unsure"
            row["check"] = "not a function start in this release's analysis"
        out[r["name"]] = row
    return out


Merged = namedtuple("Merged", "kept stale")


def merge_reviewed(rows, placed, old):
    """Keep the old file's reviewed rows in placed (match's new rows):
    (kept, stale names). A row reviewed against another reference address
    (names.toml moved since) is matched afresh and wants a new review; a
    reviewed value keeps its value, unsure. A row with no ref predates
    them and is kept."""
    refs = {r["name"]: names.ref_of(r) for r in rows}
    kept, stale = 0, []
    for n, e in old.items():
        if e.get("verdict") != "reviewed":
            continue
        if "ref" in e and e["ref"] != refs[n]:
            stale.append(n)
            if "addr" not in placed.get(n, {}):
                placed[n] = {k: v for k, v in e.items() if k in ("addr", "value")}
            placed[n].update(
                verdict="unsure",
                check="names.toml moved it from %s; review again" % names.c_value(e["ref"]),
            )
            continue
        placed[n] = e
        kept += 1
    return Merged(kept, stale)


def run(path, rid=None):
    G = host.g()
    if not G.names:
        raise CliError("match places the port's names: game.toml has no [pipeline] names")
    rows = names.load(G.names)
    v = fingerprint.current()
    if v is None:
        raise CliError("match maps from the reference dump: %s is not there" % G.rel(G.xbe))
    if v.other or v.verdict not in (fingerprint.KNOWN, fingerprint.HEADER):
        raise CliError(
            "match maps from the reference dump, and %s is not it (%s): put the reference "
            "there and analyse it, then give match the other release" % (G.rel(G.xbe), v.verdict)
        )
    ref_disasm = _reference_disasm(G)
    path = other_xbe(path)
    with open(path, "rb") as f:
        data = f.read()
    ov = fingerprint.classify(data, G.m)
    if ov.verdict == fingerprint.KNOWN:
        raise CliError("%s is the reference dump itself: nothing to map" % path)
    cert = xbe.cert_release(data)
    rid = rid or names.release_id(cert["region"], cert["version"])
    if rid == names.REFERENCE or not symbols.IDENT.match(rid.replace("-", "_")):
        raise CliError("release id %r: give one with --release (letters, digits, -)" % rid)
    out = os.path.join(G.out, "releases", rid)
    host.step("match: analyse %s" % path)
    other_disasm = _analyse(G, path, out)

    from . import match

    host.step("match: pair %s with the reference" % rid)
    m = match.Match(match.load(G.xbe, ref_disasm), match.load(path, other_disasm))
    m.run()
    placed = place_rows(m, rows)

    target = os.path.join(G.releases, rid + ".toml")
    kept, stale = 0, []
    if os.path.isfile(target):
        _, old = names.load_release(target, rows)
        kept, stale = merge_reviewed(rows, placed, old)
    rel = {
        "id": rid,
        "words": xbe.release_text(cert["region"], cert["version"]) or rid,
        "sha256": [ov.sha],
        "region": cert["region"],
        "version": cert["version"],
        "reference": v.sha,
    }
    if os.path.isfile(target):
        old_rel, _ = names.load_release(target, rows)
        rel["sha256"] = sorted(set(old_rel.get("sha256", [])) | {ov.sha})
    os.makedirs(G.releases, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(names.release_text(G, rel, rows, placed))

    s = m.summary()
    host.say(
        "%s: %d of %d reference functions paired (%d of the other's)"
        % (rid, s["matched"], s["functions"]["ref"], s["functions"]["other"])
    )
    sure = [
        r["name"] for r in rows if placed.get(r["name"], {}).get("verdict") in ("sure", "reviewed")
    ]
    unsure = [r["name"] for r in rows if placed.get(r["name"], {}).get("verdict") == "unsure"]
    unplaced = [r["name"] for r in rows if "verdict" not in placed.get(r["name"], {})]
    host.say(
        "wrote %s: %d sure or reviewed (%d kept), %d unsure, %d unplaced"
        % (G.rel(target), len(sure), kept, len(unsure), len(unplaced))
    )
    for what, ns in (
        ("reviewed for an old reference address, review again", stale),
        ("unsure, check by hand", unsure),
        ("unplaced", unplaced),
    ):
        if ns:
            host.say("  %s: %s" % (what, ", ".join(ns)))
    host.say(
        'Check each unsure row against the code and set verdict = "reviewed"; fill in '
        "each value by hand. Unplaced names leave their #if XBR_HAVE code out of %s." % rid
    )
