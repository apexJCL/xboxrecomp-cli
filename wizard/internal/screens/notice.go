package screens

import (
	"image/color"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// noticeTint is the alpha of the notice's background: enough to set the
// block apart on light and dark, little enough for the text on it to
// keep the contrast it has on the page.
const noticeTint = 0x2a

// notice is the private-bundle notice: a tinted, bordered block with the
// warning icon, the lead and tail in bold in the warning colour and the
// body between them. A sentence among sentences was easy to skip.
type notice struct {
	widget.BaseWidget
	text *widget.RichText
}

func noticeBox(lead, body, tail string) *notice {
	strong := func(s string) *widget.TextSegment {
		st := widget.RichTextStyleStrong
		st.ColorName = theme.ColorNameWarning
		st.Inline = true
		return &widget.TextSegment{Text: s, Style: st}
	}
	n := &notice{text: widget.NewRichText(
		strong(lead+" "),
		&widget.TextSegment{Text: body + " ", Style: widget.RichTextStyleInline},
		strong(tail))}
	n.text.Wrapping = fyne.TextWrapWord
	n.ExtendBaseWidget(n)
	return n
}

func (n *notice) CreateRenderer() fyne.WidgetRenderer {
	bg := canvas.NewRectangle(color.Transparent)
	icon := widget.NewIcon(theme.NewWarningThemedResource(theme.WarningIcon()))
	// The icon at the first line, not centred on a wrapped paragraph.
	content := container.NewPadded(container.NewBorder(nil, nil, container.NewVBox(icon), nil, n.text))
	r := &noticeRenderer{n: n, bg: bg, content: content}
	r.Refresh()
	return r
}

type noticeRenderer struct {
	n       *notice
	bg      *canvas.Rectangle
	content *fyne.Container
}

// colors re-reads the theme, so the box follows a light or dark switch and
// the palette's darker amber on white.
func (r *noticeRenderer) colors() (fill, border color.Color) {
	v := fyne.CurrentApp().Settings().ThemeVariant()
	c := r.n.Theme().Color(theme.ColorNameWarning, v)
	cr, cg, cb, _ := c.RGBA()
	return color.NRGBA{R: uint8(cr >> 8), G: uint8(cg >> 8), B: uint8(cb >> 8), A: noticeTint}, c
}

func (r *noticeRenderer) Layout(size fyne.Size) {
	r.bg.Resize(size)
	r.content.Resize(size)
}

func (r *noticeRenderer) MinSize() fyne.Size { return r.content.MinSize() }

func (r *noticeRenderer) Refresh() {
	r.bg.FillColor, r.bg.StrokeColor = r.colors()
	r.bg.StrokeWidth = 1
	r.bg.CornerRadius = theme.InputRadiusSize()
	r.bg.Refresh()
	r.content.Refresh()
}

func (r *noticeRenderer) Objects() []fyne.CanvasObject { return []fyne.CanvasObject{r.bg, r.content} }

func (r *noticeRenderer) Destroy() {}

// spaced is the notice with room above and below it on a page.
func spaced(o fyne.CanvasObject) fyne.CanvasObject {
	pad := theme.Padding()
	return container.New(layout.NewCustomPaddedLayout(2*pad, 2*pad, 0, 0), o)
}
