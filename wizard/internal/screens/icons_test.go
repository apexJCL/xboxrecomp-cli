package screens

import (
	"image"
	"image/color"
	"io/fs"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/driver/software"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
)

// inked counts the pixels of img near c.
func inked(img image.Image, c color.Color) int {
	n := 0
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			if _, _, _, a := img.At(x, y).RGBA(); a > 0 && near(img.At(x, y), c) {
				n++
			}
		}
	}
	return n
}

// renderIcon draws res at 48 px under th, the app's theme while it draws
// (a strokeIcon reads its colour from the app).
func renderIcon(a fyne.App, th fyne.Theme, res fyne.Resource) image.Image {
	a.Settings().SetTheme(th)
	img := canvas.NewImageFromResource(res)
	img.FillMode = canvas.ImageFillContain
	c := software.NewCanvas()
	c.SetPadded(false)
	c.SetContent(img)
	c.Resize(fyne.NewSquareSize(48))
	return software.RenderCanvas(c, th)
}

// Every embedded glyph draws in the colour asked for, in the light and
// the dark theme: the strokes take the theme's colour, nothing stays
// black or vanishes.
func TestTablerIcons(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	files, err := fs.Glob(tablerFS, "tabler/*.svg")
	if err != nil || len(files) < 13 {
		t.Fatalf("embedded glyphs: %v %v", files, err)
	}
	//lint:ignore SA1019 the fixed variants are what an image needs
	for name, base := range map[string]fyne.Theme{"light": theme.LightTheme(), "dark": theme.DarkTheme()} {
		th := Theme(base)
		for _, f := range files {
			file := strings.TrimSuffix(strings.TrimPrefix(f, "tabler/"), ".svg")
			for _, cn := range []fyne.ThemeColorName{theme.ColorNameHyperlink, theme.ColorNameSuccess, theme.ColorNameForeground} {
				res := tablerIcon(file, cn)
				want := th.Color(cn, theme.VariantLight)
				if !strings.Contains(res.Name(), string(cn)) {
					t.Errorf("%s: name %q does not carry the colour", file, res.Name())
				}
				if n := inked(renderIcon(a, th, res), want); n < 20 {
					t.Errorf("%s %s in %s: %d pixels of %v", name, file, cn, n, want)
				}
			}
		}
	}
}
