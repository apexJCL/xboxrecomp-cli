#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md, the body of its GitHub release.

  scripts/release-notes.py 0.7.4            (or v0.7.4)
  scripts/release-notes.py 0.7.4 --changelog PATH

The section is everything under `## [0.7.4] - DATE` up to the next `## `
heading, without the heading itself (the release's title names the
version). Exit 1 with the reason when the version has no section or the
section is empty, so a release is never published with blank notes.
Standard library only.
"""

import argparse
import os
import re
import sys

HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def section(text, version):
    """The body of VERSION's section, stripped, or None when it has none."""
    version = version.removeprefix("v")
    lines = None
    for line in text.splitlines():
        m = HEADING.match(line)
        if lines is not None and (m or line.startswith("## ")):
            break
        if m and m.group("version").removeprefix("v") == version:
            lines = []
        elif lines is not None:
            lines.append(line)
    if lines is None:
        return None
    # Link reference definitions sit after the last section; they are not notes.
    body = "\n".join(ln for ln in lines if not re.match(r"^\[[^\]]+\]: ", ln))
    return body.strip()


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version", help="the release, with or without its leading v")
    ap.add_argument(
        "--changelog",
        default=os.path.join(os.path.dirname(here), "CHANGELOG.md"),
        help="the changelog to read (default: the repository's CHANGELOG.md)",
    )
    args = ap.parse_args(argv)
    try:
        with open(args.changelog, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print("release-notes: cannot read %s: %s" % (args.changelog, e.strerror), file=sys.stderr)
        return 1
    body = section(text, args.version)
    if body is None:
        print(
            "release-notes: %s has no section for %s (a '## [%s] - DATE' heading)"
            % (args.changelog, args.version, args.version.removeprefix("v")),
            file=sys.stderr,
        )
        return 1
    if not body:
        print(
            "release-notes: the %s section of %s is empty" % (args.version, args.changelog),
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
