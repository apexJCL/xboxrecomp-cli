# Command reference

Every command runs through the game's wrapper (`./mygame` on macOS and
Linux, `mygame` or `mygame.cmd` on Windows, `py -3 mygame.py` anywhere),
which passes it to `xbr` with the game's root and name. `./mygame --help`
prints the player and developer commands; `./mygame <command> --help` the
options of one. A game is a directory with a `game.toml`
([manifest.md](manifest.md)).

## Player commands

### `mygame PATH`

`mygame package <this computer's target> --dump PATH`: imports the disc
image or dump folder at `PATH` (as `import` below), then packages. Any
other `package` option follows the path (`mygame PATH --replace`,
`mygame PATH --hud-pack`). A command name wins over a folder of the same
name; a path that does not exist is an error, not a command.

### `mygame import PATH`

Put the dump in `game_files/` from a disc image (`.iso`, `.xiso`, any
extension: the image is recognised by its XDVDFS descriptor, at any of the
usual partition offsets) or a folder holding `default.xbe` (the disc's
root). Before anything is written it checks the XBE's title ID against
`xbe.title_id` (another game is refused by name), every file name (one
Windows refuses, a directory loop, nesting deeper than 32) and the free
space. The files land in `game_files.partial/` first and are renamed into
place, so an interrupted import leaves the old `game_files/` as it was; the
next run tidies up. A folder is cloned on APFS (no extra space on the same
volume) and copied elsewhere.

`game_files/.xbr-dump.json` records the source, so running it again with
the same source is a size check and nothing more. A `game_files/` that
already holds the same files is adopted (the record is written, nothing is
copied). One that holds anything else is never replaced unasked:

| Option | Effect |
|---|---|
| `--replace` | replace a `game_files/` that holds another dump; the old one moves to `game_files.old/` and is removed once the new one is in place. Saves and the pipeline's files (`game_files_exclude` names) carry over, unless the source has its own |
| `--check` | compare the source with `game_files/` by name, size and sha256 and list the differences; writes nothing, exit 1 on a difference |
| `--link` | developers: make `game_files/` a link to the folder instead of a copy (no record, not on Windows); the pipeline then writes its analysis into that folder |

An archive (`.zip`, `.7z`, `.rar`) has to be extracted first, and a
`.cue`/`.bin` set is imported through its `.bin`.

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
| `--dump PATH` | import the disc image or dump folder first, as `import PATH`; the plan shows it as its first step |
| `--replace` | with `--dump`: as `import --replace` |
| `--no-build` | package the existing build as it is |
| `--allow-debug` | package a non-Release build |
| `--allow-nonstock` | package with a non-stock CMake value (`build.nonstock_vars`, or the enhancements layer off); recorded in `manifest.json` |
| `--archive` | windows: also a stored `.zip` of the folder |
| `--hud-pack [DIR]` | put the HUD texture pack (default `dist/enhance/hud/`) in the bundle and enable it in its `config.default.toml`, which the game reads on first installs and upgrades alike |
| `--seed-config FILE` | a `config.toml` with the player's options (the build wizard writes it), staged as `config.seed.toml`; each installer copies it to the data folder's `config/config.toml` only when that file does not exist yet. Every key must be one `config.default.toml` has |
| `--out OUT` | output directory (default `dist/`) |
| `--plain` | plain line output, no live progress (also when not a terminal, or `CI=1`), and a heartbeat line every 20 s of silence (`  <step> ... m:ss | <last line>`) |
| `--verbose` | show every tool's output as it runs (implies `--plain`); it always goes to `build-logs/` too |
| `--no-setup` | never run `setup`, even when something is missing |
| `--reconfigure` | configure the packaging build tree afresh |
| `--system-tools` | use the host's cmake and ninja instead of the venv's |
| `--verify` | hash every recorded output and game file instead of trusting size and mtime (for a folder copied or restored from elsewhere) |
| `--plan [--json]` | only say which stages would be reused and which would run, and why, with estimates from `build-logs/timings.json`, and list the bundle records as `bases`; runs and records nothing |
| `--clean[=all]` | rebuild everything: drop the build state, `build-pkg-*/` and the staging trees first; `gen/` stays when its key is current. `--clean=all` also drops `gen/` and the analysis cache, which then rerun |
| `--keep-bundles N` | bundles of the target kept in the output folder (default 2) |
| `--jobs N` | compile N jobs at once, and N HUD workers (default: as many as the free memory and the cores allow; see Build jobs below) |
| `--ignore-disk-space` | build even when a volume looks too small: a warning instead of the `disk` refusal (for a filesystem that compresses or deduplicates) |

`package` builds in its own tree (`build-pkg-win/`, `build-pkg-macos/`) with
the stock options from `build.stock_cmake`, so the developer's `build/`
keeps its settings. It refuses a missing or foreign dump (`xbe.title_id`),
an empty `gen/` (and, with `--no-build`, one being regenerated), and a Debug
or non-stock build without the override; a stale `gen/` is regenerated
first. A dump whose `default.xbe` is not in `xbe.sha256` packages with a
warning and `"xbe": "unknown"` in the manifest.

A second run reuses what is current. Each stage (import, generate, icon,
build, stage, wrap) records the key it ran with in
`$(git rev-parse --git-dir)/xbr/build-state.json` (`<game>/.xbr/` outside
git), never in the tree, so `git status` stays clean. A stage is skipped
when its key matches and its outputs are unchanged (size and mtime; sha256
when they differ or with `--verify`), and prints `current`; otherwise it
says why it runs. A toolchain change configures the build tree afresh. The
staged payload stays in `dist/.stage-<target>/` and is updated in place,
game files by clonefile or hard links. `manifest.json` is always written
again, and the bundle is remade only when it changed: a new commit with
the same files still gets a bundle that names it. The version and the
manifest's `built` take their time from `SOURCE_DATE_EPOCH`, else the
newest commit of the game and the toolkit, so two builds of the same
inputs agree. A build that is not stock (`--seed-config`, `--hud-pack`,
`--allow-debug` or `--allow-nonstock` on a tree that needs them) adds
`-o<6 hex>` to the version, a digest of those options and the CMake
values, so two builds of one commit with different options never share
a version; a stock build keeps the plain format.

Each wrap also copies its `manifest.json` to
`.git/xbr/bundles/<target>/<sha256>.json` (about 100 KB): the base a
patch is made against. The newest 2 bundles of the target stay in `dist/`
and older ones are deleted (`--keep-bundles N`; only when the output
folder is inside the game folder); their records stay.

`--plan --json` prints one document: `stages` (each with `stage`,
`action` `reuse` or `run`, `why` and `estimate_s`, the step's last
duration on this machine, `null` when it has none yet; `enhance hud`
defaults to 6 s), the total `estimate_s`, `unknown` (stages that run
with no timing yet), `version` and `bases` (`version`, `made`, `sha256`,
`size`, `on_disk`, newest first). A stage that runs makes every stage
chained on it run; the wrap of an unchanged stage is judged by what its
manifest names (commits, built time), and the run decides by the
manifest's sha256.

When the game's `config.default.toml` sets `[present] pacing = "sleep"`,
the bundle's README.txt gets a paragraph saying that `"spin"` is the
console-original timing and how to switch to it, and `package` prints the
same as a `note:` after the `bundle:` line, before the PRIVATE notice. A
game that ships `"spin"` or omits the key gets neither; a defaults file
that is not valid TOML gets one `WARNING:` line and no note.

### Build jobs and disk space

`build`, `all` and `package` size the compile to the memory free when they
start. A generated `gen/` file can take more than a gigabyte to compile,
so the CLI keeps a reserve (10% of the RAM, 1 to 2 GiB) and fits as many
`gen/` jobs in the rest as it can, at least one and at most the cores.
With Ninja and CMake 3.24 or later the `gen/` objects go in a Ninja pool
of that depth and the small runtime files fill the remaining memory
(256 MiB each); otherwise the whole build runs at the `gen/` depth. When
the free memory is unknown it runs 4 jobs (or fewer cores). On Linux a
cgroup limit counts too. The per-job figure is the larger of
`build.resources.gen_job_mib` (else an estimate from the largest `gen/`
file) and the largest compile this folder has measured, which is kept in
`.git/xbr/resources.json`. `--jobs N` sets the count and warns when it is
more than the memory holds. The job count never changes the output: the
pool's values are not in the compile key, the version or the manifest.

The package log says the decision as the compile starts
(`build: 7 jobs, generated code 6 at a time (9.7 GB available, about 1.3
GB per job)`) and the largest compile after it. The analyze tools warn
before they run when the largest one (`build.resources.tool_mib`) does
not fit in the free memory. Nothing caps memory; low memory only lowers
the job count.

Before anything runs, `build`, `analyze`, `all`, `package` and `enhance
hud` add up what their stages will write, per volume: the import, what
`setup` fetches, the analysis and `gen/` (`build.resources.generate_mib`),
the build tree (`tree_mib`), a HUD pack, the bundle (the game files for a
full one, the payload for a patch), and 1 GiB of slack per volume. A
stage that is current counts nothing: `package` counts the stages its
`--plan` would run. Before the first import, doctor counts the game files
as `build.resources.game_files_mib`. When a volume has
less free space, the command stops with a `disk:` message that lists each
stage's share and how to free space, and `refused: disk`.
`--ignore-disk-space` builds anyway. `import` keeps its own check.

### `mygame doctor`

What this host has (uv, the venv, llvm-mingw, makensis, the toolkit and CLI
pins, the dump, `gen/`), which targets it can package, and `next:` the first
thing to do. `memory:`, `cores:`, `disk:` and `build jobs:` say what a
package on this host would run with (Build jobs above), with a warning
when memory or disk is low (never a problem: doctor builds nothing).
`features:` names what the toolkit checkout has of the fork's
parts and what it lacks: the env table, the enhancements layer (which
reads `config.toml`), the D3D11 and Metal renderers and recomp's
`--spin-waits`. Upstream xboxrecomp has none of them, so a build on its
pin runs on the CPU rasteriser with stock settings. Exit 1 when the game's
steamos target is blocked, or when anything is missing for a game without
one.

`layout:` is the repo's drift from the standard layout
([layout.md](layout.md#doctors-layout-block)): missing entries, ignore
gaps, tracked data, a nested worktree, workflows off their templates, the
push guard. It is for maintainers: it never changes `next:` or the exit
status (`public audit` fails on it instead).

`--json` prints the same as data for the build wizard: `host`, `lines`,
`targets`, `blocked`, `problems`, `next`, `blockers`, for each package
target what a run that imports a dump cannot fetch itself (each with its
fix), `toolkit_features` (`{feature: true|false}`, null before
`setup`), and `resources`: `cores`, `mem_total`, `mem_available`,
`disk_free`, `disk_need` (bytes), `jobs`, `gen_jobs`, `gen_job_bytes`,
`gen_job_source` (`game`, `measured` or `estimate`) and `warnings`, with
`null` for what is unknown, and `layout`: `problems` and `notes`.

### `mygame fingerprint [PATH]`

The XBE's verdict against `game.toml`, for `game_files/` (the default), a
folder, a disc image or an XBE file. Each section's raw bytes are hashed and
compared with `[xbe.sections]`:

| Verdict | When |
|---|---|
| `known` | its sha256 is in `[xbe] sha256` |
| `header` | every section matches: another pressing of the same code |
| `data` | only data sections differ: modded data, the code is stock |
| `code` | a code section differs, is added or is missing: the generated code no longer matches, and the game may crash |
| `unknown` | `[xbe.sections]` is empty, or the section table cannot be read |

Code is what the pipeline disassembles: `.text` plus `[pipeline.disasm]
extra_sections` with `text_only`, else every executable section. No command
refuses a dump for its verdict: `import`, `analyze`, `build`, `all`,
`package` and `doctor` print the `data`, `code` or `unknown` warning and go
on. Only `bench golden` refuses an XBE whose sha256 is not listed, as its
references were recorded on those. `package` records the verdict in
`manifest.json` (`xbe`: `sha256`, `verdict`, `sections`), and with
`[package] stamp_env` set it ends the bundle's `launch.env.default` with
that key, for the game to print at boot. `package --plan --json` reports it
as `xbe`, and the build wizard asks before building modded code.

| Option | Effect |
|---|---|
| `--toml` | maintainers: print the `[xbe.sections]` table for the XBE, to paste in `game.toml` |
| `--json` | the verdict as JSON: `sha256`, `verdict`, `sections`, `note` |

### `mygame enhance hud`

The HUD texture pack from the owner's own dump, into `dist/enhance/hud/hud.pack`.
Needs `[enhance.hud]` in `game.toml`.

| Option | Effect |
|---|---|
| `--scale {2,4}` | upscale factor (default: `game.toml`'s) |
| `--out OUT` | output directory (default `dist/enhance/hud/`) |
| `--font FONT` | a TrueType file (`.ttf`, `.ttc`) or an installed font's full name for the Latin atlas (default: `game.toml`'s `font`, if installed) |
| `--jobs N` | processes for the upscale (default: as many as the free memory and the CPUs allow) |
| `--ignore-disk-space` | write the pack even when the disk looks too small |
| `--only PATTERN` | narrow the set to matching entries (repeatable) |
| `--language LANG` | a language of `[enhance.hud] languages` to build the text of (repeatable; default: the first listed) |
| `--list` | print the set and its hashes, write nothing |

A pack is made from game data and is never published; `package --hud-pack`
puts it in the owner's private bundle.

### `mygame clean [--bundles] [--build] [--all] [--yes]`

Free space in the game folder. It lists each path with its size and
deletes only with `--yes`; with no category it prints the sizes of each.

| Option | Effect |
|---|---|
| `--bundles` | the bundles in `dist/` (their manifest records stay, so a patch against them can still be made) |
| `--build` | `build-pkg-*/`, `dist/.stage-*` and the build state: the next `package` rebuilds the program, in minutes |
| `--all` | both, plus `gen/`, its key and the analysis cache (`disasm/`, `func_id/`, `abi/`), and the rest of `.git/xbr/` except the wizard's mark `wizard.json` |
| `--yes` | delete; without it, only list |
| `--json` | `{"sizes": {category: bytes}, "total", "deleted"}` |

It never deletes the imported game files, the venv, setup's downloads or
a Ghidra export, and refuses any path outside the game folder (a `dist/`
that links elsewhere, for example).

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
| `mygame all [--system-tools] [--jobs N]` | `analyze`, `recomp`, then `build` for the host's default target |
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
| `--jobs N` | compile N jobs at once (default: sized to the free memory) |
| `--ignore-disk-space` | build even when the disk looks too small |
| `-- ARGS` | extra CMake arguments |

### `mygame pins refresh`

Maintainers: rewrite `config/setup-pins.json` (the llvm-mingw and NSIS
downloads, by sha256) and `uv.lock`, and print the newest release or branch
head for each `[cli]` and `[toolkit]` pin. Needs the network.

`--wizard` instead rewrites the CLI's `wizard/pins.json` with the newest uv
and MinGit releases (the player's downloads, by sha256); the build's own
pins move by hand ([wizard.md](wizard.md#the-pins)).

### `mygame wizard <config|build|check|font>`

The player's build wizard for a release tag ([wizard.md](wizard.md)).
Needs `[wizard]` in `game.toml` and a CLI checkout (the bootstrap's).

| Action | Effect |
|---|---|
| `config --json` | print the config the binary embeds, read from the tag's tree |
| `build` | the macOS, Windows and Linux assets and `SHA256SUMS` in `dist/wizard/` |
| `check` | the Go module's gates, then two builds with equal sha256s |
| `font [--json]` | whether `[enhance.hud] font` is installed here, by `enhance hud`'s own lookup: `{"font", "found", "path"}` with `--json`; the wizard's `font_hint` reads it |

| Option | Effect |
|---|---|
| `--tag TAG` | the release (default: the one tag on HEAD of a clean checkout) |
| `--dev` | the working tree as it is, no tag (developers) |
| `--only LIST` | `build`: a comma-separated subset of `macos,windows,linux` |
| `--out DIR` | `build`: the output folder (default `dist/wizard/`) |
| `--no-build` | `check`: the gates only |
| `--name NAME` | `font`: look for this full name instead (to try the missing case) |

### `mygame public <init|export|pr|audit>`

Maintainers: the game's public repository, from `[public]`
([layout.md](layout.md#publishing) has the modes, the guard and the private
inputs). The CLI never pushes; `export` prints the push.

| Action | Effect |
|---|---|
| `init` | one remote for the public repo (`[public] remote`), its URL and push URL `[public] url`, no tags fetched; in export mode also the push refspec `refs/public/<branch>:refs/heads/<branch>` and the pre-push guard, which lets only the export, its tags and `public pr` branches reach the public repo |
| `export [REF]` | REF's tree (HEAD, the default and the only REF accepted) without the private set, rewritten and checked, as one commit on the public tip with `[public] author`; audited, then put in `refs/public/<branch>`, recorded in `<slug>.map`, and the push printed |
| `pr NAME [REF]` | a fork's upstream PR branch (needs `[public] upstream`): REF's commits upstream lacks (default HEAD), audited as history mode does, put in `refs/public-pr/NAME`, and the push to `pr/NAME` and its close printed |
| `audit [BASE..TIP]` | what the push would publish (default `<remote>/<branch>..refs/public/<branch>`): added lines, paths, messages, identities, new tags, the tree, the layout block and the `[cli]` and `[toolkit]` tags |

| Option | Effect |
|---|---|
| `--tag TAG` | `export`: also the annotated tag `refs/public-tags/TAG`, its message the tag's `CHANGELOG.md` section |
| `--message MSG` | `export`: the commit message (default `[public] message`, else `<name> static recompilation: <date> round`) |
| `--fresh-root OLD` | `export`: a root commit replacing the public history at OLD (its tip, 7+ hex digits); writes the guard's one-time allowance and prints an `--atomic --force-with-lease` push |
| `--drop-tags` | `export --fresh-root`: also delete the public repo's tags, through the same allowance |
| `--allow-dirty` | `export`: export HEAD although the working tree has changes |
| `--all` | `audit`: in export mode a dry-run export of HEAD, in history mode every tracked file and the whole history |

Exit 1 on a hit (no ref moves), 2 when a check cannot run: no private
patterns, no network, an `XBR_PUBLIC_DIR` inside the work tree, no
`rewrite` file in export mode, or a layout block that could not be checked.

### `xbr new DIR`

Start another game: `game.toml`, the bootstrap and its wrappers, the tools
environment, the toolkit's `templates/new-game/` with the game's constants,
the toolkit clone and the pins. Runs without a game (`uvx --from
git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame`, or `mygame
new DIR` from an existing game). Whatever it could not finish (offline, or
with no network) it names as the later command that does it: `setup` for the
toolkit clone (and the template copy that follows it), `pins refresh` for
the pins and the lock. `@<tag>` after the repository URL pins a release,
`@<sha>` a commit (`…/xboxrecomp-cli@v0.6.0 xbr new mygame`); the no-pipe
way is a clone and `uv run xbr new ../mygame`.

| Option | Effect |
|---|---|
| `--slug SLUG` | the command name, `[a-z0-9-]` (default: `DIR`'s name) |
| `--name NAME` | the game's name (default: the XBE certificate's title) |
| `--xbe DUMP` | read the title, title ID and entry point from your disc image (`.iso`, `.xiso`, any extension, as `import` judges it), the dump's folder or `default.xbe` (default: `DIR/game_files/default.xbe` if it is there); read in place, never copied. Without it `title_id` stays 0 and `import` refuses every dump until it is filled in. `new` also finds D3D's fence wait in the dump and writes the game's `xbox_Nv2aMirrorFence` line into `src/main.c`, or says why not (`porting.md`, "The D3D fence"), and lists the XDK libraries the title links |
| `--toolkit-url`, `--toolkit-branch`, `--toolkit-commit` | the toolkit pin (default: a checkout beside `DIR`, else the branch head) |
| `--cli-tag TAG`, `--cli-commit SHA` | the CLI pin (default: the release this CLI runs at, else its commit) |
| `--llvm-mingw TAG` | the llvm-mingw release |
| `--offline` | write the files only: no clone, no pins, no lock |
| `--license mit` | also a `LICENSE`: MIT, this year, "`<name>` static recompilation contributors" (default: none, the choice is the owner's; `doctor` reports it missing) |
| `--no-openspec` | skip `openspec/` (`config.yaml`, `specs/`, `changes/`), for a game that will have no `[public]` |

### `mygame bench <command>`

Drive the Proton bench host: a Linux x86-64 machine reached over ssh that
builds the Windows build in a distrobox and runs it under Proton. `mygame
bench --help` is the full text; the commands:

| Command | What it does |
|---|---|
| `setup` | once per host: the build distrobox, cmake, ninja, umu-launcher, llvm-mingw |
| `sync [--game-files[=reflink]]` | rsync the toolkit and the game to the host; `--game-files` sets up the host's one `game_files/` copy |
| `build [-- CMAKE ARGS]` | configure and compile `build-win/` on the host (shared run lock) |
| `run [GAME ARGS]` | launch the game under Proton, log to `bench-logs/<stamp>/`, pull the logs; fails on a crash or a present mismatch. The game starts from the Windows package's `launch.env.default` (`BENCH_ENV` wins), as do `pacing` and `all`; `golden` keeps its scenarios' own env |
| `golden [--record] [--only SCEN,...] [--tests]` | the golden-frame scenarios from `golden.json`, after `tests`; exit 1 on a regression, 3 when the host was busy (INCONCLUSIVE) |
| `tests` | build and ctest the toolkit's Proton tests in `build-win/` under `PROTONPATH` (exclusive run lock); first restarts a box whose `/etc/resolv.conf` went stale |
| `pacing [--scen S] [--runs N] [--gate clock\|sleep] ["A env" "B env"]` | a frame-pacing A/B of one scenario under two environments |
| `logs [STAMP]` | symbolize a run's crash reports, then pull `bench-logs/` from the host |
| `symbolize [STAMP]` | name the native addresses in a run's crash reports, into `bench-logs/<stamp>/crash-symbols.txt` (default: the newest run) |
| `gc [--apply] [--host] ...` | list, then remove, the bench runs nothing names any more |
| `shell` | an ssh shell in the host's project directory |
| `all` | `sync`, `build`, `run` |
| `integrate [--golden] [--dirty] [--stale-gen-ok]` | sync and build the integration heads, then optionally `golden` |
| `doctor` | check ssh, rsync, the host's umu-run and distrobox, a running box's `/etc/resolv.conf` (the `resolv:` line), the run lock's holder |

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
| `BENCH_ENV` | `VAR=value` pairs for the game; `RECOMP_TRACE`/`RECOMP_DEBUG` entries add up with a scenario's; a key here replaces `launch.env.default`'s |
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
anyway), and when the host tree has no configured `build-win/` (a tree
synced for goldens only, with its exe copied in), since `sync` never ships
build trees; there `--tests` fails. `bench gc` spares runs named in `golden.json`, `TASKS.md`,
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
| `mygame workflow --check` / `--print` | compare `release-wizard.yml` (with `[wizard]`) and `checks.yml` (with `[public]`) with this CLI's templates, or print them |

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
