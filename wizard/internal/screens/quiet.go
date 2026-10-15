package screens

import (
	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// quietText is secondary text (a status, the step count, the version, the
// log's last line) in the theme's placeholder colour: low contrast but
// readable in both variants, which a LowImportance label (the disabled
// colour) is not.
type quietText struct {
	*widget.RichText
	seg *widget.TextSegment
}

func newQuiet(size fyne.ThemeSizeName, align fyne.TextAlign, style fyne.TextStyle) *quietText {
	seg := &widget.TextSegment{Style: widget.RichTextStyle{
		Alignment: align, ColorName: theme.ColorNamePlaceHolder, SizeName: size, TextStyle: style, Inline: true}}
	return &quietText{RichText: widget.NewRichText(seg), seg: seg}
}

func (q *quietText) SetText(s string) {
	q.seg.Text = s
	q.Refresh()
}

// SetColor sets the text's theme colour; "" is the quiet default.
func (q *quietText) SetColor(n fyne.ThemeColorName) {
	if n == "" {
		n = theme.ColorNamePlaceHolder
	}
	q.seg.Style.ColorName = n
	q.Refresh()
}

func (q *quietText) Text() string { return q.seg.Text }
