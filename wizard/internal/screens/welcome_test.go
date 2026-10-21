package screens

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"

	"wizard/internal/model"
)

// Welcome's statements: three in one row, each with its own icon and
// only its lead shown; the rest is the tip.
func TestStatements(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	var items []*tipItem
	walkTree(w.pages[pWelcome].obj, func(o fyne.CanvasObject) {
		if v, ok := o.(*tipItem); ok {
			items = append(items, v)
		}
	})
	if len(items) != 3 {
		t.Fatalf("%d statements", len(items))
	}
	game := w.opt.Cfg.Game.Name
	want := []struct {
		lead, tip string
		icon      fyne.Resource
	}{
		{noGameLead, noGameText(game), theme.StorageIcon()},
		{downloadsLead, downloadsText, theme.DownloadIcon()},
		{sendsLead, sendsText, theme.VisibilityOffIcon()},
	}
	for i, s := range want {
		if items[i].lead != s.lead || items[i].tip != s.tip || items[i].icon.Name() != s.icon.Name() {
			t.Errorf("statement %d: %q %q", i, items[i].lead, items[i].icon.Name())
		}
	}
	page := shown(w.pages[pWelcome].obj)
	for _, s := range want {
		if strings.Contains(page, s.tip) {
			t.Errorf("the page shows the tip %q", s.tip)
		}
	}
}

// A tip opens on the pointer and closes when it leaves; a tap opens it
// too (a touch screen), and a second tap closes it. A click after a
// hover leaves it open.
func TestTips(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.win.Resize(fyne.NewSize(780, 580))
	w.render(pWelcome)
	var items []*tipItem
	walkTree(w.pages[pWelcome].obj, func(o fyne.CanvasObject) {
		if v, ok := o.(*tipItem); ok {
			items = append(items, v)
		}
	})
	one, two := items[0], items[1]
	one.MouseIn(nil)
	if w.tips.shown() != one.tip {
		t.Fatalf("hover shows %q", w.tips.shown())
	}
	bubble := w.tips.bubble
	if bubble.Size().Width > tipWidth || bubble.Position().X < 0 ||
		bubble.Position().X+bubble.Size().Width > w.tips.layer.Size().Width {
		t.Errorf("bubble at %v, %v", bubble.Position(), bubble.Size())
	}
	test.Tap(one)
	if w.tips.shown() != one.tip {
		t.Error("a click after the hover closed the tip")
	}
	one.MouseOut()
	if w.tips.shown() != "" {
		t.Error("the tip stays after the pointer left")
	}
	test.Tap(two)
	if w.tips.shown() != two.tip {
		t.Fatalf("a tap shows %q", w.tips.shown())
	}
	one.MouseOut() // another item's leaving does not close it
	if w.tips.shown() != two.tip {
		t.Error("another item closed the tip")
	}
	test.Tap(two)
	if w.tips.shown() != "" {
		t.Error("a second tap left the tip open")
	}
	test.Tap(one)
	w.render(pChecks)
	if w.tips.shown() != "" {
		t.Error("the tip followed the player to the next page")
	}
}

// A tap on an open tip's bubble closes it and goes no further: the tile
// under it stays unchosen. While a tap holds a tip open, a tap anywhere
// else only closes it; a resize or a scroll closes any tip.
func TestTipTaps(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.win.Resize(fyne.NewSize(780, 580))
	w.render(pWelcome)
	var items []*tipItem
	walkTree(w.pages[pWelcome].obj, func(o fyne.CanvasObject) {
		if v, ok := o.(*tipItem); ok {
			items = append(items, v)
		}
	})
	c := w.win.Canvas()
	middle := func(o fyne.CanvasObject) fyne.Position {
		at := a.Driver().AbsolutePositionForObject(o)
		return at.Add(fyne.NewPos(o.Size().Width/2, o.Size().Height/2))
	}
	before := w.st.Path
	tile := tileOf(t, w, model.PathUpdate)
	// A hover opens one with nothing under the bubble to catch a tap; a
	// tap opens one over the catcher.
	for _, open := range []func(){func() { items[0].MouseIn(nil) }, func() { test.Tap(items[0]) }} {
		open()
		// Lay the bubble over the tile, as a short window does.
		w.tips.bubble.Move(a.Driver().AbsolutePositionForObject(tile).Subtract(a.Driver().AbsolutePositionForObject(w.tips.layer)))
		test.TapCanvas(c, middle(w.tips.bubble))
		if w.tips.shown() != "" {
			t.Error("a tap on the bubble left it open")
		}
		if w.st.Path != before {
			t.Fatalf("a tap on the bubble chose %q", w.st.Path)
		}
	}
	test.Tap(items[1])
	test.TapCanvas(c, middle(tile))
	if w.tips.shown() != "" || w.st.Path != before {
		t.Errorf("a tap elsewhere: tip %q, path %q", w.tips.shown(), w.st.Path)
	}
	test.TapCanvas(c, middle(tile))
	if w.st.Path != model.PathUpdate {
		t.Errorf("with no tip, a tap on the tile chose %q", w.st.Path)
	}
	items[0].MouseIn(nil)
	w.win.Resize(fyne.NewSize(780, 300)) // short, so the page scrolls
	if w.tips.shown() != "" {
		t.Error("a resize left the tip open")
	}
	items[0].MouseIn(nil)
	scroll := w.pages[pWelcome].obj.(*container.Scroll)
	scroll.ScrollToBottom()
	if w.tips.shown() != "" {
		t.Error("a scroll left the tip open")
	}
}

// A failure the Done page rebuilds from a bare error has no command to
// lead with.
func TestBuildFailureError(t *testing.T) {
	err := errors.New("exit status 1")
	if got := (&buildFailure{Err: err}).Error(); got != "exit status 1" {
		t.Errorf("no command: %q", got)
	}
	if got := (&buildFailure{Cmd: "./x package", Err: err}).Error(); got != "./x package: exit status 1" {
		t.Errorf("with a command: %q", got)
	}
}

// The path tiles act as radios: none chosen at first, a tap chooses one
// and clears the other, the sentence under them follows, and Next
// waits for a choice.
func TestPathTiles(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.render(pWelcome)
	fresh, update := tileOf(t, w, model.PathFresh), tileOf(t, w, model.PathUpdate)
	if fresh.label != pathFresh || update.label != pathUpdate {
		t.Fatalf("labels %q %q", fresh.label, update.label)
	}
	if fresh.selected || update.selected || w.pathHelp.Text() != pathPickHint || !w.next.Disabled() {
		t.Fatalf("before a choice: %v %v %q", fresh.selected, update.selected, w.pathHelp.Text())
	}
	test.Tap(fresh)
	if !fresh.selected || update.selected || w.st.Path != model.PathFresh || w.pathHelp.Text() != pathFreshHint {
		t.Errorf("Fresh: %v %v %q %q", fresh.selected, update.selected, w.st.Path, w.pathHelp.Text())
	}
	test.Tap(update)
	if fresh.selected || !update.selected || w.st.Path != model.PathUpdate || w.pathHelp.Text() != pathUpdateHint {
		t.Errorf("Update: %v %v %q %q", fresh.selected, update.selected, w.st.Path, w.pathHelp.Text())
	}
	test.Tap(update)
	if !update.selected {
		t.Error("a second tap cleared the choice")
	}
	if w.next.Disabled() {
		t.Error("Next waits after a choice")
	}
	// The keyboard chooses too.
	fresh.TypedKey(&fyne.KeyEvent{Name: fyne.KeySpace})
	if w.st.Path != model.PathFresh {
		t.Error("Space did not choose the focused tile")
	}
	// The chosen tile is framed in the primary colour, the other is not.
	fr := test.WidgetRenderer(fresh).(*pathTileRenderer)
	ur := test.WidgetRenderer(update).(*pathTileRenderer)
	if fr.bg.StrokeColor != theme.Color(theme.ColorNamePrimary) || ur.bg.StrokeColor == fr.bg.StrokeColor ||
		fr.bg.StrokeWidth <= ur.bg.StrokeWidth {
		t.Error("the chosen tile is not highlighted")
	}
}

// An error the wizard has no words for: the row says something went
// wrong and where the log is, never the error's own words; Copy error
// and Copy log path put them on the clipboard.
func TestErrorDetails(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	raw := errors.New("this developer build names no release tag to clone: run it with --game DIR")
	rows := checkRows(w.opt.Cfg, nil, nil, raw, false)
	if len(rows) != 1 || rows[0].Detail != prepareOther || rows[0].Err != raw {
		t.Fatalf("rows %+v", rows)
	}
	view := w.rowView(rows[0], 100)
	page := shown(view)
	if strings.Contains(page, "release tag") || strings.Contains(page, "--game") {
		t.Errorf("the row shows the error's own words:\n%s", page)
	}
	for _, want := range []string{prepareOther, errorSeeLog, w.logFile(), copyErrorText, copyLogPathText, openLogFolder} {
		if !strings.Contains(page, want) {
			t.Errorf("no %q in:\n%s", want, page)
		}
	}
	test.Tap(buttonOf(t, view, copyErrorText))
	if got := a.Clipboard().Content(); got != raw.Error() {
		t.Errorf("Copy error copied %q", got)
	}
	test.Tap(buttonOf(t, view, copyLogPathText))
	if got := a.Clipboard().Content(); got != w.logFile() {
		t.Errorf("Copy log path copied %q", got)
	}
	// An error with plain words keeps them, with the log under them.
	space := &SpaceError{Free: 1 << 30, Need: 15 << 30}
	if r := checkRows(w.opt.Cfg, nil, nil, space, false)[0]; r.Detail != plainPrepare(space) || r.Err != space {
		t.Errorf("space row %+v", r)
	}
}

// A failure outside the pages: a box with the error's sentence when it
// is a player's, else a plain one, over the log; the words go to the log.
func TestErrorBox(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.win.Resize(fyne.NewSize(780, 580))
	w.errorBox(errors.New("exec: xdg-open: not found"))
	if w.win.Canvas().Overlays().Top() == nil {
		t.Fatal("no box")
	}
	if box := shown(w.errorBody(errors.New("exec: xdg-open: not found"))); !strings.Contains(box, errorText) ||
		strings.Contains(box, "xdg-open") || !strings.Contains(box, copyErrorText) {
		t.Errorf("a raw error's box:\n%s", box)
	}
	// A player's sentence, even wrapped, is shown as it is, over the
	// same log helper.
	if box := shown(w.errorBody(fmt.Errorf("install: %w", playerError(installNoScript)))); !strings.Contains(box, installNoScript) ||
		strings.Contains(box, errorText) || !strings.Contains(box, copyLogPathText) {
		t.Errorf("a plain error's box:\n%s", box)
	}
	b, err := os.ReadFile(w.logFile())
	if err != nil || !strings.Contains(string(b), "error: exec: xdg-open: not found") {
		t.Errorf("wizard.log: %q %v", b, err)
	}
	help := w.errorHelp(nil, w.logFile())
	if page := shown(help); strings.Contains(page, copyErrorText) || !strings.Contains(page, copyLogPathText) {
		t.Errorf("with no error:\n%s", page)
	}
}
