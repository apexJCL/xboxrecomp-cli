"""The files a game keeps the same as this CLI's templates, beside the
bootstrap (wrapper/): the workflows `xbr new` writes and the sh and cmd
wrappers.

  <slug> workflow --check   exit 1 and show the difference when a
                            workflow the game must have is not the
                            template ([wizard]: release-wizard.yml,
                            [public]: checks.yml)
  <slug> workflow --print   each template, to copy over the game's
"""

import difflib
import os
import sys

from . import scaffold

CHECKS = ".github/workflows/checks.yml"


def template(rel, game):
    with open(os.path.join(scaffold.CLI_TEMPLATES, *rel.split("/")), encoding="utf-8") as f:
        return scaffold.fill(f.read(), {"SLUG": game.slug})


def wanted(game):
    """The workflows this game must have: each is the template."""
    out = []
    if "wizard" in game.m:
        out.append(scaffold.WORKFLOW)
    if "public" in game.m:
        out.append(CHECKS)
    return out


def read(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return None


def compare(rel, have, want):
    """(ok, diff lines); a missing file is not ok."""
    if have is None:
        return False, ["%s: missing\n" % rel]
    if have == want:
        return True, []
    return False, list(
        difflib.unified_diff(want.splitlines(True), have.splitlines(True), "template", rel)
    )


def check(game):
    """[(rel, ok, diff lines)] of each workflow the game must have."""
    out = []
    for rel in wanted(game):
        ok, diff = compare(rel, read(game.path(rel)), template(rel, game))
        out.append((rel, ok, diff))
    return out


def check_bootstrap(game):
    """[(rel, ok)] of the sh and cmd wrappers beside <slug>.py (that one is
    wrapper.check's). CRLF is read as LF: the cmd file is written CRLF."""
    v = {"SLUG": game.slug}
    out = []
    for rel, text in ((game.slug, scaffold.SH_WRAPPER), (game.slug + ".cmd", scaffold.CMD_WRAPPER)):
        have = read(game.path(rel))
        out.append((rel, have is None or have == scaffold.fill(text, v)))
    return out


def main(argv, game):
    if argv == ["--print"]:
        for rel in wanted(game) or [scaffold.WORKFLOW, CHECKS]:
            sys.stdout.write("# ==> %s <==\n" % rel)
            sys.stdout.write(template(rel, game))
        return 0
    if argv != ["--check"]:
        print(__doc__.strip().replace("<slug>", game.slug), file=sys.stderr)
        return 2
    results = check(game)
    if not results:
        print("no workflow to check: game.toml has neither [wizard] nor [public]")
        return 0
    bad = 0
    for rel, ok, diff in results:
        if ok:
            print("%s: the template, unchanged" % rel)
            continue
        bad += 1
        sys.stdout.writelines(diff)
        print("%s differs from xboxrecomp-cli's template" % rel, file=sys.stderr)
    return 1 if bad else 0
