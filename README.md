# xboxrecomp-cli

`xbr` turns a dumped original-Xbox game into a native build with the
[xboxrecomp](https://github.com/apexJCL/xboxrecomp) static recompilation
toolkit: one command sets up the toolchain, lifts the XBE to C, builds the
executable, and (when you want it) packages a private bundle for Windows,
SteamOS or macOS. A game is a directory with a `game.toml`; the CLI holds no
game's values. [BLiNX 2](https://github.com/apexJCL/blinx2-recomp) is the
first game that runs on it, Burnout 3 the second.

**Your dump, your machines.** You supply the dump of a game you own. Nothing
the CLI fetches is game data (the toolkit at a pinned commit, llvm-mingw and
NSIS by sha256, Python wheels hashed in `uv.lock`, and a CPython from uv's
own hashed downloads when the host has no 3.12), nothing it writes to a
public place contains any, and a bundle, which has your dump inside, is for
your own machines only. Enhancement assets (upscaled textures, fonts) are
made locally from your dump and never distributed.

## Prerequisites

| | macOS | Linux | Windows |
|---|---|---|---|
| **uv** | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (into `~/.local/bin`; SteamOS too) | `winget install --id=astral-sh.uv -e` |
| **git** | Command Line Tools: `xcode-select --install` | the distribution's | [Git for Windows](https://git-scm.com/download/win) |
| **Python** | comes with the tools (3.9+ runs the bootstrap) | the system's | python.org's, with the `py` launcher |
| **For the Windows installer** | `brew install makensis` | `nsis` / `mingw32-nsis` from the distribution | fetched by `setup` |

That is all. `setup` fetches the rest into the game's own tree: CMake, Ninja,
the toolkit's Python packages (from the game's `uv.lock`), llvm-mingw and the
toolkit, every download checked against a pinned sha256. No Visual Studio:
the Windows executable is cross-compiled with llvm-mingw on every host,
Windows included. About 15 GB free.

**Windows notes.** Use a short checkout path (`C:\g\mygame`): the build nests
deep. Run `git config --global core.longpaths true`; `doctor` warns when the
path is long or `LongPathsEnabled` is off. Defender scans every generated
file, so an exclusion for the checkout folder speeds the build up (the CLI
never adds one). The commands below read `mygame` instead of `./mygame`
(the `mygame.cmd` wrapper; `.\mygame` in PowerShell), or `py -3 mygame.py`. Building on a Windows host
is supported and not yet verified on a real machine; macOS and Linux are.

## Quickstart: a new game in five steps

```sh
# 1. uv and git (above). Then, from the directory your game will live in:
uvx --from git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame
# 2. put your dump (default.xbe and the game's files) in mygame/game_files/
cd mygame
./mygame setup          # .venv, llvm-mingw, the toolkit (ends with doctor)
./mygame all            # analyze (parse, disasm, funcid, abi), recomp, build
```

`xbr new` writes a complete starter game and nothing else: `game.toml` with
every pin filled in (the CLI release it ran from, or its commit; the
toolkit branch head),
the `mygame` bootstrap and its wrappers, the tools environment
(`pyproject.toml`, `uv.lock`), `.gitignore` (the dump, the generated code,
the toolchain and the bundles never enter git), the toolkit's
`templates/new-game/` (CMakeLists.txt, `src/main.c`, `src/recomp_manual.c`)
with your game's constants patched in, and `config/setup-pins.json`. Put the
dump in `game_files/` first (or pass `--xbe PATH`) and the title, title ID
and entry point come from the XBE header; the dump is read, never copied.
Without network, git or uv, each piece it could not finish is named as the
command that will (`setup`, `pins refresh`); `--offline` skips them on
purpose. `xbr new --help` lists the options (`--slug`, `--name`,
the pins).

The first build is `build-win/mygame_recomp.exe`, a Windows x86-64
executable that runs natively on Windows and under Proton on Linux (D3D11).
On a Mac it is the file you carry to one of those. "macOS as a target"
below says what the macOS build needs.

To read before running: the uvx line installs the CLI's `main` into uv's
cache and runs it; `@<tag>` or `@<sha>` after the repository pins a release
or a commit of `main` (`…/xboxrecomp-cli@v0.2.0 xbr new mygame`; any commit
that has `new`). The no-pipe way is the clone:

```sh
git clone https://github.com/apexJCL/xboxrecomp-cli.git
cd xboxrecomp-cli && uv run xbr new ../mygame
```

A game made beside a clone runs that clone (the bootstrap's search order,
below), so this is also how you work on the CLI.

## Port your first game

The first run of a recompiled game crashes. That is where the port starts,
and the loop is short:

1. **Run it** (on Windows, or `./mygame bench run` on a Proton host, below)
   and read the crash report: the faulting guest function, the guest return
   addresses, the last indirect-call targets.
2. **Seed the functions the disassembler missed.** An ICALL failure names a
   target the lifter did not know was a function. Add it to
   `config/seed_functions.json` (`{"start": "0x...", "source": "...",
   "observed": true}`), then `./mygame analyze` and `./mygame recomp`:
   `recomp` alone misses new seeds. Never edit `src/recomp/gen/` by hand;
   it is regenerated, and `src/recomp/gen.key.json` records what from (the
   XBE, the toolkit's tools, the seed files, the exact stage commands), so
   `package` knows when it is stale and `build` refuses it. Seeds marked
   `"observed"` also reach `recomp` (`--seeds`), so its flag-fallback report
   lists the sites inside them.
3. **Override what the lifter got wrong** in `src/recomp_manual.c`:
   `recomp_lookup_manual()` runs before the generated dispatch, so a
   function can be wrapped, stubbed or replaced by a native one.
4. **Add what the runtime lacks.** A kernel call, a D3D path or an input
   detail the toolkit does not have yet is toolkit work: the fork's
   branch is `blinx2/portability`, and `XBOXRECOMP_DIR` points `setup`,
   `build` and the bench at your own checkout (a checkout the bootstrap did
   not clone is never moved to the pin).
5. **Build again:** `./mygame build` is incremental.

The toolkit's docs carry the long form: its README's Quick Start and
`docs/GETTING_STARTED.md` (Step 8, Debug Iteratively), `docs/pipeline/`,
and `docs/technical/indirect-calls.md` for ICALLs. Its `tools/doctor.py`
checks the pipeline's environment.

Every setting a game has is a key in `game.toml`:
[docs/manifest.md](docs/manifest.md) documents each one, with its default.
The ones a port meets first: `pipeline.disasm.extra_sections` (code outside
`.text`, such as the XDK library sections), `pipeline.split`,
`pipeline.spin_waits`, `xbe.sha256` (the dumps the goldens were recorded
on), and `[package]`, which turns packaging on.

### Environment and enhancements

The runtime reads its environment through one table (`recomp_env`;
`RECOMP_*` keys in three tiers: config, trace, debug). A game adds its own
keys in a header named by `game.env_header` and documents them in
`game.env_doc`. The toolkit's opt-in enhancements layer (`XBOXRECOMP_ENHANCE`,
render scale, present filter, pacing, read from `enhance.toml`) is off by
default and never changes stock behaviour; a game that builds it lists the
option in `build.stock_cmake`.

### macOS as a target

The toolkit builds for macOS (Apple silicon, Metal) and BLiNX 2 runs there,
but the toolkit's template `main.c` is Win32 (WinMain, the VEH crash
handler, dbghelp). A game that lists `macos` in `build.targets` needs a
`main.c` with the POSIX host code: BLiNX 2's `src/main.c` has it
(`host_main`, the signal handlers, the SDL window), and a portable template
is a toolkit follow-up. Until then a Mac is a build host for the windows
target and a run host for nothing, unless you port the host code.

## Commands

`./mygame --help` prints them; `./mygame <command> --help` the options.

| Command | What it does |
|---|---|
| `./mygame` | package for this computer (macos on a Mac, steamos on Linux, windows on Windows): sets up, generates and builds whatever is missing or stale |
| `./mygame package windows\|steamos\|macos` | the same for a target, into `dist/`; needs `[package]` in `game.toml` |
| `./mygame doctor` | what this host has, what it can package, and `next:` the first thing to do |
| `./mygame setup [--dev] [--no-toolkit]` | fetch the pinned toolchain into this tree; `--dev` adds pytest, ruff and the toolkit's test dependencies |
| `./mygame analyze` | parse, disasm, funcid, abi, and `names` when a Ghidra export exists |
| `./mygame recomp` | lift x86 to C into `pipeline.gen` |
| `./mygame all` | analyze, recomp, then build for this host's default target |
| `./mygame build [windows\|macos]` | configure once, then compile incrementally; refuses a stale `gen/` (`--stale-gen-ok` builds anyway); `--system-tools` uses the host's cmake and ninja |
| `./mygame parse\|disasm\|funcid\|abi\|ghidra\|names` | one stage, with extra arguments passed to the tool |
| `./mygame pins refresh` | maintainers: re-pin the downloads and the lock, print the newest heads |
| `./mygame new DIR` | start another game |
| `./mygame bench <command>` | the Proton bench host (below) |
| `./mygame golden`, `pacing-stats`, `benchlog-retention`, `audio-check` | the developer tools, each with the game's paths from `game.toml` |

`golden check` fails a run whose `[ENHANCE]` line shows a non-stock value
for a toolkit key or one of the game's own (`golden.enhance_stock` in
`game.toml`); `golden prune SCEN=DIR` drops the flip dumps a passing check
did not read, for runs made by hand.

`golden run [SCEN...]` is the golden pass on a Mac's own build (Metal;
`--backend cpu` for the CPU path): each scenario runs headless with dummy
audio under the Mac run lock (`~/.recomp-mac-run.lock`), dumps its frames
by flip, and stops as soon as the last frame its check reads is dumped
(from the anchor it has seen, not a fixed flip count). It warns when the
Mac is loaded (the pace check then says INCOMPLETE), then checks and prunes
like `bench golden`. Each run is `bench-logs/<stamp>-metal-<scen>/` with
its `golden.txt` verdict.

`wrapper --check` compares the game's bootstrap with the CLI's template and
`wrapper --print` prints the template, for when a game moves its CLI pin.

### How a game finds the CLI

Players and porters never install this repository. A game vendors the small
bootstrap `<slug>.py` (a copy of
[wrapper/game.py](src/xboxrecomp_cli/wrapper/game.py); standard library,
Python 3.9+) beside its `<slug>` and `<slug>.cmd` wrappers. It finds the CLI
in this order, then runs `uv run --project <cli> --locked --no-dev xbr
--game <root> --prog <slug> …`:

1. `$XBOXRECOMP_CLI_DIR`, used as it is.
2. `external/xboxrecomp-cli` in the game's tree, only at the pin
   (`cli.tag`, locked by `cli.commit` when both are set). A clone the
   bootstrap made (marked `.xbr-pin`) is moved to a new pin; another
   checkout there at another commit is refused.
3. `../xboxrecomp-cli` beside the game's checkout, used as it is.
4. A clone of `cli.url` into `external/xboxrecomp-cli` at the pin. A tag
   that names another commit than `cli.commit` is refused, and a failed
   clone or checkout leaves nothing behind and prints how to get the CLI.
   A clone already at the pin needs no network.

Without uv, `<slug> --help` still prints the help and the uv install
command for the host.

## The bench host (optional, advanced)

The Windows build is tested under Proton. `./mygame bench` drives a Linux
host over ssh: it syncs the tree, builds there, runs the game with scripted
input, and compares frames against the goldens in `golden.json`. Host
settings (`BENCH_HOST`, `BENCH_DIR`, the Proton prefix) come from the
environment or the game's `scripts/bench.env`; `./mygame bench --help` lists
the commands (`sync`, `build`, `run`, `golden`, `integrate`, `pacing`,
`symbolize`, …) and `[bench]` in `docs/manifest.md` the per-game keys. A
game with no `golden.json` can still `bench build` and `bench run`.

`bench golden` keeps, for a scenario that passes, only the frames its check
read (`--keep-frames` or `BENCH_KEEP_FRAMES=1` keeps them all). Each
scenario's checks and verdict stay in its run's `golden.txt`, and every pass
adds a line to `bench-logs/golden-sessions.tsv`; `--only SCEN` reruns one
scenario. The toolkit's Proton tests that `golden` starts with are skipped
when the host's toolkit tree, the CLI, the toolchain and the Proton and
prefix versions are those of their last pass on that host tree
(`--tests` runs them anyway).
`bench gc` lists the runs in `bench-logs/` that nothing names any more
(not in `golden.json`, `TASKS.md`, `RESUME*.md`, `openspec/` or
`[bench.gc] refs`, in any worktree; not another game's, not young, not
among the newest per scenario) with their sizes, and removes them with
`--apply`; `--host` does the same for the host's store.

## Troubleshooting

- **`no uv 0.5.31+ on PATH`**: install it (the table above) and open a new
  shell. `<slug> --help` works without it; nothing else does.
- **`xboxrecomp-cli: the pin moved` / `git checkout failed` / `git fetch
  failed`**: the game pins a CLI commit or tag the remote does not have (a
  pin that was never pushed), or there is no network for a new pin.
  Clone the CLI beside the game or set `XBOXRECOMP_CLI_DIR`, and fix
  `[cli]`.
- **`tag v0.2.0 is …, not the pinned commit …: the tag moved`**: the tag
  and the `commit` that locks it disagree. Either the tag was moved or
  re-pushed (check the release), or `game.toml` names the wrong pair. The
  same holds for `[toolkit]` in `setup`.
- **`no config/setup-pins.json`**: `./mygame pins refresh` writes it (and
  `uv.lock`); it needs the network.
- **`uv lock --check failed`**: `uv.lock` is out of date with
  `pyproject.toml`: `./mygame pins refresh`, or `uv lock`, then commit it.
- **`llvm-mingw ... quarantined`** (macOS): `xattr -dr com.apple.quarantine
  third_party/llvm-mingw-*`, as `doctor` prints.
- **`makensis: missing`**: only the windows installer needs it; `doctor`
  prints the install command for your system, including the toolbox route
  on an immutable Linux.
- **`the template changed`** (from `xbr new`): the toolkit's
  `templates/new-game/` no longer has the line the scaffold patches; the
  message names the edit to make by hand in `CMakeLists.txt` or
  `src/main.c`.
- **`implicit declaration of function 'recomp_dispatch_init'`**: an older
  template; add `extern int recomp_dispatch_init(void);` to `src/main.c`
  (`xbr new` does).
- **`title ID 0x... is not ...`**: the dump in `game_files/` is another
  game, or `xbe.title_id` is still `0` from a scaffold made without the
  dump.
- **`gen/ is stale: ...`** (from `build` or `bench integrate`): the toolkit's
  lifter, a seed file or `recomp_manual.c` changed since `recomp`; run
  `./mygame analyze && ./mygame recomp`. `--stale-gen-ok` builds anyway.
- **`gc: reference sources missing`**: a path in `[bench.gc] refs` (or
  `TASKS.md`, `openspec/`, `golden.json`, the pipeline's seeds and
  spin-wait files) is missing in the main checkout;
  gc will not guess from a partial set. Fix the path or the key. The
  refs can name the maintainer's own notes outside the repo, so in a plain
  public clone gc refuses until those are set to paths that exist there.
- **`gen/ is being regenerated`**: the last `recomp` failed or is still
  running; run it again.
- **Windows: a path error deep in the build**: the checkout path is long;
  move it to `C:\g\<slug>` and enable long paths (`doctor` says which).

## Maintainers

### Working on the CLI

```sh
git clone https://github.com/apexJCL/xboxrecomp-cli.git
# maintainers: git clone git@github.com:apexJCL/xboxrecomp-cli.git
cd xboxrecomp-cli
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Clone it beside a game's checkout, or point `XBOXRECOMP_CLI_DIR` at it, and
that game's `./<slug>` runs your copy; `doctor` shows the commit you are on
and whether it is the one the game pins. The CLI needs Python 3.12 or
newer, and its runtime uses only the standard library. The dev group has
pytest, ruff and numpy (numpy for the audio check's tests). The test that
reads the toolkit's real template runs when a toolkit checkout is beside
this one or `XBOXRECOMP_DIR` names one.

### Releases and pins

Releases are annotated `v*` tags on `main` (`v0.2.0`), never moved once
pushed. A game pins one in its `game.toml` with the commit it names:

```toml
[cli]
tag = "v0.2.0"
commit = "<the commit v0.2.0 names>"
```

The commit locks the tag, so a moved tag is refused rather than run. The
toolkit is pinned the same way (`[toolkit] tag`, a fork's release such as
`blinx2-v0.1.0`), or by `branch` and `commit` as before; a commit alone
still pins the CLI. `<slug> pins refresh` refreshes the game's download
hashes and `uv.lock` and prints, for a tag pin, the newest release with the
same prefix (and a warning when the remote's tag is not the lock), else the
branch's newest head; the maintainer then edits the pins by hand.
`xbr new` writes the release its checkout is at, or the newest one on
`[cli] url` (`--cli-tag` picks another).

### Before a public push

Run `scripts/audit-public.sh` (or `scripts/audit-public.sh --all`). It
checks what the push would publish for private paths, private host names
and addresses, binaries, game data, and any commit identity other than the
noreply one.

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
