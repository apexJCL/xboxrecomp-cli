package screens

import (
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/theme"
)

// palette is the wizard's theme: the base theme with state colours a
// player can read as text. Fyne's success green, warning amber and error
// red are bright enough for a dark background but too light on a white
// one (green is about 1.7:1), so on a light background they are darker
// shades of the same hues, each at least 4.5:1 against it (WCAG AA).
type palette struct{ fyne.Theme }

// Theme wraps base with the wizard's state colours.
func Theme(base fyne.Theme) fyne.Theme { return palette{base} }

var lightStates = map[fyne.ThemeColorName]color.NRGBA{
	theme.ColorNameSuccess: {R: 0x1b, G: 0x7a, B: 0x30, A: 0xff},
	theme.ColorNameWarning: {R: 0x9a, G: 0x56, B: 0x00, A: 0xff},
	theme.ColorNameError:   {R: 0xc6, G: 0x28, B: 0x28, A: 0xff},
}

func (p palette) Color(n fyne.ThemeColorName, v fyne.ThemeVariant) color.Color {
	if c, ok := lightStates[n]; ok && isLight(p.Theme.Color(theme.ColorNameBackground, v)) {
		return c
	}
	return p.Theme.Color(n, v)
}

// isLight decides by the background itself rather than the variant: the
// fixed light and dark themes ignore the variant they are asked for.
func isLight(c color.Color) bool {
	r, g, b, _ := c.RGBA()
	return (299*r+587*g+114*b)/1000 > 0x8000
}

// stateColor is the colour of a row's icon and status: the icon and the
// words carry the state too, so colour is never the only cue.
func stateColor(s rowState) fyne.ThemeColorName {
	switch s {
	case stateOK:
		return theme.ColorNameSuccess
	case stateWarn:
		return theme.ColorNameWarning
	case stateFail:
		return theme.ColorNameError
	}
	return theme.ColorNamePlaceHolder
}

// stateIcon is a row's icon in its state's colour.
func stateIcon(s rowState) fyne.Resource {
	switch s {
	case stateOK:
		return theme.NewSuccessThemedResource(theme.ConfirmIcon())
	case stateLater:
		return theme.NewPrimaryThemedResource(theme.InfoIcon())
	case stateWarn:
		return theme.NewWarningThemedResource(theme.WarningIcon())
	}
	return theme.NewErrorThemedResource(theme.ErrorIcon())
}
