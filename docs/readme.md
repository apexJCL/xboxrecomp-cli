# The game README standard

Every game repository built with this CLI has the same player-first README:
what the game is, where to get the builder, which copy of the game it
needs, what the build gives you, and how to report a bug. The dense
material (building by hand, every option, the toolchain, testing) moves to
two docs pages that the README links. A player who has never opened a
terminal can read the README top to bottom and end with the game installed,
or with a bug report that holds everything triage needs.

The skeleton is `src/xboxrecomp_cli/templates/readme/`: `README.md`, and the
issue form under `.github/ISSUE_TEMPLATE/`. They are game-agnostic: every
game fact comes from `game.toml` or from the game's own prose, never from
the CLI.

## Sections

In this order, with these headings, so a player who knows one game's README
finds the same things in the next:

| Section | Holds | From |
|---|---|---|
| `# <name>` | the game's name, one or two plain sentences, the hero image | `[game] name`; prose |
| Download box | a TIP box: the latest release's wizard downloads, one row per platform | `[public] url` (or `[wizard] repo`), `[game] slug`, `[wizard] targets` |
| No game included | one line: you need your own copy | fixed |
| Supported game version | an IMPORTANT box: title, title ID, region, version and the known `default.xbe` sha256 list, then how to check a copy | `[xbe] title`, `title_id`, `region`, `version`, `sha256` |
| How the builder works | the wizard's features, and the options it asks for | fixed list; `[[wizard.option]]` labels |
| Status | how far it plays, a platforms table, the top known issues | prose |
| Showcase | stills from play; *The in-game menu*; *Mods and enhancements*: the mods table, then one entry per row (below) | images under `docs/images/`; the mods' descriptors |
| Build requirements and common gotchas | the requirements checklist and the two or three failures players hit | fixed list; prose |
| Report a bug | the link to the issue form | `[public] url` |
| More documentation | `docs/playing.md` and `docs/developers.md` | fixed |
| Legal | no game data, not affiliated, bundles stay private, license | `[game] name` |

The two docs pages take what used to crowd the README:

- `docs/playing.md`: building by hand, installing each bundle, controls,
  saves, every option, known issues, troubleshooting (with a
  `#troubleshooting` anchor, which the issue form's contact link opens),
  and the crash checklist.
- `docs/developers.md`: architecture, building from source, the toolchain
  and its pins, testing and goldens, environment variables, the docs index
  and contributing.

## Settings as the player sees them

The README names a setting the way the in-game menu shows it: the page,
the option and its value, as in *Render > Aspect ratio > 16:9* or *Mods >
Bloom > off*. It never names a `config.toml` key, an environment variable
or a value as the file spells it. A player changes settings in the menu
(or in the wizard, for the options it asks), and the menu's Save keeps
them; the by-hand reference with the keys lives in `docs/playing.md`.

## Mods and enhancements

The section opens with a table of the mods the game has, one row per mod
or enhancement the in-game menu lists, in four columns:

- **Mod**: the name as the menu shows it: a mod's page title on the Mods
  page, or the option's label on the other pages.
- **Menu page**: where it is (Render, Shaders, Pack, Game, Mods,
  Controls).
- **What it does**: one plain sentence, from the mod's own summary, About
  text or help line; no internals.
- **Default**: the value the menu shows on a fresh install, in the menu's
  words (`off`, `on`, `1`, `4:3`, `Off`).

The rows: every mod on the Mods page (debug and developer mods too, said
as such), the game's own Game page options, a Controls option whose
default the game changes (BLiNX 1 turns Square left stick on), and the
toolkit's enhancements that change the picture, as far as the game's
toolkit pin has them:

| Mod | Menu page | What it does | Default |
|---|---|---|---|
| **Render scale** | Render | Draws the 3D at a multiple of the Xbox resolution (1 to 4): sharper, slower. | 1 |
| **Aspect ratio** | Render | The picture shape the game draws for: 4:3, 16:9, 16:10 or 21:9 (from the next start). | 4:3 |
| **Anisotropic filtering** | Render | Sharper textures on surfaces seen at an angle (0 to 16). | 0 |
| **Overscan crop** | Render | Crops the border a TV would hide (4:3 only). | 0 |
| **2D on guest grid** | Render | Keeps 2D art on the Xbox's pixel grid at render scale 2 and up, so tiled menus and HUD show no seams. | off |
| **Scaling filter** | Render | How the picture is stretched to the window: nearest is crisp, linear smooth, integer whole multiples only. | nearest |
| **HUD margin** | Render | Pulls the HUD in from the screen edges on a wide picture (0 to 64): only where a mod registers a HUD table. | 0 |
| **Profile** | Shaders | A post-processing shader over the picture: a CRT look, or FSR upscaling. | Off |
| **Texture pack** | Pack | Replacement art, font and text, for a game that has a pack. | on, once a pack is built |

Plumbing options (fullscreen, frame pacing, the present surface, the
overlay's size, movies) stay in `docs/playing.md`. The paragraph above the
table says every mod is off, or at the Xbox's setting, *unless the table
says otherwise*: a row whose default is on (a HUD mod, a texture pack, a
Controls default) is the exception it points at.

Take the rows from the build, never from memory:

- `RECOMP_MODS_DESCRIBE=json RECOMP_MODS_DESCRIBE_OUT=mods.json
  build/<exe>` writes every mod's descriptor and exits before the game
  starts: each mod's title, summary and settings with their labels, help
  lines, choices and defaults, and the shader presets.
- The mod's source has the About text (the descriptor's second string),
  which is where a mod says why it exists.
- The toolkit's `src/enhance/enhance_menu.c` (`k_items`) has the Render,
  Shaders and Pack labels, choices and defaults.
- The Profile row stays: every game has the Shaders page, and `features`
  fails when this row is missing.

After the table, one entry per row, in the table's order, under a
`####` heading with the mod's name:

- **A mod that changes the picture** (render scale, aspect, the HUD at
  the edges, shaders, bloom, texture packs, filters, overlays) gets an
  on/off pair: one image, the stock setting on the left and the mod's on
  the right, each half labelled, at the same moment of the same run, so
  only the setting differs. The caption says what to look at.
- **A mod a still cannot show** (input, speed, timing, audio, saves,
  shop prices) gets a short note on its origin instead: why it exists,
  and which console hardware or game behaviour it comes from (a stock
  speed cap, a 2004 TV's overscan, the NV2A's fixed 640x480).
- **A shot not taken yet** gets a marked placeholder, so it is easy to
  find and fill:

  ```markdown
  <!-- TODO(screenshot): Mods > Collision overlay, off and on, stage 1 -->
  > *Screenshot to come: the collision overlay, off and on.*
  ```

## Generated blocks

The parts that restate `game.toml` sit between marks, so they never drift
from the manifest:

```markdown
<!-- xbr:readme:release -->
...
<!-- /xbr:readme:release -->
```

| Block | Restates |
|---|---|
| `download` | the release page and the three wizard file names (`<slug>-wizard-<version>-windows-x64.zip`, `-macos.zip`, `-linux-x64.tar.gz`), for the targets `[wizard]` offers |
| `release` | the `[xbe]` keys, the region in words (`NTSC-U`, `PAL`, `region-free`: the CLI's `xbe.region_text`) and the check (`<slug> fingerprint`) |
| `wizard` | the feature list, with one line per `[[wizard.option]]` label |

Until `xbr readme` exists (below), a game fills them by hand from
`game.toml` and `<slug> fingerprint --toml`; the marks stay, so the command
can take them over without a rewrite. Everything outside the marks is the
game's own and no command touches it.

The release links point at `releases/latest`, never at one file: the
wizard's assets carry the version in their names, so a per-file link would
go stale at the next release.

## The issue form

`.github/ISSUE_TEMPLATE/bug_report.yml` (and `config.yml`, which keeps blank
issues and links the troubleshooting section) asks for:

- what went wrong: the build, the install, a crash, or wrong output;
- what happened, the steps, and whether it repeats;
- the copy of the game: the supported release, another release, a modded
  copy or not known, and the `fingerprint` output or the log's
  `[BOOT] build xbe:` line (a game without `[package] stamp_env` has no
  such line: drop that half of the hint);
- the build version (the wizard's or the bundle's name), and wizard or CLI;
- where the player plays, the GPU backend, and the system (OS, CPU, GPU,
  driver, Proton);
- the save, described: how far, where it came from, whether a new game
  shows the same problem;
- the settings the player changed, as the menu shows them (page, option,
  value) or the wizard's options; the settings file only as an
  attachment;
- the logs, with each platform's data folder (`[package] app`) and the
  wizard's `wizard.log`;
- a required tick that nothing attached holds game files, a disc image, a
  save or a bundle.

The tokens are `@NAME@`, `@SLUG@`, `@APP@` (`[package] app`), `@REPO@`
(`owner/name` from `[public] url`), `@TITLE_ID@` and `@RELEASE@` (region
and version in words). The README skeleton adds `@XBE_TITLE@`,
`@REGION@`, `@VERSION@` and `@SHA256_LIST@` (the `release` block) and
`@WIZARD_OPTIONS@` (one line per `[[wizard.option]]` label). A game whose
bug reports need more (KUF's battle number, a racing game's track) adds
fields after *Steps*; it keeps the others.

## Images

The showcase is the README's one place for pictures, and the legal rules
hold there as everywhere:

- Fresh shots of the current build, taken for the README: never golden
  references, frame dumps from test runs or A/B captures.
- A small set: a hero, two to four stills, one menu shot and one on/off
  pair per mod row that changes the picture. Name them by what they show:
  `stage3.png`, `menu.png`, `pair-<mod>.png` (`pair-aspect.png`,
  `pair-bloom.png`).
- A Mac (Metal) build is fine for every shot; shoot at stock resolution
  and 4:3 unless the shot is about render scale or aspect. The frame dump
  (`fb_dump`) holds the game's picture only; a shot with the menu, a
  shader or an overlay card needs the window shot (`window_shot`), which
  reads the drawable after the overlay pass.
- Plain PNGs under `docs/images/`, with no text or Exif chunks (`public
  export` refuses them, `layout.png_problem`), each well under 1 MB:
  downscaled to the width the README shows (1280 at most) and quantised
  where it does not band. A before/after pair is one image, side by side or
  stacked, at the same moment of the same run.
- The owner approves the final set before any public push.

## Checking a README

A README meets the standard when:

1. the section headings above are all there, in order;
2. the mods table has a row for every mod the descriptor dump lists on
   the Mods page, and each row has its entry (a pair, an origin note or
   a marked placeholder);
3. it names no `config.toml` key and no environment variable;
4. the marked blocks match `game.toml` (the sha256 list, region, version,
   title ID, slug, repo);
5. every image it links exists under `docs/images/` and passes
   `png_problem`;
6. `docs/playing.md` and `docs/developers.md` exist, and the first has a
   troubleshooting section;
7. `.github/ISSUE_TEMPLATE/bug_report.yml` exists and has no `@TOKEN@` left.

## Planned: `xbr readme`

Not built yet (openspec change `readme-standard` in the BLiNX 2 repo, phase
2): `<slug> readme` would write the skeleton for a game that has none, fill
or refresh the marked blocks from `game.toml`, write the issue form, and
with `--check` run the list above, for `doctor` and `public export` to call.
`xbr new` would write both files for a new game.
