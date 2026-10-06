# xboxrecomp-cli

The command line for games built with the
[xboxrecomp](https://github.com/apexJCL/xboxrecomp) static recompilation
toolkit. It handles a game's whole life on a developer's or a player's
machine:
- **Setup:** fetch the pinned toolchain into the game's tree.
- **Generation:** run the toolkit's pipeline (parse, disasm, funcid, abi,
  names, recomp), with a key that says when the generated code is stale.
- **Build:** compile for Windows (llvm-mingw) and macOS.
- **Packaging:** make a private bundle for Windows, SteamOS or macOS, with
  the player's own game files inside.
- **Bench:** drive a Linux/Proton host over ssh to build, run and compare
  golden frames.

A game says everything that is its own in a `game.toml` at its root
([docs/manifest.md](docs/manifest.md)). The CLI holds no game's values.
[BLiNX 2](https://github.com/apexJCL/blinx2-recomp) is the first game that
uses it.

## How a game uses it

Players never install this repository. A game vendors a small bootstrap,
`<slug>.py` (a copy of [wrapper/game.py](src/xboxrecomp_cli/wrapper/game.py)),
next to its `<slug>` and `<slug>.cmd` wrappers. The bootstrap needs only
Python 3.9 and the standard library. It finds the CLI in this order:

1. `$XBOXRECOMP_CLI_DIR`, used as it is.
2. `external/xboxrecomp-cli` in the game's tree, but only at `cli.commit`.
   A clone the bootstrap made (marked `.xbr-pin`) is moved to a new pin.
   Any other checkout there at another commit is refused.
3. `../xboxrecomp-cli` beside the game's checkout, used as it is.
4. Otherwise it clones `cli.url` into `external/xboxrecomp-cli` and checks
   out `cli.commit`. The clone is moved into place only once it is at the
   pin. If the clone or checkout fails (no network, or a pin that was never
   pushed), it leaves nothing behind and prints how to get the CLI.

It then runs:

    uv run --project <cli> --locked --no-dev xbr --game <root> --prog <slug> ARGS

The game's commands and help therefore read `<slug> ...` as before. The
bootstrap needs [uv](https://docs.astral.sh/uv/) 0.5.31 or newer. Without
uv, `<slug> --help` still prints the help (from a CLI it finds; it clones
nothing without uv) and how to install uv. With neither uv nor a CLI, it
prints how to get both: install uv, then clone this repository beside the
game's checkout
(`git clone https://github.com/apexJCL/xboxrecomp-cli.git`) or set
`XBOXRECOMP_CLI_DIR`.

`<slug> wrapper --check` compares a game's copy of the bootstrap with this
CLI's template, and `<slug> wrapper --print` prints the template.

## Working on it

    git clone https://github.com/apexJCL/xboxrecomp-cli.git
    # maintainers: git clone git@github.com:apexJCL/xboxrecomp-cli.git
    cd xboxrecomp-cli
    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .

Clone it beside a game's checkout, or point `XBOXRECOMP_CLI_DIR` at it,
and that game's `./<slug>` runs your copy. `<slug> doctor` shows the
commit you are on and whether it is the one the game pins
(`cli.commit`).

The CLI needs Python 3.12 or newer, and its runtime uses only the standard
library. The dev group has pytest, ruff and numpy (numpy for the audio
check's tests).

The game's developer tools also run through it. Each one reads the game's
paths from `game.toml`:
- `<slug> golden`
- `<slug> pacing-stats`
- `<slug> benchlog-retention`
- `<slug> audio-check` (runs in the game's `.venv`, which has numpy)

## Releases and pins

There are no releases. A game pins a commit of `main` in its `game.toml`,
in the same way it pins the toolkit. `<slug> pins refresh` refreshes the
game's download hashes and prints the toolkit's and this CLI's newest
heads next to the pinned ones. The maintainer then edits the two
`commit` lines by hand.

Before a public push, run `scripts/audit-public.sh` (or
`scripts/audit-public.sh --all`). It checks what the push would publish
for private paths, private host names and addresses, binaries, game
data, and any commit identity other than the noreply one.

The private patterns are not in the repository. They live in the clone's
`.git/info/audit-private`, one extended regex per line (`#` starts a
comment), and the script refuses to run without them. Write the bench
host's name and the personal email there once per clone:

    printf '%s\n' 'hostname' 'name@example[.]com' > "$(git rev-parse --git-path info/audit-private)"

`XBR_AUDIT_PRIVATE=<file>` points the script at another file.

Where each file came from: [docs/origin.md](docs/origin.md).

## License

MIT, see [LICENSE](LICENSE). The games this builds are not included and
never will be: a bundle contains the player's own dump.
