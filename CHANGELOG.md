# Changelog

All notable changes to xboxrecomp-cli are listed here, newest first. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the versions follow [Semantic Versioning](https://semver.org/). A game pins
a release by its tag in `game.toml`'s `[cli]` table.

## [Unreleased]

### Added
- A native Windows build host: with `BENCH_BUILD_HOST` (and
  `BENCH_BUILD_DIR`), bench commands build the Windows exe on that PC under
  its own lock instead of cross-building on the Mac, then pull it and check
  its sha256. The game, toolkit and CLI travel as git bundles, and gen/
  only when its digest moved. `bench buildhost build` and
  `bench buildhost run ARGS [--pull REL]` drive it directly.

## [0.13.0] - 2026-10-28

### Added
- A golden scenario can boot on a seeded hdd: `RECOMP_HDD_DIR=@seed:PATH`
  in its env names an hdd tree under the game root (private and gitignored,
  like the reference frames). `golden run` copies it into the run's `hdd/`
  and points `RECOMP_SAVE_DIR` at the copy's `UDATA/`. `bench golden`
  rsyncs it once to the host's `<base>/golden-seeds/PATH`, and
  `run_game.sh` copies it into each run and deletes the copy however the
  run ends. A missing seed stops both before any run. Every seed path is
  checked first: relative, no `..`, inside the game root and outside the
  game files, with a `UDATA/` and no symlinks.
- `dump_slack` takes `[before, after]` as well as one flip count, for a
  host that reaches an anchored frame only early or only late.
- Flip-timed pad scripts in golden scenarios (`f<flip>` steps, from the
  toolkit's pad recorder). `load_golden` parses every scenario's script
  with the runtime's grammar (decimal only, stick values `lx=<v>` and the
  like) and refuses an unknown button.
- `golden play SCEN --record FILE`: plays the scenario's boot in a window,
  with the keyboard and the host's sound, on a copy of its hdd seed (never
  the player's own saves) under the Mac run lock, and records the pads.
  `--no-script` starts at the title.
- `golden pad FILE [--from F] [--to F] [--shift N]`: turns a recording
  into a scenario's pad script.
- A scenario can name its script as a file, `RECOMP_DEBUG=pad_script=@PATH`
  (or the older `RECOMP_PAD_SCRIPT=@PATH`), with PATH under the game root
  and kept private like a seed. `bench golden` ships it to the host's
  `golden-seeds/PATH`, and `run_game.sh` hands Wine the copy as a `Z:`
  path. A path that is absolute, starts with `~` or leaves the root is
  refused at load.
- `NOREF`: a frame with no reference yet is INCOMPLETE, never a pass, and
  the run still dumps it so `golden reference` can record one.
- `golden check` compares the runtime's `PAD: script of N step(s)` with
  the CLI's count of the scenario's script. A different count FAILs, and
  a missing line is INCOMPLETE.
- `--backend` on `golden dumpat`, `frames` and `pulls`: frames that
  `skip_backends` leaves out are no longer dumped, pulled or waited for.
  `golden run` and `bench golden` (d3d11) plan per backend.

### Changed
- `menu.enabled` and `overlay.stats` are built-in stock keys (stock `off`):
  the golden check fails a run with the in-game menu or the stats strip on
  without any `game.toml` row, and `golden.enhance_stock` rows for them are
  refused.
- A golden run with no seed gets an empty `hdd/` of its own, removed after,
  instead of the runtime's default hdd root (the player's own disk), unless
  the scenario sets a plain `RECOMP_HDD_DIR`. `golden play` does the same.
- The input pins that `golden play` drops come from `game.toml`
  (`[input] play_unset`). The CLI keeps only `RECOMP_HEADLESS`.
- The pace check judges a run against the references of its own backend
  (a label of `metal` or `metal-...`), or all of them when none of those is
  paced. A frame that paces differently per backend is no longer a
  mismatch against the other backend's reference.

### Fixed
- benchdb records builds from every host and verb. A top-level `host` key
  in `benchdb.toml` names the writer's host; its records go to that host's
  inbox (a relative inbox is relative to the config). `all` times its
  recomp. Ingest fills the build host of older runs from their shipped,
  built-on and synced lines, and leaves an unknown name empty.

## [0.12.0] - 2026-10-24

### Added
- Native Windows bench hosts (`BENCH_HOST_OS=windows`) for `bench golden`
  and `bench run`: the controller ships the exe and changed runtime files
  over ssh to a per-run directory and drives the game through PowerShell
  (`bench/host/windows/`). The host's lock, `.bench-lock` beside
  `BENCH_DIR` by default, records the holder's pid and start time; a lock
  whose holder is gone is taken over in place, and that run's game is
  ended. The D3D11 shader cache lives at `<base>/shader-cache`, so it stays
  warm across runs.
- `known_divergence` on a golden frame: a host-scoped, known difference
  (with the item that tracks it) reads KNOWN on that host instead of FAIL.
  It applies only to an ordinary outside-limits compare; a size mismatch,
  a missing reference or a gross failure stays FAIL. A scenario whose only
  failures are KNOWN reads `pass-with-known`, never `pass`, and keeps its
  frames through every prune. The public export drops these entries.
- `golden-sessions.tsv` lines end with the host's OS (`host=posix` or
  `host=windows`).

### Changed
- `recomp` passes `--back-edge-yield` when the toolkit's recomp defines it
  (upstream xboxrecomp v0.16.0 and the fork after its v0.16 merge). There
  the flag is off by default. Without it a loop back edge no longer checks
  the guest scheduler, and a guest thread spinning on another's store holds
  the one guest CPU. The fork's toolkits before that merge yield with no flag
  and get none, so their `gen/` is unchanged.

### Fixed
- `bench run` no longer records `flips: 0` for a run without the flip
  trace. `run-info.txt` falls back to the D3D11 backend's last
  `[d3d11] present #N` line (logged at the first present and every
  300th) and marks the
  count as a floor (`at least`). `bench run` now prints the run's flips/s.
  Under `min_flips`, a floor count within 299 of the minimum is
  INCONCLUSIVE; one further below fails as before.

## [0.11.0] - 2026-10-21

### Added
- `[toolchain] sdl3` in `game.toml`: the SDL3 release a game's toolkit is
  tested against. `pins refresh` pins its GitHub source tarball
  (`SDL3-<ver>.tar.gz`) under `sdl3` in `setup-pins.json`; a game without
  the key gets the same file as before.
- `doctor` on macOS prints an `sdl3:` line with the installed version
  (`pkg-config`, else Homebrew's keg) and the pin, and a warning when the
  installed major.minor is older. It never blocks the macos target.

### Changed
- `golden run` sets `SDL_AUDIO_DRIVER=dummy` (SDL3's name) as well as
  `SDL_AUDIODRIVER=dummy`, so a run stays silent on toolkits on either SDL.
  The old name goes once no game pins an SDL2 toolkit.
- `docs/porting.md` installs `sdl3` and names `SDL_AUDIO_DRIVER`.

## [0.10.1] - 2026-10-21

### Fixed
- `package` and `setup` move a toolkit clone this CLI made to the game's
  `[toolkit]` pin when an update moved it. Before, setup ran only for a
  missing toolkit, so an updated game was built against the toolkit its
  first setup cloned and could fail to compile. `doctor` reports such a
  clone as a problem (`toolkit not at the pin`). A developer's own
  checkout (no pin mark) is still left alone.
- A failed step prints the compiler's, linker's and Ninja's error lines
  before the log's last lines, and the wizard's failure excerpt leads with
  them: a compile with several errors no longer scrolls them out of view.

## [0.10.0] - 2026-10-21

### Added
- `public export --fresh-root OLD [--drop-tags]`: an approved history
  rewrite. The round's commit is a root replacing the public tip OLD, and
  a one-time allowance lets the guard pass exactly that push (and the
  listed tag deletes); the next plain export removes it.
- `public pr NAME [REF]`: a fork's upstream PR branch, audited commit by
  commit and put in `refs/public-pr/NAME`. The guard lets only it reach
  `pr/NAME`, rebased or deleted.
- `xbr new` writes `packaging/config.default.toml` with the toolkit's
  option defaults at the toolkit branch it pins (`[render]`, `[display]`
  and `[present]`, with `present.pacing = "sleep"`), so a new game no
  longer starts from another game's copy and its drift. The template is
  the one place the CLI holds those defaults.
- doctor's `defaults:` line names the keys a game's `config.default.toml`
  sets to another value than the toolkit's defaults (a note, never a
  problem).

### Changed
- The build wizard: a fresh build can tick several platforms (the shared
  work runs once, then one package run per platform); Welcome shows its
  statements as tiles with tooltips and Fresh and Update as two large
  tiles; the credits open from a footer button on every page; player
  errors keep their words, other errors point to the log with Copy error,
  Copy log path and Open the log folder.
- The `.gitignore` baseline anchors its top-level lines (`/build-*/`,
  `/external`), so a spec named `build-x` or a `src/external/` stays
  tracked, and matches the dump, `.venv*`, `third_party`, `external` and
  `bench-logs` without a trailing slash, so a worktree's symlinks are
  ignored. Existing games see the drift in doctor, and `public audit` and
  `export` fail on it until the game's `.gitignore` and
  `scripts/test_gitignore.py` take the new lines.
- The refusal list takes `third_party`, `external` and `.venv*` at the top,
  as folders or symlinks.
- The home-path checks (`public audit`, `scripts/audit-public.sh`) exempt
  a Wine prefix's own `drive_c/users/steamuser/` and `drive_c/users/public/`
  (also as `C:` and with backslashes) and GitHub's `github.com/users/`,
  which are no one's home; every other home path stays a hit, without
  case, in either separator. The script also checks drive-letter Windows
  paths, and prints a hit's line as committed. Path names get the
  built-in checks too.
- `doctor` on a plain clone of the public repo (HEAD on a public remote's
  branch) gives one note for the push guard instead of four problems; the
  maintainer's repo, `public audit` and `export` keep them.
- `public export` loads the rewrite file before fetching, and runs its
  `!fail` checks over the commit message too. `public audit` reads a tip
  that is not a fast-forward of its base whole only when an
  `--fresh-root` allowance approves it, and refuses it otherwise.
- `public init` writes the new guard: run it again in each maintainer repo.
  The guard needs git 2.31 or newer and refuses every push to the public
  repo on an older one.

### Fixed
- `bench run`, `pacing` and `all` start the game from the Windows
  package's `launch.env.default` (`<content>/launch.env.default.windows`),
  as the installed launchers do, with `BENCH_ENV` and a scenario's pins
  replacing its keys; the launcher-only keys (`LOG_KEEP`, `UMU_RUN`, ...)
  stay out. Without it the exe got no `RECOMP_PB_BACKEND`, fell
  back to the CPU rasteriser and looked hung. The bench warns when no
  backend is set. `golden` is unchanged: its scenarios pin their own env.
- `package` works on a game folder that is not a git repository, or a
  repo with no commits yet, and on a toolkit or CLI tree like that: the
  version names a digest of the tree's files (`cnogit<6>` or
  `cnocommit<6>` in place of the sha, always `-dirty`), the manifest's
  `sources` record it as `content`, and the README says which. It used to
  stop with a traceback after git's `fatal:` lines. A game folder inside
  another repo is named by its files, not that repo's commit. A real git
  error (dubious ownership, a corrupt repo) or an unreadable file stops
  package with git's message or the file's name. A normal repo's version
  is unchanged.
- `doctor` says "no commits yet" for a repo before its first commit, and
  for both modes how package names the tree.
- The `xbr new` `.gitignore` ignores `.xbr/`, the stage records of a
  folder outside git.
- The HUD pack's stage key names the CLI's source folder, so an installed
  CLI keys it too.

## [0.9.0] - 2026-10-18

### Added
- The standard game repo layout: `[public]` and `[layout]` in `game.toml`
  describe a game repo. `public init` installs a pre-push guard, `public
  export` builds the scrubbed public history as one commit on the public
  tip, and `public audit` checks it; refs move only after the audit
  passes. `doctor` reports the layout, and `xbr new` scaffolds a game repo
  with `checks.yml` and the `.gitignore` baseline.
- The build wizard asks for a Fresh build or an Update of an installed
  one; an update is planned against the install's manifest, so the HUD
  pack can be added, kept or cleared without rebuilding from the disc.
- `package --hud-pack`, `--keep-hud-pack` and `--clear-hud-pack`, a pack
  store for kept packs, and a strict `--patch-from`. Each manifest records
  the game tree it was built from.
- Compile jobs are sized from free memory and cores (`--jobs` overrides);
  `package`, `build` and the import refuse before starting when the disk
  is short. `doctor` reports memory, cores, disk and jobs; the wizard
  shows a "This computer" check, a Build jobs choice and Retry after a
  disk refusal.
- `fingerprint [PATH]`: the XBE's verdict from its sections' hashes
  (`[xbe.sections]`): known, header only, modded data or modded code.
  `--toml` prints the table for `game.toml`.
- `manifest.json` records `xbe` (`sha256`, `verdict`, `sections`), and
  `[package] stamp_env` names a `launch.env.default` key the bundle sets
  to the verdict, for the game to print at boot.
- `package --plan --json` reports `xbe`; the build wizard asks once before
  building modded code.
- `doctor` prints an `assets:` line against `[data] reference_files`.

### Changed
- A patch built from other game files, or one that would delete game
  files, is refused: `game_files/` is shared across versions, so a
  rollback could not bring them back.
- In export mode, `public export` and `public audit` exit 2 when the
  rewrite file is missing, instead of running with no rewrite rules; the
  range audit also runs the rewrite file's `!fail` checks over added
  lines.
- A modded or unknown dump only warns, in `import`, `analyze`, `build`,
  `all`, `package` and `doctor`. `manifest.json`'s `xbe` was the string
  `"unknown"` for an unlisted dump and absent otherwise.

## [0.8.0] - 2026-10-17

### Added
- Incremental `package`: each stage (import, generate, icon, compile, HUD
  pack, stage, wrap) records its inputs in `.git/xbr/`, and a run redoes
  only the stages whose inputs changed. `--verify` hashes every input.
  `--clean` drops the build trees and the stage, and `--clean=all` drops
  gen/ too.
- `package --plan [--json]` says what a run will do, each stage's reason,
  the time it will take, the bases it can patch from, and the output it
  predicts. `clean` frees the build folder (`--bundles`, `--build`,
  `--all`).
- Patches: `package` makes an update from the installed version (or from
  `--patch-from`) when it is under half the full bundle. SteamOS gets
  `install.sh patch`, Windows a versioned layout with `apply.exe` and
  rollback, and macOS a patch DMG with `apply.command`. Every applier
  checks the base first and changes nothing on a mismatch. See
  docs/bundle.md.
- Reproducible builds: the version and build times come from the commits
  (or `SOURCE_DATE_EPOCH`), and a non-stock build carries a `-o` options
  digest in its version.
- `[wizard] private`, and manifest schema 2 with the game tree's `source`
  and the `volatile` fields.
- The wizard keeps and updates its clone between releases, says what the
  build will make (an update or the full installer, with Change… and
  Rebuild everything), gives each update's apply step, and shows the
  build folder's size with Free space. Developers get `--source
  PATH[#REF]` and `--patch-from`.

### Changed
- A game whose stock build leaves the enhancements layer off reads no
  config file. Once a build exists, its CMake cache decides.

## [0.7.7] - 2026-10-17

### Changed
- The README is now for porters; the player's guide moved to
  docs/wizard.md, For players.

### Fixed
- The SteamOS bundle pins GE-Proton11-7 instead of GE-Proton11-9. Under
  11-9, winedevice crashes in hidclass.sys (`unimplemented function
  win32u.dll.__wine_send_input`), so winebus never starts and the game sees
  no controllers, in Desktop and Game Mode alike. 11-7 is the release the
  Proton benches and goldens run on. Updating downloads 11-7 once; the
  GE-Proton11-9 copy in compatibilitytools.d can then be deleted, and
  rolling back to an 11-9 bundle brings the missing controllers back.
- The SteamOS launcher pins its own sha256-checked umu and Proton: a
  `PROTONPATH` in the bundle's `launch.env.default` no longer overrides the
  pin (only the environment or `config/launch.env` do), so umu stops
  querying GitHub on every launch. A second launch while the game is
  starting or running says so instead of starting twice, and the
  runtime-update marker is cleared when the runtime is absent, so the
  "Checking the Steam Linux Runtime" notice shows once.

## [0.7.6] - 2026-10-16

### Added
- `doctor` names the toolkit's features (env table, enhancements, D3D11,
  Metal, spin waits) and what it lacks; `doctor --json` gives them as
  `toolkit_features`.

### Changed
- A game can pin upstream xboxrecomp (`sp00nznet/xboxrecomp`) instead of
  the fork. When the toolkit's recomp has no `--spin-waits`, `recomp`
  leaves the flag out with one warning, so `gen/` is what the toolkit's
  own commands give.
- When the toolkit at the pin has no enhancements layer, the wizard leaves
  out the options that set a `config.toml` key, which nothing would read,
  and `wizard config --json` lists them under `omitted` with the reason.
  The config's new `reads_config` key says whether the build reads
  `config.toml`; when false, the Options page says the game has no
  settings file instead of pointing at it. `wizard config` and
  `wizard build` now read the toolkit at its pin, so they need the toolkit
  checkout or the network, for a game on the fork too.
- The README links Burnout 3 to its releases at apexJCL/burnout3-recomp.
- `golden` treats `render.aniso` as a non-stock key, so goldens run with
  anisotropic filtering off, and the manifest lists it as a toolkit
  enhancement key.
- README text can depend on the toolkit: a line holding only
  `@if enhance@` or `@if !enhance@` opens a block that `@endif@` closes, in
  `README.txt.in`, `README.intro` and any `README.part`. The first is kept
  when the toolkit at the pin has the enhancements layer, the second when
  it has none (docs/manifest.md, "Capability blocks"). A game on the fork
  gets the same README as before.
- The release workflow `xbr new` writes edits and publishes only a draft
  the workflow made. A release a maintainer made by hand, or one already
  published, keeps its title, notes, pre-release mark and draft state; the
  job attaches the assets only when the release has none of them, and
  otherwise uploads nothing and names the clashing files. It creates a
  release only when gh says there is none; any other error stops the job.
  Games made before this version copy the new workflow from the template.

### Fixed
- `build` and `package` configure a build tree afresh when its cache names
  another toolkit than the current one (a move between the upstream and
  fork pins, or a new `XBOXRECOMP_DIR`), and say so in one line. The tree
  kept the old toolkit, so `build macos` linked the fork's `gen/` against
  upstream libraries and failed on an undefined `g_xbox_spin_sleep`.
- On a toolkit pin with no enhancements layer, `package` no longer tells
  players about files the game does not read. It stages no
  `config.default.toml` (a game needs none), README.txt no longer says the
  game makes `config.toml` or lists the options, and the SteamOS section
  drops the gamescope scaling and key binding text. `--hud-pack` and
  `--seed-config` are refused before the build. A developer's own toolkit
  checkout (no pin mark, or `XBOXRECOMP_DIR`) answers from its working
  tree, offline, as that is what gets built. A clone made by setup is
  checked at the pin with the wizard's check, from the clone's history;
  that needs the network only when the clone lacks the pinned commit or
  there is no toolkit yet (when setup needs it anyway). Each pin is read
  once per run.

## [0.7.5] - 2026-10-15

### Added
- A plain README with a Getting started for players.
- `CHANGELOG.md`, and `scripts/release-notes.py` to print one version's
  notes for its GitHub release.
- `xbr new` writes a starter `CHANGELOG.md`.

### Changed
- The release workflow `xbr new` writes publishes the release with the
  tag's `CHANGELOG.md` section as its notes, once all assets are
  attached, marks `-rc.` tags as pre-releases, and stops before building
  when the tag has no notes.

## [0.7.4] - 2026-10-15

### Added
- The wizard's Building page lists the build's parts in order, each with
  its own state, progress, clock and log; Details keeps the combined log.
- The wizard asks for an output folder (beside the wizard by default),
  checks it can write there and has the space, and passes it to `package`.
- When a build fails, the wizard shows the step, its exit status, an
  excerpt of the log and the log folder.
- The font hint on the Options page appears only while the font the HUD
  pack needs is missing (`xbr wizard font` does the lookup).
- Credits for the game, the CLI, the toolkit and the third-party projects
  (`[[wizard.credit]]`), and a `NOTICES.txt` with their licences in every
  wizard archive.
- Every wizard binary carries a gamepad icon, so `[wizard] icon` is now
  optional.

### Changed
- `package --plain` prints a heartbeat after 20 seconds of silence and
  writes step logs as it goes, so a long recompile no longer looks stuck.
- Stopping a build asks for confirmation and says what a re-run reuses.
- Links that open the browser are marked as external.

### Fixed
- The leak check before a public push also catches Windows paths and
  JSON-escaped paths.
- `bench` no longer copies the CLI's ignored caches (the wizard's Go
  module cache) to the bench host, where the sync failed.

## [0.7.3] - 2026-10-15

### Fixed
- `package windows` now completes on a Windows host: the host name lookup
  works there, the installer finds its files, and the installer's space
  estimate includes the game files that setup copies.

## [0.7.2] - 2026-10-15

### Added
- `[wizard]` options can be a `range` (a slider with a label per value)
  and carry a `hint` and a `hint_link`.

### Changed
- The wizard's pages are written for players rather than developers: a
  step bar, a Welcome page that says what the wizard downloads and sends,
  and a Checks page with one plain row per dependency, fix buttons and a
  log that opens on demand.
- The update check waits until the player leaves the Welcome page and
  honours its checkbox.
- Light mode uses darker status colours, for readable contrast.
- Homebrew runs with analytics off.

### Fixed
- Links open on Windows.

## [0.7.1] - 2026-10-15

### Added
- `xbr new` finds D3D's fence wait in the dump and writes the game's
  fence-mirror line, so a fresh scaffold no longer hangs at its first
  fence wait.

## [0.7.0] - 2026-10-15

### Added
- The player build wizard: a generic desktop app that a game configures
  through a `[wizard]` table in `game.toml` and publishes, one download
  per platform, that turns a player's own dump into an installable build.
- `xbr wizard config`, `wizard build` and `wizard check` to configure,
  build reproducibly and gate the wizard.
- `doctor --json`, `package --seed-config` (the chosen options become the
  first install's settings) and `pins refresh --wizard`.
- `xbr new` writes a release workflow that builds the wizard on a tag.

### Changed
- New games disassemble every code section by default
  (`text_only = false`), so calls into the XDK libraries are no longer
  stubs. Games that set the key keep their behaviour.

## [0.6.7] - 2026-10-14

### Changed
- `xbr new` lists both the Windows and macOS build targets when the
  toolkit's template supports the Mac, and the Mac builds natively.

## [0.6.6] - 2026-10-14

### Added
- `xbr new --xbe` accepts a disc image or a dump folder as well as
  `default.xbe`.

### Changed
- The SteamOS README's Controls section says where the bindings live and
  recommends the Gamepad template on the Steam Deck.
- `bench integrate` keeps the mouse off, as the goldens do.

### Fixed
- The quick start's `new` then `import` no longer rejects every dump for a
  wrong title ID.
- `xbr --help` works outside a game and says how to start one.

## [0.6.5] - 2026-10-13

### Added
- A game that ships sleep-based frame pacing gets a note in its bundle
  README and in `package`'s output on how to switch back to the
  console-original pacing.

### Fixed
- A defaults file that is not valid UTF-8 gives a warning instead of a
  traceback.

## [0.6.4] - 2026-10-13

### Added
- `bench` runs the toolkit's audio voice tests (`apu_voice_mask`,
  `apu_voice_lock`) under Proton.

## [0.6.3] - 2026-10-12

### Added
- `[enhance.hud]` keys `font_margin`, `font_stretch` and
  `font_full_height` to fine-tune the re-rendered font atlas.

### Changed
- The font atlas uses one proportional scale for the whole font, so text
  has an even weight, and descenders stay on the baseline.

## [0.6.2] - 2026-10-12

### Added
- Golden anchors can start after another anchor (`"after"`), so a chain
  of screens is found at any pace.
- A golden frame can set a `verdict_window` for screens that animate on
  their own clock.

### Fixed
- Unknown or cyclic anchor chains are reported when the golden file
  loads, not mid-run.

## [0.6.1] - 2026-10-12

### Fixed
- The SteamOS launcher tells the game when it runs under gamescope, so
  Steam's scaling filter (FSR) works in Game Mode.

## [0.6.0] - 2026-10-11

### Added
- `<game> import PATH` builds `game_files/` from a disc image (`.iso`,
  `.xiso`) or an extracted folder in one step, with `--check` and `--link`.
- `package <target> --dump PATH`, and `<game> PATH` as its shorthand.

## [0.5.1] - 2026-10-11

### Fixed
- The SteamOS installer installs the bundle's subdirectories, so a HUD
  pack reaches the installed game, and repairs a damaged install.
- The Windows installer removes a HUD pack left by an earlier install.

## [0.5.0] - 2026-10-11

### Changed
- Games ship their stock settings as `config.default.toml` and players
  keep only their own changes in `config/config.toml`, so new settings
  reach existing installs. A game that still ships
  `enhance.toml.default` is told to rename it.
- The README is a short reference; the long form moved to `docs/`.

## [0.4.0] - 2026-10-11

### Added
- Version 2 HUD packs: art, font and text can be switched off separately,
  text is encoded at 2x as BC3, and text is taken from the game's language
  archives. Version 1 packs still load.

## [0.3.1] - 2026-10-11

### Added
- `enhance hud` also replaces pre-rendered text kept in MIXF/XPR0
  containers, such as mission objectives.

## [0.3.0] - 2026-10-10

### Added
- `xbr enhance hud` makes a private, upscaled HUD texture pack from the
  owner's own dump, optionally re-rendering the font from a TrueType font
  the owner supplies.
- `package --hud-pack` adds that pack to a bundle.

## [0.2.0] - 2026-10-10

### Added
- `game.toml` can pin the CLI and the toolkit by release tag. With a
  commit as well, a tag that moved is refused rather than run.
- `pins refresh` reports the newest release for a tag pin, and `new`
  pins the release it runs from.

## [0.1.0] - 2026-10-10

### Added
- First release: the toolchain CLI extracted from BLiNX 2 as `xbr`, driven
  by a game's `game.toml`, so any game can use it.
- `new`, `setup`, `doctor`, `analyze`, `recomp`, `build`, `all` and
  `pins refresh` to start a game and build it.
- `package windows|steamos|macos` for private bundles, including a
  SteamOS installer that works on a stock Steam Deck.
- The bench and golden tools for testing under Proton and on the Mac.
- `scripts/audit-public.sh` for checking a push before it goes public.

[Unreleased]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.7...v0.8.0
[0.7.7]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.5...v0.7.6
[0.7.5]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.7...v0.7.0
[0.6.7]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.6...v0.6.7
[0.6.6]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.5...v0.6.6
[0.6.5]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/apexJCL/xboxrecomp-cli/releases/tag/v0.1.0
