# wizard/: the player build wizard

The Go module behind `xbr wizard` ([docs/wizard.md](../docs/wizard.md)). It
is generic: one game's wizard is this module plus the config `xbr wizard
build` writes into `internal/gamecfg/config.json` of a staged copy (the
committed file is `{}`; the module is never built in place for a release).

## Layout

| Package | What |
|---|---|
| `cmd/wizard` | flags, the config (embedded, `--config`, or `--game DIR`), `--dry` |
| `cmd/icongen` | the icon set from one SVG or PNG: the PNG sizes, `wizard.ico`, `Icon.png` (run by `xbr wizard build`) |
| `icon/` | the generic icon: the Tabler glyph and the tile made from it (`icon/README.md`) |
| `internal/gamecfg` | the embedded config: parse and validate; `icon.png`, the window icon the binary embeds |
| `internal/model` | the player's choices and the commands they make |
| `internal/hostinfo` | OS, target, the default build and output folders, saved settings, free space |
| `internal/fetch` | downloads by sha256 and size, safe unpacking, the pins |
| `internal/repo` | git: the clone at the tag, the commit check |
| `internal/runner` | the game's bootstrap under uv; `doctor --json`; `--plain` progress; cancel |
| `internal/update` | a newer release of the game's repo (3 s, skippable) |
| `internal/termlaunch` | a terminal for a step that needs one (sudo on Linux) |
| `internal/screens` | the Fyne pages |

## Dependencies

Two direct ones, each pinned in `go.mod`:

- `fyne.io/fyne/v2` v2.8.2: one toolkit that draws the same window on
  Windows, macOS, X11 and Wayland from one code base, and builds with cgo
  only through it (OpenGL and the windowing glue). Its `fyne` tool
  (`pins.json` `fyne_tool`) makes the macOS app bundle.
- `github.com/ncruces/zenity` v0.10.15: the system's own file dialogs
  (Fyne's are its own drawing, awkward for a disc image path). `--no-dialog`
  turns them off.

Everything else is the standard library. The generic icon's glyph is
Tabler Icons' `device-gamepad-2` (MIT, `LICENSES/tabler-icons-MIT.txt`),
a file in `icon/`, not a Go dependency.

## Developing

The pinned Go only, in user space: `xbr wizard check` (from a game:
`<game> wizard check --dev`) fetches the Go tarball in `pins.json` by
sha256 into `.tools/`, then runs `check.sh` with it first on PATH and
`GOTOOLCHAIN=local`. `go.mod`'s `go` and `toolchain` lines and the `go` pin
move together.

To try the wizard on a checkout without a release:

    <game> wizard build --dev --only macos     # or windows, linux
    <the binary> --game /path/to/game          # no clone, the tree as it is
    <the binary> --dry --config cfg.json       # print the plan

`internal/gamecfg/testdata/blinx2.json` is a sample config for the tests.
