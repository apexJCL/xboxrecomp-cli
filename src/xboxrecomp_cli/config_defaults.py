"""The toolkit's option defaults, as the config.default.toml `new` writes.

That template (templates/new-game/packaging/config.default.toml) is the one
place the CLI holds them: `new` fills in the game's name and writes it, and
doctor reads it back to say where a game's own file differs. The values
follow the toolkit's enhancements layer (src/enhance/enhance.c and the
RECOMP_ENV_ENHANCE_KEYS rows of recomp_env.h, documented in its
docs/runtime/config.md) at the branch `new` pins (scaffold.TOOLKIT_BRANCH),
not the private integration branch ahead of it: a key that toolkit lacks
would be logged as unused on every launch. No game's values belong in it:
a game that needs another value sets it in its own file.
"""

import os
import tomllib

TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "templates",
    "new-game",
    "packaging",
    "config.default.toml",
)
# Where a game keeps its defaults file, relative to [package] content.
NAME = "config.default.toml"


def template_text(name):
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read().replace("@NAME@", name)


def flatten(doc, prefix=""):
    """{dotted key: value} of a TOML document's leaves."""
    out = {}
    for k, v in doc.items():
        if isinstance(v, dict):
            out.update(flatten(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


def toolkit_defaults():
    """{dotted key: default} of the keys the template sets (the commented
    [textures] table is not among them)."""
    return flatten(tomllib.loads(template_text("")))


def drift(path):
    """[(key, game value, toolkit default)] of the keys the game's file sets
    to another value than the toolkit's default, in the template's order.
    A key the file leaves out takes the toolkit's default, and a key the
    template lacks (the game's own, or one of [input]) is the game's, so
    neither is drift. Raises OSError or tomllib.TOMLDecodeError."""
    with open(path, "rb") as f:
        game = flatten(tomllib.load(f))
    return [
        (k, game[k], v)
        for k, v in toolkit_defaults().items()
        if k in game and (game[k] != v or type(game[k]) is not type(v))
    ]


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"%s"' % v
    return str(v)


def doctor_line(path):
    """doctor's `defaults:` line for the game's config.default.toml, or ''
    when the game has none. A differing key is a note, not a problem: a
    game may keep another value on purpose (its comment says why)."""
    if not os.path.isfile(path):
        return ""
    try:
        diff = drift(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return "%s: unreadable (%s)" % (NAME, e)
    if not diff:
        return "%s: the toolkit's defaults" % NAME
    return "%s: differs from the toolkit's defaults: %s" % (
        NAME,
        ", ".join("%s = %s (toolkit %s)" % (k, toml_value(g), toml_value(t)) for k, g, t in diff),
    )
