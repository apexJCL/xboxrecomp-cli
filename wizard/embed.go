// Package wizard is the generic player build wizard of xboxrecomp-cli: a
// windowed front end over the game's own CLI (docs/wizard.md). This file
// only embeds the pins the binary downloads by and the window's icon.
package wizard

import _ "embed"

// PinsJSON is wizard/pins.json: the player's downloads (uv, MinGit, the
// Homebrew installer) by sha256, beside the build's own inputs.
//
//go:embed pins.json
var PinsJSON []byte

// IconPNG is the window's icon at 256 pixels: the generic gamepad as
// committed, the game's own in a staged copy (xbr wizard build writes it
// over this file, as it writes config.json).
//
//go:embed internal/gamecfg/icon.png
var IconPNG []byte
