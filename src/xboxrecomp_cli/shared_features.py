"""The features every game gets from the toolkit, and whether this one has
them. Each row names what a player would miss: a toolkit that has the code, a
game that stages it, a config row and a README row. A forgotten line was
silent before this (four of eight games had no Shaders page and
nothing said so).

The table is the CLI's, like toolkit.FEATURE_FILES, since a game's pin can
name a public toolkit that predates any table kept there. No game constants:
a feature is about the toolkit, and a game either has it or is told so.
"""

import os
import re

from . import manifest, toolkit

# name: the option, as in config_defaults.
# code: a file the toolkit must have.
# stage: the toolkit file and text that say the toolkit stages it by itself,
#   and the call that does it from a game's CMake while its pin predates that.
# config: [section] and key in packaging/config.default.toml; a commented row
#   counts, since off is the default.
# readme: the menu page in the README's mods table.
SHARED = (
    {
        "name": "present.shader",
        "label": "present shaders, the menu's Shaders page",
        "code": "src/kernel/nv2a_present_chain.c",
        # The link-closure walk: the first scan matched only an exe's own
        # links, so a game on the umbrella alone was not staged.
        "stage": ("cmake/librashader.cmake", "_xboxrecomp_links_enhance"),
        "call": "xboxrecomp_present_shaders",
        "config": ("present", "shader"),
        "readme": "Shaders",
    },
)

# Trees that hold no CMake of the game's own.
_SKIP_DIRS = {".git", ".venv", "analysis", "external", "third_party", "bench-logs", "dist"}


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _game_calls(game, call):
    """Whether a CMake file of the game calls `call`, comments aside."""
    pat = re.compile(r"^[^#\n]*\b%s\(" % re.escape(call), re.M)
    for d, dirs, files in os.walk(game.root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith("build")]
        for f in files:
            if f == "CMakeLists.txt" or f.endswith(".cmake"):
                if pat.search(_read(os.path.join(d, f)) or ""):
                    return True
    return False


def _config_row(game, section, key):
    text = _read(game.path("packaging/config.default.toml"))
    if text is None:
        return False
    cur = ""
    for line in text.splitlines():
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            cur = m.group(1)
        elif cur == section and re.match(r"\s*#?\s*%s\s*=" % re.escape(key), line):
            return True
    return False


def _readme_row(game, page):
    text = _read(game.path("README.md"))
    if text is None:
        return False
    # A template's rows waiting in a comment are not rows yet.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.search(r"^\|[^|\n]*\|\s*%s\s*\|" % re.escape(page), text, re.M) is not None


def gaps(game, row, tk):
    """What the game lacks of one row; tk None: no toolkit here yet, so what
    depends on it cannot be told."""
    out = []
    has_tk = tk is not None and os.path.isdir(tk)
    if has_tk and not os.path.isfile(os.path.join(tk, *row["code"].split("/"))):
        out.append("the toolkit has no %s: move [toolkit] past it" % row["code"])
    stage_file, marker = row["stage"]
    auto = has_tk and marker in (_read(os.path.join(tk, *stage_file.split("/"))) or "")
    if has_tk and not auto and not _game_calls(game, row["call"]):
        out.append("no %s(<exe>) in the game's CMake" % row["call"])
    section, key = row["config"]
    if not _config_row(game, section, key):
        out.append("no %s row under [%s] in packaging/config.default.toml" % (key, section))
    if not _readme_row(game, row["readme"]):
        out.append("no %s row in the README's mods table (docs/readme.md)" % row["readme"])
    return out


def report(game, tk=None):
    """[(row, gaps)], or [] for a game that does not build the layer the
    features live in."""
    if not manifest.builds_enhance(game.m):
        return []
    tk = tk or toolkit.toolkit_dir()
    return [(row, gaps(game, row, tk)) for row in SHARED]


def problems(game, tk=None):
    """The gaps as layout problems, for doctor and the public audit."""
    return ["%s: %s" % (row["name"], g) for row, found in report(game, tk) for g in found]


def text_lines(game, tk=None):
    """(lines, whether every feature is there), as `features` prints them.
    With no toolkit here a row without gaps is not called ok: the code and
    the staging were not checked."""
    tk = tk or toolkit.toolkit_dir()
    rep = report(game, tk)
    if not rep:
        return ["no shared features: the stock build has no enhancements layer"], True
    has_tk = tk is not None and os.path.isdir(tk)
    lines = []
    for row, found in rep:
        verdict = "ok" if has_tk else "toolkit not found; staging not checked"
        lines.append("%s: %s (%s)" % (row["name"], "MISSING" if found else verdict, row["label"]))
        lines += ["  %s" % g for g in found]
    return lines, not any(found for _, found in rep)
