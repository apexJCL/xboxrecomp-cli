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
  @slug@                                package for this computer (macos on a
                                        Mac, steamos on Linux, windows on
                                        Windows): sets up, generates and builds
                                        whatever is missing or out of date
  @slug@ package windows|steamos|macos  the same, for a given target: dist/
  @slug@ doctor                         what this host has, and what it can package
  @slug@ setup [--dev] [--no-toolkit]   fetch the pinned toolchain into this tree

Developer commands:
  @slug@ analyze | recomp | all         the pipeline (parse disasm funcid abi names
                                        recomp; ghidra is optional)
  @slug@ build [windows|macos]          compile @windows_dir@/ or @macos_dir@/
  @slug@ pins refresh                   maintainers: re-pin the downloads
  @slug@ new DIR                        start another game: a manifest, bootstrap
                                        and template in DIR (xbr new --help)
  @slug@ bench <command>                drive the Proton bench host (@slug@ bench --help)

`@slug@ <command> --help` for the options. Windows: `@slug@.cmd`, or
`py -3 @slug@.py`. Docs: docs/packaging.md.
"""


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
