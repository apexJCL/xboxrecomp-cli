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

## Releases

Releases are annotated `v*` tags on `main` (`v0.4.0`), never moved once
pushed; `src/xboxrecomp_cli/__init__.py` carries the version. A game pins a
release by tag and commit ([bootstrap.md](bootstrap.md), "Pins"): the
commit locks the tag, so a moved tag is refused rather than run.

## Before a public push

```sh
scripts/audit-public.sh            # the range origin/main..main
scripts/audit-public.sh A..B       # another range
scripts/audit-public.sh --all      # every tracked file at HEAD, and the whole history
```

It checks what the push would publish for private paths, private host names
and addresses, binaries, game data, and any commit identity other than the
noreply one. The private patterns are not in the repository: they live in
the clone's `.git/info/audit-private`, one extended regex per line (`#`
starts a comment), and the script refuses to run without them. Write them
once per clone:

```sh
printf '%s\n' 'hostname' 'name@example[.]com' > "$(git rev-parse --git-path info/audit-private)"
```

`XBR_AUDIT_PRIVATE=<file>` points the script at another file.

Where each file came from: [origin.md](origin.md).
