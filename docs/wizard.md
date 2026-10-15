# The build wizard

A player who owns the game downloads one small program from the game's
release page, points it at their disc image or dump folder, picks a few
options and gets the game installed. The program is the **build wizard**:
a front end over the game's own CLI. It never builds anything itself; it
fetches what the CLI needs to start (uv, and git on Windows), clones the
game at the release tag, and runs `<game> package <target> --dump DUMP
--plain` there, the same command a developer runs.

The wizard is generic Go in this repository's `wizard/` module. What makes
it one game's is a JSON config that `xbr wizard build` reads from
`game.toml`'s `[wizard]` table at a release tag and embeds in the binary.
A game without the table has no wizard.

## What the player sees

1. **Welcome**: the game and the release, the install folder (per OS,
   changeable), and a newer-release note when the release repo has one.
2. **Checks**: uv (downloaded by sha256 when missing or older than 0.5.31),
   git (MinGit on Windows; the Command Line Tools on macOS, with Homebrew's
   installer offered for the libraries; the distribution's package hint on
   Linux), 15 GB free, then the clone at the tag and `<game> doctor --json`.
   A target `doctor` reports blockers for is shown with them.
3. **Dump**: the disc image or folder (a file dialog, or typed).
4. **Target**: the targets `[wizard] targets` offers that this host can
   package (Windows and macOS natively; SteamOS on Linux, a Steam Deck
   included).
5. **Options**: one control per `[[wizard.option]]`.
6. **Summary**: every command the build will run.
7. **Build**: the CLI's own `--plain` output with a progress bar; Cancel
   stops the whole process tree.
8. **Done**: the bundle's folder, and the target's install step.

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

## The install folder

Everything the wizard fetches and builds goes in one folder, so deleting
it removes all of it (the installed game and its data folder are
elsewhere):

| Path | What |
|---|---|
| `tools/uv/`, `tools/git/` | uv and MinGit when the host has none |
| `tools/uv-cache/`, `tools/python/` | uv's cache and the Python it installs (`UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` for every child) |
| `src/` | the clone at the tag, with its own `external/`, `.venv/`, build folders and `dist/` |

The default is `<slug>-recomp` under `%LOCALAPPDATA%`, `~/Library/Application
Support` or `$XDG_DATA_HOME` (`~/.local/share`). The wizard refuses a
folder that is, holds or sits inside the game's data folder for the host.
On Windows the children run with `core.longpaths` on (`GIT_CONFIG_*`), so
the clone and the CLI's toolkit clone work without the system-wide
`LongPathsEnabled`.

The binary also takes `--dry` (print the plan and exit, no window),
`--root DIR`, `--no-dialog`, `--no-update-check` and `--version`.
Developers run it on a checkout with `--game DIR` (no clone; the config
comes from `<game> wizard config --json --dev`) or `--config FILE`.

## The config

`<game> wizard config --json [--tag TAG | --dev]` prints what the binary
embeds: the game's names, the release (repo, tag, commit), the `[cli]` and
`[toolkit]` pins, the targets, the data folders, `[[wizard.fetch]]`, and
the options, each config option with the TOML literal of every value. It
is read from the tag's tree (`git show TAG:game.toml`), never the working
tree: the binary clones that tag, so its options must be the tag's. The
checks, on top of `game.toml`'s own ([manifest.md](manifest.md#wizard)):

- `wizard.icon` is a PNG in the tag's tree;
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
| `<slug>-wizard-<tag>-macos.zip` | macOS | `go build` for arm64 and amd64, `lipo`, `fyne package` (the app bundle with the icon), a zip with fixed dates |
| `<slug>-wizard-<tag>-windows-x64.zip` | macOS or Linux | `go build` with llvm-mingw's clang (`-H windowsgui`), a zip with fixed dates |
| `<slug>-wizard-<tag>-linux-x64.tar.gz` | any host with Docker | `docker build --output` from `wizard/linux/Dockerfile` on the pinned builder image; no bind mounts |
| `SHA256SUMS` | | the archives |

The module is staged into `<out>/.work/src` with the config in
`internal/gamecfg/config.json`; the CLI's checkout is never written. Every
build uses `-trimpath -ldflags="-s -w -buildid="`, `GOTOOLCHAIN=local`,
`GOFLAGS=-mod=readonly` and the module cache under `wizard/.tools/`; the
archives' dates are the tag commit's (`SOURCE_DATE_EPOCH`). Two builds of
the same tag give the same bytes. Each binary that this host can run is
started with `--dry` (the Linux one in the builder image).

Go and llvm-mingw come from `wizard/pins.json` into `wizard/.tools/` by
sha256 (`WIZARD_GO=DIR` uses another Go root of the pinned version). Only
Docker is needed beyond that, and only for the Linux asset.

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
`staticcheck` when installed), then builds every asset twice and compares
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

The build and `verify` jobs are read-only (`contents: read`, checkouts
with `persist-credentials: false`, setup-uv with no restored cache).
Only `release` can write, and it runs only on a tag push or a ticked
`release`; it checks the assets against `SHA256SUMS`, creates a draft
release for the tag when there is none yet, and attaches the four files
by name.
