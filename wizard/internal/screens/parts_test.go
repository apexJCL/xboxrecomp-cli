package screens

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/gamecfg"
	"wizard/internal/runner"
)

// feedParts drives c as a build does: a "$ " line starts the next
// command, "" ends it well, any other line is its output; the lines come
// evenly over the first end seconds.
func feedParts(c *runner.Checklist, at func(int) time.Time, lines []string, end int) {
	cmd := 0
	for k, l := range lines {
		now := at(k * end / len(lines))
		switch {
		case l == "":
			c.EndCommand(nil, false, now)
			cmd++
		case strings.HasPrefix(l, "$ "):
			c.StartCommand(cmd, now)
			c.Feed(l, now)
		default:
			c.Feed(l, now)
		}
	}
}

func count(s []string, x string) int {
	n := 0
	for _, v := range s {
		if v == x {
			n++
		}
	}
	return n
}

func TestPartText(t *testing.T) {
	for cli, want := range map[string]string{"enhance hud": "Redraw the HUD and menus", "plan": "Build the game", "fetch": "Fetch"} {
		if got := partName(cli); got != want {
			t.Errorf("%s: %q, want %q", cli, got, want)
		}
	}
	t0 := time.Unix(0, 0)
	run := runner.Part{State: runner.PartWorking, Start: t0, Pct: -1}
	for _, c := range []struct {
		p     func(p runner.Part) runner.Part
		final bool
		want  string
	}{
		{func(p runner.Part) runner.Part { return p }, false, "4:12"},
		{func(p runner.Part) runner.Part { p.Step, p.Count = "recomp", "1201/26190"; return p }, false, "recomp · [1201/26190] · 4:12"},
		{func(p runner.Part) runner.Part { p.Step, p.Pct, p.Count = "compile", 42, "4/10"; return p }, false, "compile · 42% · 4:12"},
		{func(p runner.Part) runner.Part { p.State, p.End = runner.PartDone, t0.Add(65*time.Second); return p }, false, "done · 1:05"},
		{func(p runner.Part) runner.Part {
			p.State, p.End = runner.PartFailed, t0.Add(3700*time.Second)
			return p
		}, false, "failed · 1:01:40"},
		{func(p runner.Part) runner.Part { p.State, p.End = runner.PartCanceled, t0.Add(time.Second); return p }, false, "stopped · 0:01"},
		{func(p runner.Part) runner.Part { p.State = runner.PartSkipped; return p }, true, "not needed"},
		{func(runner.Part) runner.Part { return runner.Part{} }, false, "waiting"},
		{func(runner.Part) runner.Part { return runner.Part{} }, true, "not run"},
	} {
		if got := partStatus(c.p(run), c.final, t0.Add(252*time.Second)); got != c.want {
			t.Errorf("%q, want %q", got, c.want)
		}
	}
	parts := []runner.Part{{State: runner.PartDone}, {State: runner.PartSkipped}, {State: runner.PartWorking}, {}, {}, {}}
	if got := overallText(parts, 0.42, 760*time.Second); got != "Part 3 of 6 · 42% · 12:40 in all" {
		t.Errorf("overall %q", got)
	}
	if got := overallText(parts[3:], 0, 5*time.Second); got != "0:05 in all" {
		t.Errorf("before any part: %q", got)
	}
	// Between two commands: the part that ran last.
	if got := overallText([]runner.Part{{State: runner.PartDone}, {}}, 0.5, 0); got != "Part 1 of 2 · 50% · 0:00 in all" {
		t.Errorf("between commands: %q", got)
	}
}

// rowTexts are a parts view's names and statuses, row by row.
func rowTexts(v *partsView) []string {
	var out []string
	for _, r := range v.rows {
		s := r.nameL.Text + "|" + r.status.Text() + "|" + r.toggle.Text
		if r.saidT != nil {
			s += "|" + doneSaid
		}
		out = append(out, s)
	}
	return out
}

// The checklist through a build: rows from the commands, the plan
// reshaping them, the running row's bar and log, a failure opening its
// row with the excerpt.
func TestPartsView(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	c := runner.NewChecklist([][]string{{"enhance", "hud"}, {"package", "windows", "--plain"}})
	v := newPartsView(func(i int) string { return strings.Join(c.Parts[i].Lines, "\n") })
	t0 := time.Unix(0, 0)
	at := func(s int) time.Time { return t0.Add(time.Duration(s) * time.Second) }
	show := func(s int) { v.show(c.Snapshot(partShown), false, at(s)) }
	show(0)
	if got := rowTexts(v); len(got) != 2 || got[0] != "Redraw the HUD and menus|waiting|Log" || got[1] != "Build the game|waiting|Log" {
		t.Fatalf("before the build: %q", got)
	}
	if !v.rows[0].toggle.Disabled() {
		t.Error("a part with no lines offers its log")
	}
	c.StartCommand(0, at(0))
	c.Feed("  upscale: 50% (10/20)", at(3))
	show(5)
	r := v.rows[0]
	if !strings.Contains(rowTexts(v)[0], "upscale · 50% · 0:05") || !r.bar.Visible() || r.bar.Value != 0.5 || r.bar.Pulse || r.act == nil {
		t.Fatalf("running: %q bar %v %v", rowTexts(v)[0], r.bar.Visible(), r.bar.Value)
	}
	test.Tap(r.toggle)
	if !r.detail.Visible() || r.log.Text != "  upscale: 50% (10/20)" || r.toggle.Text != partHideLog {
		t.Fatalf("the open log: %v %q", r.detail.Visible(), r.log.Text)
	}
	c.EndCommand(nil, false, at(60))
	c.StartCommand(1, at(60))
	c.Feed("$ ./game package windows --plain", at(60))
	show(60)
	test.Tap(v.rows[1].toggle)
	c.Feed("plan: setup (a), build (b), package (windows)", at(61))
	show(62)
	if len(v.rows) != 4 {
		t.Fatalf("after the plan: %d rows", len(v.rows))
	}
	if !v.rows[0].detail.Visible() {
		t.Error("a rebuild closed an open log")
	}
	// The plan's first stage is the placeholder's output: its log stays open.
	if !v.rows[1].detail.Visible() || v.open["/"+runner.PlanPart] || !strings.HasSuffix(v.rows[1].log.Text, "package (windows)") {
		t.Errorf("the placeholder's open log: %v %q", v.rows[1].detail.Visible(), v.rows[1].log.Text)
	}
	if got := rowTexts(v); got[0] != "Redraw the HUD and menus|done · 1:00|Hide log" || !strings.HasPrefix(got[1], "Get the tools|0:02|") {
		t.Errorf("after the plan: %q", got)
	}
	if !v.rows[1].bar.Pulse {
		t.Error("a part with no percentage does not pulse")
	}
	c.Feed("== [2/3] build ==", at(70))
	c.Feed("Traceback (most recent call last):", at(71))
	c.Feed("OSError: no space", at(72))
	show(75)
	b := v.rows[2]
	// A closed log is not refilled on every refresh; it fills on opening.
	if b.log.Text != "" || !b.stale || b.saidT != nil {
		t.Errorf("the closed log: %q, stale %v", b.log.Text, b.stale)
	}
	// The command fails with no line after the ones already shown.
	c.EndCommand(fmt.Errorf("exit status 1"), false, at(80))
	v.show(c.Snapshot(partShown), true, at(80))
	if !strings.HasSuffix(b.log.Text, "OSError: no space") {
		t.Errorf("the opened log: %q", b.log.Text)
	}
	got := rowTexts(v)
	if got[1] != "Get the tools|done · 0:10|Hide log" || !strings.HasPrefix(got[2], "Compile|failed · 0:10|Hide log|"+doneSaid) ||
		!strings.HasPrefix(got[3], "Make the installer|not run") {
		t.Errorf("after the failure: %q", got)
	}
	if !b.detail.Visible() || b.saidT == nil || b.saidT.Text != "Traceback (most recent call last):\nOSError: no space" || b.bar.Visible() {
		t.Errorf("the failed row: open %v", b.detail.Visible())
	}
	// Closed by the player, it stays closed.
	test.Tap(b.toggle)
	v.show(c.Snapshot(partShown), true, at(81))
	if b.detail.Visible() {
		t.Error("the failure reopened its row")
	}
	v.reset()
	if len(v.box.Objects) != 0 || len(v.open) != 0 {
		t.Error("reset kept rows")
	}
}

// At a phone-narrow width the names and the toggle keep their room; the
// status gives way.
func TestPartsNarrow(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	c := runner.NewChecklist([][]string{{"enhance", "hud"}, {"package", "windows"}})
	c.StartCommand(1, time.Unix(0, 0))
	c.Feed("plan: setup (a), generate (b), build (c), package (windows)", time.Unix(1, 0))
	c.Feed("== [2/4] generate ==", time.Unix(2, 0))
	c.Feed("  recomp ... 4:00, ~19 min left | [5500/26190] Translating fn_00012345", time.Unix(3, 0))
	v := newPartsView(func(int) string { return "" })
	v.show(c.Snapshot(partShown), false, time.Unix(300, 0))
	// The narrowest the rows go: the names, the glyphs and the toggle.
	width := v.box.MinSize().Width
	if width > 380 {
		t.Errorf("the checklist needs %v wide", width)
	}
	w := test.NewTempWindow(t, container.NewVScroll(v.box))
	w.SetPadded(false)
	w.Resize(fyne.NewSize(width, 400))
	r := v.rows[2]
	full := widget.NewLabel(r.status.Text()).MinSize().Width
	if r.status.Size().Width >= full {
		t.Errorf("the status was not cut: %v of %v", r.status.Size().Width, full)
	}
	if x := fyne.CurrentApp().Driver().AbsolutePositionForObject(r.toggle).X + r.toggle.Size().Width; x > width {
		t.Errorf("the toggle ends at %v, past %v", x, width)
	}
}

// The overall line moves with the clock while the build runs, and a tick
// from a build that ended leaves the final line alone.
func TestBuildTick(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.partsAfter = func(time.Duration, func()) {}
	w.parts = runner.NewChecklist([][]string{{"package", "windows"}})
	w.buildStart = time.Now().Add(-65 * time.Second)
	w.building, w.clockRun = true, 2
	w.parts.StartCommand(0, w.buildStart)
	w.showTick(2)
	if got := w.overall.Text(); got != "Part 1 of 1 · 1:05 in all" {
		t.Errorf("tick: %q", got)
	}
	w.overall.SetText("x")
	w.showTick(1)
	if w.overall.Text() != "x" {
		t.Error("an earlier build's tick moved the clock")
	}
	w.building, w.buildEnd = false, w.buildStart.Add(90*time.Second)
	w.parts.EndCommand(nil, false, w.buildEnd)
	w.flushParts()
	w.showTick(2)
	if got := w.overall.Text(); got != "Part 1 of 1 · 100% · 1:30 in all" {
		t.Errorf("after the end: %q", got)
	}
}

// Lines from the build's goroutine while the UI refreshes: run with -race.
func TestPartsConcurrent(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.partsAfter = func(time.Duration, func()) {}
	w.parts = runner.NewChecklist([][]string{{"package", "windows"}})
	w.building = true
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.partsDo(func(c *runner.Checklist) { c.StartCommand(0, time.Now()) })
		w.partsDo(func(c *runner.Checklist) { c.Feed("plan: setup (a), build (b)", time.Now()) })
		for i := range 2000 {
			w.partsDo(func(c *runner.Checklist) { c.Feed(fmt.Sprint("  compile: ", i/20, "%"), time.Now()) })
		}
	}()
	for range 50 {
		w.flushParts()
	}
	wg.Wait()
	w.flushParts()
	if len(w.buildParts.rows) != 2 {
		t.Fatalf("%d rows", len(w.buildParts.rows))
	}
}

// The stop dialog: the sentence, the way back and the stop at the right,
// the stop in red with no default, and never wider than the window.
func TestStopDialog(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.win.Resize(fyne.NewSize(780, 580))
	d, body := w.stopDialog()
	var row *fyne.Container
	var all []string
	var buttons []*widget.Button
	walkTree(body, func(o fyne.CanvasObject) {
		switch v := o.(type) {
		case *widget.Label:
			all = append(all, v.Text)
		case *widget.Button:
			buttons = append(buttons, v)
		case *fyne.Container:
			if len(v.Objects) == 3 {
				row = v
			}
		}
	})
	if !slices.Contains(all, stopText) || stopTitle != "Are you sure?" {
		t.Errorf("texts %q", all)
	}
	if len(buttons) != 2 || buttons[0].Text != "Go back" || buttons[1].Text != "Cancel Build" {
		t.Fatalf("buttons %v", buttons)
	}
	back, stop := buttons[0], buttons[1]
	if back.Icon.Name() != theme.NavigateBackIcon().Name() || stop.Icon.Name() != theme.MediaStopIcon().Name() {
		t.Error("icons")
	}
	if stop.Importance != widget.DangerImportance || back.Importance == widget.HighImportance {
		t.Error("the stop is not red, or the way back is the default")
	}
	if _, spacer := row.Objects[0].(layout.SpacerObject); !spacer {
		t.Error("the buttons are not right-aligned")
	}
	if stopWidth(780) != 420 || stopWidth(300) != 252 || stopWidth(20) != 0 {
		t.Error("width")
	}
	d.Show()
	// The dialog follows the window's width while it shows.
	wide := body.Size().Width
	w.win.Resize(fyne.NewSize(300, 580))
	narrow := body.Size().Width
	if narrow >= wide || narrow > stopWidth(300) {
		t.Errorf("the dialog's content is %v wide in a 300 window, %v in 780", narrow, wide)
	}
	test.Tap(back)
	if w.stopD != nil {
		t.Error("a closed dialog still follows the window")
	}
	w.win.Resize(fyne.NewSize(780, 580))
	if w.canceled {
		t.Fatal("Go back stopped the build")
	}
	d, body = w.stopDialog()
	d.Show()
	buttons = nil
	walkTree(body, func(o fyne.CanvasObject) {
		if b, ok := o.(*widget.Button); ok {
			buttons = append(buttons, b)
		}
	})
	test.Tap(buttons[1])
	if !w.canceled {
		t.Fatal("Cancel Build did not stop the build")
	}
}

// The glyph after a hint link: the link's colour, the same address on a
// tap, no focus of its own.
func TestHintLinkGlyph(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	var opened []string
	w.openLink = func(u string) { opened = append(opened, u) }
	l := &gamecfg.Link{Text: "Get the font", URL: "https://example.org/font"}
	row, ok := w.hintLink(l).(*fyne.Container)
	if !ok || len(row.Objects) != 2 {
		t.Fatalf("link row: %#v", row)
	}
	link := row.Objects[0].(*widget.Hyperlink)
	glyph := row.Objects[1].(*tapRow)
	if _, focus := any(glyph).(fyne.Focusable); focus {
		t.Error("the glyph takes focus")
	}
	if _, focus := any(link).(fyne.Focusable); !focus {
		t.Error("the link takes no focus")
	}
	img := glyph.content.(*canvas.Image)
	if !strings.HasPrefix(img.Resource.Name(), string(theme.ColorNameHyperlink)) || img.MinSize().Height != theme.TextSize() {
		t.Errorf("glyph %q %v", img.Resource.Name(), img.MinSize())
	}
	test.Tap(glyph)
	link.OnTapped()
	if !slices.Equal(opened, []string{l.URL, l.URL}) {
		t.Errorf("opened %q", opened)
	}
	if w.hintLink(nil) != nil {
		t.Error("no link, a row")
	}
}

// The font lines: shown until the CLI says the font is installed, shown
// again when it cannot tell, and an older answer never overrides a newer.
func TestFontCheck(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	if len(w.fonts) != 1 {
		t.Fatalf("%d font lines", len(w.fonts))
	}
	l := w.fonts[0]
	var jobs []func()
	w.async = func(f func()) { jobs = append(jobs, f) }
	w.fillOptions() // no checks yet: cannot tell
	if !l.need.Visible() || l.found.Visible() || len(jobs) != 0 {
		t.Fatal("before the checks")
	}
	w.env = &Env{}
	answers := []*runner.Font{
		{Font: "PT Sans Narrow Bold", Found: false},
		{Font: "PT Sans Narrow Bold", Found: true, Path: "/fonts/PTSansNarrow-Bold.ttf"},
	}
	var asked int
	w.fontCheck = func(runner.Runner) (*runner.Font, error) {
		f := answers[asked]
		asked++
		return f, nil
	}
	w.fillOptions()
	w.fillOptions()
	if len(jobs) != 2 {
		t.Fatalf("%d checks", len(jobs))
	}
	// The newer check answers first: found. The older one's "missing"
	// comes late and is dropped.
	answers[0], answers[1] = answers[1], answers[0]
	jobs[1]()
	jobs[0]()
	if l.need.Visible() || !l.found.Visible() || l.found.Text() != "PT Sans Narrow Bold is installed." {
		t.Fatalf("found: need %v, found %v %q", l.need.Visible(), l.found.Visible(), l.found.Text())
	}
	w.fontCheck = func(runner.Runner) (*runner.Font, error) { return nil, fmt.Errorf("exit status 2") }
	w.fillOptions()
	jobs[2]()
	if !l.need.Visible() || l.found.Visible() {
		t.Fatal("a failed check hid the link")
	}
	var link *widget.Hyperlink
	walkTree(l.need, func(o fyne.CanvasObject) {
		if h, ok := o.(*widget.Hyperlink); ok {
			link = h
		}
	})
	if link == nil {
		t.Error("the font lines have no link")
	}
}

// A whole build through a stand-in for the CLI: each command's lines go
// to its own part, the plan replaces its placeholder, the bundle ends it.
func TestBuildParts(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in is a shell script")
	}
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.partsAfter = func(time.Duration, func()) {}
	w.buildLog.after = func(time.Duration, func()) {}
	dir := t.TempDir()
	uv := filepath.Join(dir, "uv")
	script := `#!/bin/sh
for a; do last=$a; case $a in import|enhance|package) cmd=$a;; esac; done
case $cmd in
import) echo "copying files";;
enhance) echo "  upscale: 100% (20/20)";;
package)
  echo "plan: setup (toolkit), build (build-win), package (windows)"
  echo "== [1/3] setup =="
  echo "== [2/3] build =="
  echo "  compile: 40% (4/10)"
  echo "== [3/3] package =="
  echo "bundle: /out/Game-1-windows";;
esac
`
	if err := os.WriteFile(uv, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	w.env = &Env{Runner: runner.Runner{UV: uv, Game: dir, Slug: "game"}}
	w.st.Dump = "/d/game.iso"
	if err := w.st.Set("hud_pack", "true"); err != nil {
		t.Fatal(err)
	}
	steps, targets := w.st.StepTargets("", t.TempDir())
	w.prog = &runner.Progress{}
	w.parts = runner.NewChecklist(steps)
	w.building = true
	if _, err := w.runSteps("", steps, targets); err != nil {
		t.Fatal(err)
	}
	w.building, w.buildEnd = false, time.Now()
	w.flushParts()
	var got []string
	for _, p := range w.parts.Parts {
		got = append(got, fmt.Sprintf("%s:%d:%d", p.Name, p.State, len(p.Lines)))
	}
	// import and enhance hud: the command line and one line; the plan's
	// first stage took the command line, the plan and its own stage line.
	want := []string{"import:2:2", "enhance hud:2:2", "setup:2:3", "build:2:2", "package:2:2"}
	if !slices.Equal(got, want) {
		t.Errorf("parts %q, want %q", got, want)
	}
	if w.prog.Bundle != "/out/Game-1-windows" || !strings.HasPrefix(w.overall.Text(), "Part 5 of 5 · 100% · ") {
		t.Errorf("bundle %q, overall %q", w.prog.Bundle, w.overall.Text())
	}
	if len(w.buildParts.rows) != 5 || w.buildParts.rows[2].nameL.Text != "Get the tools" {
		t.Errorf("rows %q", rowTexts(w.buildParts))
	}
}

// Several platforms in one run: a platform that fails in its own build
// lets the next one run; one that fails in shared work stops the rest.
func TestBuildPlatforms(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in is a shell script")
	}
	isolate(t)
	dir := t.TempDir()
	uv := filepath.Join(dir, "uv")
	script := `#!/bin/sh
clean=
for a; do [ "$prev" = package ] && tgt=$a; [ "$a" = --clean ] && clean=+clean; prev=$a; done
echo "$tgt$clean" >> "$0.ran"
stage=$(cat "$0.fail-$tgt" 2>/dev/null)
[ "$stage" = pre ] && { echo "error: no toolkit"; exit 2; }
echo "plan: setup (toolkit), generate (gen), build (build), package ($tgt)"
echo "full log: /logs/$tgt/run.log"
n=0
for s in setup generate build package; do
  n=$((n+1)); echo "== [$n/4] $s =="
  [ "$s" = "$stage" ] && { echo "error: $tgt broke"; exit 2; }
  [ "$stage" = disk ] && [ "$s" = build ] && { echo "xbr: package: refused: disk"; exit 2; }
done
echo "bundle: /out/Game-1-$tgt"
`
	if err := os.WriteFile(uv, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	fresh := func() *Wizard {
		a := test.NewApp()
		t.Cleanup(a.Quit)
		w := New(a, sampleOptions(t))
		w.partsAfter = func(time.Duration, func()) {}
		w.buildLog.after = func(time.Duration, func()) {}
		return w
	}
	runOn := func(w *Wizard, fail map[string]string) (*Wizard, []targetResult, error, string) {
		os.Remove(uv + ".ran")
		for _, tg := range []string{"windows", "steamos", "macos"} {
			os.Remove(uv + ".fail-" + tg)
		}
		for tg, st := range fail {
			if err := os.WriteFile(uv+".fail-"+tg, []byte(st), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		w.env = &Env{Runner: runner.Runner{UV: uv, Game: dir, Slug: "game"}}
		w.st.SetTargets([]string{"windows", "steamos", "macos"})
		steps, targets := w.st.StepTargets("", t.TempDir())
		w.prog = &runner.Progress{}
		w.parts = runner.NewGroupedChecklist(steps, targets)
		w.building = true
		res, err := w.runSteps("", steps, targets)
		w.building, w.buildEnd = false, time.Now()
		w.flushParts()
		ran, _ := os.ReadFile(uv + ".ran")
		return w, res, err, strings.Join(strings.Fields(string(ran)), " ")
	}
	run := func(fail map[string]string) (*Wizard, []targetResult, error, string) {
		return runOn(fresh(), fail)
	}
	ends := func(res []targetResult) []string {
		var out []string
		for _, r := range res {
			out = append(out, platformEnd(r))
		}
		return out
	}

	// The Steam Deck's own build fails: Windows and the Mac still get theirs.
	w, res, err, ran := run(map[string]string{"steamos": "build"})
	var f *buildFailure
	if !errors.As(err, &f) || f.Step != "build" || ran != "windows steamos macos" {
		t.Fatalf("err %v, ran %q", err, ran)
	}
	want := []string{"Windows: ready", "Steam Deck or Linux: stopped at " + "build", "macOS: ready"}
	if got := ends(res); !slices.Equal(got, want) || res[2].Bundle != "/out/Game-1-macos" {
		t.Errorf("ends %q, want %q", got, want)
	}
	w.buildErr, w.results = err, res
	w.fillDone()
	got := shown(w.doneBox)
	for _, s := range []string{doneSome, "The Windows build is ready.", "/out/Game-1-windows", "/out/Game-1-macos", fmt.Sprintf(doneStoppedAt, "build")} {
		if !strings.Contains(got, s) {
			t.Errorf("the Done page lacks %q:\n%s", s, got)
		}
	}
	if w.pages[pDone].title != titleStopped {
		t.Errorf("title %q", w.pages[pDone].title)
	}

	// Windows fails making the code every platform shares: the rest stop.
	_, res, err, ran = run(map[string]string{"windows": "generate"})
	if err == nil || ran != "windows" {
		t.Fatalf("err %v, ran %q", err, ran)
	}
	want = []string{"Windows: stopped at " + "generate", fmt.Sprintf(donePlatformNot, "Steam Deck or Linux"), fmt.Sprintf(donePlatformNot, "macOS")}
	if got := ends(res); !slices.Equal(got, want) {
		t.Errorf("ends %q, want %q", got, want)
	}

	// Stopped before the plan (the toolkit fetch, a refused config): every
	// platform would stop there the same way.
	_, res, err, ran = run(map[string]string{"windows": "pre"})
	if err == nil || ran != "windows" || len(res) != 3 || !errors.Is(res[2].Err, errNotBuilt) {
		t.Fatalf("pre-plan: err %v, ran %q, results %v", err, ran, res)
	}

	// Short of disk space, even mid-run where a compile failure would
	// skip only its own tree: the rest would only hit the same wall.
	w, res, err, ran = run(map[string]string{"windows": "disk"})
	if err == nil || ran != "windows" || len(res) != 3 || !errors.Is(res[1].Err, errNotBuilt) {
		t.Fatalf("disk: err %v, ran %q, results %v", err, ran, res)
	}

	// Windows fails to compile: the Steam Deck shares its build tree and
	// is skipped, the Mac has its own and still builds. Done points at
	// the failed platform's log, not the Mac's.
	w, res, err, ran = run(map[string]string{"windows": "build"})
	if err == nil || ran != "windows macos" {
		t.Fatalf("same tree: err %v, ran %q", err, ran)
	}
	want = []string{"Windows: stopped at " + "build", fmt.Sprintf(donePlatformNot, "Steam Deck or Linux"), "macOS: ready"}
	if got := ends(res); !slices.Equal(got, want) {
		t.Errorf("ends %q, want %q", got, want)
	}
	w.buildErr, w.results = err, res
	w.fillDone()
	if got := shown(w.doneBox); !strings.Contains(got, fmt.Sprintf(doneLogs, "/logs/windows")) {
		t.Errorf("the Done page lacks the Windows log folder:\n%s", got)
	}

	// Rebuild everything cleans once a session: the first platform's run
	// cleans; one that stopped before the CLI cleaned leaves it to the
	// next run; a Retry after a clean does not clean again.
	w = fresh()
	w.st.Clean = true
	_, _, _, ran = runOn(w, map[string]string{"windows": "pre"})
	if ran != "windows+clean" || w.cleaned {
		t.Errorf("stopped before the clean: ran %q, cleaned %v", ran, w.cleaned)
	}
	_, _, _, ran = runOn(w, map[string]string{"steamos": "build"})
	if ran != "windows+clean steamos macos" || !w.cleaned {
		t.Errorf("first clean: ran %q, cleaned %v", ran, w.cleaned)
	}
	if _, _, _, ran = runOn(w, nil); ran != "windows steamos macos" {
		t.Errorf("retry: ran %q", ran)
	}

	// All made: Done, a ready line each.
	w, res, err, _ = run(nil)
	if err != nil || len(res) != 3 {
		t.Fatalf("err %v, results %v", err, res)
	}
	w.buildErr, w.results = err, res
	w.fillDone()
	if got := shown(w.doneBox); strings.Contains(got, doneSome) || !strings.Contains(got, "The macOS build is ready.") || w.pages[pDone].title != titleDone {
		t.Errorf("Done page:\n%s", got)
	}
}
