package screens

import (
	"fmt"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"

	"wizard/internal/gamecfg"
)

// welcomeRows is how many credits Welcome shows before its box scrolls:
// the game's, ours and the wizard's own at once, with the folder pickers
// still on a 580-high window's first screen.
const welcomeRows = 8

// creditsList is the open-source credits in the CLI's order (the game's
// own first): each name is a link to the project's page, with what it
// does and its licence dimmed. compact is Welcome's one line per entry,
// cut short in a narrow window so the folder pickers stay in view; the
// dialog's two lines are there to be read, so nothing is cut and the
// role wraps instead.
func (w *Wizard) creditsList(compact bool) (list *fyne.Container, rows []fyne.CanvasObject) {
	for _, c := range w.opt.Cfg.CreditList() {
		link := w.hintLink(&gamecfg.Link{Text: c.Name, URL: c.URL})
		if link == nil {
			continue
		}
		meta := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
		meta.SetText(fmt.Sprintf(creditMeta, c.Role, c.License))
		var row fyne.CanvasObject
		if compact {
			meta.Truncation = fyne.TextTruncateEllipsis
			row = container.NewBorder(nil, nil, link, nil, meta)
		} else {
			meta.Wrapping = fyne.TextWrapWord
			// The two lines read as one entry: the role sits close under
			// its name.
			row = container.New(layout.NewCustomPaddedVBoxLayout(-theme.InnerPadding()), link, meta)
		}
		rows = append(rows, row)
	}
	return container.NewVBox(rows...), rows
}

// welcomeCredits is the list on Welcome, in a box welcomeRows tall that
// scrolls the rest.
func (w *Wizard) welcomeCredits() fyne.CanvasObject {
	list, rows := w.creditsList(true)
	scroll := container.NewVScroll(list)
	if n := min(len(rows), welcomeRows); n > 0 {
		h := float32(n)*rows[0].MinSize().Height + float32(n-1)*theme.Padding()
		scroll.SetMinSize(fyne.NewSize(0, h))
	}
	return scroll
}

// creditsSize is the credits dialog's size in a window this big.
func creditsSize(win fyne.Size) fyne.Size {
	return fyne.NewSize(max(min(560, win.Width-48), 0), max(min(520, win.Height-96), 0))
}

// creditsDialog is the header's info button: the list, two lines an
// entry, then where each licence's full text is. body is the dialog's
// content, for the tests.
func (w *Wizard) creditsDialog() (_ dialog.Dialog, body fyne.CanvasObject) {
	list, _ := w.creditsList(false)
	body = container.NewBorder(nil, para(creditsNotices), nil, nil, container.NewVScroll(list))
	d := dialog.NewCustom(creditsTitle, closeText, body, w.win)
	d.SetOnClosed(func() {
		if w.creditsD == d {
			w.creditsD = nil
		}
	})
	w.creditsD = d
	w.fitDialogs(w.win.Canvas().Size())
	return d, body
}
