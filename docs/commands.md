# Command reference

Every command runs through the game's wrapper (`./mygame` on macOS and
Linux, `mygame` or `mygame.cmd` on Windows, `py -3 mygame.py` anywhere),
which passes it to `xbr` with the game's root and name. `./mygame --help`
prints the player and developer commands; `./mygame <command> --help` the
options of one. A game is a directory with a `game.toml`
([manifest.md](manifest.md)).

## Player commands

### `mygame`

Package for this computer: `macos` on a Mac, `steamos` on Linux, `windows`
on Windows (the first of `package.targets` the host can make when the game
has no bundle for the host). Runs `setup`, the pipeline and the build first
when something is missing or out of date, after printing a plan of the
steps and why each runs.

### `mygame package <windows|steamos|macos>`

A private bundle for one target in `dist/`. Needs `[package]` in `game.toml`.

| Option | Effect |
|---|---|
| `--no-build` | package the existing build as it is |
| `--allow-debug` | package a non-Release build |
| `--allow-nonstock` | package with a non-stock CMake value (`build.nonstock_vars`, or the enhancements layer off); recorded in `manifest.json` |
| `--archive` | windows: also a stored `.zip` of the folder |
| `--hud-pack [DIR]` | put the HUD texture pack (default `dist/enhance/hud/`) in the bundle and enable it in its `config.default.toml`, which the game reads on first installs and upgrades alike |
| `--out OUT` | output directory (default `dist/`) |
| `--plain` | plain line output, no live progress (also when not a terminal, or `CI=1`) |
| `--verbose` | show every tool's output as it runs (implies `--plain`); it always goes to `build-logs/` too |
| `--no-setup` | never run `setup`, even when something is missing |
| `--reconfigure` | configure the packaging build tree afresh |
| `--system-tools` | use the host's cmake and ninja instead of the venv's |

`package` builds in its own tree (`build-pkg-win/`, `build-pkg-macos/`) with
the stock options from `build.stock_cmake`, so the developer's `build/`
keeps its settings. It refuses a missing or foreign dump (`xbe.title_id`),
an empty `gen/` (and, with `--no-build`, one being regenerated), and a Debug
or non-stock build without the override; a stale `gen/` is regenerated
first. A dump whose `default.xbe` is not in `xbe.sha256` packages with a
warning and `"xbe": "unknown"` in the manifest.

### `mygame doctor`

What this host has (uv, the venv, llvm-mingw, makensis, the toolkit and CLI
pins, the dump, `gen/`), which targets it can package, and `next:` the first
thing to do. Exit 1 when the game's steamos target is blocked, or when
anything is missing for a game without one.

### `mygame enhance hud`

The HUD texture pack from the owner's own dump, into `dist/enhance/hud/hud.pack`.
Needs `[enhance.hud]` in `game.toml`.

| Option | Effect |
|---|---|
| `--scale {2,4}` | upscale factor (default: `game.toml`'s) |
| `--out OUT` | output directory (default `dist/enhance/hud/`) |
| `--font FONT` | a TrueType file (`.ttf`, `.ttc`) or an installed font's full name for the Latin atlas (default: `game.toml`'s `font`, if installed) |
| `--jobs JOBS` | processes for the upscale (default: the CPUs) |
| `--only PATTERN` | narrow the set to matching entries (repeatable) |
| `--language LANG` | a language of `[enhance.hud] languages` to build the text of (repeatable; default: the first listed) |
| `--list` | print the set and its hashes, write nothing |

A pack is made from game data and is never published; `package --hud-pack`
puts it in the owner's private bundle.

### `mygame setup`

Fetch the pinned toolchain into the game's tree: `.venv/` from `uv.lock`
(CMake, Ninja, the toolkit's Python packages), llvm-mingw (and NSIS on
Windows) checked against `config/setup-pins.json`, and the toolkit at its
pin. Ends with `doctor`.

| Option | Effect |
|---|---|
| `--dev` | also the dev group: pytest, ruff and the toolkit's test dependencies |
| `--no-toolkit` | do not clone the toolkit |
| `--force` | fetch the toolchain again |

## Developer commands

### The pipeline

| Command | Stage |
|---|---|
| `mygame analyze` | `parse`, `disasm`, `funcid`, `abi`, then `names` when a Ghidra export exists |
| `mygame recomp` | lift x86 to C into `pipeline.gen` |
| `mygame all [--system-tools]` | `analyze`, `recomp`, then `build` for the host's default target |
| `mygame parse\|disasm\|funcid\|abi\|ghidra\|names [-- ARGS]` | one stage; extra arguments go to the stage's tool |

`src/recomp/gen.key.json` records what `gen/` was generated from (the XBE,
the toolkit's tools, the seed files, the stage commands). `build` refuses a
stale `gen/` and `package` regenerates it. Never edit `gen/` by hand: a
function the disassembler missed goes in a seed file (`pipeline.seeds`),
then `analyze` and `recomp` again (`recomp` alone misses new seeds).

### `mygame build [windows|macos]`

Configure once, then compile incrementally: `build.windows_dir` (default
`build-win/`) or `build.macos_dir` (default `build/`). Default target: macos
on a macOS host, windows elsewhere.

| Option | Effect |
|---|---|
| `--system-tools` | use the host's cmake and ninja instead of the venv's |
| `--stale-gen-ok` | build even when `gen/` is stale against its key |
| `-- ARGS` | extra CMake arguments |

### `mygame pins refresh`

Maintainers: rewrite `config/setup-pins.json` (the llvm-mingw and NSIS
downloads, by sha256) and `uv.lock`, and print the newest release or branch
head for each `[cli]` and `[toolkit]` pin. Needs the network.

### `xbr new DIR`

Start another game: `game.toml`, the bootstrap and its wrappers, the tools
environment, the toolkit's `templates/new-game/` with the game's constants,
the toolkit clone and the pins. Runs without a game (`uvx --from
git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame`, or `mygame
new DIR` from an existing game). Whatever it could not finish (offline, or
with no network) it names as the later command that does it: `setup` for the
toolkit clone (and the template copy that follows it), `pins refresh` for
the pins and the lock.

| Option | Effect |
|---|---|
| `--slug SLUG` | the command name, `[a-z0-9-]` (default: `DIR`'s name) |
| `--name NAME` | the game's name (default: the XBE certificate's title) |
| `--xbe XBE` | read the title, title ID and entry point from this dump (default: `DIR/game_files/default.xbe` if it is there); never copied |
| `--toolkit-url`, `--toolkit-branch`, `--toolkit-commit` | the toolkit pin (default: a checkout beside `DIR`, else the branch head) |
| `--cli-tag TAG`, `--cli-commit SHA` | the CLI pin (default: the release this CLI runs at, else its commit) |
| `--llvm-mingw TAG` | the llvm-mingw release |
| `--offline` | write the files only: no clone, no pins, no lock |

### `mygame bench <command>`

Drive the Proton bench host: a Linux x86-64 machine reached over ssh that
builds the Windows build in a distrobox and runs it under Proton. `mygame
bench --help` is the full text; the commands:

| Command | What it does |
|---|---|
| `setup` | once per host: the build distrobox, cmake, ninja, umu-launcher, llvm-mingw |
| `sync [--game-files[=reflink]]` | rsync the toolkit and the game to the host; `--game-files` sets up the host's one `game_files/` copy |
| `build [-- CMAKE ARGS]` | configure and compile `build-win/` on the host (shared run lock) |
| `run [GAME ARGS]` | launch the game under Proton, log to `bench-logs/<stamp>/`, pull the logs; fails on a crash or a present mismatch |
| `golden [--record] [--only SCEN,...] [--tests]` | the golden-frame scenarios from `golden.json`, after `tests`; exit 1 on a regression, 3 when the host was busy (INCONCLUSIVE) |
| `tests` | build and ctest the toolkit's Proton tests (exclusive run lock) |
| `pacing [--scen S] [--runs N] [--gate clock\|sleep] ["A env" "B env"]` | a frame-pacing A/B of one scenario under two environments |
| `logs [STAMP]` | symbolize a run's crash reports, then pull `bench-logs/` from the host |
| `symbolize [STAMP]` | name the native addresses in a run's crash reports, into `bench-logs/<stamp>/crash-symbols.txt` (default: the newest run) |
| `gc [--apply] [--host] ...` | list, then remove, the bench runs nothing names any more |
| `shell` | an ssh shell in the host's project directory |
| `all` | `sync`, `build`, `run` |
| `integrate [--golden] [--dirty] [--stale-gen-ok]` | sync and build the integration heads, then optionally `golden` |
| `doctor` | check ssh, rsync, the host's umu-run and distrobox, the run lock's holder |

Flags shared by the commands that run the game: `--kill-game`
(`BENCH_KILL_GAME=1`) ends a game running outside the bench once the lock is
held; `--keep-frames` (`BENCH_KEEP_FRAMES=1`) keeps every frame dump of a
passing scenario; `--proton-log` (`BENCH_PROTON_LOG=full`) keeps Proton's
whole log.

Configuration comes from the environment or the game's `scripts/bench.env`
(`KEY=value` lines, never synced or committed; they win over the
environment). Quote a host path that starts with `~`.

| Variable | Meaning (default) |
|---|---|
| `BENCH_HOST` | ssh target, `user@host` (required) |
| `BENCH_DIR` | project root on the host (`~/xbox-recomp`) |
| `BENCH_BOX`, `BENCH_IMAGE` | distrobox name and image (`xbr-build`, `fedora:42`) |
| `LLVM_MINGW_ROOT`, `LLVM_MINGW_TAG` | llvm-mingw on the host (`$BENCH_DIR/llvm-mingw`; the tag from `game.toml`) |
| `PROTONPATH` | Proton for umu-run (`GE-Proton`, the latest GE) |
| `BENCH_PREFIX` | Wine prefix on the host (`$BENCH_DIR/prefix`) |
| `BENCH_GAME_FILES` | the host's single `game_files/` copy; every other tree links to it |
| `BENCH_ENV` | `VAR=value` pairs for the game; `RECOMP_TRACE`/`RECOMP_DEBUG` entries add up with a scenario's |
| `BENCH_TIMEOUT` | stop the game with SIGINT after this many seconds |
| `BENCH_FRAMES` | `1`: frame dumps into `bench-logs/<stamp>/frames/` on the host |
| `BENCH_HOLD_MAX` | pacing: the longest hold of the run lock if the command dies (14400 s) |
| `BENCH_GAME_DIR` | the project tree to drive (this checkout) |
| `XBOXRECOMP_DIR` | the local toolkit checkout (`external/xboxrecomp`, else `../xboxrecomp`) |

The run lock (`~/.recomp-run.lock` on the host) is taken by the host-side
scripts themselves: shared for builds, exclusive for runs and tests, waiting
up to an hour (exit 75). Never wrap `bench` in an outer `flock`. The host
keeps the toolkit at `$BENCH_DIR/xboxrecomp` and the game at
`$BENCH_DIR/<game>`; `run-info.txt` records the exe's sha256 and the
commits it was built from.

`bench golden` keeps, for a passing scenario, only the frames its check
read; each scenario's verdict is in its run's `golden.txt`, and every pass
adds a line to `bench-logs/golden-sessions.tsv`. The toolkit's Proton tests
are skipped when the host's toolkit, the CLI, the toolchain and the Proton
and prefix versions are those of their last pass (`--tests` runs them
anyway). `bench gc` spares runs named in `golden.json`, `TASKS.md`,
`RESUME*.md`, `openspec/` or `[bench.gc] refs` in any worktree, other games'
runs, young runs and the newest per scenario.

## Developer tools

Each tool takes the game's paths from `game.toml` and then its own
arguments; `mygame <tool> --help` prints them.

| Tool | What it does |
|---|---|
| `mygame golden <subcommand>` | the golden-frame compare: `plan`, `frames`, `diff`, `check`, `prune`, `record`, `reference`, `dumpat`, `anchors`, `pulls`, `refs`, and `run` |
| `mygame pacing-stats LOG...` | frame-pacing statistics from run logs (`RECOMP_TRACE=flip,pacing=all`): interval percentiles, flips/s, an A/B with `--arm` |
| `mygame benchlog-retention` | retention for the `bench-logs/` store; a dry run unless `--apply` |
| `mygame audio-check WAV` | checks on an audio dump or an xemu reference capture; runs in the game's `.venv` (needs numpy) |
| `mygame wrapper --check` / `--print` | compare the game's bootstrap with this CLI's template, or print the template |

`golden check` fails a run whose `[ENHANCE]` line shows a non-stock value
for a toolkit key or one of the game's (`golden.enhance_stock`); `golden
prune SCEN=DIR` drops the flip dumps a passing check did not read.

`golden run [SCEN...] [--backend metal|cpu] [--secs S] [--exe PATH] [--out
DIR] [--keep-frames] [--lock-wait S] [VAR=val...]` is the golden pass on a
Mac's own build: each scenario runs headless with dummy audio under the Mac
run lock (`~/.recomp-mac-run.lock`), dumps its frames by flip, stops once
the last frame its check reads is dumped, warns when the Mac is loaded (the
pace check then says INCOMPLETE), then checks and prunes as `bench golden`
does. Each run is `bench-logs/<stamp>-<backend>-<scen>/` with its `golden.txt`.
