"""The bootstrap every game vendors: wrapper/game.py, copied into the game
as <slug>.py beside its <slug> (sh) and <slug>.cmd wrappers.

  <slug> wrapper --check   exit 1 and show the difference when the game's
                           copy is not this CLI's template
  <slug> wrapper --print   the template, to copy over the game's
"""

import difflib
import os
import sys

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game.py")


def template_text():
    with open(TEMPLATE, encoding="utf-8", newline="") as f:
        return f.read()


def check(game):
    """(ok, diff lines) of the game's <slug>.py against the template."""
    path = os.path.join(game.root, game.slug + ".py")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            have = f.read().replace("\r\n", "\n")
    except OSError as e:
        return False, ["%s: %s" % (path, e.strerror)]
    want = template_text()
    if have == want:
        return True, []
    return False, list(
        difflib.unified_diff(
            want.splitlines(True), have.splitlines(True), "template", game.slug + ".py"
        )
    )


def main(argv, game):
    if argv == ["--print"]:
        sys.stdout.write(template_text())
        return 0
    if argv != ["--check"]:
        print(__doc__.strip().replace("<slug>", game.slug), file=sys.stderr)
        return 2
    ok, diff = check(game)
    if ok:
        print("%s.py: the template, unchanged" % game.slug)
        return 0
    sys.stdout.writelines(diff)
    print("%s.py differs from xboxrecomp-cli's wrapper/game.py" % game.slug, file=sys.stderr)
    return 1
