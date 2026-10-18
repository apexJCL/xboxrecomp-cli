# xboxrecomp-cli

**From a disc dump to a game your players can install: one toolchain for
the whole port.** `xbr` is the toolchain I put around the
[xboxrecomp](https://github.com/sp00nznet/xboxrecomp) static recompiler:
it scaffolds a new port, runs the pipeline at pinned versions, builds for
Windows, macOS and the Steam Deck, tests the result against golden frames
and ships a build wizard, so a player who owns the game can build it on
their own computer (on Windows and macOS) without opening a terminal.

```sh
uvx --from git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame --xbe /path/to/game.iso
```

## What it gives you

- **A workbench in one command.** `xbr new` reads your dump (title, title
  ID, entry point, the XDK libraries it links), copies the toolkit's game
  template with those constants filled in, finds D3D's fence wait and
  writes the fence-mirror line into `src/main.c`, and adds the bootstrap,
  a release workflow and a changelog.
- **One pipeline, pinned.** `./mygame all` runs analyze, recomp and build.
  The CLI, the toolkit, CMake, Ninja, llvm-mingw and the Python tools are
  pinned by tag, commit or sha256, so a checkout builds the same way on
  every machine. `gen/` records what it was generated from, and a stale
  one is regenerated or refused, never built by mistake.
- **Every target from one tree.** The Windows executable is cross-compiled
  with llvm-mingw on macOS, Linux or Windows (no Visual Studio). The same
  build runs on the Steam Deck and other Linux PCs under Proton, through
  umu-launcher. macOS builds natively on Apple silicon.
- **Installable bundles.** `./mygame package windows|steamos|macos` makes a
  setup program, a Steam-ready archive with an `install.sh`, or a `.dmg`,
  with your dump inside, for your own computers.
- **A wizard for your players.** `./mygame wizard build` makes a small Go
  and Fyne program for Windows, macOS and Linux, one download each. The
  player points it at their dump, picks the options you offer (render
  scale, fullscreen, ...), and it builds and installs the game for them.
  The builds are reproducible (two builds give the same sha256), and the
  release workflow `xbr new` writes attaches them to every tagged release.
- **Regression tests on real frames.** `golden run` replays scenarios on
  your Mac's Metal build and compares the frames with recorded references;
  `bench golden` does the same for the Direct3D 11 build under Proton on a
  Linux host over ssh, with frame-pacing A/B runs and crash symbolization
  on the side.
- **Your toolkit, upstream or fork.** Pin
  [sp00nznet/xboxrecomp](https://github.com/sp00nznet/xboxrecomp) or my
  fork [apexJCL/xboxrecomp](https://github.com/apexJCL/xboxrecomp/tree/blinx2/portability), which
  adds the Direct3D 11 and Metal renderers, the macOS host and the
  options file. `doctor` says what the pinned toolkit has, and the rest
  degrades cleanly on upstream.
- **No game data, anywhere.** The CLI holds no game's values (a game is a
  folder with a `game.toml`) and nothing it publishes holds game files.
  Every player supplies their own dump.

## Built with xbr

| Game | Repository | Releases | Status |
|---|---|---|---|
| BLiNX 2: Masters of Time & Space | [apexJCL/blinx2-recomp](https://github.com/apexJCL/blinx2-recomp) | [releases](https://github.com/apexJCL/blinx2-recomp/releases) | Plays on Windows 11, on the Steam Deck and Linux (Proton) and on macOS (Apple silicon) |
| Burnout 3: Takedown | [apexJCL/burnout3-recomp](https://github.com/apexJCL/burnout3-recomp) | [releases](https://github.com/apexJCL/burnout3-recomp/releases) | Plays on the Steam Deck and Linux (Proton) and on macOS (Apple silicon); first pre-release with wizards |

To build one from source: clone it and run `./<slug> /path/to/game.iso`.

More are on the way. If you port a game with `xbr`, open an issue and I
will add it here.

## Getting started

### 1. Prepare your machine

You need [uv](https://docs.astral.sh/uv/) 0.5.31 or newer, git and
Python 3.9+ ([per system](docs/bootstrap.md#what-the-host-needs)). `setup`
fetches everything else into the game's own tree. Nothing to install
from this repository: each game's `./<slug>` bootstrap fetches the CLI at
the release it pins ([docs/bootstrap.md](docs/bootstrap.md)).

### 2. Start a port

```sh
uvx --from git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame --xbe /path/to/game.iso
cd mygame
./mygame import /path/to/game.iso   # your dump into game_files/
./mygame setup                      # the pinned toolchain and toolkit
./mygame all                        # analyze, recomp, build
```

Add `@vX.Y.Z` after the repository URL to pin a release
([latest](https://github.com/apexJCL/xboxrecomp-cli/releases/latest)). On
Windows the commands read `mygame` (cmd) or `.\mygame` (PowerShell)
instead of `./mygame`.

`new` pins my toolkit fork by default. For upstream, add
`--toolkit-url https://github.com/sp00nznet/xboxrecomp.git --toolkit-branch main`;
the macOS host, the Direct3D 11 and Metal renderers and the config options
need the fork, and `doctor` lists what the pinned toolkit has.

### 3. Make it run

The first run of a new game crashes; the port starts there. Read the crash
report, seed the functions the disassembler missed, override what the
lifter got wrong, then `./mygame analyze && ./mygame recomp && ./mygame build`.
[docs/porting.md](docs/porting.md) walks through the loop, the manifest
keys a port meets first and the macOS target.

### 4. Package it

Turn packaging on in `game.toml` (the scaffold has the table commented
out) and add the files `package` stages, which `xbr new` does not write:

- `packaging/launch.env.default.windows`: the launcher's defaults;
- `packaging/windows/README.part`: the install section of the bundle's README;
- `packaging/config.default.toml` (on the fork's toolkit): the option
  defaults, including every key a wizard option names;
- `LICENSE` at the game's root.

Then build a bundle:

```toml
[package]
app = "MyGame"
targets = ["windows", "steamos"]
```

```sh
./mygame package windows    # dist/: a setup program
./mygame package steamos    # dist/: a .tar with install.sh --steam
```

The Windows installer needs `makensis` (`doctor` says how to get it); the
macOS `.dmg` takes the game's own templates for now. The keys and the
files they expect: [docs/manifest.md](docs/manifest.md#package).

### 5. Ship the wizard

Add a `[wizard]` table with your public repository and the options players
choose from, then check and build it:

```toml
[wizard]
repo = "https://github.com/you/mygame-recomp.git"

[[wizard.option]]
key = "scale"
label = "Render scale"
kind = "range"
min = 1
max = 4
default = 1
config = "render.scale"
```

Each `config` key must be in `packaging/config.default.toml`, which the
fork's toolkit reads; on an upstream pin the wizard leaves those options
out and keeps the `flag` ones.

```sh
./mygame wizard config --json --dev   # what the binary will embed
./mygame wizard check --dev           # Go gates, then two builds with equal sha256s
./mygame wizard build --dev           # dist/wizard/: <slug>-wizard-*-{macos,windows-x64,linux-x64}
```

The builds need Go (fetched by sha256 from the pins). The macOS asset
needs a Mac host and the Linux asset Docker; elsewhere, build the rest
with `--only windows,linux`. The release workflow below builds all three
on GitHub's runners.

To release, add a `## [1.0.0] - YYYY-MM-DD` section to `CHANGELOG.md`,
commit, then `git tag v1.0.0 && git push origin v1.0.0`. The
`release-wizard.yml` workflow builds the three wizards, checks
`SHA256SUMS` and publishes them on the tag's release with that section as
its notes. [docs/wizard.md](docs/wizard.md) covers the screens, the
config, the build and the workflow; it also has the
[player's guide](docs/wizard.md#for-players) you can link from your own
README.

## Commands

Every command runs through the game's wrapper (`./mygame`), which passes
it to `xbr` with the game's root. `./mygame --help` lists them;
[docs/commands.md](docs/commands.md) has every option.

| Command | What it does |
|---|---|
| `xbr new DIR` | Prepares a new game workbench from your dump |
| `mygame PATH` | Imports a dump and packages it for this computer |
| `mygame import PATH` | Puts a disc image or dump folder in `game_files/` |
| `mygame setup` | Fetches the pinned toolchain and toolkit |
| `mygame doctor` | Says what this host has, what it can package and what to do next |
| `mygame fingerprint [PATH]` | Says whether a dump's XBE is known, differs only in its header, or has modded data or code |
| `mygame clean` | Frees space: lists the bundles and build trees it would delete, deletes with `--yes` |
| `mygame all` | Runs analyze, recomp and build |
| `mygame analyze`, `recomp` | Runs the pipeline's stages |
| `mygame build [windows\|macos]` | Configures once, then compiles incrementally |
| `mygame package <target>` | Makes a private bundle for `windows`, `steamos` or `macos` |
| `mygame wizard <config\|build\|check>` | Builds the player's build wizard for a release |
| `mygame golden <subcommand>` | Records and checks golden frames; `golden run` on the Mac |
| `mygame bench <command>` | Builds and runs on a Proton host over ssh: runs, goldens, pacing |
| `mygame enhance hud` | Makes a HUD texture pack from your own dump |
| `mygame pins refresh` | Moves the toolchain pins and lock, and names newer releases |

## Documentation

| Document | Contents |
|---|---|
| [docs/porting.md](docs/porting.md) | The porting loop, the keys a port meets first, the D3D fence, macOS |
| [docs/commands.md](docs/commands.md) | Every command, option and tool |
| [docs/manifest.md](docs/manifest.md) | `game.toml`, key by key |
| [docs/bundle.md](docs/bundle.md) | What `package` writes: `manifest.json`, patches, each target's layout and applier |
| [docs/wizard.md](docs/wizard.md) | The build wizard: the player's guide, the config, the build, the release workflow |
| [docs/bootstrap.md](docs/bootstrap.md) | How a game finds the CLI, the pins, what the host needs |
| [docs/troubleshooting.md](docs/troubleshooting.md) | The messages and their fixes |
| [docs/maintainers.md](docs/maintainers.md) | Working on the CLI, releases, the audit before a public push |
| [docs/origin.md](docs/origin.md) | Where each file came from |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |

## For players

You don't need this repository. Download the wizard from your game's
**Releases** page (see [Built with xbr](#built-with-xbr)) and follow
[the player's guide](docs/wizard.md#for-players). You need your own,
legally dumped copy of the game; nothing is included or downloaded, and
what is built from it stays on your own computers.

## Credits and licence

Built on the [xboxrecomp](https://github.com/sp00nznet/xboxrecomp) toolkit
by sp00nznet, which [apexJCL/xboxrecomp](https://github.com/apexJCL/xboxrecomp/tree/blinx2/portability)
extends for these ports; its runtime carries code from
[xemu](https://github.com/xemu-project/xemu). The wizard's credits list
every project a build relies on.

MIT, see [LICENSE](LICENSE). Third-party notices: `NOTICES.txt` in each
wizard archive, and [LICENSES/](LICENSES) here. No game is included, and
none ever will be.
