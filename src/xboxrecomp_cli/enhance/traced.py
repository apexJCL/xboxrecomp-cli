"""Coverage by on-screen magnification (D14): the trace's figure as advice.

A sidecar's `mag_scale1` is the most host pixels one source texel covered at
render scale 1, over the run. At the owner's render scale R an upscale only
shows where that figure times R is above 1. The proposal is advice: the
owner's choices are written as rules, so the build stays declarative.
"""

import re
import tomllib

from . import classes

# Over an entry already written, the block is replaced, never added to.
BEGIN = "# xbr enhance textures: traced rules, rewritten by --write-rules. Begin."
END = "# xbr enhance textures: traced rules. End."
SKIP = 0
RULE_TABLE = re.compile(r"\s*\[\s*rule\s*[.\]]")


def propose(mag_scale1, render_scale):
    """SKIP when never magnified at R, else the smallest of 2, 3, 4 at or
    above the figure, capped at 4."""
    m = mag_scale1 * render_scale
    if m <= 1:
        return SKIP
    for s in (2, 3):
        if s >= m:
            return s
    return 4


def row(e, render_scale, untraced="class", config=None):
    """One line of the table: the proposal, what the class default would have
    taken, and what the proposal saves against it. An entry the class already
    skips stays skipped, with its reason: the trace cannot make it packable."""
    config = dict(config or {}, render_scale=render_scale)
    t = classes.treatment(e, config)
    default = 0 if t["skip"] else classes.pack_bytes(e, t["scale"], t["encode"], t["mips"])
    mag = e.get("mag_scale1")
    note = ""
    if t["skip"]:
        scale, note = SKIP, t["skip"]
    elif mag is None:
        scale = SKIP if untraced == "skip" else t["scale"]
        note = "untraced"
    else:
        scale = propose(mag, render_scale)
        chain = classes.chain_levels(e["width"] * 3, e["height"] * 3, t["mips"], e.get("levels", 1))
        if scale == 3 and chain > 1:
            # Toolkit task 1.2: the sampler's fractional shift for a 3x chain,
            # which a one-level guest with a pack chain needs as well.
            note = "3x on a mipped entry needs the fractional sampler shift"
    new = classes.pack_bytes(e, scale, t["encode"], t["mips"]) if scale else 0
    return {
        "hash": e["hash"],
        "name": e["origins"][0]["name"] if e.get("origins") else "",
        "class": e["class"],
        "mag_scale1": mag,
        "proposal": scale,
        "default_bytes": default,
        "bytes": new,
        "saved": default - new,
        "note": note,
    }


def table(entries, render_scale, untraced="class", config=None):
    return [row(e, render_scale, untraced, config) for e in entries]


def choices_from(rows):
    """{hash: scale or SKIP} of a table's proposals, leaving out what the class
    skips anyway and untraced entries, which have no figure to decide by."""
    return {
        r["hash"]: r["proposal"] for r in rows if r["mag_scale1"] is not None and r["default_bytes"]
    }


def rules_text(choices):
    """The owner's choices as the config's rules (D8), by hash: a name may hold
    several hashes, one per level prefix, and only this one was traced."""
    out = [BEGIN]
    for h in sorted(choices):
        out.append('[rule."hash:%s"]' % h.upper())
        s = choices[h]
        out.append("skip = true" if s == SKIP else "scale = %d" % s)
    out.append(END)
    return "\n".join(out) + "\n"


def _without_block(text):
    """The file's text with an earlier block taken out."""
    a = text.find(BEGIN)
    if a < 0:
        return text
    b = text.find(END, a)
    if b < 0:
        raise ValueError("a traced rules block with no end line")
    return text[:a] + text[b + len(END) :].lstrip("\n")


def merge_rules(text, choices):
    """A config's text with its traced block replaced. Rules apply in order and
    the trace is advice, so the block goes before the owner's first rule, with
    that rule's comment staying on it, and leaves out a hash the owner rules."""
    head = _without_block(text)
    owned = {k.upper() for k in tomllib.loads(head).get("rule", {})}
    block = rules_text({h: s for h, s in choices.items() if "HASH:" + h.upper() not in owned})
    lines = head.splitlines(keepends=True)
    at = next((i for i, x in enumerate(lines) if RULE_TABLE.match(x)), None)
    if at is None:
        at = len(lines)
    else:
        while at and lines[at - 1].lstrip().startswith("#"):
            at -= 1
    before = "".join(lines[:at]).rstrip("\n")
    after = "".join(lines[at:])
    return (before + "\n\n" if before else "") + block + ("\n" + after if after else "")


def read_rules(config):
    """{hash: scale or SKIP} of a loaded config's hash rules."""
    out = {}
    for key, rule in config.get("rule", {}).items():
        if not key.lower().startswith("hash:"):
            continue
        if rule.get("skip"):
            out[key[5:].upper()] = SKIP
        elif "scale" in rule:
            out[key[5:].upper()] = rule["scale"]
    return out
