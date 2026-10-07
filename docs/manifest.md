# game.toml

A game's manifest. It holds everything the CLI needs to know about one
game, at the game's root, beside its bootstrap (`<slug>.py`). The CLI
reads and validates it on every command:
- An unknown key or table is an error, so a typo never falls back to a
  default without a word.
- A missing required key is an error, and so is a value of the wrong type.
  The error names the key.
- Paths are relative to the game root, written with `/`, and must stay
  inside it.
- Tables other than `[package]` are always present in effect: a table you
  leave out takes its defaults.

The CLI never writes this file, except once: `xbr new DIR` writes a starter
one (every required key filled in, the defaults written out with their
comments) and never touches it again.

Three tables are also read by the bootstrap and by the help that prints
without uv, using a line parser instead of a TOML parser: `[cli]` (`commit`,
`url`), `[game]` (`name`, `slug`) and `[build]` (`windows_dir`,
`macos_dir`). Write those keys as plain one-line strings: `key = "value"`.

BLiNX 2's manifest is the full example
([blinx2-recomp/game.toml](https://github.com/apexJCL/blinx2-recomp/blob/main/game.toml)).

## Top level

| Key | Default | Meaning |
|---|---|---|
| `schema` | required | The manifest format; this CLI reads `1`. |

## [game]

| Key | Default | Meaning |
|---|---|---|
| `name` | required | The name players see: help, installer, app, README. |
| `slug` | required | The command and prog name, `[a-z0-9-]`: the bootstrap is `<slug>.py`. |
| `env_header` | `""` | The game's `recomp_env` keys. Reserved: doctor names it. |
| `env_doc` | `""` | Their documentation. Reserved, like `env_header`. |

## [xbe]

| Key | Default | Meaning |
|---|---|---|
| `path` | `<data.game_files>/default.xbe` | The dumped executable. |
| `title_id` | required | `package` refuses an XBE with another title ID. |
| `sha256` | `[]` | The known dumps, the ones the goldens were recorded on. With another dump, the game still builds, packages (`"xbe": "unknown"` in `manifest.json`) and plays, with a warning, but `bench golden` refuses it. An empty list turns the check off. |

## [cli]

| Key | Default | Meaning |
|---|---|---|
| `commit` | required | The xboxrecomp-cli commit this game is tested with (40 hex). `doctor` compares the running CLI with it. |
| `url` | `""` | Where the bootstrap clones the CLI from, when it finds none (see the README). |

## [toolkit]

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The toolkit repository `setup` clones. |
| `branch` | required | Its branch; `pins refresh` prints that branch's head. |
| `commit` | required | The pinned toolkit commit (40 hex). |

## [toolchain]

| Key | Default | Meaning |
|---|---|---|
| `llvm_mingw` | `""` | The llvm-mingw release tag. The windows target and the bench need it. |
| `nsis` | `"3.13"` | The NSIS version, used on Windows build hosts only. |

## [pipeline]

| Key | Default | Meaning |
|---|---|---|
| `game_name` | required | `recomp --game-name`. It is written into every generated file, so it is fixed for the life of the game. |
| `gen` | required | The generated code. `gen.key.json` and the regeneration marker sit beside it. |
| `out` | `"analysis"` | Every intermediate. |
| `analysis_json` | `""` | Where `parse` writes the XBE analysis. Empty writes it beside the XBE (`<stem>_analysis.json`); a game whose dump is read-only, such as a link to the player's own copy, names a path under its tree (`parse` makes the directory). |
| `seeds` | `[]` | `--seed-functions` files for disasm. |
| `icall_seeds` | `true` | Seed from the toolkit's icall_feedback database, when there is one. |
| `spin_waits` | `""` | `recomp --spin-waits`. Empty passes no flag. |
| `exclude_manual` | `""` | `recomp --exclude-manual`. Empty passes no flag. |
| `split` | `250` | Functions per generated file. `SPLIT` in the environment overrides it. |
| `names_hooks` | `[]` | Scripts run after `names`, given the functions.json path. |
| `ghidra` | `true` | Whether the optional Ghidra stage is offered. |

### [pipeline.disasm]

| Key | Default | Meaning |
|---|---|---|
| `text_only` | `true` | `--text-only`, as upstream's README has it. |
| `extra_sections` | `[]` | `--extra-sections`: code outside `.text`, such as the XDK libraries. |

## [build]

| Key | Default | Meaning |
|---|---|---|
| `exe` | required | The CMake target and file name (`.exe` on Windows). |
| `exe_dir` | `""` | Where the exe lands inside the build dir, if not at its top. |
| `targets` | `["windows"]` | The build targets this game supports: `windows`, `macos`. `build` with no target builds this host's own (`macos` on a Mac, else `windows`) when it is listed, else the first listed. |
| `windows_dir`, `macos_dir` | `"build-win"`, `"build"` | The developer build dirs. |
| `toolchain_file` | `""` | The CMake toolchain file for the windows target. Empty uses the CLI's own `src/xboxrecomp_cli/cmake/llvm-mingw-x86_64.cmake`, locally and on the bench host. |
| `stock_cmake` | `["-DXBOXRECOMP_ENHANCE=ON"]` | Passed on every packaging configure, so that a cache cannot keep a non-stock value. It is also the stock rule: `package` refuses a cache whose `VAR` differs from a `-DVAR=VALUE` here (booleans by CMake's truth), without `--allow-nonstock`. |
| `nonstock_vars` | `[]` | Cache variables `package` refuses without `--allow-nonstock`. |
| `icon_var` | `""` | The CMake variable that compiles the icon into the exe. |

## [data]

| Key | Default | Meaning |
|---|---|---|
| `game_files` | `"game_files"` | The dumped disc. |
| `game_files_exclude` | `["default_analysis.json", "UDATA", "TDATA"]` | Left out of a bundle. |
| `dir_env` | `<APP>_DATA_DIR` | The variable that moves the player's data. |
| `windows`, `steamos`, `macos` | `%LOCALAPPDATA%\<app>`, `~/Games/<app>`, `~/Library/Application Support/<app>` | The data folder on each target, as the bundle's README names it. |

## [input]

| Key | Default | Meaning |
|---|---|---|
| `script_env` | `"RECOMP_INPUT_SCRIPT"` | The variable that holds a golden scenario's input script. |
| `presets` | `[]` | The `@name` presets the game compiles in. `bench golden` refuses a scenario that names any other. |

## [golden]

| Key | Default | Meaning |
|---|---|---|
| `json` | `""` | The golden scenarios. Empty means no golden, pacing or `golden` command. |
| `frames` | `<json dir>/frames` | The reference frames, which are game output: keep them out of git. |
| `audio` | `""` | The audio thresholds. |

## [package]

This table is optional. Without it, `package` says so.

| Key | Default | Meaning |
|---|---|---|
| `app` | required | The file, app and folder name (`BLiNX2`). |
| `product` | `<slug>-recomp` | `manifest.json`'s `product`. |
| `targets` | `["windows", "steamos"]` | The bundles: `windows`, `steamos`, `macos`. `macos` needs `build.targets` to include it. The command with no arguments packages this host's own bundle when it is listed, else the first listed one this host can make. |
| `brew` | `[]` | The Homebrew formulae the macos target links. `doctor` checks them. |
| `dylib_companions` | `[]` | `NAME=brew:FORMULA`: libraries loaded with dlopen, bundled too. |
| `content` | `"packaging"` | The game's own packaging content: `launch.env.default.<windows\|macos>`, `enhance.toml.default`, `<target>/README.part` for windows and macos, and optionally `README.intro`. |
| `templates` | `""` | The game's own packaging templates: the macos ones (required for `macos`), and any copy of a CLI template that must differ, at the same path (`README.txt.in`, `windows/launcher.c`, `steamos/launch.sh`, ...), which then wins. |
| `icon` | `"xbe"` | The XBE's title image, or the generic icon. |

### The CLI's templates

The installers, launchers and README frame are the CLI's own, for every
game, in [src/xboxrecomp_cli/package/templates](../src/xboxrecomp_cli/package/templates).
`package` fills their `@KEY@` placeholders from game.toml. A game overrides
one only if it must: its own copy at the same path under
`package.templates` wins (`README.part` and `README.intro` may also sit
under `package.content`).

- **README** (`README.txt.in`): the title line, the data folders and the
  saves section. Its first paragraphs, the PRIVATE notice and what built
  the bundle, come from `README.intro`: the CLI's names the game
  (`game.name`) and `package.product`; a game with its own wording puts
  `README.intro` in `package.content` (the same placeholders work there).
  The target's install section `<target>/README.part` follows.
- **windows** (`launcher.c`, `installer.nsi.in`, `app.rc.in`): the
  launcher `<app>.exe`, with the icon compiled in, sets the game's
  environment (`RECOMP_HDD_DIR`, `RECOMP_GAME_FILES`, `RECOMP_ENHANCE_CONFIG`,
  `RECOMP_STDIO_LOG`) below `data.windows` (or the folder that the
  `data.dir_env` variable names, when set)
  and starts `build.exe`. `data.windows` must be a folder below
  `%LOCALAPPDATA%`. The per-user NSIS installer puts the program in
  `%LOCALAPPDATA%\Programs\<app>`; the Start Menu shortcut is `game.name`
  without the characters a file name cannot hold (`:` becomes ` -`).
- **steamos** (`install.sh`, `install_lib.py`, `launch.sh` and the
  install section `README.part`): filled with the name, `package.app` and
  `product`, `build.exe`, `pipeline.game_name`, `data.steamos` (the install
  root), `<SLUG>_ROOT` (the variable that moves it), and the umu-launcher
  pin from `pins.py`. They run on a Steam Deck with stock SteamOS (the
  installer offers to download the pinned umu-launcher when umu-run is
  missing) and on a distribution that ships umu-run. The bundle also gets
  the CLI's `running_game.py` with `build.exe` baked in, for
  `install.sh status`.
- **macos**: still the game's own, in `<package.templates>/macos/`.

## [bench]

| Key | Default | Meaning |
|---|---|---|
| `remote_name` | `""` | The project's directory on the bench host. Empty uses the checkout's basename. |
| `main_branch` | `"main"` | `integrate` refuses any other branch. |
| `toolkit_branch` | `""` | `integrate` warns when the toolkit is on another branch. |
| `toolkit_tests` | `true` | `golden` runs the toolkit's Proton tests first. |
| `pacing_scenario` | `""` | `pacing`'s default scenario. Empty uses the first in golden.json. |
| `sync_excludes` | `[]` | More rsync excludes for `sync`, after the ones every game has: the game's own local output (rsync does not read `.gitignore`). |
| `crash_tag` | `"[CRASH]"` | The start of the line the game's crash handler prints. `run`, `golden` and `symbolize` fail or symbolize a run whose `game-stdio.log` has a line starting with it. Symbolizing reads BLiNX 2's report format (`RIP=`, `[i] 0x...` frames); another format's report keeps its tagged lines only. |

Host settings are not part of the manifest: `BENCH_HOST`, `BENCH_DIR` and
the rest come from the environment or the game's `scripts/bench.env`
(`<game> bench --help`).
