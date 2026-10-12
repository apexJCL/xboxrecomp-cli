# xboxrecomp-cli

`xbr` turns a dumped original-Xbox game into a native build with the
[xboxrecomp](https://github.com/apexJCL/xboxrecomp) static recompilation
toolkit: one command sets up the toolchain, lifts the XBE to C, builds the
executable and packages a private bundle for Windows, SteamOS or macOS.
A game is a directory with a `game.toml`; the CLI holds no game's values.
[BLiNX 2](https://github.com/apexJCL/blinx2-recomp) is the first game that
runs on it, Burnout 3 the second.

**Your dump, your machines.** You supply the dump of a game you own. Nothing
the CLI fetches is game data (the toolkit at a pinned commit, llvm-mingw and
NSIS by sha256, Python wheels hashed in `uv.lock`, and a CPython from uv's
own hashed downloads when the host has no 3.12), nothing it writes to a
public place contains any, and a bundle, which has your dump inside, is for
your own machines only. Enhancement assets (upscaled textures, fonts) are
made locally from your dump and never distributed.

## Installation

Players and porters do not install this repository: a game's `./<slug>`
bootstrap fetches the CLI at the release the game pins
([docs/bootstrap.md](docs/bootstrap.md)). The host needs:

| | macOS | Linux | Windows |
|---|---|---|---|
| **uv** 0.5.31+ | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `winget install --id=astral-sh.uv -e` |
| **git** | `xcode-select --install` | the distribution's | [Git for Windows](https://git-scm.com/download/win) |
| **Python** 3.9+ | with the Command Line Tools | the system's | [python.org](https://www.python.org/downloads/), with the `py` launcher |
| **makensis** (the Windows installer only) | `brew install makensis` | `nsis` or `mingw32-nsis` | fetched by `setup` |

`setup` fetches the rest into the game's own tree, every download checked
against a pinned sha256: CMake, Ninja, the toolkit's Python packages, the
toolkit, llvm-mingw. No Visual Studio: the Windows executable is
cross-compiled with llvm-mingw on every host, Windows included. About 15 GB
free.

**Windows:** use a short checkout path (`C:\g\mygame`) and run
`git config --global core.longpaths true`; `doctor` warns when the path is
long or `LongPathsEnabled` is off. Commands read `mygame` (the `mygame.cmd`
wrapper; `.\mygame` in PowerShell) or `py -3 mygame.py` instead of
`./mygame`. Defender scans every generated file, so an exclusion for the
checkout folder speeds the build up (the CLI never adds one). Building on a
Windows host is supported and not yet verified on a real machine.

## Quick start

```sh
uvx --from git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame
cd mygame
./mygame import /path/to/game.iso   # or the dump's folder, or copy it to game_files/
./mygame setup     # .venv, llvm-mingw, the toolkit; ends with doctor
./mygame all       # analyze, recomp, build: build-win/mygame_recomp.exe
```

`xbr new` prepares a game's workbench: `game.toml` with every pin filled in,
the `mygame` bootstrap and its wrappers, the tools environment
(`pyproject.toml`, `uv.lock`), a `.gitignore` that keeps the dump, the
generated code, the toolchain and the bundles out of git, the toolkit's
`templates/new-game/` with the game's constants, and `config/setup-pins.json`.
The title, title ID and entry point come from the XBE header, which is read
and never copied. `@<tag>` after the repository URL pins a release, `@<sha>`
a commit (`…/xboxrecomp-cli@v0.6.0 xbr new mygame`); the no-pipe way is a
clone and `uv run xbr new ../mygame`.

The first build is a Windows x86-64 executable that runs natively on Windows
and under Proton on Linux (Direct3D 11). The first run crashes; the port
starts there ([docs/porting.md](docs/porting.md)).

## Commands

`./mygame --help` lists them; `./mygame <command> --help` the options. Full
reference: [docs/commands.md](docs/commands.md).

| Command | Summary |
|---|---|
| `xbr new DIR` | a new game's workbench |
| `mygame PATH` | import the disc image or dump folder at `PATH`, then the same as `mygame` |
| `mygame` | package for this computer, building whatever is missing or stale |
| `mygame package <target>` | the same for `windows`, `steamos` or `macos`, into `dist/` (`--dump PATH` imports first) |
| `mygame import PATH` | put the dump in `game_files/` from a disc image or a folder, checking its title first |
| `mygame doctor` | what this host has, what it can package, and the next thing to do |
| `mygame enhance hud` | the owner's HUD texture pack from their own dump (`package --hud-pack` bundles it) |
| `mygame setup` | fetch the pinned toolchain into the game's tree |
| `mygame analyze` / `recomp` / `all` | the pipeline: parse, disasm, funcid, abi, names; lift to C; then build |
| `mygame build [windows\|macos]` | configure once, compile incrementally |
| `mygame pins refresh` | maintainers: re-pin the downloads and the lock |
| `mygame bench <command>` | build, run and golden-test the Windows build under Proton on a remote Linux host |
| `mygame golden` / `pacing-stats` / `benchlog-retention` / `audio-check` / `wrapper` | developer tools, each with the game's paths from `game.toml` |

## The manifest

Every setting a game has is a key in `game.toml`:
[docs/manifest.md](docs/manifest.md) documents each table and key with its
default, from the XBE's title ID and the known dumps (`[xbe]`) through the
pipeline, the build, the data folders, packaging and the bench.

## macOS

The toolkit builds for macOS (Apple silicon, Metal) and BLiNX 2 runs there,
but the toolkit's template `main.c` is Win32. A game that lists `macos` in
`build.targets` needs a `main.c` with the POSIX host code; until a portable
template exists, a Mac is a build host for the windows target
([docs/porting.md](docs/porting.md#macos-as-a-target)).

## The bench host

`mygame bench` drives a Linux x86-64 machine over ssh: it syncs the tree,
builds there in a distrobox, runs the game under Proton with scripted input
and compares frames against the goldens in `golden.json`. Configuration
(`BENCH_HOST`, `BENCH_DIR`, the Proton prefix) comes from the environment or
the game's `scripts/bench.env`. Optional; a game with no `golden.json` can
still `bench build` and `bench run`. Commands and variables:
[docs/commands.md](docs/commands.md#mygame-bench-command).

## Documentation

| Document | Contents |
|---|---|
| [docs/commands.md](docs/commands.md) | every command, option and tool |
| [docs/manifest.md](docs/manifest.md) | `game.toml`, key by key |
| [docs/porting.md](docs/porting.md) | the porting loop, the keys a port meets first, environment and enhancements, macOS |
| [docs/bootstrap.md](docs/bootstrap.md) | how a game finds the CLI, the pins |
| [docs/troubleshooting.md](docs/troubleshooting.md) | the messages and their fixes |
| [docs/maintainers.md](docs/maintainers.md) | working on the CLI, releases, the audit before a public push |
| [docs/origin.md](docs/origin.md) | where each file came from |

## Contributing

```sh
git clone https://github.com/apexJCL/xboxrecomp-cli.git
cd xboxrecomp-cli
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

A clone beside a game's checkout (or `XBOXRECOMP_CLI_DIR`) is the CLI that
game runs. Python 3.12 or newer; the runtime uses only the standard library.
Releases are annotated `v*` tags on `main`, never moved; run
`scripts/audit-public.sh` before a push ([docs/maintainers.md](docs/maintainers.md)).

## License

MIT, see [LICENSE](LICENSE). The games this builds are not included and
never will be: a bundle contains the player's own dump.
