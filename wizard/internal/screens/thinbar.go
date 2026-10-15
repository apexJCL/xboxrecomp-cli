package screens

import (
	"image/color"
	"math"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// thinBarHeight is the bar's drawn height. Fyne's ProgressBar is a text
// line tall and always prints a percentage, which reads wrong for the
// eight discrete steps and is too heavy under the checks table.
const thinBarHeight = 6

// thinBar is a flat progress bar: continuous (Segments 0) or split into
// Segments equal parts, the first round(Value*Segments) of them filled.
// It is drawn centred in whatever height it is given, so a layout that
// stretches it (a Border's centre beside a button) keeps it thin.
type thinBar struct {
	widget.BaseWidget
	Value    float64 // 0 to 1
	Segments int
}

func newThinBar(segments int) *thinBar {
	b := &thinBar{Segments: segments}
	b.ExtendBaseWidget(b)
	return b
}

func (b *thinBar) SetValue(v float64) {
	b.Value = math.Max(0, math.Min(1, v))
	b.Refresh()
}

// filled is the number of segments drawn in the primary colour.
func (b *thinBar) filled() int {
	return int(math.Round(b.Value * float64(b.Segments)))
}

func (b *thinBar) CreateRenderer() fyne.WidgetRenderer {
	r := &thinBarRenderer{b: b, track: canvas.NewRectangle(color.Transparent)}
	r.Refresh()
	return r
}

type thinBarRenderer struct {
	b     *thinBar
	track *canvas.Rectangle   // continuous: the empty part
	parts []*canvas.Rectangle // continuous: the fill; segmented: one per segment
	size  fyne.Size
}

func (r *thinBarRenderer) colors() (fill, empty color.Color) {
	th := r.b.Theme()
	v := fyne.CurrentApp().Settings().ThemeVariant()
	return th.Color(theme.ColorNamePrimary, v), th.Color(theme.ColorNameInputBorder, v)
}

func (r *thinBarRenderer) Layout(size fyne.Size) {
	r.size = size
	h := float32(math.Min(float64(size.Height), thinBarHeight))
	y := (size.Height - h) / 2
	if r.b.Segments <= 0 {
		r.track.Move(fyne.NewPos(0, y))
		r.track.Resize(fyne.NewSize(size.Width, h))
		r.parts[0].Move(fyne.NewPos(0, y))
		r.parts[0].Resize(fyne.NewSize(size.Width*float32(r.b.Value), h))
		return
	}
	gap := theme.Padding() / 2
	n := float32(r.b.Segments)
	w := (size.Width - gap*(n-1)) / n
	for i, p := range r.parts {
		p.Move(fyne.NewPos(float32(i)*(w+gap), y))
		p.Resize(fyne.NewSize(w, h))
	}
}

func (r *thinBarRenderer) MinSize() fyne.Size { return fyne.NewSize(0, thinBarHeight) }

func (r *thinBarRenderer) Refresh() {
	fill, empty := r.colors()
	want := max(r.b.Segments, 1)
	for len(r.parts) < want {
		r.parts = append(r.parts, canvas.NewRectangle(fill))
	}
	r.parts = r.parts[:want]
	r.track.FillColor = empty
	r.track.CornerRadius = thinBarHeight / 2
	for i, p := range r.parts {
		p.CornerRadius = thinBarHeight / 2
		p.FillColor = fill
		if r.b.Segments > 0 && i >= r.b.filled() {
			p.FillColor = empty
		}
	}
	r.Layout(r.size)
	for _, o := range r.Objects() {
		o.Refresh()
	}
}

func (r *thinBarRenderer) Objects() []fyne.CanvasObject {
	var out []fyne.CanvasObject
	if r.b.Segments <= 0 {
		out = append(out, r.track)
	}
	for _, p := range r.parts {
		out = append(out, p)
	}
	return out
}

func (r *thinBarRenderer) Destroy() {}
