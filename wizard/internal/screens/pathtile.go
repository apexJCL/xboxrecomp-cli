package screens

import (
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/driver/desktop"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// pathIconSize is a tile's icon, large enough to tell the two apart at
// a glance.
const pathIconSize = 48

// pathTile is one of Welcome's two paths as a large button: its icon
// over its label, framed in the theme's primary colour while chosen.
// Fyne's buttons have no chosen state and lay their icon beside the
// text, so the tile draws its own.
type pathTile struct {
	widget.BaseWidget
	icon     fyne.Resource
	label    string
	tapped   func()
	selected bool
	hovered  bool
	focused  bool
}

func newPathTile(icon fyne.Resource, label string, tapped func()) *pathTile {
	t := &pathTile{icon: icon, label: label, tapped: tapped}
	t.ExtendBaseWidget(t)
	return t
}

// setSelected marks the tile chosen or not.
func (t *pathTile) setSelected(on bool) {
	if t.selected != on {
		t.selected = on
		t.Refresh()
	}
}

func (t *pathTile) CreateRenderer() fyne.WidgetRenderer {
	bg := canvas.NewRectangle(nil)
	bg.CornerRadius = theme.InputRadiusSize()
	img := canvas.NewImageFromResource(t.icon)
	img.FillMode = canvas.ImageFillContain
	img.SetMinSize(fyne.NewSquareSize(pathIconSize))
	// Rich text, not a Label: a Label holds a tappable selection that
	// would take the tile's taps.
	label := widget.NewRichText(&widget.TextSegment{Text: t.label,
		Style: widget.RichTextStyle{Alignment: fyne.TextAlignCenter, TextStyle: fyne.TextStyle{Bold: true}}})
	body := container.NewPadded(container.NewVBox(container.NewCenter(img), label))
	r := &pathTileRenderer{t: t, bg: bg, img: img, label: label,
		objects: []fyne.CanvasObject{bg, body}, layout: layout.NewStackLayout()}
	r.Refresh()
	return r
}

func (t *pathTile) Tapped(*fyne.PointEvent) {
	if t.tapped != nil {
		t.tapped()
	}
}

func (t *pathTile) MouseIn(*desktop.MouseEvent)    { t.hovered = true; t.Refresh() }
func (t *pathTile) MouseMoved(*desktop.MouseEvent) {}
func (t *pathTile) MouseOut()                      { t.hovered = false; t.Refresh() }

func (t *pathTile) Cursor() desktop.Cursor { return desktop.PointerCursor }

// The keyboard reaches the tiles as it does the buttons: Tab to one,
// Space or Return to choose it.
func (t *pathTile) FocusGained()     { t.focused = true; t.Refresh() }
func (t *pathTile) FocusLost()       { t.focused = false; t.Refresh() }
func (t *pathTile) TypedRune(r rune) {}
func (t *pathTile) TypedKey(e *fyne.KeyEvent) {
	switch e.Name {
	case fyne.KeySpace, fyne.KeyReturn, fyne.KeyEnter:
		t.Tapped(nil)
	}
}

type pathTileRenderer struct {
	t       *pathTile
	bg      *canvas.Rectangle
	img     *canvas.Image
	label   *widget.RichText
	objects []fyne.CanvasObject
	layout  fyne.Layout
}

func (r *pathTileRenderer) Layout(s fyne.Size)           { r.layout.Layout(r.objects, s) }
func (r *pathTileRenderer) MinSize() fyne.Size           { return r.layout.MinSize(r.objects) }
func (r *pathTileRenderer) Objects() []fyne.CanvasObject { return r.objects }
func (r *pathTileRenderer) Destroy()                     {}

// Refresh colours the frame: the primary colour, thick, on the chosen
// tile; the input border on the other; the hover and focus tints over
// either, as Fyne's buttons do.
func (r *pathTileRenderer) Refresh() {
	t := r.t
	fill := theme.Color(theme.ColorNameButton)
	r.bg.StrokeColor, r.bg.StrokeWidth = theme.Color(theme.ColorNameInputBorder), 1
	if t.selected {
		fill = theme.Color(theme.ColorNameSelection)
		r.bg.StrokeColor, r.bg.StrokeWidth = theme.Color(theme.ColorNamePrimary), 3
	}
	switch {
	case t.focused:
		fill = blend(fill, theme.Color(theme.ColorNameFocus))
	case t.hovered:
		fill = blend(fill, theme.Color(theme.ColorNameHover))
	}
	r.bg.FillColor = fill
	r.label.Refresh()
	r.bg.Refresh()
	r.img.Refresh()
}

// blend lays a translucent theme tint over a fill, as the hover and
// focus colours are meant to be drawn.
func blend(under, over color.Color) color.Color {
	// RGBA is alpha-premultiplied: over + under*(1-over's alpha).
	ur, ug, ub, ua := under.RGBA()
	or, og, ob, oa := over.RGBA()
	mix := func(u, o uint32) uint8 { return uint8((o + u*(0xffff-oa)/0xffff) >> 8) }
	return color.RGBA{R: mix(ur, or), G: mix(ug, og), B: mix(ub, ob), A: mix(ua, oa)}
}
