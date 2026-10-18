package screens

import (
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/gamecfg"
	"wizard/internal/repo"
	"wizard/internal/update"
)

// allText is every label's and rich text's words under o.
func allText(o fyne.CanvasObject) string {
	var b strings.Builder
	walkTree(o, func(c fyne.CanvasObject) {
		switch v := c.(type) {
		case *widget.Label:
			b.WriteString(v.Text + "\n")
		case *widget.RichText:
			b.WriteString(v.String() + "\n")
		}
	})
	return b.String()
}

// A local source (D14) shows its banner on every page, names itself in
// the technical details and the Game source row, and still checks for a
// newer wizard against the game's repo with the release the binary was
// built for.
func TestLocalSource(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	opt.NoUpdateCheck = false
	opt.Cfg.Release.Dev = true // a --dev config: no tag of its own
	opt.Source = &Source{
		Local:  &repo.Local{Path: "/dev/game", Ref: "HEAD"},
		Commit: "3d75004aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Dirty: true,
	}
	opt.Update = &gamecfg.Release{Repo: opt.Cfg.Release.Repo, Tag: "v1", Commit: "c"}
	w := New(a, opt)
	var asked atomic.Int32
	w.latest = func() (*update.Release, string) { asked.Add(1); return nil, "stub" }
	w.doctor = loadDoctor(t, "doctor.json")
	w.env = &Env{Root: w.root, Game: w.root}
	want := "Local source: 3d75004, not a release; nothing is published; uncommitted changes in /dev/game are not built"
	for i := range w.pages {
		w.render(i)
		if s := allText(w.win.Content()); !strings.Contains(s, want) {
			t.Fatalf("page %d has no banner:\n%s", i, s)
		}
	}
	if p := w.pinsText(); !strings.Contains(p, "local source checkout (3d75004aaaaa)") {
		t.Fatal(p)
	}
	if w.updateRelease().Tag != "v1" {
		t.Fatal("the update check does not use the binary's release")
	}
	w.render(pWelcome)
	test.Tap(w.next)
	for i := 0; i < 100 && asked.Load() == 0; i++ {
		time.Sleep(10 * time.Millisecond)
	}
	if asked.Load() != 1 {
		t.Fatal("no update check in local mode with a release binary")
	}

	// A developer build of the wizard has no release to compare.
	opt.Update = nil
	w2 := New(a, opt)
	if !w2.updateRelease().Dev {
		t.Fatal("a local source with no embedded release should skip the check")
	}

	env := &Env{Game: "/r/src", Local: opt.Source.Local, Commit: opt.Source.Commit}
	r := rowByName(checkRows(opt.Cfg, env, loadDoctor(t, "doctor.json"), nil, false), rowSource)
	if r.Status != statusLocal || !strings.Contains(r.Detail, "/dev/game at 3d75004aaaaa") {
		t.Fatalf("source row: %+v", r)
	}
}

// A release build shows no banner.
func TestNoBannerForRelease(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	if s := allText(w.win.Content()); strings.Contains(s, "Local source") {
		t.Fatal(s)
	}
}
