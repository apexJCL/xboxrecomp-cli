# The build wizard

A player who owns the game downloads one small program from the game's
release page, points it at their disc image or dump folder, picks a few
options and gets the game installed. The program is the **build wizard**:
a front end over the game's own CLI. It never builds anything itself; it
fetches what the CLI needs to start (uv, and git on Windows), clones the
game at the release tag, and runs `<game> package <target> --dump DUMP
--out OUT --plain` there, the same command a developer runs.

The wizard is generic Go in this repository's `wizard/` module. What makes
it one game's is a JSON config that `xbr wizard build` reads from
`game.toml`'s `[wizard]` table at a release tag and embeds in the binary.
A game without the table has no wizard.

## What the player sees

1. **Welcome**: what the wizard does and what it never does (no game
   inside, downloads by checksum, sends nothing), the build folder and
   the output folder (both changeable and remembered), and a box for the
   newer-release check. The check goes
   out only when the player leaves this page with the box ticked, so
   nothing is sent before they have seen it; a newer release shows as a
   note then. Right under the intro, "Made with these open-source
   projects" lists the credits (see Credits below).
2. **Checks**: uv (downloaded by sha256 when missing or older than 0.5.31),
   git (MinGit on Windows; the Command Line Tools on macOS, with Homebrew's
   installer offered for the libraries; the distribution's package hint on
   Linux), 15 GB free, then the clone at the tag and `<game> doctor --json`.
   The page is just the list of what the build depends on (build tools,
   game source, one row per target), with no sentence above it. The game
   files are the next page's, and the host's own lines (host, gen/, next)
   are information; both appear only in the raw log. The host's limits
   reach the page as each target's blockers, and a failed setup (no git,
   too little disk) as one "Getting ready" row. Each row leads with an
   icon for what it is about: a platform row has a small original glyph
   for its kind of computer (a window, a laptop, a handheld; a monitor for
   a target the wizard does not know), not a vendor logo. The state's icon
   and colour sit with the status (green ready, amber needs something, red
   failed; darker shades on a light background). Each row opens to a plain explanation, its fix button and the CLI's own lines; a
   target `doctor` reports blockers for says what it needs there. The full
   output is in a collapsible log pane, whose last line turns red when the
   checks stop.
3. **Dump**: the disc image or folder (a file dialog, or typed). Next
   checks the free space for the build in the build folder and for the
   bundle (the game files and about 1 GB) in the output folder, as one
   sum when both are on one disk.
4. **Target**: one choice per platform, each with its glyph, for the
   targets `[wizard] targets` offers that this host can
   package (Windows and macOS natively; SteamOS on Linux, a Steam Deck
   included).
5. **Options**: one control per `[[wizard.option]]`: a checkbox for a
   bool, radio buttons for a choice of up to three values and a drop-down
   for more, a slider for a range with its value and label beside it,
   and the option's hint and hint link under it. A link ends in an
   external-link glyph (Tabler, in the link's colour), which says the
   browser opens; the glyph is tappable too but takes no focus, so the
   link's words stay the one control. An option with `font_hint` asks
   `<game> wizard font --json` each time the page opens: while the
   game's font is missing, or the wizard cannot tell (no checks yet, a
   CLI pinned before the command), the sentence and the link show; once
   found they give way to "<font> is installed." in the success colour.
   A note at the foot names the chosen platform's `config.toml` (its
   data folder plus `config/config.toml`) for the settings the page
   leaves out.
6. **Summary**: the game, the two folders, target and changed options; under
   "Technical details", the wizard, release, CLI and toolkit versions and
   every command the build will run.
7. **Build**: a checklist of the build's parts, in order. The wizard
   knows its own commands (each prerequisite, such as `import` and
   `enhance hud`, then `package`); `package`'s row reads "Build the
   game" until its `plan:` line names the stages, which then take its
   place (an open log stays open on the first stage). A stage the plan
   did not name gets a row when it starts, one jumped over reads "not
   needed", and with no plan line the row becomes the first stage that
   starts, or stands for the whole command when none does. Each row has a glyph for its kind, the
   part in the player's words ("Translate the game's code" for
   `generate`), and its state as a glyph, a colour and words: waiting,
   running (a spinner, the CLI's step, its percentage or `[i/n]`, and
   the part's clock), done, failed or stopped with its time, not
   needed, or not run after a stop. The running row has a thin bar
   that pulses while its step prints no percentage. "Log" opens a
   row's own lines in place, selectable, with Copy; a failed row opens
   by itself with what it said. Above the rows, "Part 3 of 6 · 42% ·
   12:40 in all" and the overall bar tick every second; below them the
   Details pane still holds every line of the build together, with the
   CLI's heartbeat on its collapsed line. Cancel asks first ("Are you
   sure?", "Go back" or a red "Cancel Build", no default button; the
   dialog is 420 wide, narrower in a narrower window, and follows a
   resize), then stops the whole process tree. The next build reuses most of what
   finished: the dump, the tools, a finished `gen/` and every compiled
   object; the HUD pack and the staging are redone, and an interrupted
   recomp starts over.
8. **Done**: the bundle, "Open the folder", and the target's install step
   ("Install now" on the host's own target: the Windows setup program,
   the `.dmg`, or `install.sh` in a terminal). When the build fails: the
   part that stopped, the checklist as it ended with the failed row open
   on what it said (its last lines, from the Python traceback when there
   is one, in a box that can be selected and copied), the exit status,
   where the full logs are, and Retry ("Retry runs the build again and
   reuses most of what already finished."); the same goes to `wizard.log` in
   the settings folder. A cancelled build says the player stopped it and
   shows the checklist. Every outcome ends with the notice that the bundle
   is for the player's own computers only.

The chosen config options are written as a small `config.toml` (the seed)
and passed as `package --seed-config`; each installer copies it to the data
folder's `config/config.toml` only when that file does not exist and no
earlier version's `config/enhance.toml` does (the runtime renames that one
to `config.toml` only while `config.toml` is missing), so a player's own
settings are never replaced. Flag options (`--hud-pack`) are passed to
`package`; `--hud-pack` also runs `import DUMP` and `enhance hud` first.

The Windows installer and the SteamOS `install.sh` do the copy
themselves. The macOS bundle has no installer: the game's own launcher
script (`packaging/macos/<App>.in`, which `package macos` puts in the app)
must copy `Resources/config.seed.toml` the same way. BLiNX 2's
`packaging/macos/BLiNX2.in` is the model; a CLI template for that launcher
is a follow-up.

## Credits

The wizard credits the open-source projects it and the build rely on.
On Welcome the list sits right under the intro, so everyone is credited
before the player does anything: one line per project, its name as a
link with its role and licence dimmed beside it (cut short in a narrow
window), in a box about eight rows tall that scrolls. On every other page
an info button (Tabler's `info-circle`, low importance) at the right end
of the header line, after the step counter, opens the same list as
"Open-source credits": two lines per project there, the name as a link
over its role and licence, then "NOTICES.txt, which came with the wizard,
holds each licence's full text." The dialog is 560 by 520, smaller in a
smaller window, and follows a resize. Welcome hides the button, since the
list is already on the page. Each link opens in the browser.

The CLI owns the list and its order (`credits.py`), and `wizard config`
puts it in the config as `credits`. Our repos come first, then the
third-party projects:

1. the game's own repository (or repositories): its `[[wizard.credit]]`
   entries with `own = true`, in `game.toml`'s order;
2. xboxrecomp-cli, the toolkit fork and the toolkit's upstream;
3. the game's other `[[wizard.credit]]` entries (anything only the game
   uses, such as a font), in `game.toml`'s order. They lead the
   third-party projects rather than being sorted into them: the CLI's
   list is grouped by what uses each project, and a game's extra
   belongs to none of its groups;
4. the third-party projects the wizard and the build use directly: Go,
   Fyne, zenity, Tabler Icons; xemu, SDL, OpenSSL, libepoxy (in the
   game); Python, uv, Git, Homebrew, CMake, Ninja, llvm-mingw, NSIS,
   Capstone, pefile, umu-launcher (the build's tools).

A game that lists nothing gets the generic order, xboxrecomp-cli first.
A url is listed once, where it first comes in that order, so an entry
the game repeats is shown as the game wrote it. Transitive dependencies are not listed; their licences are in
NOTICES.txt. A config from a CLI older than the key has no `credits`, and
the wizard then shows its own short list (xboxrecomp-cli, Go, Fyne,
zenity, Tabler Icons).

## The build folder and the output folder

Everything the wizard fetches and builds goes in one folder, the **build
folder**, so deleting it removes all of it (the installed game and its
data folder are elsewhere):

| Path | What |
|---|---|
| `tools/uv/`, `tools/git/` | uv and MinGit when the host has none |
| `tools/uv-cache/`, `tools/python/` | uv's cache and the Python it installs (`UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` for every child) |
| `src/` | the clone at the tag, with its own `external/`, `.venv/`, build folders and `build-logs/` |

The default is `<slug>-recomp` under `%LOCALAPPDATA%`, `~/Library/Application
Support` or `$XDG_DATA_HOME` (`~/.local/share`). The wizard refuses a
folder that is, holds or sits inside the game's data folder for the host.
On Windows the children run with `core.longpaths` on (`GIT_CONFIG_*`), so
the clone and the CLI's toolkit clone work without the system-wide
`LongPathsEnabled`.

The bundle goes to the **output folder** (`package --out`). Its default is
`dist` beside the wizard (beside the `.app` on a Mac), worked out again on
every run so a moved wizard is followed. That place is refused when it is
temporary (a zip's preview, macOS translocating a quarantined app), a
system folder (Program Files and the Windows folder, `/Applications` and
`/System`, `/usr` and `/opt`) or cannot be written; the default is then
the build folder's `dist`, and `wizard.log` says why
(`output folder: <why>; using <dir>`). A folder the player picks is kept
until changed; when it is no longer there as a folder (its drive
unplugged), the default applies, with a log line. The wizard refuses an
output folder that is, holds or sits inside the game's data folder, the
build folder's `src` or a dump folder.

Both choices live in the settings file (`settings.json` in the wizard's
settings folder: `root` for the build folder, a name kept from before the
output folder existed, and `out`, empty for the default).

The binary also takes `--dry` (print the plan, both folders included, and
exit, no window), `--root DIR` (the build folder), `--no-dialog`, `--no-update-check` and `--version`.
Developers run it on a checkout with `--game DIR` (no clone; the config
comes from `<game> wizard config --json --dev`) or `--config FILE`.

## The config

`<game> wizard config --json [--tag TAG | --dev]` prints what the binary
embeds: the game's names, the release (repo, tag, commit), the `[cli]` and
`[toolkit]` pins, the targets, the data folders, `[[wizard.fetch]]`, and
the options, each config option with the TOML literal of every value,
and the credits (see Credits). It
is read from the tag's tree (`git show TAG:game.toml`), never the working
tree: the binary clones that tag, so its options must be the tag's. The
checks, on top of `game.toml`'s own ([manifest.md](manifest.md#wizard)):

- `wizard.icon`, when set, is a PNG or an SVG in the tag's tree;
- each config option's key is in `<package.content>/config.default.toml`,
  with the option's default as its value there, and of the option's kind;
- each flag is a switch `package` takes, and not one the wizard sets
  itself (`--dump`, `--plain`, `--seed-config`, `--out`, `--replace`,
  `--verbose`).

With no `--tag`, the release is the one tag on HEAD of a clean checkout.
`--dev` reads the working tree with no tag; the binary then has no clone
step and takes `--game DIR`.

## The build

`<game> wizard build [--tag TAG | --dev] [--only LIST] [--out DIR]` writes
to `dist/wizard/`:

| Asset | Built on | How |
|---|---|---|
| `<slug>-wizard-<tag>-macos.zip` | macOS | `go build` for arm64 and amd64, `lipo`, `fyne package` (the app bundle, its `.icns` from `Icon.png`), a zip with fixed dates |
| `<slug>-wizard-<tag>-windows-x64.zip` | macOS or Linux | `go build` with llvm-mingw's clang (`-H windowsgui`) and the icon as a resource (`llvm-windres` into `cmd/wizard/rsrc_windows_amd64.syso`), a zip with fixed dates |
| `<slug>-wizard-<tag>-linux-x64.tar.gz` | any host with Docker | `docker build --output` from `wizard/linux/Dockerfile` on the pinned builder image; no bind mounts. The tar holds the binary, `<slug>-wizard.png`, `<slug>-wizard.desktop` and `NOTICES.txt` |
| `SHA256SUMS` | | the archives |

Every archive carries `NOTICES.txt` (in the macOS app, under
`Contents/Resources/`): the wizard's MIT licence, then the licence of
everything its binaries contain, as published: the Go standard library,
the Tabler glyphs (`LICENSES/tabler-icons-MIT.txt`), the MinGW-w64
runtime that llvm-mingw links into the Windows program
(`wizard/notices/mingw-w64-runtime.txt`, a copy from the pinned
llvm-mingw that the Windows build checks it still matches) and each Go module that
any of the three binaries links (`go list -deps` for each target), Fyne's
font licences and GLFW's C sources included. The build gathers the files
from the module cache: the licence files at each module's root, and those
in each linked package's folder and in its folders that are no package of
their own, with every text in a REUSE-style `LICENSES/` folder at either
place. A linked module with no licence file stops the build. The file
is the same in all three archives and between two builds.

The module is staged into `<out>/.work/src` with the config in
`internal/gamecfg/config.json`; the CLI's checkout is never written. Every
build uses `-trimpath -ldflags="-s -w -buildid="`, `GOTOOLCHAIN=local`,
`GOFLAGS=-mod=readonly` and the module cache under `wizard/.tools/`; the
archives' dates are the tag commit's (`SOURCE_DATE_EPOCH`). Two builds of
the same tag give the same bytes. Each binary that this host can run is
started with `--dry` (the Linux one in the builder image).

Go and llvm-mingw come from `wizard/pins.json` into `wizard/.tools/` by
sha256 (`WIZARD_GO=DIR` uses another Go root of the pinned version). Go is
needed for every asset, the Linux one included, since the icons are
rendered on the host first. Only Docker is needed beyond that, and only
for the Linux asset.

## The icon

Without `[wizard] icon` the wizard carries the generic icon: Tabler Icons'
`device-gamepad-2` glyph (MIT, `LICENSES/tabler-icons-MIT.txt`) in white on
an original deep blue tile, `wizard/icon/wizard-icon.svg`
(`wizard/icon/README.md` says how it is made). A game sets its own with
`icon`, a PNG (512 pixels or more) or an SVG in its tree.

`wizard build` renders the set with the module's `cmd/icongen` (`go run
./cmd/icongen -in FILE -out DIR`, pure Go, no cgo): `icon-16.png` to
`icon-1024.png`, `wizard.ico` (PNG entries at 16, 24, 32, 48 and 256) and
`Icon.png` (1024). It draws at 1024 pixels with Fyne's software painter
and averages down to each size in integers, so two builds give the same
bytes. The Windows exe carries the `.ico` (Explorer, the taskbar, the
window), `fyne package` makes the macOS `.icns` from `Icon.png`, and the
Linux tar has the 256 PNG and a `.desktop` entry. The binary also sets its
window icon from an embedded 256 render (`wizard/internal/gamecfg/icon.png`,
the generic one, replaced by the game's in the staged copy), so X11 and
Wayland taskbars show it.

On Linux and the Steam Deck the wizard runs from the unpacked folder as it
is. For a menu entry, put the three files where the desktop looks:

    install -Dm755 <slug>-wizard ~/.local/bin/<slug>-wizard
    install -Dm644 <slug>-wizard.png ~/.local/share/icons/hicolor/256x256/apps/<slug>-wizard.png
    install -Dm644 <slug>-wizard.desktop ~/.local/share/applications/<slug>-wizard.desktop

## The pins

`wizard/pins.json` holds every download, by sha256 and size:

| Key | Used by | Moved by |
|---|---|---|
| `go` | the build (per build host) | hand, with `go.mod`'s `toolchain` line |
| `llvm_mingw` | the Windows asset | hand |
| `fyne_tool` | the macOS app bundle | hand |
| `builder` | the Linux asset: the base image digest, the Debian snapshot, the platform | hand |
| `uv`, `mingit` | the player (the binary embeds the file) | `xbr pins refresh --wizard` |
| `homebrew_install` | the player on macOS (the installer script at a commit) | hand |

A moved player pin reaches players with the next wizard build.

## The gates

`<game> wizard check [--tag TAG | --dev] [--no-build]` runs
`wizard/check.sh` on the pinned Go (`gofmt -l` empty, `go vet`, `go test`,
`go test -race` where the host supports it, `staticcheck` when installed),
then builds every asset twice and compares
the sha256s of the archives and the binaries. The second build shares no
compiled output with the first: an empty `GOCACHE`, and the Linux asset
with `docker build --no-cache` and an empty `GOCACHE` inside (the builder
image itself is kept: its inputs are pinned).

## The game's release workflow

`xbr new` writes `.github/workflows/release-wizard.yml`; a game made before
copies it from
[src/xboxrecomp_cli/templates/new-game](../src/xboxrecomp_cli/templates/new-game)
with `@SLUG@` filled. On a `v*` tag push it builds the macOS and Windows
assets on `macos-15` and the Linux one on `ubuntu-24.04-arm` (the builder
image runs natively there, as on an Apple-silicon Mac), checks
`SHA256SUMS` against what each build printed, and attaches the four files
to the tag's release. A manual run takes the tag and attaches only when
`release` is ticked. Every action is pinned by commit.

The release notes are the tag's section of the game's `CHANGELOG.md`
(`## [1.0.0] - YYYY-MM-DD` for `v1.0.0`; `xbr new` writes the file with an
empty `[Unreleased]`). The first job, `notes`, takes that section from the
tagged commit and stops the run, before any build, when the tag has none
or it is empty, so the section belongs in the commit you tag.

The `notes`, build and `verify` jobs are read-only (`contents: read`,
checkouts with `persist-credentials: false`, setup-uv with no restored
cache). Only `release` can write, and it runs only on a tag push or a
ticked `release`. It checks the assets against `SHA256SUMS`, creates the
release as a draft with the notes (or, when one exists, gives it this
run's notes), attaches the four files by name, and publishes it last, so
the release is never public with files missing. A tag with `-rc.` in it
is published as a pre-release.
