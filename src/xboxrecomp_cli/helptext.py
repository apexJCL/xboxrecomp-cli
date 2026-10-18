"""The top-level help, as text: what `<game> --help` prints.

Standard library only and Python 3.9 syntax: the game's bootstrap runs this
file with the host's Python when uv is missing, so a fresh host still sees
the command list. main.py prints the same text, so the help reads the same
whichever Python runs it (argparse's own layout changes between versions).

  python3 helptext.py GAME_ROOT SLUG
"""

import os
import re
import sys

DOC = """\
@slug@: set up, generate, build and package @name@ on this host.

Commands:
  @slug@ /path/to/dump                  your disc image (.iso) or the folder
                                        with default.xbe: checks it is @name@,
                                        puts its files in place, then the same
                                        as the line below
  @slug@                                package for this computer (macos on a
                                        Mac, steamos on Linux, windows on
                                        Windows): sets up, generates and builds
                                        whatever is missing or out of date
  @slug@ package windows|steamos|macos  the same, for a given target: dist/
  @slug@ import PATH [--replace]        only the import (--check compares, writes
                                        nothing)
  @slug@ doctor                         what this host has, and what it can package
  @slug@ fingerprint [PATH]             whether your dump's code is the one the
                                        port was made from (modded data or code)
  @slug@ enhance hud [--font FONT]      your HUD texture pack from your own dump
                                        (dist/enhance/hud/; package --hud-pack
                                        puts it in your bundle; never share it)
  @slug@ clean --bundles|--build|--all  free space: lists what it would delete
                                        (deletes with --yes)
  @slug@ setup [--dev] [--no-toolkit]   fetch the pinned toolchain into this tree

Developer commands:
  @slug@ analyze | recomp | all         the pipeline (parse disasm funcid abi names
                                        recomp; ghidra is optional)
  @slug@ build [windows|macos]          compile @windows_dir@/ or @macos_dir@/
  @slug@ pins refresh                   maintainers: re-pin the downloads
  @slug@ wizard build [--tag TAG]       the player's build wizard for a release
                                        (dist/wizard/; config, check)
  @slug@ public init|export|pr|audit    maintainers: the public repo (one commit
                                        per round; docs/layout.md)
  @slug@ workflow --check               the CI workflows against the CLI's
                                        templates
  @slug@ new DIR                        start another game: a manifest, bootstrap
                                        and template in DIR (xbr new --help)
  @slug@ bench <command>                drive the Proton bench host (@slug@ bench --help)

`@slug@ <command> --help` for the options. Windows: `@slug@.cmd`, or
`py -3 @slug@.py`. Docs: docs/packaging.md.
"""


OUTSIDE = """\
usage: @prog@ [--game DIR] command ...

@prog@: the xboxrecomp toolchain. There is no game.toml here, so the one
command that runs is `new`:

  @prog@ new DIR [--xbe DUMP]
      start a game in DIR; DUMP is your disc image (.iso, .xiso), dump
      folder or default.xbe, read in place for the title, title ID and
      entry point
  @prog@ new --help
      its options

Inside a game (or with --game DIR) `@prog@ --help` lists that game's
commands. After `new`: `./SLUG import DUMP`, `./SLUG setup` and `./SLUG all`
in DIR (its README.md).
"""


def outside_help(prog="xbr"):
    """What --help prints where no game.toml is: the game's own help names
    the game, so it cannot be rendered before there is one."""
    return OUTSIDE.replace("@prog@", prog)


def render(slug, name, windows_dir="build-win", macos_dir="build"):
    """DOC for this game: its first line is the description, the rest the
    epilog argparse prints under the options."""
    text = DOC
    for k, v in (
        ("slug", slug),
        ("name", name),
        ("windows_dir", windows_dir),
        ("macos_dir", macos_dir),
    ):
        text = text.replace("@%s@" % k, v)
    return text


def top_help(slug, name, windows_dir="build-win", macos_dir="build"):
    """The whole `--help` output."""
    doc = render(slug, name, windows_dir, macos_dir)
    first, rest = doc.split("\n\n", 1)
    return (
        "usage: %s [-h] command ...\n\n%s\n\npositional arguments:\n  command\n\n"
        "options:\n  -h, --help  show this help message and exit\n\n%s"
    ) % (slug, first, rest)


def line_value(text, table, key):
    """`key = "..."` inside [table], as the bootstrap reads [cli]."""
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            cur = line.strip("[]").strip()
            continue
        if cur != table:
            continue
        m = re.match(r'^%s\s*=\s*"([^"\\]*)"\s*(#.*)?$' % re.escape(key), line)
        if m:
            return m.group(1)
    return None


def main(argv):
    root, slug = argv[0], argv[1]
    with open(os.path.join(root, "game.toml"), encoding="utf-8") as f:
        text = f.read()
    name = line_value(text, "game", "name") or slug
    dirs = [line_value(text, "build", k) for k in ("windows_dir", "macos_dir")]
    sys.stdout.write(top_help(slug, name, dirs[0] or "build-win", dirs[1] or "build"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
