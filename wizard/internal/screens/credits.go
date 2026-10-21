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

// creditsList is the open-source credits in the CLI's order (the game's
// own first): each name is a link to the project's page over what it
// does and its licence, dimmed. The lines are there to be read, so
// nothing is cut and the role wraps instead.
func (w *Wizard) creditsList() (list *fyne.Container, rows []fyne.CanvasObject) {
	for _, c := range w.opt.Cfg.CreditList() {
		link := w.hintLink(&gamecfg.Link{Text: c.Name, URL: c.URL})
		if link == nil {
			continue
		}
		meta := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
		meta.SetText(fmt.Sprintf(creditMeta, c.Role, c.License))
		meta.Wrapping = fyne.TextWrapWord
		// The two lines read as one entry: the role sits close under its
		// name.
		rows = append(rows, container.New(layout.NewCustomPaddedVBoxLayout(-theme.InnerPadding()), link, meta))
	}
	return container.NewVBox(rows...), rows
}

// creditsSize is the credits dialog's size in a window this big.
func creditsSize(win fyne.Size) fyne.Size {
	return fyne.NewSize(max(min(560, win.Width-48), 0), max(min(520, win.Height-96), 0))
}

// creditsDialog is the footer's credits button: a modal with the list,
// two lines an entry, then where each licence's full text is. body is
// the dialog's content, for the tests.
func (w *Wizard) creditsDialog() (_ dialog.Dialog, body fyne.CanvasObject) {
	list, _ := w.creditsList()
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
