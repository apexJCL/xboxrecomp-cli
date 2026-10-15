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
without uv, using a line parser instead of a TOML parser: `[cli]` (`tag`,
`commit`, `url`), `[game]` (`name`, `slug`) and `[build]` (`windows_dir`,
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
| `tag` | `""` | The xboxrecomp-cli release this game is tested with (`v0.2.0`). The bootstrap clones and checks out that tag; `pins refresh` names a newer `v*` release. |
| `commit` | `""` | The commit (40 hex). Alone, it is the pin. With `tag`, it locks the tag: a tag that names another commit (moved, or re-pushed) is refused. `doctor` compares the running CLI with it. |
| `url` | `""` | Where the bootstrap clones the CLI from, when it finds none (see the README). |

`tag` or `commit` is required; both is the safe form. A tag alone trusts
the tag: the bootstrap fetches it when the clone lacks it, and a clone that
already has it uses its own copy (a tag moved on the remote is never
fetched over it). `tag` needs xboxrecomp-cli 0.2.0 or newer: an older CLI
stops on `cli.tag: unknown key`, so pin a 0.2.0+ release (or commit) when
you add it. A `url` may not start with `-`.

## [toolkit]

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The toolkit repository `setup` clones. |
| `tag` | `""` | The toolkit release (`blinx2-v0.1.0`). `setup` clones and checks out that tag; `pins refresh` names a newer release with the same prefix. |
| `branch` | `""` | Its branch; `pins refresh` prints that branch's head. Required without `tag`. |
| `commit` | `""` | The pinned toolkit commit (40 hex). Required without `tag`; with it, it locks the tag as in `[cli]`. |

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
| `text_only` | `false` | `--text-only` when true. The default sweeps every executable section except the PE data sections (`.data`, `.rdata`, `.idata`, `.reloc` …, which are skipped by name whatever their flags), as Burnout 3 does: the XDK libraries (D3D, DSOUND, XGRPH, XPP, ...) sit in sections of their own, and with `.text` alone they have no function boundaries, so every call inside them becomes a stub that returns without running. Upstream's README passes `--text-only`, which is why a title made that way stubs its libraries. Set it true when the XBE flags data sections executable (BLiNX 2's model sections), which would otherwise be swept into phantom functions, and list the code sections in `extra_sections`. |
| `extra_sections` | `[]` | `--extra-sections`: code sections swept besides `.text`. Read with `text_only = true` (BLiNX 2 lists the XDK libraries and its middleware), or for a section the sweep would otherwise skip. |

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
| `game_files_exclude` | `["default_analysis.json", "UDATA", "TDATA"]` | Left out of a bundle; an import carries them into the new tree. |
| `dir_env` | `<APP>_DATA_DIR` | The variable that moves the player's data. |
| `windows`, `steamos`, `macos` | `%LOCALAPPDATA%\<app>`, `~/Games/<app>`, `~/Library/Application Support/<app>` | The data folder on each target, as the bundle's README names it. |

`import` writes `.xbr-dump.json` into `game_files` (where the dump came
from, its size and its XBE's sha256): it is never bundled, and like
`game_files_exclude` it does not count when a source is compared with the
folder. While it runs, `import` uses two folders beside `game_files`,
`<game_files>.partial` and `<game_files>.old`; a game's `.gitignore` (the
one `xbr new` writes has them) and the bench sync leave them out, as they
do `game_files`.

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
| `enhance_stock` | `{}` | The game's own enhancement keys and their stock values, e.g. `{ "fps.mode" = "lock30", "fx.glow" = "on" }`. The golden check fails a run whose `[ENHANCE]` line shows another value for one of them (`--allow-enhance KEY=VALUE` lets an evaluation run through), as it does for the toolkit's `render.scale`, `display.aspect` and `present.pacing`, which it knows already and which may not be repeated here. Values that both read as numbers compare as numbers (`1` is `1.0`). |

## [package]

This table is optional. Without it, `package` says so.

| Key | Default | Meaning |
|---|---|---|
| `app` | required | The file, app and folder name (`BLiNX2`). |
| `product` | `<slug>-recomp` | `manifest.json`'s `product`. |
| `targets` | `["windows", "steamos"]` | The bundles: `windows`, `steamos`, `macos`. `macos` needs `build.targets` to include it. The command with no arguments packages this host's own bundle when it is listed, else the first listed one this host can make. |
| `brew` | `[]` | The Homebrew formulae the macos target links. `doctor` checks them. |
| `dylib_companions` | `[]` | `NAME=brew:FORMULA`: libraries loaded with dlopen, bundled too. |
| `content` | `"packaging"` | The game's own packaging content: `launch.env.default.<windows\|macos>`, `config.default.toml` (the option defaults the game reads beside its executable; a game that still has `enhance.toml.default` is refused until it renames it; `[present] pacing = "sleep"` there adds the frame pacing paragraph to README.txt and the note to `package`'s output), `<target>/README.part` for windows and macos, and optionally `README.intro`. |
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
  The frame pacing paragraph (`@OPTION_NOTES@`) follows the options
  paragraph when `config.default.toml` sets `[present] pacing = "sleep"`.
  The target's install section `<target>/README.part` follows.
- **windows** (`launcher.c`, `installer.nsi.in`, `app.rc.in`): the
  launcher `<app>.exe`, with the icon compiled in, sets the game's
  environment (`RECOMP_HDD_DIR`, `RECOMP_GAME_FILES`, `RECOMP_CONFIG`,
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

## [enhance.hud]

This table is optional. Without it, `enhance hud` says the game names no HUD set. The pack it makes is the owner's own (made from their game files, never distributed; `package --hud-pack` puts it in their private bundle).

| Key | Default | Meaning |
|---|---|---|
| `archives` | required | The IPK1 archives to scan, as basenames under `data.game_files` (`["media"]` reads `media.iph` and `media.ipk`). |
| `textures` | required | fnmatch patterns, `/` separators, lower case, of the entry names to upscale (`"dds/font_*"`). An entry is a DDS file, or a MIXF container (`.mix`) whose XPR0 textures each become an entry named `<entry>/<name in the MIX>`. Only DXT1/3/5 textures without mip chains are taken; the rest are listed as skipped. |
| `scale` | `4` | The upscale factor, 2 or 4 (`--scale` overrides). |
| `max_texels` | `0` | Entries over this many guest texels are left out; 0 is no limit. Full-screen backgrounds are not HUD, and a 1024x512 one at 4x is 32 MB of texels. |
| `font_atlas` | `""` | The Latin font atlas's entry name. With a font (`font` or `--font`), it is re-rendered from the font instead of upscaled, at one scale for the whole font (the stock cap height times `scale`) so every glyph keeps the font's proportions and stroke weight. Each glyph is centred across on its stock ink box, on the stock baseline; a descender keeps it too. One that would leave the region its quad samples (`font_margin`) is shifted inside it, or shrunk by one factor on both axes; it is never enlarged. A mark the font draws clear of the baseline (a dash, an underscore, a quote) may move up or down instead. `enhance hud` names any glyph shrunk by more than 8%. `font_stretch` and `font_full_height` trade some of the even weight for the stock spacing and brackets. |
| `font_rows` | `[]` | Required with `font_atlas`: one string per band of ink rows in the atlas, the glyphs of that band in atlas order (spaces are ignored; `\uFFFD` keeps the stock glyph, for an icon the game draws at a code). Read off the dump. |
| `font_margin` | `[]` | How far past each glyph's stock ink box the game's quads sample, in stock atlas texels (guest px when the game draws its text at 1:1): `[left, top, right, bottom]`. A redrawn glyph stays inside that box, clipped to its slot (the space up to the middle of the gaps to its neighbours). Empty means the slot. Measure it on the running game (an atlas painted in rings around each ink box shows which ring each quad reaches). |
| `font_stretch` | `0` | The most, in percent, a redrawn glyph is widened or narrowed towards its stock ink width. The game advances by each glyph's slot, so a font whose `E` is narrower than the stock one opens the gaps around it; a stretch closes them, and keeps the stems within that percent of each other. 0 keeps the font's own widths. |
| `font_full_height` | `""` | Glyphs, usually the brackets (`()[]{}`), drawn to span their stock ink box from top to bottom instead of sitting on the baseline at the font's height. Typefaces disagree on how far a bracket reaches above the capitals and below the baseline; one taller than the stock one sits low against the text it encloses. |
| `font` | `""` | The font the atlas is redrawn from, by its full name (`"PT Sans Narrow Bold"`): `enhance hud` looks for it in the host's font folders (a `.ttf`, or a face of a `.ttc` collection) when `--font` is not given. `--font` takes a file or another installed font's full name; in a collection file it picks this face. The user installs the font; the CLI never ships one. |
| `languages` | `{}` | Per language, the archives holding its text: `{ us = ["lng_us"], jp = ["lng_jp"] }`. `enhance hud --language us` (repeatable; default the first listed) picks the languages built; an archive named here is read only when its language is picked, whether or not `archives` lists it. Every entry of a picked language's archives is text. |
| `text` | `[]` | fnmatch patterns over `<entry>` or `<entry>/<name in the MIX>`, lower case, of baked text outside the language archives (`"dds/sm_*"`). A `.mix` is not text by itself: games keep pictures in them too. |

Each pack entry has a kind the runtime switches off on its own (`textures.font`, `textures.text`): the `font_atlas` entry is **font**, the language archives' entries and the `text` patterns' are **text**, the rest is **art**. Text is built at 2x in BC3 (a quarter of the raw bytes; the sources are DXT already), art and the atlas at `scale` as A8R8G8B8. `hud-pack.toml` beside the pack gives per kind the entries, scale, BC3 count and bytes.

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

## [bench.gc]

What `bench gc` reads to decide which bench runs are still named somewhere. Every game's `golden.json`, `audio.json`, `TASKS.md`, `OPEN_QUESTIONS.md`, `RESUME*.md` and `openspec/` are read anyway, in every worktree of the game's repository.

| Key | Default | Meaning |
|---|---|---|
| `refs` | `[]` | More files or directories whose `.md`, `.json` and `.txt` files name runs, relative to the game root. Unlike every other path here, these may leave the root with `../` (a workspace's notes beside it). In the main checkout each one must exist, or gc refuses to run. |
| `refs_exclude` | `[]` | Glob patterns, relative to the game root, of files under `refs` not to read: inventories that list every run (a cleanup log), which would protect them all. |
| `keep` | `3` | The newest runs kept per scenario and kind (golden, pacing, plain). |
| `days` | `7` | Runs younger than this many days are kept. |

Host settings are not part of the manifest: `BENCH_HOST`, `BENCH_DIR` and
the rest come from the environment or the game's `scripts/bench.env`
(`<game> bench --help`).
