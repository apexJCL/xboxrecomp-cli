# Maintainers

## Working on the CLI

```sh
git clone https://github.com/apexJCL/xboxrecomp-cli.git
cd xboxrecomp-cli
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Clone it beside a game's checkout, or point `XBOXRECOMP_CLI_DIR` at it, and
that game's `./<slug>` runs your copy; `doctor` shows the commit you are on
and whether it is the one the game pins. The CLI needs Python 3.12 or newer
and its runtime uses only the standard library. The dev group has pytest,
ruff and numpy (numpy for the audio check's tests). The test that reads the
toolkit's real template runs when a toolkit checkout is beside this one or
`XBOXRECOMP_DIR` names one.

## Releasing

Releases are annotated `v*` tags on `main` (`v0.6.0`), never moved once
pushed; `src/xboxrecomp_cli/__init__.py` carries the version. A game pins a
release by tag and commit ([bootstrap.md](bootstrap.md), "Pins"): the
commit locks the tag, so a moved tag is refused rather than run. Every
pushed tag gets a GitHub Release whose notes are its section of
[CHANGELOG.md](../CHANGELOG.md).

1. **The release commit** (`release: X.Y.Z, <what it brings>`) bumps
   `__version__` and moves the `[Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section, with its compare link at the bottom
   and an empty `[Unreleased]` above it. Write the entries for the people
   who use the CLI, grouped Added, Changed, Fixed, Removed; no shas. The
   tests fail when `__version__` has no section.
2. **Tag** that commit: `git tag -a vX.Y.Z -m "vX.Y.Z: <what it brings>"`.
3. **Push** after the audit below: `git push origin main vX.Y.Z`.
4. **Publish** the release from the changelog:

   ```sh
   gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" \
     --notes-file <(scripts/release-notes.py X.Y.Z)
   ```

   `scripts/release-notes.py` prints the section and exits 1 when there is
   none, so a missing entry stops the command before anything is
   published. With several tags in one push round, publish them oldest
   first, so the newest is the one GitHub marks Latest.

A tag that predates CHANGELOG.md (v0.7.3 and earlier) gets its release the
same way from main's CHANGELOG.md, oldest first: the `gh release create`
line above with that version, or `gh release edit vX.Y.Z --notes-file
<(scripts/release-notes.py X.Y.Z)` for a release that already exists.

The release is made by hand rather than by a workflow on the tag: the CLI
has no assets to build, so the command above is the whole job, and the
repository keeps no workflow that holds a write token. A game's own
release workflow (the one `xbr new` writes) builds and attaches the wizard
and takes its notes from the game's changelog the same way.

## Before a public push

```sh
scripts/audit-public.sh            # the range origin/main..main
scripts/audit-public.sh A..B       # another range
scripts/audit-public.sh --all      # every tracked file at HEAD, and the whole history
```

It checks what the push would publish for private paths, private host names
and addresses, binaries, game data, and any commit identity other than the
noreply one. The private patterns are not in the repository: one extended
regex per line (`#` starts a comment), matched without case, and the script
refuses to run without them. It reads the first of these files that holds
a pattern (a file of comments counts as missing):

1. `$XBR_PUBLIC_DIR/audit-private`: the folder the games' `public audit`
   and `public export` read too ([layout.md](layout.md#private-inputs)), so
   one file serves the CLI and every game;
2. `$XBR_AUDIT_PRIVATE`;
3. the clone's `.git/info/audit-private`.

To keep them per clone instead:

```sh
printf '%s\n' 'hostname' 'name@example[.]com' > "$(git rev-parse --git-path info/audit-private)"
```

The same folder holds the games' `rewrite` rules (the format is in
[layout.md](layout.md#the-rewrite-format)); this script reads only the
patterns, since the CLI's commits are pushed as they are.

`scripts/audit-public-allowed-messages` lists the commits already on the
public `main` whose messages match a pattern added after they were pushed.
Their messages are skipped, since published history is not rewritten; their
files and identities are still checked.

Where each file came from: [origin.md](origin.md).
