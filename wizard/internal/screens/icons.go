package screens

import (
	"bytes"
	"embed"
	"fmt"
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/theme"
)

// Tabler Icons 3.34.1 outline glyphs (tabler/README.md; MIT,
// LICENSES/tabler-icons-MIT.txt), as published.
//
//go:embed tabler/*.svg
var tablerFS embed.FS

// strokeIcon is a Tabler glyph in a theme colour. Fyne's themed resources
// recolour fills only, and Tabler draws with strokes in currentColor, so
// the colour goes in when Fyne reads the content. The name carries the
// colour's name, as Fyne's own themed resources do: its caches keep one
// rendering per colour and drop them when the theme changes.
type strokeIcon struct {
	file  string
	color fyne.ThemeColorName
	svg   []byte
}

func tablerIcon(file string, c fyne.ThemeColorName) *strokeIcon {
	b, err := tablerFS.ReadFile("tabler/" + file + ".svg")
	if err != nil {
		panic(err) // a name below that is not embedded: a build mistake
	}
	return &strokeIcon{file: file, color: c, svg: b}
}

func (s *strokeIcon) Name() string { return string(s.color) + "_tabler_" + s.file + ".svg" }

func (s *strokeIcon) Content() []byte {
	c := theme.Color(s.color)
	if app := fyne.CurrentApp(); app != nil {
		v := app.Settings().ThemeVariant()
		c = app.Settings().Theme().Color(s.color, v)
	}
	return bytes.ReplaceAll(s.svg, []byte("currentColor"), []byte(hexColor(c)))
}

func hexColor(c color.Color) string {
	n := color.NRGBAModel.Convert(c).(color.NRGBA)
	return fmt.Sprintf("#%02x%02x%02x", n.R, n.G, n.B)
}

// externalLinkIcon follows a hint link's text: the address opens in the
// browser. The link's own colour, so the two read as one control.
func externalLinkIcon() fyne.Resource { return tablerIcon("external-link", theme.ColorNameHyperlink) }

// infoIcon is the glyph after each Welcome statement, in the text's colour.
func infoIcon() fyne.Resource { return tablerIcon("info-circle", theme.ColorNameForeground) }
