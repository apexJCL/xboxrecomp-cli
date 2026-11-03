# The wizard rework: design spec

The target design for the build wizard (`wizard/internal/screens`). It is
the contract to build from: the screens in `screens/` show the look, and
this file says what each part does, which words it uses and which CLI
command is behind it. Nothing here is built yet; where this file and the
code disagree, the code is today's wizard and this file is where it is
going.

The goal: a player with no build knowledge gets their build without
reading anything technical, and when something goes wrong they can hand
the technical side to whoever helps them in one step.

## How to read the screens

- `screens/NN-name.html` opens in any browser; the links between screens
  work (Back, Next, Get help). `screens/NN-name.png` is the same screen at
  780 px, the window's opening width (`wizard-window-w`).
- They are HTML renditions, **not code to port**. The wizard stays Fyne
  (v2.8.2) in Go. Each screen section below names the Fyne widget or the
  wizard's own widget (`pathTile`, `thinBar`, `notice`, `tipItem`,
  `partsView`, `logPane`, `outlineButton`) that draws each part.
- Colours, type and spacing are the xboxrecomp-cli design system's tokens
  (the wizard theme, `palette.go`). The departures are listed under
  [Design system notes](#design-system-notes).
- Sample values (Stage A–D, times, sizes, update IDs, the paths) are
  placeholders. The real ones come from the config, the plan and the run.

| File | Screen |
|---|---|
| `01-welcome` / `01b-welcome-explore` | Welcome, Play or Explore chosen |
| `02-your-game` / `02b-your-game-checked` | Your game, before and after the pick |
| `03-platforms` | Platforms |
| `04-options` | Options |
| `05-review` | Review |
| `06-building` | Building |
| `07-done` | Done |
| `10-needs-setup` | Platforms when a check needs the player (a Mac without the Command Line Tools) |
| `11-build-stopped` | A failed build |
| `12-get-help` | Get help: the report |
| `20-update-device`, `21-update-changes`, `22-update-done` | The Update build's own pages |
| `30-explore-what`, `31-explore-exporting`, `32-explore-done` | Explore: the levels |

## The structure

Welcome asks one question with two answers, then a second question
inside the one picked:

- **Play**: make the game run on a computer.
  - **Fresh build**: today's `model.PathFresh`.
  - **Update build**: today's `model.PathUpdate`.
- **Explore**: look inside the game.
  - **The levels**: `<game> extract`, below. Later Explore options go in
    this list.

Explore shows only when the game's config has `[extract]` (the wizard
config needs a flag or the stage list for that; see
[Explore](#explore-the-levels)). Without it, Welcome shows the Play
choice alone, with no section tiles.

Settings keep both answers for the next run: `path` as today, plus a new
`section` (`"play"` or `"explore"`).

The paths:

| Path | Pages |
|---|---|
| Play · Fresh | Welcome → Your game → Platforms → Options → Review → Building → Done |
| Play · Update | Welcome → Device → Changes → Your game (only when the build folder lacks the game files) → Review → Building → Done |
| Explore · The levels | Welcome → Your game (only when the build folder lacks the game files) → What to export → Exporting → Done |

### What moves, against today's wizard

- **Checks is no longer a page.** `runChecks` starts when the player
  leaves Welcome (where `leaveWelcome` starts the update check today) and
  runs while they pick their game. Its state is the header's status chip.
  Its results land on the Platforms cards. A blocker becomes a "One thing
  first" card on the page that needs it (`10-needs-setup`); nothing stops
  the player while everything is fine. The rows and the raw lines move
  into "What was checked" on Platforms and into the help report.
- **"Step n of m" becomes named steps.** The `thinBar` under the header
  has one segment per step, named under it: Fresh: Your game, Platforms,
  Options, Review. Update: Device, Changes, Review. Explore: Your game,
  What to export. Welcome, Building and Done have no stepper.
- **Platform is part of Update's Device page** as a one-choice switch,
  since an update is for one computer.
- **Summary becomes Review**: what will happen in a short table, each row
  with Change, which goes back to that page. The technical part is
  collapsed.
- **Get help is on every page**, in the header. It opens the report
  (`12-get-help`).

## The frame (every page)

- **Header**: the game's icon (the config's, else the brand tile), the
  game's name, and under it the section and path ("Play · Fresh build",
  "Play · Update build", "Explore · The levels"; "Build wizard v…" on
  Welcome). Then the status chip, then Get help.
- **Status chip** (from leaving Welcome until the checks end): a pill with
  an icon and words, never colour alone. "Getting your computer ready…"
  (`circle-dashed`), "Your computer is ready" (`circle-check`,
  `wizard-success`), "One thing needs you" (amber, `pill-warn-*`). It
  hides on Building and Done.
- **Stepper**: a `thinBar` with one segment per step and the names under
  it. The current name is bold.
- **Footer**: Back on the left; the `outlineButton` "♥️ Open-source"
  centred, as today; one high-importance button on the right (Next,
  Start, Build, Export, Try again). A page whose Next can't go yet
  disables it and says why beside it ("After the step above").
- Controls are at least 44 px tall: people tap them on the Steam Deck.

## Pages

### Welcome (`01-welcome`, `01b-welcome-explore`)

- Title: "What would you like to do with BLiNX 2?" (the config's game
  name); under it "Everything here starts from your own copy of the
  game."
- Two section tiles (`pathTile`, a 52 px icon over the label and a
  sentence), one picked at a time, framed in the primary colour:
  - **Play** (`player-play`): "Turn it into a game for your Windows PC,
    Mac, or Steam Deck." (the config's targets, as `welcomeIntro` lists
    them).
  - **Explore** (`compass`): "Open its levels in Godot or Blender, to
    look around or mod them."
- Under the picked tile, its own question as radio cards:
  - Play: "What do you want to make?" with Fresh build ("The complete
    installer. For a first install, a new device, or to start over.") and
    Update build ("A small update for a device that has a build from this
    wizard."). Today's `pathFresh*` and `pathUpdate*`, shortened.
  - Explore: "What do you want to explore?" with The levels ("Each level
    as a Godot scene, with a model most 3D tools open."), picked.
- The three statements (`noGameLead`, `downloadsLead`, `sendsLead`) stay
  as one row of `tipItem`s; "How?" opens the three tips at once.
- **Folders and update check**: one collapsed row ("Uses the usual
  places. Most players never change these."). Opened, it is today's build
  folder and output folder rows (`buildFolderIs`, `outputFolderIs`,
  Change…), the "Build folder found" line, and the newer-wizard box
  (`updateCheckText`).
- Next reads **Start**.

### Your game (`02-your-game`, `02b-your-game-checked`)

- Title: "Where is your copy of BLiNX 2?"; under it "The wizard checks
  that it is the right game, then copies the files into its own folder.
  Your original is not changed." (from `dumpIntro`).
- Empty: a drop zone (dashed, the disc glyph) with "Drop your disc image
  or game folder here" and "A disc image (.iso or .xiso), or the folder
  that holds default.xbe", then `chooseImage` and `chooseFolder`. Fyne
  takes drops on the window (`Window.SetOnDropped`). "Type the path
  instead" opens today's entry.
- Picked and checked: a card with "This is BLiNX 2", the path (mono,
  ellipsised), Change…, and the checks as ticked lines: the kind (disc
  image or folder) and whether the code is the game's own (the plan's
  modded-code answer, which today only shows on Summary), and free space
  for both folders (today's `checkDump`).
- A refused pick shows its sentence in the card (`dumpNothing`,
  `dumpNoXBE`, `dumpNoSpaceOut`, `outClashText`) with the danger colour,
  an icon and Change….
- Next is disabled until the game is checked.

### Platforms (`03-platforms`, `10-needs-setup`)

- Title: "Where will you play?"; under it "Pick every computer you play
  on. One run makes an installer for each, and the shared work is done
  once."
- One card per configured target: the platform glyph (`platforms.go`),
  the name ("Windows PC", "Mac", "Steam Deck or Linux PC"), "This
  computer" on the host's own, a check box, and the target's state from
  the checks as icon plus words: Ready, "Not on this computer"
  (`blockMacOnly`, disabled, with its reason), "Needs the step above".
  This host's card is ticked first; the last ticked stays ticked
  (`targetCheck` as today).
- **One thing first** (`10-needs-setup`): a blocker from the checks is
  one amber card above the platforms: its title, the plain explanation
  (`blockCLT`, `blockBrewNone`, `blockNSIS…`, `gitMissing…`,
  `prepareNoSpace`), the action as the page's primary button
  (`actionButton`: "Install Apple's tools", "Install Homebrew",
  "Install: …"), and "It didn't work", which opens Get help. The page
  checks again by itself, as today (`cltOpened`). Several blockers are
  several cards, in the checks' order.
- **What was checked**: one collapsed row ("Everything the build needs is
  in place."). Opened, it is today's checks rows (`CheckRow`), each with
  its details and the CLI's lines, and the raw log (`logPane`).
- A failure the wizard has no words for (`prepareOther`) is a red card in
  the same place with Retry and Get help.

### Options (`04-options`)

- Title: "How should it look?"; under it `optionsIntro`. "Reset to
  recommended" sets every option to its default.
- One card, one row per `[[wizard.option]]` as today (`optionControl`):
  a check box, radios, a drop-down or a slider. The slider shows its value
  and label to the right of the name ("2× · 1280×960") and the ends under
  it.
- The font line (`font_hint`) shows as today; found, it is
  `optionFontFound` with the success icon.
- Under the card, one line: "You can change these and more later, in the
  game's settings file." The path itself (`optionsLater`) goes under
  Review's Technical details.

### Review (`05-review`)

- Title: "Ready to build"; under it what it makes ("This makes two
  installers, one for each computer you picked.").
- A table: Game, For, Settings, Finished game (the output folder), each
  with Change, which goes to that page.
- One line for disk space: ticked when there is enough; otherwise
  `summaryDisk` / `summaryDiskAll` in the warning colour.
- **Advanced** (collapsed): Build jobs (`jobsSelect`) and Rebuild
  everything (`summaryRebuild`, `rebuildHint`).
- **Technical details** (collapsed): today's `techPins`, the commands,
  the seed and the plan.
- The modded-code dialog (`modCodeTitle`) stays, on Build.
- Next reads **Build**, with the `hammer` glyph.

### Building (`06-building`)

- Title: "Building BLiNX 2"; on the right "Part 4 of 6 · 12:40 so far"
  (`buildPart`, `buildAll`). Under it the overall `thinBar`, then
  `buildIntro`.
- One card with the parts (`partsView`): the kind glyph, the part's words
  (`partNames`), its state as glyph, colour and words. The running part is
  tinted, bold, and has its own thin bar (pulsing without a percentage).
  Several platforms share the generic parts; the last row names them in
  order ("Make the installers · Windows, then Steam Deck"), and each
  platform's own parts get a row when they start.
- Footer: "Show what it's doing" (the `logPane`, today's Details), the
  credits button, Cancel (`stopDialog` as today).

### Done (`07-done`)

- A success heading: "BLiNX 2 is ready", and under it how long it took.
- One card per platform made: glyph, name, one line of what installing
  does, Open the folder, and for the host's own platform **Install now**
  (`installNow`, the page's one primary button). The others get "How to
  install", which opens `installText` for that target.
- The `notice` (`noticeLead`, `noticeBody`, `noticeTail`).
- A link card into Explore: "Want to look around the levels?", only when
  Explore exists. It goes to What to export, reusing the game files.
- "The build folder keeps work for the next build." with Free space
  (`freeTitle`, `freeText`).
- Footer: Start over (back to Welcome), Quit.

### Build stopped (`11-build-stopped`)

Replaces today's failed Done page.

- Heading: "The build stopped at “Compile”" (`doneStoppedAt`'s part);
  under it "Nothing is lost. Try again reuses most of what already
  finished." (`doneRetry`).
- A card: "Stuck? Send a help report" / "One report holds what went wrong
  and where. Never your game files." and **Make a help report**.
- The checklist as it ended, folded: the done parts as one line, the
  failed part open with "What the build said" (`failureExcerpt`, mono,
  selectable), the parts not run.
- Footer: Back to review, the credits button, **Try again**.
- Several platforms: each platform's end comes first, as today
  (`donePlatformMade`, `…Stop`, `…Not`).
- A refused update keeps `failedFresh` and "Make a Fresh build instead".
- A disk refusal keeps `doneDisk`.

### Get help (`12-get-help`)

Opens from the header on every page and from a failure. It is the one
place the technical side reaches the player.

1. **Copy the report**: the report in a read-only mono box, then
   Copy report (primary), Save as a file…, and Open the log folder. One
   line under the box: "No game files. Your user name is shortened to ~.
   Nothing is sent until you post it."
2. **Post it where it helps**: two link cards, which open the browser.
   "The game misbehaves" goes to the game's repository (the first
   `own = true` credit) and "The build or this wizard" goes to
   apexJCL/xboxrecomp-cli. The card for the likelier one is framed: a
   failure before `package`'s `plan:` line, or a check, points at the
   wizard; a failure in or after the compile points at the game. Each
   opens a new issue with the title and the report filled in (`/issues/new?title=…&body=…`); a
   report too long for a URL is copied first, and the body says to paste
   it.

The report is Markdown:

```
### BLiNX 2 build wizard report
Wizard v… · release v… (commit7) · xboxrecomp-cli v… · toolkit …     (techPins)
Computer: OS and arch · memory (free now) · cores                    (hostinfo, doctor)
Path: Fresh build · platforms: windows, steamos · options: scale=2, hud_pack
Stopped at: Compile (build) · exit status 1 · 6:52 in                (buildFailure)
Command: blinx2 package windows --dump ~/… --out ~/… --plain …

Last lines:
…                                                                    (failureExcerpt)
```

- With no failure, "Stopped at" and "Last lines" give way to the checks'
  rows and the log's last lines.
- Every path under the user's home becomes `~`. The report holds no file
  contents besides log lines.
- Saved as a file, it goes next to `wizard.log` as
  `wizard-report-<date>.md`, and the folder opens.

### Update: Which build is on the device? (`20-update-device`)

Today's `basePage`, with Platform folded in.

- Title: "Which build is on the device?"; under it "An update only works
  on the build it was made for."
- A segmented switch for the device (the targets this host can make).
- Radio cards, one per build (`baseRow`): its name and date ("This
  computer's install · v1, made 12 Oct 21:40", "Made here · v0.9…"), then
  `hasDetail` and the size. A build that cannot take the update is
  disabled with its reason (`cannot…`). The last card is "Not listed? Pick
  a file from the device…" (`hasFile`); picked, it shows `hasFileWhere`.
- "Make a Fresh build instead" (`freshInstead`) under the list.

### Update: What should the update change? (`21-update-changes`)

Today's `changesPage`.

- Title: "What should the update change?"; under it "The new version's
  code always comes along. Tweaks are up to you."
- "On the device now:" as chips (the version and each tweak).
- One card per tweak (`tweakTexts`): its check box with `made` / `keeps`
  / `none`, and Clear previous tweaks (`changesClear`) on a row of its
  own.
- Then the lines: `changesNoDisc` or `changesDisc`, and the config note
  (`changesConfig`, `changesOverride`, `changesStatic`).

### Update: Done (`22-update-done`)

- "The update is ready", and under it the device, the update ID and the
  size.
- "Put it on the device": the apply step (`applyPatchText`) as numbered
  steps, with Apply on this computer (the host's own target) and Open the
  folder.
- `doneWrongBuild` as an info line.
- The notice, worded for an update.

## Explore: the levels

**The story.** As a player who owns the game, I want to open its levels
in Godot or Blender, so I can look around them or mod them, without
learning the command line.

**Behind it**: `<game> extract` (docs/commands.md). It writes a Godot 4
project: one glTF per stage (`stages/<name>/`, its textures as PNG), a
scene per stage, a viewer that frames it, and `report.txt`. Run it as:

```
<game> extract [--stage NAME …] [--collision] [--placements] [--import] --out <output folder>/extract
```

### What to export (`30-explore-what`)

- Title: "Which levels do you want to explore?"; under it "Each level
  becomes a scene you can open and change in Godot. Its model is a plain
  glTF file, which Blender and most 3D tools open too."
- **Levels**: a check box per stage, all ticked, with a count and Select
  none / Select all. The names are the config's `[[extract.stage]]`
  names, from `extract --list` (a `--json` form would help) or from the
  wizard config. One `--stage` per tick; all ticked passes none.
- **Also include**:
  - "Walls and floors": `--collision`. "The invisible surfaces the player
    walks on, as solid shapes. Press C in a level to see them." Shown only
    when some stage names `collision`.
  - "Enemies, items and props": `--placements`. "Where the game puts them
    when a level loads. A box marks any it can't draw." Shown only when
    some stage names `placements`.
  - "Get Godot and check that it opens": `--import`, ticked. "Downloads the
    free Godot editor once, checked against a known checksum. Then Open in
    Godot works even if you don't have it."
- The output line: "Goes to <output folder>/extract" with Change.
- Next reads **Export**, with the `cube` glyph.

### Exporting (`31-explore-exporting`)

- The Building page's layout: "Exporting the levels", "Level 3 of 4 · 0:52
  so far", the overall bar, one row per stage (from the run's per-stage
  step lines), then "Get Godot and check that it opens" when `--import` is
  on.
- Cancel and "Show what it's doing" as on Building.

### Done (`32-explore-done`)

- "4 levels are ready to explore"; under it the time and, with
  `--import`, "and checked in Godot".
- A card "A Godot project": "Open a level's scene and press Play: the view
  frames the whole level." Open the folder, and **Open in Godot**. That
  button is new wizard work: the CLI never launches Godot. It runs the
  pinned Godot that `--import` fetched (`third_party/`, or `XBR_GODOT`)
  with `--path <project> -e`. Without it, the button opens the folder.
- Two lines: the glTF note ("Each level is also a .gltf file in the stages
  folder, which Blender and most 3D tools open.") and the report: "Some of
  the game's effects look a little different outside it, and a few are
  left out. The report lists each one." It links to `report.txt`.
- The notice: "For your own use only. The project is made from your own
  copy of BLiNX 2. Never share, upload or publish it."
- Footer: Export more (back to What to export), Quit.

**Done when**: Explore appears only for a game with `[extract]`; the
import runs only when the build folder lacks the game files; a failed
export uses Build stopped and Get help; Open in Godot works on a computer
without Godot (with the import box ticked); `TestCopy` passes with the
new words.

## Copy

New words go in `copy.go` with the rest, and `TestCopy`'s rule still holds:
no tool names, commands or CLI words in what the player reads. The
exceptions are the names a player has to know to use what they get:
**Godot**, **Blender** and **glTF** on the Explore pages. The CLI's lines
stay in the details, the log and the report.

## Design system notes

The screens use the xboxrecomp-cli design system with these departures,
on purpose:

- **Primary button**: fill `#1c5cab` (the `accent` light value) with white
  text. Fyne's `fyne-primary` `#296ff6` gives white text 4.4:1, under the
  4.5:1 floor. `#296ff6` stays for selection frames, check boxes and the
  bars.
- **Control height**: 44 px instead of Fyne's 36 px, for touch on the
  Steam Deck.
- **Page title**: 22 px bold (26 px on Welcome) instead of 18 px.
- **Status pills** in the header use the report pills (`pill-ok-*`,
  `pill-warn-*`), always with an icon and words.
- **New Tabler glyphs** (outline, 3.34.1, copied as the others are):
  `player-play`, `compass`, `cube`, `lifebuoy`, `folder`, `disc`, `copy`,
  `refresh`, `shield-check`, `chevron-right`.
