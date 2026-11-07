"""What an inventory entry is, and what a build would do with it (D2, D3).

Every entry gets one class the runtime can switch off (`font`, `text`, `ui`,
`world`), facts that pick its treatment (`alpha`, `normal`, `data`, `linear`,
`palette`, `grid`, `rt`, `volatile`, `cube`), and the reason it is skipped, if it is.

An entry is a dict, as inventory.json holds it (inventory.py). Its names are
in `origins`, each {"source", "name"} plus "entry", the container's path, for
a texture inside a .mix: game.toml's patterns may name either.

The treatment of an entry is the class defaults, then the config's class
table, then its rules in order; a rule may bring a skipped entry back, except
what could never match (`volatile`, `cube`). An `rt` entry is skipped until a
rule says otherwise: the fact is one sighting of its address and size as a
render target, not proof, and it carries a hash a pack can match.
"""

import fnmatch
import math
import tomllib

CLASSES = ("font", "text", "ui", "world")
SOURCES = ("archive", "hook", "loose", "dump")
PRESETS = ("hud", "ui", "world", "all", "traced")
# The presets a game cannot redefine: `hud` is its [enhance.hud], `traced`
# the magnification's (traced.py).
FIXED_PRESETS = ("hud", "traced")
CONFIG_SCHEMA = 1

# Sampled as slopes: an upscale would invent them.
NORMAL_FORMATS = {"G8B8", "LIN_G8B8", "V8U8", "LIN_V8U8", "DSDT", "LIN_DSDT"}
# `volatile` and `cube` cannot usefully match a pack entry at all; the others
# are left out until a rule brings them in.
NEVER = ("volatile", "cube")
SKIP_FACTS = NEVER + ("rt", "normal", "data", "linear")
SKIP_WHY = {
    "rt": "uploaded at an address and size also sampled as a render target: "
    "its texels may be the game's own rendering; a rule may bring it back",
    "volatile": "its hash keeps changing: an entry would match one frame at most",
    "normal": "a bump or normal map: an upscale invents slopes",
    "data": "a ramp or lookup table: read as data, not as an image",
    "linear": "a linear (rect) texture: read at guest rate when drawn 1:1",
    "cube": "a cube map: both backends upload its faces from the guest, never from a pack",
    "needs_dump": "the load changes its bytes: only a dump knows its hash",
    "no_texels": "no source names it and its dump holds no texels: nothing to build from",
}
# Why the trace leaves an entry out; a rule giving it a scale answers that.
TRACE_SKIP = "the trace: no gain at this render scale"
# A config's preset for its own classes and rules, over every entry (D8).
CUSTOM = "custom"
BUILTINS = ("xbr", "lanczos", "epx", "nearest", "box", "atlas")
# Formats whose alpha the file's own bytes settle; DXT1's punch-through needs
# the decoded texels, which only a dump has.
ALPHA_FORMATS = ("DXT3", "DXT5", "A8", "AL8", "A8L8", "A1R5G5B5", "A4R4G4B4", "A8R8G8B8")
# A dump's decoded alpha is a decoder's fill for these, not the texture's.
OPAQUE_FORMATS = ("G8B8", "R8B8", "R5G6B5", "R6G5B5", "X1R5G5B5", "X8R8G8B8", "L8", "L16", "V8U8")
BLOCK = {"bc1": 8, "bc3": 16}


def _low(patterns):
    return [p.lower().replace("\\", "/") for p in patterns]


def game_config(m):
    """game.toml's [enhance.textures] with [enhance.hud] mapped into it, so a
    game that only has the HUD table works: its archives and languages are
    the archive feeder's, its textures the `ui` class, its text and font atlas
    theirs. A game with neither runs on a dump alone."""
    enh = m.get("enhance", {})
    hud = enh.get("hud") or {}
    tex = enh.get("textures") or {}
    langs = hud.get("languages", {})
    arcs = list(tex.get("archives") or [])
    if not arcs:
        arcs = list(hud.get("archives", []))
        arcs += [a for names in langs.values() for a in names if a not in arcs]
    reader = "hook" if hud.get("source") == "hook" else "archive"
    srcs = list(tex.get("sources") or [])
    if not srcs:
        srcs = ([reader] if arcs else []) + ["dump"]
    classes = {k: _low(v) for k, v in tex.get("classes", {}).items()}
    return {
        "sources": srcs,
        "archives": arcs,
        "loose": list(tex.get("loose", [])),
        "hook": tex.get("hook") or hud.get("hook", ""),
        "exclude": _low(tex.get("exclude", [])),
        "presets": dict(tex.get("presets", {})),
        "preview": _low(tex.get("preview", [])),
        "font": classes.get("font", []),
        "text": classes.get("text", []) + _low(hud.get("text", [])),
        "ui": classes.get("ui", []) or _low(hud.get("textures", [])),
        "ui_exclude": _low(hud.get("exclude", [])) if not classes.get("ui") else [],
        "world": classes.get("world", []),
        "grid": classes.get("grid", []),
        "font_atlas": hud.get("font_atlas", "").lower().replace("\\", "/"),
        "language_of": {a: lang for lang, names in langs.items() for a in names},
        "hud": hud,
        "reencodes_small_levels": bool(tex.get("reencodes_small_levels")),
    }


def _names(entry):
    for o in entry.get("origins", ()):
        yield o["name"]
        if o.get("entry"):
            yield o["entry"]


def matches(entry, patterns):
    """Whether a pattern names the entry, by any of its names."""
    return any(fnmatch.fnmatchcase(n, p) for n in _names(entry) for p in patterns)


def language_of(entry, cfg):
    """The one language this texture belongs to, or "". A language archive
    also carries shared art (a stage's own textures, copied per language); the
    same hash outside it, or in a second language, is not that language's."""
    langs = {cfg["language_of"].get(o["source"], "") for o in entry.get("origins", ())}
    return langs.pop() if len(langs) == 1 else ""


def is_font(entry, cfg):
    atlas = cfg["font_atlas"]
    if atlas and any(o["name"] == atlas for o in entry.get("origins", ())):
        return True
    return matches(entry, cfg["font"])


def facts_of(e, cfg):
    """The entry's facts, sorted. `alpha_used` (from dumped texels) settles
    alpha when there is one; an archive entry goes by its format."""
    w, h = e["width"], e["height"]
    fmt = e.get("format", "")
    out = set()
    used = e.get("alpha_used")
    if fmt.removeprefix("LIN_") in OPAQUE_FORMATS:
        used = False
    if used if used is not None else fmt in ALPHA_FORMATS:
        out.add("alpha")
    if fmt in NORMAL_FORMATS or e.get("bump") is True:
        out.add("normal")
    # D14's third way in: point-sampled, no mips, one texel to one pixel.
    one_to_one = (
        e.get("levels", 1) == 1
        and e.get("mag_filter") == "point"
        and e.get("min_filter") == "point"
        and e.get("mag_scale1") == 1
    )
    if min(w, h) == 1 or max(w, h) <= 8 or one_to_one:
        out.add("data")
    if e.get("layout") == "linear" or fmt.startswith("LIN_"):
        out.add("linear")
    if fmt == "P8" or e.get("palette"):
        out.add("palette")
    if e.get("rt") is True:
        out.add("rt")
    if e.get("volatile"):
        out.add("volatile")
    if e.get("cube"):
        out.add("cube")
    if is_font(e, cfg) or matches(e, cfg["grid"]):
        out.add("grid")
    return sorted(out)


def class_of(e, cfg):
    if is_font(e, cfg):
        return "font"
    if language_of(e, cfg) or matches(e, cfg["text"]):
        return "text"
    if matches(e, cfg["ui"]) and not matches(e, cfg["ui_exclude"]):
        return "ui"
    # The owner's names outrank what one run saw of the draws.
    if matches(e, cfg["ui_exclude"]) or matches(e, cfg["world"]):
        return "world"
    if e.get("drawn_2d") is True:
        return "ui"
    return "world"


def _no_texels(e):
    """A dump's sidecar with no usable .xtd and no source to read instead: a
    linear texture, or a file a run cut short."""
    return e.get("has_texels") is False and not e.get("origins")


def skip_reason(e, cfg):
    """The fact or setting that keeps the entry out by default, or ""."""
    for f in SKIP_FACTS:
        if f in e["facts"]:
            return f
    if e.get("hash_guess"):
        return "needs_dump"
    if _no_texels(e):
        return "no_texels"
    if matches(e, cfg["exclude"]):
        return "excluded"
    # A game that lists its world names keeps the rest out; an unnamed entry
    # has no name to test.
    if e["class"] == "world" and cfg["world"] and e.get("origins"):
        if not matches(e, cfg["world"]):
            return "excluded"
    return ""


def classify(e, cfg):
    e["facts"] = facts_of(e, cfg)
    e["class"] = class_of(e, cfg)
    e["language"] = language_of(e, cfg)
    e["skip"] = skip_reason(e, cfg)
    return e


def why(reason):
    return SKIP_WHY.get(reason, reason)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def is_cnn(upscaler):
    return upscaler not in BUILTINS


def class_defaults(cls, render_scale, upscaler=None):
    """D2's row for a class at the config's render scale."""
    r = render_scale
    if cls == "font":
        return dict(
            scale=4, upscaler="atlas", alpha="split", edges="clamp", mips="none", encode="raw"
        )
    if cls == "text":
        return dict(
            scale=2, upscaler="xbr", alpha="split", edges="clamp", mips="none", encode="bc3"
        )
    if cls == "ui":
        return dict(
            scale=clamp(r, 2, 4),
            upscaler="xbr",
            alpha="split",
            edges="clamp",
            mips="none",
            encode="raw",
        )
    up = upscaler or "lanczos"
    return dict(
        scale=clamp(r, 2, 3) if is_cnn(up) else 2,
        upscaler=up,
        alpha="split",
        edges="auto",
        mips="full",
        encode="auto",
    )


def load_config(path):
    """A config/enhance-textures.toml (D8). Only what the estimate reads is
    looked at; the full schema is the build's."""
    with open(path, encoding="utf-8") as f:
        return parse_config(f.read())


def parse_config(text):
    doc = tomllib.loads(text)
    if doc.get("schema", CONFIG_SCHEMA) != CONFIG_SCHEMA:
        raise ValueError("schema %r, this reads %d" % (doc.get("schema"), CONFIG_SCHEMA))
    return doc


def _rule_matches(key, e):
    if key.lower().startswith("hash:"):
        return key[5:].upper() == e["hash"]
    return matches(e, _low([key]))


def treatment(e, config=None, preset=None, cfg=None, proposal=None):
    """What the build does with an entry: the class row, the config's class
    table, the trace's `proposal` (a scale, 0 to leave it out), then the rules
    in order, since the trace is advice and the owner decides per entry (D14).
    `skip` holds why it is left out, or ""."""
    config = config or {}
    r = config.get("render_scale", 1)
    table = config.get("class", {}).get(e["class"], {})
    t = class_defaults(e["class"], r, table.get("upscaler"))
    t.update(max_texels=0, max_side=0, skip=e.get("skip", ""), rules=[])
    picked = config.get("coverage", {}).get("languages")
    if preset == "hud" and cfg and not picked:
        # As `enhance hud`, whose --language defaults to the first listed.
        picked = list(cfg["hud"].get("languages", {}))[:1]
    if picked and e.get("language") and e["language"] not in picked:
        t["skip"] = "language %s not picked" % e["language"]
    if preset == "hud" and cfg:
        hud_treatment(t, e, cfg["hud"])
    t.update({k: v for k, v in table.items() if k != "skip"})
    if table.get("skip"):
        t["skip"] = "class %s" % e["class"]
    if proposal:
        t["scale"] = proposal
    elif proposal is not None and not t["skip"]:
        t["skip"] = TRACE_SKIP
    never = any(f in e.get("facts", ()) for f in NEVER)
    for key, rule in config.get("rule", {}).items():
        if not _rule_matches(key, e):
            continue
        t["rules"].append(key)
        t.update({k: v for k, v in rule.items() if k != "skip"})
        if "skip" in rule:
            t["skip"] = 'rule "%s"' % key if rule["skip"] else ""
        elif "scale" in rule and t["skip"] == TRACE_SKIP:
            t["skip"] = ""
    if never:
        t["skip"] = next(f for f in NEVER if f in e["facts"])
    elif e.get("hash_guess"):
        # An entry under a hash the runtime never computes matches nothing.
        t["skip"] = "needs_dump"
    elif _no_texels(e):
        # A rule cannot bring it back: there is nothing to upscale.
        t["skip"] = "no_texels"
    if not t["skip"] and t["max_texels"] and e["width"] * e["height"] > t["max_texels"]:
        t["skip"] = "over max_texels %d" % t["max_texels"]
    if not t["skip"] and t["max_side"]:
        # The largest whole scale that stays inside the cap.
        asked = t["scale"]
        while t["scale"] > 1 and max(e["width"], e["height"]) * t["scale"] > t["max_side"]:
            t["scale"] -= 1
        if t["scale"] < 2:
            t["skip"] = "over max_side %d" % t["max_side"]
        elif t["scale"] != asked:
            t["clamped_from"] = asked
    return t


def hud_treatment(t, e, hud):
    """The `hud` preset is what `enhance hud` makes: the game's scale raw,
    text at its own scale in BC3, no mips, the game's cap, and alpha straight
    into the kernel, which sees a glyph's edge in it. Its patterns chose the
    entries, so what the classes would keep out by default comes back; the
    classifier's world is plain art to that command."""
    if e["class"] != "text":
        t.update(scale=hud.get("scale", 4), encode="raw")
    if e["class"] == "world":
        t.update(upscaler="xbr", edges="clamp", kind_as="ui")
    if t["skip"] in ("normal", "data", "linear", "excluded"):
        t["skip"] = ""
    t.update(alpha="native", mips="none", max_texels=hud.get("max_texels", 0))


def in_preset(e, preset, cfg):
    """Whether an entry is in a preset's set, skipped or not. `traced` is
    every entry: the proposals pick (traced.py)."""
    named = cfg["presets"].get(preset)
    if named is not None and preset not in FIXED_PRESETS:
        return e["class"] in named or matches(e, _low(n for n in named if n not in CLASSES))
    if preset == "hud":
        return _in_hud(e, cfg)
    if preset == "ui":
        return e["class"] in ("ui", "font", "text")
    if preset == "world":
        return e["class"] in ("ui", "font", "text", "world")
    return True


def _in_hud(e, cfg):
    hud = cfg["hud"]
    if not hud or e.get("levels", 1) != 1 or not e.get("format", "").startswith("DXT"):
        return False
    # `enhance hud` refuses a mipped file, even where the runtime binds its
    # first level alone and so hashes it as one level.
    read = {"origins": [o for o in e.get("origins", ()) if o.get("levels", 1) == 1]}
    if not matches(read, _low(hud.get("textures", []))):
        return False
    return not matches(read, _low(hud.get("exclude", [])))


def _level_bytes(w, h, encode):
    if encode in BLOCK:
        return ((w + 3) // 4) * ((h + 3) // 4) * BLOCK[encode]
    return w * h * 4


def encode_of(e, encode):
    """`auto` as the build resolves it: BC1 when opaque, BC3 with alpha."""
    if encode == "auto":
        return "bc3" if "alpha" in e.get("facts", ()) else "bc1"
    return encode


def chain_levels(w, h, mips, source_levels):
    """Levels of an upscaled chain (D4): `full` to 1x1, `keep` the source's."""
    if mips == "full":
        return 1 + int(math.floor(math.log2(max(w, h))))
    if mips == "keep":
        return max(1, source_levels)
    return 1


def pack_bytes(e, scale, encode, mips):
    """Bytes of the entry's encoded chain at a scale (D6)."""
    w, h = e["width"] * scale, e["height"] * scale
    enc = encode_of(e, encode)
    n = 0
    for _ in range(chain_levels(w, h, mips, e.get("levels", 1))):
        n += _level_bytes(w, h, enc)
        w, h = max(1, w // 2), max(1, h // 2)
    return n


def stock_bytes(e):
    """What the guest's own upload holds: BGRA8, whatever the format."""
    return e.get("texels", e["width"] * e["height"]) * 4
