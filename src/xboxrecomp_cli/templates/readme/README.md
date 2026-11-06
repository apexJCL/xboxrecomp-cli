<!--
  The standard player README (xboxrecomp-cli docs/readme.md). Sections keep
  this order and these headings. Blocks between xbr:readme marks come from
  game.toml; the rest is the game's own prose. Comments starting "write:"
  say what goes there; delete them once written.
-->
# @NAME@

<!-- write: one or two sentences for a player: what the game is, and that this
     turns their own disc into a PC, Mac and Steam Deck game. No toolkit
     jargon here; the developer docs explain the how. -->

![@NAME@ on PC](docs/images/hero.png)

<!-- xbr:readme:download -->
> [!TIP]
> **Get the builder:** download the wizard for your computer from the
> [latest release](https://github.com/@REPO@/releases/latest), point it at
> your own disc image, and it builds and installs the game for you.
>
> | Your computer | Download |
> |---|---|
> | Windows 10 or 11 (x86-64) | `@SLUG@-wizard-<version>-windows-x64.zip` |
> | Mac (Apple silicon) | `@SLUG@-wizard-<version>-macos.zip` |
> | Steam Deck or Linux PC | `@SLUG@-wizard-<version>-linux-x64.tar.gz` |
<!-- /xbr:readme:download -->

**No game is included.** You need your own copy of @NAME@; everything the
builder makes from it stays on your machines.

<!-- xbr:readme:release -->
> [!IMPORTANT]
> **Supported game version.** This port is built for one release of
> @NAME@ only:
>
> | | |
> |---|---|
> | Title | @XBE_TITLE@ |
> | Title ID | `@TITLE_ID@` |
> | Region | @REGION@ |
> | Version | @VERSION@ |
> | `default.xbe` sha256 | @SHA256_LIST@ |
>
> The wizard checks your copy on its Dump page before it builds; by hand,
> `./@SLUG@ fingerprint /path/to/your/disc.iso` says the same. Another
> region or version is **not supported**: it warns "another release", and
> the game may crash or misbehave. A modded copy warns too.
<!-- /xbr:readme:release -->

## How the builder works

<!-- xbr:readme:wizard -->
- Checks your computer and fetches the build tools it needs, each download
  checked against a known checksum. It sends nothing of yours.
- Checks your disc image or game folder, and says before building when it
  is another release or a modded copy.
- Builds an installer for this computer, or for another one: a Windows
  setup program, a Mac app (`.dmg`) or a Steam Deck bundle that adds the
  game to Steam.
- Lets you pick the basic options before building:
@WIZARD_OPTIONS@
- Makes small updates for a device that already has a build, and reuses
  most of the work when you build again.
<!-- /xbr:readme:wizard -->

Step by step, with the SmartScreen and Gatekeeper prompts: [docs/playing.md](docs/playing.md).

## Status

<!-- write: one paragraph on how far the port plays (start to finish? up to
     where?), then the platforms table, then the top known issues (at most
     five; the full list is in docs/playing.md). -->

| You play on | Graphics | State |
|---|---|---|
| Windows 10 or 11 | Direct3D 11 | <!-- write --> |
| Steam Deck, Linux (Proton) | Direct3D 11 (DXVK) | <!-- write --> |
| Mac (Apple silicon) | Metal | <!-- write --> |

## Showcase

<!-- write: two to four stills from play, with a caption each. Fresh shots of
     the current build, never test frames; plain PNGs under docs/images/
     (no text chunks; public export refuses them). -->

### The in-game menu

<!-- write: a shot of the overlay menu and what it changes while you play. -->

### Mods and enhancements

<!-- write: the table from the build's descriptor dump (RECOMP_MODS_DESCRIBE=json),
     one row per mod the menu lists; names, pages and defaults as the menu shows
     them. Never a config.toml key or an environment variable. -->

Every mod is off, or at the Xbox's setting, unless the table says
otherwise. Open the in-game menu, go to the page in the table and change
the option; most apply at once, and Save keeps them for the next start.

| Mod | Menu page | What it does | Default |
|---|---|---|---|
| **Render scale** | Render | Draws the 3D at a multiple of the Xbox resolution (1 to 4): sharper, slower. | 1 |
| **Aspect ratio** | Render | The picture shape the game draws for: 4:3, 16:9, 16:10 or 21:9 (from the next start). | 4:3 |
| **Anisotropic filtering** | Render | Sharper textures on surfaces seen at an angle (0 to 16). | 0 |
| **Overscan crop** | Render | Crops the border a TV would hide (4:3 only). | 0 |
| **2D on guest grid** | Render | Keeps 2D art on the Xbox's pixel grid at render scale 2 and up, so tiled menus and HUD show no seams. | off |
| **Scaling filter** | Render | How the picture is stretched to the window: nearest is crisp, linear smooth, integer whole multiples only. | nearest |
<!-- write: HUD margin (Render) only when a mod registers a HUD table;
     Profile (Shaders) only when the descriptor dump lists shader presets:
| **HUD margin** | Render | Pulls the HUD in from the screen edges on a wide picture (0 to 64). | 0 |
| **Profile** | Shaders | A post-processing shader over the picture: a CRT look, or FSR upscaling. | Off |
     -->
<!-- write: the game's rows (Mods page, Game page, Pack, and Controls when the
     game changes a Controls default) -->

<!-- write: one #### entry per row, in the table's order: an on/off pair for a
     mod that changes the picture (one image, stock left, the mod's right, same
     moment of the same run), or a short note on the mod's origin (why it exists, which
     hardware or game behaviour it comes from) for one a still cannot show. A
     shot not taken yet: TODO(screenshot) comment plus a "Screenshot to come"
     line. -->

#### Render scale

<!-- TODO(screenshot): Render > Render scale, 1 and 3, the same moment -->
> *Screenshot to come: the same moment at render scale 1 and 3.*

## Build requirements and common gotchas

- About 15 GB free for the build, plus room for the installer.
- A Windows 10 or 11 PC, an Apple silicon Mac, or a Steam Deck or Linux PC
  (in Desktop Mode on the Deck).
- An internet connection for the first build (the tools download once).
- Your copy is the supported version (the box above).

<!-- write: the two or three failures players actually hit, each with its fix;
     the rest in docs/playing.md#troubleshooting. -->

## Report a bug

[Open a bug report](https://github.com/@REPO@/issues/new?template=bug_report.yml):
the form asks for what we need (your game version, the build version, where
you play, your settings and the logs). Never attach game files, a disc
image, a save or a built bundle.

## More documentation

| Document | For |
|---|---|
| [docs/playing.md](docs/playing.md) | building by hand, installing, controls, saves, every option, known issues, troubleshooting |
| [docs/developers.md](docs/developers.md) | how the port works, building from source, testing, contributing |

## Legal

This repository holds no game data and no game code. @NAME@ and its assets
belong to their owners; this project is not affiliated with them. A bundle
built from your copy is for your own machines only. License: see
[LICENSE](LICENSE) and [NOTICE](NOTICE).
