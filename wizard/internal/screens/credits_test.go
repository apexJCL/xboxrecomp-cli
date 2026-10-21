package screens

import (
	"slices"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/driver/software"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/gamecfg"
)

// creditTexts is each link's words and each quiet line under a tree.
func creditTexts(o fyne.CanvasObject) (links []*widget.Hyperlink, metas []*quietText) {
	walkTree(o, func(o fyne.CanvasObject) {
		switch v := o.(type) {
		case *widget.Hyperlink:
			links = append(links, v)
		case *quietText:
			metas = append(metas, v)
		}
	})
	return links, metas
}

// The credits button: centred in the footer on every page, in the
// user's words; a tap opens the dialog. Welcome lists no credits.
func TestCreditsButton(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.win.Resize(fyne.NewSize(780, 580))
	if w.credits.Text != "♥️ Open-source" ||
		w.credits.Importance != widget.LowImportance {
		t.Errorf("button %q %v", w.credits.Text, w.credits.Importance)
	}
	for i := range w.pages {
		w.render(i)
		if !w.credits.Visible() {
			t.Errorf("page %d: the button is hidden", i)
		}
		at := a.Driver().AbsolutePositionForObject(w.credits)
		if mid := at.X + w.credits.Size().Width/2; mid < 380 || mid > 400 {
			t.Errorf("page %d: the button's middle is at %v", i, mid)
		}
	}
	test.Tap(w.credits)
	if w.creditsD == nil {
		t.Fatal("the button opened no dialog")
	}
	w.creditsD.Hide()
	if w.creditsD != nil {
		t.Error("a closed dialog still follows the window")
	}
	// A tap on the pink frame opens it too.
	var frame *outlineButton
	walkTree(w.win.Content(), func(o fyne.CanvasObject) {
		if v, ok := o.(*outlineButton); ok {
			frame = v
		}
	})
	if frame == nil || frame.btn != w.credits {
		t.Fatal("the credits button is not in its outline frame")
	}
	test.Tap(frame)
	if w.creditsD == nil {
		t.Fatal("a tap on the frame opened no dialog")
	}
	w.creditsD.Hide()
	w.render(pWelcome)
	links, _ := creditTexts(w.pages[pWelcome].obj)
	if len(links) != 0 || strings.Contains(shown(w.pages[pWelcome].obj), "Made with these") {
		t.Errorf("Welcome still lists %d credits", len(links))
	}
}

// The dialog: two lines an entry, wrapped not cut, the NOTICES sentence,
// links through openLink, sized to the window and following a resize.
func TestCreditsDialog(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	var opened []string
	w.openLink = func(u string) { opened = append(opened, u) }
	w.win.Resize(fyne.NewSize(780, 580))
	w.render(pDump)
	d, body := w.creditsDialog()
	links, metas := creditTexts(body)
	if len(links) != len(w.opt.Cfg.CreditList()) || metas[0].Wrapping != fyne.TextWrapWord ||
		metas[0].Truncation != fyne.TextTruncateOff {
		t.Fatalf("%d links; first line wraps %v, cuts %v", len(links), metas[0].Wrapping, metas[0].Truncation)
	}
	var notes bool
	walkTree(body, func(o fyne.CanvasObject) {
		if l, ok := o.(*widget.Label); ok && l.Text == creditsNotices {
			notes = true
		}
	})
	if links[0].Text != "blinx2-recomp" || links[1].Text != "xboxrecomp-cli" {
		t.Errorf("order: %q, %q", links[0].Text, links[1].Text)
	}
	if !notes || creditsTitle != "Open-source credits" {
		t.Error("no NOTICES sentence, or the title changed")
	}
	links[1].OnTapped()
	if !slices.Equal(opened, []string{w.opt.Cfg.CreditList()[1].URL}) {
		t.Errorf("opened %q", opened)
	}
	if creditsSize(fyne.NewSize(780, 580)) != fyne.NewSize(560, 484) ||
		creditsSize(fyne.NewSize(300, 400)) != fyne.NewSize(252, 304) ||
		creditsSize(fyne.NewSize(20, 20)) != fyne.NewSize(0, 0) {
		t.Error("size")
	}
	d.Show()
	wide := body.Size()
	w.win.Resize(fyne.NewSize(300, 400))
	narrow := body.Size()
	if narrow.Width >= wide.Width || narrow.Width > 252 || narrow.Height >= wide.Height {
		t.Errorf("the dialog's content is %v in a 300x400 window, %v in 780x580", narrow, wide)
	}
	d.Hide()
	if w.creditsD != nil {
		t.Error("a closed dialog still follows the window")
	}
}

// A config from a CLI older than the credits key: the wizard's own list.
func TestCreditsFallback(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	opt.Cfg.Credits = nil
	w := New(a, opt)
	list, _ := w.creditsList()
	links, _ := creditTexts(list)
	var names []string
	for _, l := range links {
		names = append(names, l.Text)
	}
	if len(names) != len(gamecfg.FallbackCredits) || names[0] != "xboxrecomp-cli" {
		t.Errorf("fallback %q", names)
	}
}

// Both themes draw the list: the role in the quiet colour, the glyph in
// the link's, the info glyph in the text's.
func TestCreditsThemes(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	_, body := w.creditsDialog()
	_, metas := creditTexts(body)
	if metas[0].seg.Style.ColorName != theme.ColorNamePlaceHolder {
		t.Errorf("role colour %q", metas[0].seg.Style.ColorName)
	}
	if !strings.HasPrefix(infoIcon().Name(), string(theme.ColorNameForeground)) {
		t.Errorf("info glyph %q", infoIcon().Name())
	}
	c := software.NewCanvas()
	c.SetContent(body)
	c.Resize(fyne.NewSize(560, 480))
	//lint:ignore SA1019 the deprecated fixed variants are right for fixed-variant images
	for name, th := range map[string]fyne.Theme{"light": theme.LightTheme(), "dark": theme.DarkTheme()} {
		img := software.RenderCanvas(c, Theme(th))
		if b := img.Bounds(); b.Dx() == 0 || b.Dy() == 0 {
			t.Errorf("%s: nothing drawn", name)
		}
		if svg := string(infoIcon().Content()); !strings.Contains(svg, "#") || strings.Contains(svg, "currentColor") {
			t.Errorf("%s: info glyph not coloured", name)
		}
	}
}
