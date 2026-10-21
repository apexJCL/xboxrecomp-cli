package screens

import (
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/driver/desktop"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// Fyne has no tooltip, so Welcome's statements bring their own: a
// pointer over one shows its sentence in a bubble under it, and a tap
// does too, for a touch screen (a Steam Deck) where nothing hovers.

// tipWidth is a tip bubble's widest, so a sentence reads as a short
// paragraph, not one long line.
const tipWidth = 320

// tipLayer is the window's top layer, over the page: one tip's bubble at
// a time. It is part of the window's content, not an overlay: an overlay
// takes every mouse event, so the item under it would lose the pointer at
// once and its tip would blink. Neither the bubble nor the catcher is
// Hoverable, so the pointer reaches the page under them; both take taps,
// so a tap on the bubble cannot choose the tile under it.
type tipLayer struct {
	layer   *fyne.Container // no layout: the bubble sits where show puts it
	bubble  *tipTap
	catcher *tipTap // under the bubble while a tap holds it open
	bg      *canvas.Rectangle
	text    *widget.RichText
	seg     *widget.TextSegment
	owner   *tipItem
	byTap   bool // a tap opened it, so the next tap on its item closes it
}

func newTipLayer() *tipLayer {
	l := &tipLayer{bg: canvas.NewRectangle(nil), seg: &widget.TextSegment{Style: widget.RichTextStyleParagraph}}
	l.bg.CornerRadius = theme.InputRadiusSize()
	l.bg.StrokeWidth = 1
	l.text = widget.NewRichText(l.seg)
	l.text.Wrapping = fyne.TextWrapWord
	l.bubble = newTipTap(container.NewStack(l.bg, l.text), func() { l.hide(nil) })
	l.bubble.Hide()
	// A touch screen has no pointer to leave with: a tap anywhere closes
	// the tip it opened, and does only that, as a pop-up would.
	l.catcher = newTipTap(nil, func() { l.hide(nil) })
	l.catcher.Hide()
	l.layer = container.NewWithoutLayout(l.catcher, l.bubble)
	return l
}

// tipTap is a part of the tip layer that a tap closes.
type tipTap struct {
	widget.BaseWidget
	obj    fyne.CanvasObject
	tapped func()
}

func newTipTap(obj fyne.CanvasObject, tapped func()) *tipTap {
	if obj == nil {
		// Transparent, not nil: the software painter (the screenshots)
		// cannot fill a rectangle with no colour.
		obj = canvas.NewRectangle(color.Transparent)
	}
	t := &tipTap{obj: obj, tapped: tapped}
	t.ExtendBaseWidget(t)
	return t
}

func (t *tipTap) CreateRenderer() fyne.WidgetRenderer { return widget.NewSimpleRenderer(t.obj) }
func (t *tipTap) Tapped(*fyne.PointEvent)             { t.tapped() }

// show puts t's sentence under t, or above it when the window ends
// first, inside the window either way.
func (l *tipLayer) show(t *tipItem, byTap bool) {
	l.owner, l.byTap = t, byTap
	l.seg.Text = t.tip
	l.bg.FillColor = theme.Color(theme.ColorNameOverlayBackground)
	l.bg.StrokeColor = theme.Color(theme.ColorNameInputBorder)
	pad := theme.Padding()
	area := l.layer.Size()
	width := min(tipWidth, area.Width-2*pad)
	if width <= 0 {
		return
	}
	// A wrapped text's height follows its width: size it first, then ask.
	l.text.Resize(fyne.NewSize(width, 0))
	size := fyne.NewSize(width, l.text.MinSize().Height)
	d := fyne.CurrentApp().Driver()
	at := d.AbsolutePositionForObject(t).Subtract(d.AbsolutePositionForObject(l.layer))
	x := min(at.X, area.Width-pad-size.Width)
	y := at.Y + t.Size().Height + pad
	if y+size.Height > area.Height && at.Y-pad-size.Height >= 0 {
		y = at.Y - pad - size.Height
	}
	l.bubble.Move(fyne.NewPos(max(x, pad), y))
	l.bubble.Resize(size)
	l.bubble.Show()
	l.bubble.Refresh()
	if byTap {
		l.catcher.Move(fyne.NewPos(0, 0))
		l.catcher.Resize(area)
		l.catcher.Show()
	} else {
		l.catcher.Hide()
	}
}

// hide closes the bubble when t opened it; nil closes any.
func (l *tipLayer) hide(t *tipItem) {
	if t != nil && l.owner != t {
		return
	}
	l.owner, l.byTap = nil, false
	l.bubble.Hide()
	l.catcher.Hide()
}

// shown is the tip on show, or "".
func (l *tipLayer) shown() string {
	if l.owner == nil || !l.bubble.Visible() {
		return ""
	}
	return l.seg.Text
}

// tipItem is one statement: its icon, its lead in bold and an info glyph
// that says there is more; the pointer or a tap shows the rest.
type tipItem struct {
	widget.BaseWidget
	layer     *tipLayer
	icon      fyne.Resource
	lead, tip string
}

func newTipItem(l *tipLayer, icon fyne.Resource, lead, tip string) *tipItem {
	t := &tipItem{layer: l, icon: icon, lead: lead, tip: tip}
	t.ExtendBaseWidget(t)
	return t
}

// CreateRenderer draws the lead as rich text, not a Label: a Label holds
// a tappable selection that would take the item's taps.
func (t *tipItem) CreateRenderer() fyne.WidgetRenderer {
	lead := widget.NewRichText(&widget.TextSegment{Style: widget.RichTextStyleStrong, Text: t.lead})
	lead.Wrapping = fyne.TextWrapWord
	top := func(o fyne.CanvasObject) fyne.CanvasObject { return container.NewVBox(container.NewPadded(o)) }
	return widget.NewSimpleRenderer(container.NewBorder(nil, nil,
		top(widget.NewIcon(t.icon)), top(widget.NewIcon(infoIcon())), lead))
}

func (t *tipItem) MouseIn(*desktop.MouseEvent)    { t.layer.show(t, false) }
func (t *tipItem) MouseMoved(*desktop.MouseEvent) {}
func (t *tipItem) MouseOut()                      { t.layer.hide(t) }

// Tapped opens the tip, or closes the one a tap opened: a pointer that
// hovers has opened it already, and a click then must not close it.
func (t *tipItem) Tapped(*fyne.PointEvent) {
	if t.layer.owner == t && t.layer.byTap && t.layer.bubble.Visible() {
		t.layer.hide(t)
		return
	}
	t.layer.show(t, true)
}

func (t *tipItem) Cursor() desktop.Cursor { return desktop.PointerCursor }
