package screens

import (
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// Fyne's buttons are filled or bare; the credits button is an outline
// (the user's 2026-10-19 request): a bare button inside a pink frame.

// The frame's pink, by background: a darker one on a light background
// and a lighter one on a dark background, each at least 4.5:1 against it.
var (
	outlineLight = color.NRGBA{R: 0xc2, G: 0x18, B: 0x5b, A: 0xff}
	outlineDark  = color.NRGBA{R: 0xf4, G: 0x8f, B: 0xb1, A: 0xff}
)

// outlineColor is the frame's pink for the current theme.
func outlineColor() color.Color {
	if isLight(theme.Color(theme.ColorNameBackground)) {
		return outlineLight
	}
	return outlineDark
}

// outlineButton is a low-importance button in a rounded pink frame.
type outlineButton struct {
	widget.BaseWidget
	btn *widget.Button
}

func newOutlineButton(b *widget.Button) *outlineButton {
	b.Importance = widget.LowImportance
	o := &outlineButton{btn: b}
	o.ExtendBaseWidget(o)
	return o
}

func (o *outlineButton) CreateRenderer() fyne.WidgetRenderer {
	frame := canvas.NewRectangle(color.Transparent)
	frame.StrokeWidth = 1.5
	frame.CornerRadius = theme.InputRadiusSize()
	r := &outlineRenderer{frame: frame, btn: o.btn}
	r.Refresh()
	return r
}

type outlineRenderer struct {
	frame *canvas.Rectangle
	btn   *widget.Button
}

func (r *outlineRenderer) Layout(s fyne.Size) {
	r.frame.Resize(s)
	r.btn.Resize(s)
}

func (r *outlineRenderer) MinSize() fyne.Size           { return r.btn.MinSize() }
func (r *outlineRenderer) Objects() []fyne.CanvasObject { return []fyne.CanvasObject{r.frame, r.btn} }
func (r *outlineRenderer) Destroy()                     {}

// Refresh takes the theme's pink again: the player may switch between
// light and dark while the wizard runs.
func (r *outlineRenderer) Refresh() {
	r.frame.StrokeColor = outlineColor()
	r.frame.Refresh()
	r.btn.Refresh()
}

// Tapped passes a tap on the frame to the button: the window gives it to
// the button inside, but a tap on the wrapper itself must work too.
func (o *outlineButton) Tapped(e *fyne.PointEvent) { o.btn.Tapped(e) }
