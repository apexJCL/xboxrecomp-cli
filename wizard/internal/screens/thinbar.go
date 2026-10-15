package screens

import (
	"image/color"
	"math"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// thinBarHeight is the bar's drawn height. Fyne's ProgressBar is a text
// line tall and always prints a percentage, which reads wrong for the
// eight discrete steps and is too heavy under the checks table.
const thinBarHeight = 6

// pulsePeriod is one sweep of the sheen across the bar's fill.
const pulsePeriod = 1600 * time.Millisecond

// thinBar is a flat progress bar: continuous (Segments 0) or split into
// Segments equal parts, the first round(Value*Segments) of them filled.
// It is drawn centred in whatever height it is given, so a layout that
// stretches it (a Border's centre beside a button) keeps it thin.
//
// A continuous bar can pulse: a translucent sheen sweeps over the filled
// part (the whole track while nothing is filled), so a build that prints
// nothing for minutes still shows that it runs. The fraction stays what
// the player reads; only the sheen moves.
type thinBar struct {
	widget.BaseWidget
	Value    float64 // 0 to 1
	Segments int
	Height   float32 // drawn height; 0 is thinBarHeight
	Pulse    bool

	anim *fyne.Animation
	// running is whether anim is started. Fyne's runner does not dedupe
	// Start, and a second copy would keep ticking after Stop.
	running bool
	sweep   float32 // the sheen's place in its sweep, 0 to 1
	r       *thinBarRenderer
}

func newThinBar(segments int) *thinBar {
	b := &thinBar{Segments: segments}
	b.ExtendBaseWidget(b)
	return b
}

func (b *thinBar) SetValue(v float64) {
	b.Value = math.Max(0, math.Min(1, v))
	if b.Value >= 1 {
		b.StopPulse() // done: nothing left to show as running
	}
	b.Refresh()
}

func (b *thinBar) height() float32 {
	if b.Height > 0 {
		return b.Height
	}
	return thinBarHeight
}

// StartPulse sweeps the sheen until StopPulse.
func (b *thinBar) StartPulse() {
	b.Pulse = true
	if b.anim == nil {
		b.anim = fyne.NewAnimation(pulsePeriod, b.tick)
		b.anim.Curve = fyne.AnimationLinear
		b.anim.RepeatCount = fyne.AnimationRepeatForever
	}
	b.startAnim()
	b.Refresh()
}

func (b *thinBar) StopPulse() {
	b.Pulse = false
	b.stopAnim()
	b.Refresh()
}

func (b *thinBar) startAnim() {
	if b.anim != nil && !b.running {
		b.anim.Start()
		b.running = true
	}
}

func (b *thinBar) stopAnim() {
	if b.anim != nil && b.running {
		b.anim.Stop()
		b.running = false
	}
}

// tick is the animation's frame: it moves the sheen and nothing else.
func (b *thinBar) tick(t float32) {
	b.sweep = t
	if b.r != nil {
		b.r.placeSheen()
		b.r.sheen.Refresh()
	}
}

// filled is the number of segments drawn in the primary colour.
func (b *thinBar) filled() int {
	return int(math.Round(b.Value * float64(b.Segments)))
}

func (b *thinBar) CreateRenderer() fyne.WidgetRenderer {
	r := &thinBarRenderer{b: b, track: canvas.NewRectangle(color.Transparent), sheen: canvas.NewRectangle(color.Transparent)}
	b.r = r
	r.Refresh()
	// A renderer the cache dropped and rebuilt picks the sweep up again.
	if b.Pulse {
		b.startAnim()
	}
	return r
}

type thinBarRenderer struct {
	b     *thinBar
	track *canvas.Rectangle   // continuous: the empty part
	parts []*canvas.Rectangle // continuous: the fill; segmented: one per segment
	sheen *canvas.Rectangle   // continuous and pulsing: the band that sweeps
	size  fyne.Size
}

func (r *thinBarRenderer) colors() (fill, empty color.Color) {
	th := r.b.Theme()
	v := fyne.CurrentApp().Settings().ThemeVariant()
	return th.Color(theme.ColorNamePrimary, v), th.Color(theme.ColorNameInputBorder, v)
}

func (r *thinBarRenderer) Layout(size fyne.Size) {
	r.size = size
	h := min(size.Height, r.b.height())
	y := (size.Height - h) / 2
	if r.b.Segments <= 0 {
		r.track.Move(fyne.NewPos(0, y))
		r.track.Resize(fyne.NewSize(size.Width, h))
		r.parts[0].Move(fyne.NewPos(0, y))
		r.parts[0].Resize(fyne.NewSize(size.Width*float32(r.b.Value), h))
		r.placeSheen()
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

// placeSheen puts the band at its place in the sweep: a fifth of the bar
// wide, from the left edge of the fill to its right edge.
func (r *thinBarRenderer) placeSheen() {
	h := min(r.size.Height, r.b.height())
	span := r.size.Width * float32(r.b.Value)
	if r.b.Value <= 0 {
		span = r.size.Width // nothing filled yet: the whole track
	}
	band := min(r.size.Width/5, span)
	r.sheen.Move(fyne.NewPos((span-band)*r.b.sweep, (r.size.Height-h)/2))
	r.sheen.Resize(fyne.NewSize(band, h))
}

func (r *thinBarRenderer) MinSize() fyne.Size { return fyne.NewSize(0, r.b.height()) }

func (r *thinBarRenderer) Refresh() {
	fill, empty := r.colors()
	want := max(r.b.Segments, 1)
	for len(r.parts) < want {
		r.parts = append(r.parts, canvas.NewRectangle(fill))
	}
	r.parts = r.parts[:want]
	radius := r.b.height() / 2
	r.track.FillColor = empty
	r.track.CornerRadius = radius
	// Over the fill the band is the colour drawn on primary (the primary
	// itself would not show on primary); over an empty track it is the
	// primary. Both translucent.
	sheen := fill
	if r.b.Value > 0 {
		sheen = r.b.Theme().Color(theme.ColorNameForegroundOnPrimary, fyne.CurrentApp().Settings().ThemeVariant())
	}
	cr, cg, cb, _ := sheen.RGBA()
	r.sheen.FillColor = color.NRGBA{R: uint8(cr >> 8), G: uint8(cg >> 8), B: uint8(cb >> 8), A: 0x66}
	r.sheen.CornerRadius = radius
	for i, p := range r.parts {
		p.CornerRadius = radius
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
	if r.b.Segments <= 0 && r.b.Pulse {
		out = append(out, r.sheen)
	}
	return out
}

func (r *thinBarRenderer) Destroy() {
	r.b.stopAnim()
	if r.b.r == r {
		r.b.r = nil
	}
}
