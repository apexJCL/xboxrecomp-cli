"""The toolkit's option defaults, as the config.default.toml `new` writes.

That template (templates/new-game/packaging/config.default.toml) is the one
place the CLI holds them: `new` fills in the game's name and writes it, and
doctor reads it back to say where a game's own file differs. The values
follow the toolkit's enhancements layer (src/enhance/*.c and the
RECOMP_ENV_ENHANCE_KEYS rows of recomp_env.h, documented in its
docs/runtime/config.md) in the toolkit release this CLI ships with: a push
round publishes both, so the template gains a key in the round that
publishes the toolkit reading it (an older toolkit would log it as unused on
every launch). No game's values belong in it: a game that needs another
value sets it in its own file.

The ports' own defaults (PORT) are the one exception: keys every game ships
at another value than the toolkit's, as the in-game menu, which the ports
turn on while the toolkit leaves it off.
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
# {key: (the template's value, the toolkit's default)} where every port
# differs from the toolkit on purpose: the menu ships on in every game.
PORT = {"menu.enabled": (True, False)}


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


def template_defaults():
    """{dotted key: value} of the keys the template sets (the commented
    lines, as the [textures] table, are not among them)."""
    return flatten(tomllib.loads(template_text("")))


def toolkit_defaults():
    """template_defaults() with the PORT keys at the toolkit's own value."""
    out = template_defaults()
    for k, (_, tk) in PORT.items():
        out[k] = tk
    return out


def drift(path):
    """[(key, game value, template value)] of the keys the game's file sets
    to another value than the template's, in the template's order: the
    toolkit's default, or a PORT key's port value.
    A key the file leaves out takes the toolkit's default, and a key the
    template lacks (the game's own, or one of [input]) is the game's, so
    neither is drift. Raises OSError or tomllib.TOMLDecodeError."""
    with open(path, "rb") as f:
        game = flatten(tomllib.load(f))
    return [
        (k, game[k], v)
        for k, v in template_defaults().items()
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
        ", ".join(
            "%s = %s (%s %s)"
            % (k, toml_value(g), "new games" if k in PORT else "toolkit", toml_value(t))
            for k, g, t in diff
        ),
    )
