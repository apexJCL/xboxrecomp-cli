# Changelog

All notable changes to xboxrecomp-cli are listed here, newest first. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the versions follow [Semantic Versioning](https://semver.org/). A game pins
a release by its tag in `game.toml`'s `[cli]` table.

## [Unreleased]

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

[Unreleased]: https://github.com/apexJCL/xboxrecomp-cli/compare/v0.7.6...HEAD
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
