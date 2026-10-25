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
- Tables other than `[package]`, `[enhance]`, `[wizard]`, `[public]` and
  `[layout]` are always present in effect: a table you leave out takes its
  defaults.

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
| `sha256` | `[]` | The known dumps, the ones the goldens were recorded on. Another dump still builds, packages and plays, with the warning its fingerprint calls for (below), but `bench golden` refuses it. An empty list turns the check off. |
| `sections` | `{}` | A table, `[xbe.sections]`: each section's name and the sha256 of its raw bytes, for the dumps `sha256` lists (`xbr fingerprint --toml` prints it). It classifies an unlisted XBE: `header` when every section matches (no warning), `data` when only data sections differ ("modded data, code stock"), `code` when a section the pipeline translates (`.text` and `[pipeline.disasm] extra_sections`, or every executable section without `text_only`) differs ("modded code, unsupported, may crash", naming them). Empty: an unlisted XBE is `unknown`. `manifest.json` records `xbe` = `{sha256, verdict, sections}`. Nothing refuses but `bench golden`. |

## [cli]

| Key | Default | Meaning |
|---|---|---|
| `tag` | `""` | The xboxrecomp-cli release this game is tested with (`v0.2.0`). The bootstrap clones and checks out that tag; `pins refresh` names a newer `v*` release. |
| `commit` | `""` | The commit (40 hex). Alone, it is the pin. With `tag`, it locks the tag: a tag that names another commit (moved, or re-pushed) is refused. `doctor` compares the running CLI with it. |
| `url` | `""` | Where the bootstrap clones the CLI from, when it finds none ([bootstrap.md](bootstrap.md#search-order)). |

`tag` or `commit` is required; both is the safe form. A tag alone trusts
the tag: the bootstrap fetches it when the clone lacks it, and a clone that
already has it uses its own copy (a tag moved on the remote is never
fetched over it). `tag` needs xboxrecomp-cli 0.2.0 or newer: an older CLI
stops on `cli.tag: unknown key`, so pin a 0.2.0+ release (or commit) when
you add it. A `url` may not start with `-`.

## [toolkit]

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The toolkit repository `setup` clones: the fork (`https://github.com/apexJCL/xboxrecomp.git`) or upstream (`https://github.com/sp00nznet/xboxrecomp.git`). |
| `tag` | `""` | The toolkit release (`blinx2-v0.1.0`). `setup` clones and checks out that tag; `pins refresh` names a newer release with the same prefix. |
| `branch` | `""` | Its branch; `pins refresh` prints that branch's head. Required without `tag`. |
| `commit` | `""` | The pinned toolkit commit (40 hex). Required without `tag`; with it, it locks the tag as in `[cli]`. |

`$XBOXRECOMP_DIR` names another checkout and wins over the pin: the CLI
uses it as it is. On upstream's pin the parts only the fork has degrade
(`doctor`'s `features:` line): recomp gets no `--spin-waits` (one warning),
and the wizard leaves out the config options (see
[wizard.md](wizard.md#the-config)).

## [toolchain]

| Key | Default | Meaning |
|---|---|---|
| `llvm_mingw` | `""` | The llvm-mingw release tag. The windows target and the bench need it. |
| `nsis` | `"3.13"` | The NSIS version, used on Windows build hosts only. |
| `sdl3` | `""` | The SDL3 release the toolkit is tested against, such as `"3.4.18"`. `pins refresh` pins its source tarball, and `doctor` on macOS warns when the installed SDL3 is an older major.minor. Leave it empty for a toolkit still on SDL2. |

## [pipeline]

| Key | Default | Meaning |
|---|---|---|
| `game_name` | required | `recomp --game-name`. It is written into every generated file, so it is fixed for the life of the game. |
| `gen` | required | The generated code. `gen.key.json` and the regeneration marker sit beside it. |
| `out` | `"analysis"` | Every intermediate. |
| `analysis_json` | `""` | Where `parse` writes the XBE analysis. Empty writes it beside the XBE (`<stem>_analysis.json`); a game whose dump is read-only, such as a link to the player's own copy, names a path under its tree (`parse` makes the directory). |
| `seeds` | `[]` | `--seed-functions` files for disasm. |
| `icall_seeds` | `true` | Seed from the toolkit's icall_feedback database, when there is one. |
| `spin_waits` | `""` | `recomp --spin-waits`. Empty passes no flag; so does a toolkit whose recomp has no such flag (upstream xboxrecomp), with one warning. |
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
| `stock_cmake` | `["-DXBOXRECOMP_ENHANCE=ON"]` | Passed on every packaging configure, so that a cache cannot keep a non-stock value. It is also the stock rule: `package` refuses a cache whose `VAR` differs from a `-DVAR=VALUE` here (booleans by CMake's truth), without `--allow-nonstock`. Without `-DXBOXRECOMP_ENHANCE=ON` here the game is built without the enhancements layer even on a toolkit that has it: `package` then stages no `config.default.toml` (none is needed) and the wizard offers no config options. |
| `nonstock_vars` | `[]` | Cache variables `package` refuses without `--allow-nonstock`. |
| `icon_var` | `""` | The CMake variable that compiles the icon into the exe. |

### [build.resources]

What a build of this game needs, for the job count and the disk check
(commands.md, Build jobs and disk space). All optional; measured figures
raise them, never lower.

| Key | Default | Meaning |
|---|---|---|
| `gen_job_mib` | `0` | The peak memory of one compile of a generated file, in MiB. 0 estimates it from the largest `gen/` file (140 MiB per MB, at least 512). |
| `tool_mib` | `0` | The peak memory of the largest analyze tool, in MiB, for the warning before it runs. 0: no warning until one is measured. |
| `tool_step` | `""` | That tool's name in the warning (`disasm`). |
| `generate_mib` | `1024` | What the analysis and `gen/` take on disk, in MiB. |
| `tree_mib` | `512` | What a build tree takes on disk, in MiB. |
| `game_files_mib` | `0` | What the imported game files take on disk, in MiB. Doctor and the disk check count it before the first import, when there are no game files to measure. 0: unknown. |
| `gen_targets` | `[build.exe]` | The CMake targets whose objects go in the generated-code job pool. |

## [data]

| Key | Default | Meaning |
|---|---|---|
| `game_files` | `"game_files"` | The dumped disc. |
| `game_files_exclude` | `["default_analysis.json", "UDATA", "TDATA"]` | Left out of a bundle; an import carries them into the new tree. |
| `reference_files` | `0` | The reference dump's file count, without `game_files_exclude` names: `doctor` prints the dump's count against it (`assets:`), for information only. `0`: no line. |
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
| `play_unset` | `[]` | The game's own input pins (a golden env's pad or keyboard off) that `golden play` drops, so a person can play. |

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
| `content` | `"packaging"` | The game's own packaging content: `launch.env.default.<windows\|macos>`, `config.default.toml` (the option defaults the game reads beside its executable; a game that still has `enhance.toml.default` is refused until it renames it; `[present] pacing = "sleep"` there, in a game with `[pipeline] spin_waits`, adds the frame pacing paragraph to README.txt and the note to `package`'s output; not staged, nor needed, when the toolkit at the pin has no enhancements layer, see "Capability blocks" below), `<target>/README.part` for windows and macos, and optionally `README.intro`. |
| `templates` | `""` | The game's own packaging templates: the macos ones (required for `macos`), and any copy of a CLI template that must differ, at the same path (`README.txt.in`, `windows/launcher.c`, `steamos/launch.sh`, ...), which then wins. |
| `icon` | `"xbe"` | The XBE's title image, or the generic icon. |
| `stamp_env` | `""` | A launch.env key: `package` ends the staged `launch.env.default` with `<key>=<sha256> <verdict>[ <sections>]`, the XBE the build was made from, for the game to print at boot. `""`: no line. |

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
  paragraph when `config.default.toml` sets `[present] pacing = "sleep"`
  and the game sets `[pipeline] spin_waits`: without spin waits the
  game's busy loops hold a core either way, so "sleep" saves nothing.
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
  install section `README.part`, whose Controls section points to
  `[input.bindings]` only when `config.default.toml` has that table):
  filled with the name, `package.app` and `product`, `build.exe`,
  `pipeline.game_name`, `data.steamos` (the install root), `<SLUG>_ROOT` (the variable that moves it), and the umu-launcher
  pin from `pins.py`. They run on a Steam Deck with stock SteamOS (the
  installer offers to download the pinned umu-launcher when umu-run is
  missing) and on a distribution that ships umu-run. The bundle also gets
  the CLI's `running_game.py` with `build.exe` baked in, for
  `install.sh status`.
- **macos**: still the game's own, in `<package.templates>/macos/`.

### Capability blocks

A game may pin upstream xboxrecomp, which has no enhancements layer
(`src/enhance/`) and so reads neither `config.default.toml` nor
`config.toml`. `package` asks the toolkit it builds on: a developer's own
checkout (no pin mark, or `XBOXRECOMP_DIR`) from its working tree, a
clone setup made at game.toml's pin as the wizard does (`reads_config`).
Without the layer it stages no
`config.default.toml`, refuses `--hud-pack` and `--seed-config` before
building, and drops the text that describes those files.

That text sits in capability blocks, which work in `README.txt.in`,
`README.intro` and every `README.part`, the CLI's and a game's own. A
block opens with a line that holds only `@if enhance@` (kept when the
toolkit has the layer) or `@if !enhance@` (kept when it has none), and
closes with a line that holds only `@endif@` (spaces around a marker
are allowed):

```
@if enhance@
Options: copy a line from config.default.toml to config/config.toml.
@endif@
@if !enhance@
This build has no options file.
@endif@
```

The marker lines never reach the README, and text outside a block is
kept as it is, so a game pinned to the fork gets the same README as
before. Blocks do not nest; an unclosed block, a stray `@endif@` or an
unknown capability stops `package` with the file's name. `enhance` is the
one capability so far.

## [wizard]

This table is optional, and needs `[package]`. With it, `wizard build` makes
the player's build wizard for a release tag ([wizard.md](wizard.md)). Its
values are read at the tag, so a change reaches players with the next
release.

| Key | Default | Meaning |
|---|---|---|
| `repo` | required unless `private` | The game's public repository, `https://`: the wizard clones it at the release tag and asks its releases for a newer one. |
| `private` | `false` | `true` for a game with no public repository yet. `repo` may then be left out; `wizard config` names no repo, the wizard skips its update check and builds a checkout the developer names ([wizard.md](wizard.md#for-developers)), and `wizard build` refuses the game, so no wizard of it is published. With `private = false` a missing `repo` is an error, so a public game cannot lose it by accident. |
| `icon` | `""` | Optional; a PNG (512x512 or larger) or an SVG in the game's tree: the wizard's icon on every platform. The wizard's own gamepad icon otherwise. |
| `targets` | `package.targets` | The targets the wizard offers, each one of `package.targets`. |

`[[wizard.fetch]]`: archives the build needs beyond the clone (none for a
game whose `setup` fetches everything), each fetched by sha256 into the
clone before `package` runs.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | A name for the progress lines, unique. |
| `url` | required | `https://` only. |
| `sha256` | required | The archive's. |
| `dest` | required | A path inside the clone, `/`-separated, every segment named (no empty, `.` or `..`). |
| `unpack` | `true` | Unpack the `.zip`, `.tar.gz` or `.tar` into the folder `dest`; `false` saves the download itself as the file `dest`. |
| `hosts` | all three | The player hosts that need it: `windows`, `macos`, `linux`. |

`[[wizard.option]]`: the options screen, one control each.

| Key | Default | Meaning |
|---|---|---|
| `key` | required | `[a-z][a-z0-9_]*`, unique. |
| `label` | required | What the player reads: short and plain. |
| `hint` | `""` | One line under the control, for a caveat that always holds. |
| `font_hint` | `""` | Words after the hint shown only while the game's font (`[enhance.hud] font`) is not installed on the player's computer, or the wizard cannot tell (`wizard font`); with it set, `hint_link` follows the same rule. Needs `[enhance.hud] font`. |
| `hint_link` | none | A link after the hint, `{ text = "...", url = "https://..." }`: both keys, words to show and an https address with no spaces or control characters (where to get that font). The page opens it in the browser. |
| `kind` | required | `bool` (a checkbox), `choice` (one of `values`: radio buttons up to three, a drop-down above) or `range` (a whole number from `min` to `max`: a slider). |
| `values` | `[]` | `choice`: two or more strings. |
| `default` | required | `bool`: `true` or `false`; `choice`: one of `values`; `range`: a whole number on a step. |
| `min`, `max` | none | `range` only, both required: whole numbers, `min` below `max`, at most 10000 apart. |
| `step` | `1` | `range` only: 1 or more, dividing `max - min`. |
| `labels` | none | `range` only: a table from a value (as a string) to what it means, shown beside the slider at that value: `labels = { "1" = "640×480, as on the Xbox", "6" = "about 4K" }`. Every key is a value of the range, written plainly (`"1"`, not `"01"`). |
| `config` | `""` | A dotted key of `<package.content>/config.default.toml` (`render.scale`): the choice goes in the seed config.toml (`package --seed-config`). The default must equal that file's value; a choice's values must read as its type, and a range's key must be a whole number there. |
| `flag` | `""` | A switch of `package` (`--hud-pack`), passed when the box is ticked. Only `kind = "bool"`. |

Each option has exactly one of `config` and `flag`. `wizard config` checks
the icon, the config keys and the flags against the tag's tree.

`[[wizard.credit]]`: the game's own open-source credits
([wizard.md](wizard.md#credits)). Entries with `own = true` (the game's
repository) come first, before the CLI's own repos; the others (anything
only the game uses, such as a font) lead the third-party projects. No
two entries share a url.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | The project's name, the link's words. |
| `role` | required | What it does for this game, in a few plain words. |
| `license` | required | An SPDX licence expression (`MIT`, `OFL-1.1`, `MIT OR Apache-2.0`). |
| `url` | required | Its page, `https://` with no spaces or control characters: the GitHub page when there is one. |
| `own` | `false` | `true` for the game's own repository: listed with my repos, first. |


## [public]

This table is optional. With it, the game has a public repository, and
`public init`, `public export` and `public audit` publish to it
([layout.md](layout.md#publishing)); doctor then also requires
`checks.yml` and `openspec/`. The table is published with the tree, so
nothing private goes in it: the patterns and rewrite rules live in
`$XBR_PUBLIC_DIR` ([layout.md](layout.md#private-inputs)).

| Key | Default | Meaning |
|---|---|---|
| `url` | required | The public repository, the ssh form `git@github.com:<owner>/<repo>.git`. `[wizard] repo` must name the same repo. |
| `remote` | `"public"` | The remote `public init` keeps for it. |
| `branch` | `"main"` | The public branch. |
| `mode` | `"export"` | `export`: one fresh commit per round, built from the private tree; `history`: the private branch itself is pushed, so every commit must be clean and carry `author`. |
| `author` | required | `"Name <email>"`: the export's author, committer and tagger, and the one identity the audit accepts. |
| `exclude` | `[]` | Globs on tracked paths never exported, beyond the private set (which they cannot shrink). |
| `allow` | `[]` | Globs for images and binaries allowed besides `docs/images/*.png`. |
| `upstream` | `""` | A fork's upstream ref (`origin/main`): its commits are public already, so the audit skips them, and the export adds the newest one REF contains as a second parent. |
| `upstream_files` | `[]` | Globs allowed while each file equals the upstream's blob (a fork's screenshots). |
| `message` | `""` | The export commit's message; empty is `<name> static recompilation: <YYYY-MM-DD> round`. |

## [layout]

This table is optional: where the repo differs from the standard layout
([layout.md](layout.md)).

| Key | Default | Meaning |
|---|---|---|
| `main` | `"src/main.c"` | The main C source. |
| `extra` | `[]` | Top-level tracked names beyond the standard ones (a fork's upstream files, such as `regen.sh` or `tools`); doctor notes any other. |

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
| `remote_name` | `""` | The project's directory on the bench host. Empty uses the game's slug with `BENCH_BUILD=local` (one base per game) and the checkout's basename with `BENCH_BUILD=host`. |
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

## [bench.db]

What `benchdb` keeps of this game's runs and builds ([commands.md](commands.md#benchdb-verb)). The db path, the roots and the hosts are workspace facts in the private `benchdb.toml`, never here.

| Key | Default | Meaning |
|---|---|---|
| `keep_releases` | `2` | The newest published releases (not withdrawn) whose builds' runs keep their large files: the current and the previous one. |
| `internal_keep` | `3` | The newest internal builds since the last release whose runs keep their large files. The newest integration build is one of them and is never pushed out by branch builds. |
| `heavy_log` | `1048576` | A log over this many bytes is a large file. Frames and disk images always are. |
| `hash_max` | `1048576` | Files up to this many bytes get a sha256 in the artifacts table. |

## [bench.db.regress]

The thresholds of `benchdb regress`.

| Key | Default | Meaning |
|---|---|---|
| `flips_drop` | `0.25` | A run group is flagged when its median flips/s is at most `1 - flips_drop` times the baseline's. |
| `ms_rise` | `1.5` | A run group is flagged when its median frame-time p50 or p95 is at least this many times the baseline's. |
| `build_rise` | `1.5` | A build value is flagged when it is at least this many times the median of the comparable builds before it... |
| `build_min_secs` | `30` | ...and at least this many seconds longer. |
| `build_window` | `5` | How many comparable builds before it make the median. |
| `build_min_baselines` | `3` | Fewer comparable builds are printed with their count and never flagged. |
| `build_min_edges` | `20` | An incremental build is compared by seconds per Ninja edge only when it ran at least this many edges. |

## [release]

Which builds belong to a published version (`benchdb releases`).

| Key | Default | Meaning |
|---|---|---|
| `tag_glob` | `"v*"` | The public tags that are releases. |
| `inputs` | `[]` | The paths, relative to the game root, whose changes make a different build. A release commit that touches none of them (a changelog, docs) still matches the build benched before it. Empty is `src/`, `config/` and the `[pipeline]` and `[build]` paths. The `[toolkit]` and `[cli]` tables of `game.toml` are always inputs. |

Host settings are not part of the manifest: `BENCH_HOST`, `BENCH_DIR` and
the rest come from the environment or the game's `scripts/bench.env`
(`<game> bench --help`).
