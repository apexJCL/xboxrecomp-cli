# How a game finds the CLI

Players and porters never install this repository. A game vendors the small
bootstrap `<slug>.py`, a copy of
[wrapper/game.py](../src/xboxrecomp_cli/wrapper/game.py) (standard library,
Python 3.9 or newer), beside its `<slug>` (sh) and `<slug>.cmd` (Windows)
wrappers. `xbr new` writes the three; `<slug> wrapper --check` compares the
copy with this CLI's template and `<slug> wrapper --print` prints the
template for when a game moves its pin.

## Search order

The bootstrap reads `[cli]` from `game.toml` with a line parser (so `tag`,
`commit` and `url` stay plain one-line strings), then looks for the CLI:

1. `$XBOXRECOMP_CLI_DIR`, used as it is.
2. `external/xboxrecomp-cli` in the game's tree, only at the pin
   (`cli.tag`, locked by `cli.commit` when both are set). A clone the
   bootstrap made (marked `.xbr-pin`) is moved to a new pin; another
   checkout there at another commit is refused, never run unpinned.
3. `../xboxrecomp-cli` beside the game's checkout, used as it is: a
   developer's clone.
4. A clone of `cli.url` into `external/xboxrecomp-cli` at the pin. A tag
   that names another commit than `cli.commit` is refused; a failed clone
   or checkout leaves nothing behind and prints how to get the CLI. A clone
   already at the pin needs no network.

It then runs

```sh
uv run --project <cli> --locked --no-dev xbr --game <root> --prog <slug> ARGS
```

so every message and the help read `<slug> ...`. uv 0.5.31 or newer must be
on `PATH`; `setup` never installs it. Without uv, `<slug> --help` still
prints the help and the uv install command for the host; nothing else runs.

## Pins

A release is an annotated `v*` tag on `main`, never moved once pushed. A
game pins one with the commit it names:

```toml
[cli]
tag = "v0.6.0"
commit = "<the commit v0.6.0 names>"
url = "https://github.com/apexJCL/xboxrecomp-cli.git"
```

The commit locks the tag: a moved tag is refused rather than run. A commit
alone also pins. The toolkit is pinned the same way (`[toolkit] tag`, a
fork's release such as `blinx2-v0.1.0`, or `branch` and `commit`). `<slug>
pins refresh` prints, for a tag pin, the newest release with the same
prefix and warns when the remote's tag is not the lock; the maintainer then
edits `game.toml` by hand. `xbr new` writes the release its checkout is at,
or the newest one on `[cli] url` (`--cli-tag` picks another).

## What the host needs

A developer's or porter's machine (a player's wizard fetches these itself,
[wizard.md](wizard.md)):

| | macOS | Linux | Windows |
|---|---|---|---|
| **uv** 0.5.31+ | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `winget install --id=astral-sh.uv -e` |
| **git** | `xcode-select --install` | the distribution's | [Git for Windows](https://git-scm.com/download/win) |
| **Python** 3.9+ | with the Command Line Tools | the system's | [python.org](https://www.python.org/downloads/), with the `py` launcher |
| **makensis** (the Windows installer only) | `brew install makensis` | `nsis` or `mingw32-nsis` | fetched by `setup` |

`setup` fetches the rest into the game's own tree, every download checked
against a pinned sha256: CMake, Ninja, the toolkit's Python packages
(hashed in `uv.lock`), the toolkit at its pinned commit, llvm-mingw, and a
CPython from uv's own hashed downloads when the host has no 3.12. No Visual
Studio: the Windows executable is cross-compiled with llvm-mingw on every
host, Windows included. About 15 GB free. Nothing it fetches is game data,
and nothing it writes to a public place contains any; a bundle, which has
the dump inside, is for the owner's machines only.

**Windows:** use a short checkout path (`C:\g\mygame`) and run
`git config --global core.longpaths true`; `doctor` warns when the path is
long or `LongPathsEnabled` is off. Commands read `mygame` (the `mygame.cmd`
wrapper; `.\mygame` in PowerShell) or `py -3 mygame.py` instead of
`./mygame`. Defender scans every generated file, so an exclusion for the
checkout folder speeds the build up (the CLI never adds one). Building on a
Windows host works (tested on Windows 11 through the wizard).
