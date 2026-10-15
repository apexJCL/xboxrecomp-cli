# The wizard's generic icon

- `device-gamepad-2.svg`: the outline gamepad of Tabler Icons 3.34.1
  (<https://tabler.io/icons>, the file
  `https://unpkg.com/@tabler/icons@3.34.1/icons/outline/device-gamepad-2.svg`),
  copied as it is. MIT licence: `LICENSES/tabler-icons-MIT.txt` at the
  repository's root.
- `wizard-icon.svg`: that glyph in white on an original tile, 1024 by
  1024: a square with a 22% corner radius filled `#1e4d6b`, the glyph at
  62% of the tile, centred, its stroke 7% of the glyph's size. The glyph's
  coordinates are scaled into the tile rather than placed with a
  transform, since Fyne's SVG rasterizer does not scale a stroke's width
  with a transform.

It reads as a gamepad without any console maker's mark or colours. A game
sets its own with `[wizard] icon` (docs/manifest.md).

`cmd/icongen` renders the set (`go run ./cmd/icongen -in icon/wizard-icon.svg
-out DIR`); `internal/gamecfg/icon.png` is its 256-pixel render, committed
because the binary embeds it, and `TestIcongen` fails when the two drift.
