package screens

import (
	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/theme"
)

// Platform glyphs: small original drawings that read as each kind of
// computer, not the vendors' logos (those are trademarks, and this binary
// is published). One filled path each, so theme.NewThemedResource
// recolours them for the light and dark themes. Fyne's rasteriser fills
// by the non-zero rule whatever fill-rule says, so each hole winds
// against its outline.
const (
	// A window: a title bar over four panes.
	glyphWindow = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#000000" fill-rule="evenodd" d="M3 4h18v16H3z M5 8.5v4.25h6.25V8.5z M12.75 8.5v4.25H19V8.5z M5 14.25V18h6.25v-3.75z M12.75 14.25V18H19v-3.75z"/></svg>`
	// A laptop: an open screen over a base.
	glyphLaptop = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#000000" fill-rule="evenodd" d="M4.5 5h15v11h-15z M6.5 7v7h11V7z M1.5 17h21v1a1.5 1.5 0 0 1-1.5 1.5H3A1.5 1.5 0 0 1 1.5 18z"/></svg>`
	// A handheld: a wide body, a screen between two thumbsticks (the
	// sticks are drawn the other way round from the body, so they too
	// are holes).
	glyphHandheld = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#000000" fill-rule="evenodd" d="M4 7h16a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H4a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3z M7 9v6h10V9z M4.2 8.7a1.7 1.7 0 1 0 0 3.4a1.7 1.7 0 1 0 0-3.4z M19.8 8.7a1.7 1.7 0 1 0 0 3.4a1.7 1.7 0 1 0 0-3.4z"/></svg>`
	// A desktop: a monitor on a stand, also the generic computer.
	glyphDesktop = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#000000" fill-rule="evenodd" d="M2.5 4h19v12h-19z M4.5 6v8h15V6z M10.5 16h3v2.5h3V20h-9v-1.5h3z"/></svg>`
)

// genericComputer is the glyph of a target this wizard does not know.
// Fyne's own ComputerIcon is a laptop, which would read as macOS here.
var genericComputer = theme.NewThemedResource(fyne.NewStaticResource("platform-computer.svg", []byte(glyphDesktop)))

// platformGlyphs maps the config's target keys to a glyph; the keys are
// the CLI's generic targets, never a game's.
var platformGlyphs = map[string]fyne.Resource{
	"windows": theme.NewThemedResource(fyne.NewStaticResource("platform-windows.svg", []byte(glyphWindow))),
	"macos":   theme.NewThemedResource(fyne.NewStaticResource("platform-macos.svg", []byte(glyphLaptop))),
	"steamos": theme.NewThemedResource(fyne.NewStaticResource("platform-steamos.svg", []byte(glyphHandheld))),
	"linux":   genericComputer,
}

// platformIcon is a target's glyph, or the generic computer.
func platformIcon(target string) fyne.Resource {
	if r, ok := platformGlyphs[target]; ok {
		return r
	}
	return genericComputer
}

// rowIcon is the icon in front of a checks row's name: the platform for a
// target row, otherwise what the row is about.
func rowIcon(r checkRow) fyne.Resource {
	switch {
	case r.Target != "":
		return platformIcon(r.Target)
	case r.Name == rowTools:
		return theme.SettingsIcon()
	case r.Name == rowSource:
		return theme.DownloadIcon()
	case r.Name == rowNotes:
		return theme.DocumentIcon()
	}
	// "Getting ready": the setup that failed.
	return theme.SettingsIcon()
}
