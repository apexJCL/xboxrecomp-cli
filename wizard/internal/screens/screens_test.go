package screens

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"image"
	"image/color"
	"image/png"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/driver/software"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"

	"wizard"
	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/hostinfo"
	"wizard/internal/model"
	"wizard/internal/runner"
	"wizard/internal/update"
)

func sampleOptions(t *testing.T) Options {
	t.Helper()
	b, err := os.ReadFile("../gamecfg/testdata/blinx2.json")
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := gamecfg.Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	pins, err := fetch.LoadPins(wizard.PinsJSON)
	if err != nil {
		t.Fatal(err)
	}
	return Options{Cfg: cfg, Pins: pins, Version: "test", Root: t.TempDir(), NoDialog: true, NoUpdateCheck: true}
}

func isolate(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())
}

func loadDoctor(t *testing.T, name string) *runner.Doctor {
	t.Helper()
	b, err := os.ReadFile("../runner/testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	d, err := runner.ParseDoctor(b)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// Every page builds and renders on the sample config, headless.
func TestPages(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.doctor = loadDoctor(t, "doctor.json")
	w.env = &Env{Root: w.root, Game: w.root}
	w.showChecks(nil)
	w.fillTargets()
	w.dumpEntry.SetText("/d/b2.iso")
	w.st.Dump = "/d/b2.iso"
	if err := w.st.Set("scale", "2"); err != nil {
		t.Fatal(err)
	}
	w.fillSummary()
	if !strings.Contains(w.summaryTech.Text, "--seed-config") || !strings.Contains(w.summaryTech.Text, "scale = 2") {
		t.Fatalf("summary details:\n%s", w.summaryTech.Text)
	}
	w.prog = &runner.Progress{Failed: "build", Log: "/r/build-logs/x.log"}
	w.fillDone()
	for i := range w.pages {
		w.render(i)
		if w.body.Objects[0] == nil {
			t.Fatalf("page %d is empty", i)
		}
	}
	w.prog = &runner.Progress{Bundle: "/r/dist/BLiNX2-1-steamos.tar"}
	w.fillDone()
}

// The frame: the step text and bar follow the page, the footer is the
// version alone and the same object on every page; the pins are in the
// Summary's details.
func TestFrame(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	w := New(a, opt)
	w.render(pChecks)
	if w.step.Text() != "Step 2 of 8" {
		t.Fatalf("step %q", w.step.Text())
	}
	if w.stepBar.Value != 2.0/8 || w.stepBar.filled() != 2 {
		t.Fatalf("bar %v (%d filled)", w.stepBar.Value, w.stepBar.filled())
	}
	if w.title.Text != titleChecks {
		t.Fatalf("title %q", w.title.Text)
	}
	if !strings.Contains(w.footer.Text(), opt.Version) || strings.Contains(w.footer.Text(), opt.Cfg.Release.Tag) {
		t.Fatalf("footer %q", w.footer.Text())
	}
	content := w.win.Content()
	for i := range w.pages {
		w.render(i)
		if w.win.Content() != content || !w.footer.Visible() {
			t.Fatalf("page %d: the frame changed", i)
		}
	}
	w.fillSummary()
	for _, s := range []string{opt.Version, opt.Cfg.Release.Tag, opt.Cfg.CLI.Tag, opt.Cfg.Toolkit.Tag} {
		if !strings.Contains(w.summaryTech.Text, s) {
			t.Fatalf("details lack %q:\n%s", s, w.summaryTech.Text)
		}
	}
}

// renderSized draws obj on a canvas of the given size with the built-in
// light theme.
func renderSized(obj fyne.CanvasObject, size fyne.Size) image.Image {
	c := software.NewCanvas()
	c.SetPadded(false)
	c.SetContent(obj)
	c.Resize(size)
	//lint:ignore SA1019 a fixed variant is what an image needs
	return software.RenderCanvas(c, theme.LightTheme())
}

func near(a, b color.Color) bool {
	r1, g1, b1, _ := a.RGBA()
	r2, g2, b2, _ := b.RGBA()
	d := func(x, y uint32) bool { return x>>8 < y>>8+8 && y>>8 < x>>8+8 }
	return d(r1, r2) && d(g1, g2) && d(b1, b2)
}

func TestThinBar(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	//lint:ignore SA1019 a fixed variant is what an image needs
	primary := theme.LightTheme().Color(theme.ColorNamePrimary, theme.VariantLight)
	// count is the primary pixels across the middle row and the runs
	// they make (one per filled segment).
	count := func(img image.Image) (px, runs int) {
		y := img.Bounds().Dy() / 2
		in := false
		for x := img.Bounds().Min.X; x < img.Bounds().Max.X; x++ {
			p := near(img.At(x, y), primary)
			if p {
				px++
				if !in {
					runs++
				}
			}
			in = p
		}
		return px, runs
	}
	for _, v := range []float64{0, 0.5, 1} {
		b := newThinBar(0)
		b.SetValue(v)
		img := renderSized(b, fyne.NewSize(200, thinBarHeight))
		if got, _ := count(img); got < int(200*v)-4 || got > int(200*v)+4 {
			t.Errorf("continuous at %v: %d primary pixels, want about %d", v, got, int(200*v))
		}
		s := newThinBar(8)
		s.SetValue(v)
		img = renderSized(s, fyne.NewSize(200, thinBarHeight))
		if _, runs := count(img); runs != int(8*v) || s.filled() != runs {
			t.Errorf("segmented at %v: %d segments drawn, %d filled", v, runs, s.filled())
		}
	}
	if newThinBar(0).MinSize().Height != thinBarHeight {
		t.Fatal("the bar is not thin")
	}
}

func TestPrepareStepTotal(t *testing.T) {
	cfg := sampleOptions(t).Cfg
	if n := prepareTotal(cfg, "macos"); n != 3 {
		t.Fatalf("no fetch entries: %d", n)
	}
	cfg.Fetch = []gamecfg.Fetch{{Name: "a", Hosts: []string{"macos"}}, {Name: "b"}}
	if n := prepareTotal(cfg, "macos"); n != 5 {
		t.Fatalf("macos: %d", n)
	}
	if n := prepareTotal(cfg, "windows"); n != 4 {
		t.Fatalf("windows: %d", n)
	}
}

func rowByName(rows []checkRow, name string) checkRow {
	for _, r := range rows {
		if r.Name == name {
			return r
		}
	}
	return checkRow{}
}

func TestCheckRows(t *testing.T) {
	cfg := sampleOptions(t).Cfg
	env := &Env{Game: "/r/src", Git: "/usr/bin/git"}

	rows := checkRows(cfg, env, loadDoctor(t, "doctor.json"), nil, true)
	for _, tg := range cfg.Targets {
		if r := rowByName(rows, targetName(tg)); r.State != stateOK || r.hasDetail() {
			t.Errorf("%s: %+v", tg, r)
		}
	}
	tools := rowByName(rows, rowTools)
	if tools.State != stateLater || !strings.Contains(tools.Detail, "cmake, ninja, Python for the tools, toolkit, llvm-mingw.") {
		t.Errorf("build tools: %+v", tools)
	}
	// Only external dependencies: the game files and the host's own
	// lines are in no row (the log has them).
	var names []string
	for _, r := range rows {
		names = append(names, r.Name)
		for _, k := range []string{"game files:", "host:", "gen/:", "game env:", "DEVELOPER_DIR:", "next:"} {
			if slices.ContainsFunc(r.Raw, func(l string) bool { return strings.HasPrefix(l, k) }) {
				t.Errorf("%s shows the %q line", r.Name, k)
			}
		}
	}
	if want := append([]string{rowTools, rowSource}, targetName("windows"), targetName("steamos"), targetName("macos")); !slices.Equal(names[:len(want)], want) {
		t.Errorf("rows %q", names)
	}
	if r := rowByName(rows, rowSource); r.State != stateWarn || r.Status != statusNotPinned {
		t.Errorf("game source (a developer's cli): %+v", r)
	}
	for _, tg := range cfg.Targets {
		if r := rowByName(rows, targetName(tg)); r.Target != tg {
			t.Errorf("%s row has target %q", tg, r.Target)
		}
	}
	// No doctor line lands in two rows.
	seen := map[string]string{}
	for _, r := range rows {
		for _, l := range r.Raw {
			if prev, dup := seen[l]; dup {
				t.Errorf("%q in %s and %s", l, prev, r.Name)
			}
			seen[l] = r.Name
		}
	}

	blocked := checkRows(cfg, env, loadDoctor(t, "doctor-blocked.json"), nil, true)
	for _, tg := range []string{"macos", "windows"} {
		r := rowByName(blocked, targetName(tg))
		_, why := loadDoctor(t, "doctor-blocked.json").Target(tg)
		if r.State != stateWarn || len(r.Actions) != 1 || r.Actions[0].Kind != "brew" || r.Detail != plainBlocker(why[0], true) {
			t.Errorf("%s: %+v", tg, r)
		}
	}
	if r := rowByName(blocked, rowTools); strings.Contains(r.Detail, "makensis") {
		t.Errorf("makensis is a blocker here, not a fetched tool: %q", r.Detail)
	}

	busy := checkRows(cfg, nil, nil, nil, true)
	if len(busy) != 2+len(cfg.Targets) {
		t.Errorf("%d busy rows", len(busy))
	}
	for _, r := range busy {
		if r.State != stateBusy || r.Status != statusChecking {
			t.Errorf("busy: %+v", r)
		}
	}

	need := &NeedError{Msg: gitMissingMac, Action: runner.Action{Kind: "clt", Command: "xcode-select --install"}}
	failed := checkRows(cfg, nil, nil, fmt.Errorf("checks: %w", need), true)
	if len(failed) != 1 || failed[0].State != stateFail || !failed[0].Open || failed[0].Detail != gitMissingMac ||
		len(failed[0].Actions) != 1 || failed[0].Actions[0].Kind != "clt" {
		t.Errorf("failed: %+v", failed)
	}

	dev := checkRows(cfg, &Env{Game: "/dev/game", Dev: true}, loadDoctor(t, "doctor.json"), nil, true)
	if r := rowByName(dev, rowSource); r.Status != statusDevMode || !strings.Contains(r.Detail, "/dev/game") {
		t.Errorf("developer mode: %+v", r)
	}
}

func TestPlainBlocker(t *testing.T) {
	cases := []struct {
		reason string
		brew   bool
		want   string
	}{
		{"Command Line Tools: xcode-select --install", true, blockCLT},
		{"Homebrew: brew install sdl3 libepoxy", true, fmt.Sprintf(blockBrewHave, "sdl3, libepoxy")},
		{"Homebrew: brew install sdl3 libepoxy", false, blockBrewNone},
		{"makensis: brew install makensis", true, blockNSIS + " " + blockNSISBrew},
		{"makensis: brew install makensis", false, blockNSIS + " " + blockNSISNoBrew},
		{"Homebrew: brew install", true, "Homebrew: brew install"},
		{"makensis: sudo apt install nsis", false, blockNSIS + " " + fmt.Sprintf(blockNSISSudo, "sudo apt install nsis")},
		{"makensis: in a toolbox or distrobox: distrobox create -n x", false,
			blockNSIS + " " + fmt.Sprintf(blockNSISOther, "in a toolbox or distrobox: distrobox create -n x")},
		{"the macos target needs a macOS host (Apple clang, codesign, hdiutil)", false, blockMacOnly},
		{"not a target of this game", false, "not a target of this game"},
	}
	for _, c := range cases {
		if got := plainBlocker(c.reason, c.brew); got != c.want {
			t.Errorf("%q (brew %v):\n got %q\nwant %q", c.reason, c.brew, got, c.want)
		}
	}
	// The disk-space sentence and the Welcome page state one number.
	if got := plainPrepare(&SpaceError{Free: 2 << 30, Need: hostinfo.NeedBytes}); got != fmt.Sprintf(prepareNoSpace, 2.0, 15.0) {
		t.Errorf("space: %q", got)
	}
	if !strings.Contains(fmt.Sprintf(installFolderIs, hostinfo.NeedBytes>>30), "About 15 GB") {
		t.Error("the Welcome page's size is not NeedBytes")
	}
	if got := plainPrepare(&OverlapError{Root: "/a", Data: "/a/b"}); !strings.Contains(got, "/a/b") {
		t.Errorf("overlap: %q", got)
	}
	if got := plainPrepare(errors.New("as it is")); got != "as it is" {
		t.Errorf("other: %q", got)
	}
}

func TestChecksView(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.env = &Env{Root: w.root, Game: w.root}
	w.doctor = loadDoctor(t, "doctor-blocked.json")
	w.showChecks(nil)
	w.render(pChecks)
	mac, ok := w.rowParts[targetName("macos")]
	if !ok || mac.detail.Visible() {
		t.Fatal("the blocked row has no toggle, or starts open")
	}
	test.Tap(mac.line)
	if !mac.detail.Visible() {
		t.Fatal("tapping the row did not open the detail")
	}
	if mac.toggle.Text != hideDetailsText {
		t.Fatalf("the open toggle reads %q", mac.toggle.Text)
	}
	test.Tap(mac.toggle)
	if mac.detail.Visible() {
		t.Fatal("the Details button did not close the detail")
	}
	if mac.toggle.Text != detailsText {
		t.Fatalf("the toggle reads %q", mac.toggle.Text)
	}
	if _, ok := any(mac.toggle).(fyne.Focusable); !ok {
		t.Fatal("the Details button cannot take keyboard focus")
	}
	test.Tap(mac.toggle)
	found := false
	var walk func(o fyne.CanvasObject)
	walk = func(o fyne.CanvasObject) {
		switch v := o.(type) {
		case *fyne.Container:
			for _, c := range v.Objects {
				walk(c)
			}
		case interface{ Tapped(*fyne.PointEvent) }:
			found = true
		}
	}
	walk(mac.detail)
	if !found {
		t.Fatal("the open detail has no fix button")
	}

	w.doctor = loadDoctor(t, "doctor.json")
	w.showChecks(nil)
	for _, tg := range w.opt.Cfg.Targets {
		if _, ok := w.rowParts[targetName(tg)]; ok {
			t.Errorf("%s: a ready target has a toggle", tg)
		}
	}
}

func TestLogPane(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	p := newLogPane(nil)
	var due []func()
	p.after = func(_ time.Duration, f func()) { due = append(due, f) }
	run := func() {
		if len(due) != 1 {
			t.Fatalf("%d flushes armed, want 1", len(due))
		}
		due[0]()
		due = nil
	}
	for _, s := range []string{"one", "two", "three"} {
		p.Append(s)
	}
	run()
	if p.last.Text() != "three" || p.expanded.Visible() {
		t.Fatalf("collapsed: %q", p.last.Text())
	}
	test.Tap(p.show)
	if !p.expanded.Visible() || p.collapsed.Visible() || p.text.Text != "one\ntwo\nthree" || !p.text.Selectable {
		t.Fatalf("expanded: %q", p.text.Text)
	}
	test.Tap(p.copyBtn)
	if got := a.Clipboard().Content(); got != "one\ntwo\nthree" {
		t.Fatalf("copied %q", got)
	}
	test.Tap(p.hide)
	if p.expanded.Visible() {
		t.Fatal("hide did not collapse")
	}
	p.Clear()
	for i := range 600 {
		p.Append(strconv.Itoa(i))
	}
	run()
	if n := len(strings.Split(p.text.Text, "\n")); n != shownLines {
		t.Fatalf("%d lines shown", n)
	}
	test.Tap(p.copyBtn)
	if n := len(strings.Split(a.Clipboard().Content(), "\n")); n != 600 {
		t.Fatalf("%d lines copied", n)
	}
	if p.last.Text() != "599" {
		t.Fatalf("last %q", p.last.Text())
	}
}

// copyStrings are every string literal in copy.go: the player's text.
func copyStrings(t *testing.T) []string {
	t.Helper()
	f, err := parser.ParseFile(token.NewFileSet(), "copy.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	ast.Inspect(f, func(n ast.Node) bool {
		// The technical details are for whoever helps the player, in the
		// CLI's words on purpose.
		if v, ok := n.(*ast.ValueSpec); ok && strings.HasPrefix(v.Names[0].Name, "tech") {
			return false
		}
		if l, ok := n.(*ast.BasicLit); ok && l.Kind == token.STRING {
			s, err := strconv.Unquote(l.Value)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, s)
		}
		return true
	})
	return out
}

func TestCopy(t *testing.T) {
	game := sampleOptions(t).Cfg.Game.Name
	for _, s := range []string{welcomeHeading(game), welcomeIntro(game, nil), noGameText(game), dumpIntro(game), bundleRule(game)} {
		if !strings.Contains(s, game) {
			t.Errorf("does not name the game: %q", s)
		}
	}
	// The CLI's words, which a player never needs.
	banned := regexp.MustCompile(`(?i)\b(uv|venv|xbr|cmake|ninja|llvm-mingw|toolkit|clone|repo)\b`)
	strs := copyStrings(t)
	if len(strs) < 50 {
		t.Fatalf("only %d strings in copy.go", len(strs))
	}
	for _, s := range strs {
		if m := banned.FindString(s); m != "" {
			t.Errorf("%q in %q", m, s)
		}
	}
}

// The welcome page names only the config's platforms and promises no
// build time: both are the game's.
func TestWelcomeIntro(t *testing.T) {
	one := welcomeIntro("G", []string{"windows"})
	if !strings.Contains(one, "a Windows PC.") || strings.Contains(one, "Mac") || strings.Contains(one, "Steam Deck") {
		t.Errorf("one target: %q", one)
	}
	all := welcomeIntro("G", []string{"windows", "macos", "steamos"})
	if !strings.Contains(all, "a Windows PC, a Mac, or a Linux PC such as the Steam Deck.") {
		t.Errorf("three targets: %q", all)
	}
	// cat lists steamos before macos; the Steam Deck still goes last.
	if cat := welcomeIntro("G", []string{"windows", "steamos", "macos"}); cat != all {
		t.Errorf("target order changed the sentence: %q", cat)
	}
	two := welcomeIntro("G", []string{"steamos", "windows"})
	if !strings.Contains(two, "a Windows PC or a Linux PC such as the Steam Deck.") {
		t.Errorf("two targets: %q", two)
	}
	hours := regexp.MustCompile(`(?i)\b(hour|hours|minutes?)\b`)
	if m := hours.FindString(all); m != "" {
		t.Errorf("promises a time: %q", all)
	}
}

// Nothing goes to the internet before the player leaves the Welcome page,
// and then only with the box ticked, once per run.
func TestUpdateCheckWaits(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	opt.NoUpdateCheck = false
	opt.Cfg.Release.Dev = false
	w := New(a, opt)
	var asked atomic.Int32
	w.latest = func() (*update.Release, string) { asked.Add(1); return nil, "stub" }
	// A finished doctor and env keep the Checks page from running Prepare.
	w.doctor = loadDoctor(t, "doctor.json")
	w.env = &Env{Root: w.root, Game: w.root}
	w.render(pWelcome)
	time.Sleep(50 * time.Millisecond)
	if n := asked.Load(); n != 0 {
		t.Fatalf("%d checks before leaving Welcome", n)
	}
	w.settings.NoUpdateCheck = true
	test.Tap(w.next)
	time.Sleep(50 * time.Millisecond)
	if n := asked.Load(); n != 0 || w.cur != pChecks {
		t.Fatalf("%d checks with the box unticked (page %d)", n, w.cur)
	}
	test.Tap(w.back)
	w.settings.NoUpdateCheck = false
	for range 2 {
		if w.cur != pWelcome {
			t.Fatalf("page %d, want Welcome", w.cur)
		}
		test.Tap(w.next)
		test.Tap(w.back)
	}
	for i := 0; i < 100 && asked.Load() == 0; i++ {
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)
	if n := asked.Load(); n != 1 {
		t.Fatalf("%d checks after leaving Welcome twice, want 1", n)
	}
}

// The Homebrew installer stays as pinned; Terminal opens a wrapper beside
// it that turns analytics off, and runBrew's environment does the same.
func TestHomebrewWrapper(t *testing.T) {
	script := []byte("#!/bin/bash\necho installer\n")
	srv := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) { rw.Write(script) }))
	defer srv.Close()
	sum := sha256.Sum256(script)
	pins := &fetch.Pins{HomebrewInstall: fetch.Asset{URL: srv.URL + "/install.sh", SHA256: hex.EncodeToString(sum[:]), Size: int64(len(script))}}
	root := t.TempDir()
	// A leftover wrapper without the exec bit must get it back.
	if err := os.MkdirAll(filepath.Join(root, "tools"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "tools", "homebrew-install-run.sh"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	wrap, err := HomebrewScript(context.Background(), pins, root)
	if err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(wrap)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "export HOMEBREW_NO_ANALYTICS=1\n") || !strings.Contains(string(body), `"$(dirname "$0")/homebrew-install.sh"`) {
		t.Errorf("wrapper:\n%s", body)
	}
	for _, f := range []string{wrap, filepath.Join(root, "tools", "homebrew-install.sh")} {
		if fi, err := os.Stat(f); err != nil || fi.Mode().Perm() != 0o755 {
			t.Errorf("%s: %v %v, want 0755", f, fi.Mode(), err)
		}
	}
	if got, _ := os.ReadFile(filepath.Join(root, "tools", "homebrew-install.sh")); string(got) != string(script) {
		t.Errorf("installer changed: %q", got)
	}
	if !slices.Contains(brewEnv(), "HOMEBREW_NO_ANALYTICS=1") {
		t.Error("brewEnv lacks HOMEBREW_NO_ANALYTICS=1")
	}
}

// The package names no game: the game comes from the config.
func TestNoGameInSource(t *testing.T) {
	game := regexp.MustCompile(`(?i)blinx|burnout|\bb3\b`)
	files, _ := filepath.Glob("*.go")
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		if m := game.Find(b); m != nil {
			t.Errorf("%s names a game: %q", f, m)
		}
	}
}

// TestScreenshots renders every page, light and dark, at the window's
// size for the user's review (D8). It runs only when WIZARD_SHOTS names
// a directory; nothing it writes belongs in the repo.
func TestScreenshots(t *testing.T) {
	dir := os.Getenv("WIZARD_SHOTS")
	if dir == "" {
		t.Skip("WIZARD_SHOTS is not set")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	opt.Version = "v0.7.1"
	opt.NoDialog = false
	// A made-up player's paths, written from ~ so no real home directory
	// (and nothing the public audit flags) lands in the source.
	opt.Root = filepath.Join("~", "Library", "Application Support", opt.Cfg.Game.Slug+"-recomp")
	w := New(a, opt)
	// The window's own test canvas has no painter: draw its content on a
	// software canvas of the window's size, padded as a real window is.
	c := software.NewCanvas()
	c.SetContent(w.win.Content())
	c.Resize(fyne.NewSize(780, 580))
	n := 0
	shot := func(name string) {
		t.Helper()
		n++
		//lint:ignore SA1019 the deprecated fixed variants are right for fixed-variant images
		for variant, th := range map[string]fyne.Theme{"light": theme.LightTheme(), "dark": theme.DarkTheme()} {
			c.Content().Refresh()
			img := software.RenderCanvas(c, Theme(th))
			f, err := os.Create(filepath.Join(dir, fmt.Sprintf("%02d-%s-%s.png", n, name, variant)))
			if err != nil {
				t.Fatal(err)
			}
			if err := png.Encode(f, img); err != nil {
				t.Fatal(err)
			}
			f.Close()
		}
	}
	logLines := func(p *logPane, lines ...string) {
		p.after = func(time.Duration, func()) {} // flushed by hand below
		p.Clear()
		for _, l := range lines {
			p.Append(l)
		}
		p.flush()
	}
	src := filepath.Join(opt.Root, "src")
	prepLog := []string{
		"uv: 0.9.4 from https://github.com/astral-sh/uv/releases/download/0.9.4/uv-aarch64-apple-darwin.tar.gz",
		"  uv: 100% of 17.9 MB",
		"uv: " + filepath.Join(opt.Root, "tools", "uv", "uv"),
		"git: /usr/bin/git",
		fmt.Sprintf("game: %s %s into %s", opt.Cfg.Release.Repo, opt.Cfg.Release.Tag, src),
		"  Receiving objects:  64% (3283/5130), 41.20 MiB | 9.80 MiB/s",
	}

	w.render(pWelcome)
	shot("welcome")

	w.checking = true
	w.setRows(checkRows(opt.Cfg, nil, nil, nil, false))
	w.checksBar.SetValue(2.0 / 4)
	logLines(w.checksLog, prepLog...)
	w.render(pChecks)
	shot("checks-busy")

	// A player's tree: the bootstrap cloned the CLI at its pin (the saved
	// reports are from a developer's checkout).
	asPlayer := func(d *runner.Doctor) *runner.Doctor {
		for i, l := range d.Lines {
			if strings.HasPrefix(l, "cli:") {
				d.Lines[i] = "cli:        " + filepath.Join(src, ".xbr", "xboxrecomp-cli") + " @ a7cdc15c80fa (pinned v0.6.7)"
			}
		}
		return d
	}
	ready := asPlayer(loadDoctor(t, "doctor.json"))
	blocked := asPlayer(loadDoctor(t, "doctor-blocked.json"))
	w.checking = false
	w.env = &Env{Root: opt.Root, Game: src, Git: "/usr/bin/git"}
	w.doctor = ready
	logLines(w.checksLog, append(prepLog[:5:5], "doctor: ./"+opt.Cfg.Game.Slug+" doctor --json", checksDone)...)
	w.showChecks(nil)
	w.render(pChecks)
	shot("checks-ready")
	w.checksLog.setOpen(true)
	shot("checks-ready-log-open")
	w.checksLog.setOpen(false)

	w.doctor = blocked
	w.showChecks(nil)
	w.render(pChecks)
	test.Tap(w.rowParts[targetName("macos")].toggle)
	shot("checks-blocked-open")

	w.doctor = nil
	space := &SpaceError{Free: 6 << 30, Need: hostinfo.NeedBytes}
	logLines(w.checksLog, prepLog[:4]...)
	w.checksLog.Fail(fmt.Sprintf(checksStopped, lowerFirst(plainPrepare(space))))
	w.checksLog.flush()
	w.showChecks(space)
	w.refreshNav()
	w.render(pChecks)
	shot("checks-failed")

	w.doctor = ready
	w.showChecks(nil)
	w.dumpEntry.SetText("~/Games/disc.iso")
	w.render(pDump)
	shot("your-game")
	w.dumpEntry.SetText("~/Games")
	w.dumpNote.SetText(fmt.Sprintf(dumpNoXBE, "~/Games"))
	w.dumpNote.Show()
	shot("your-game-error")
	w.dumpNote.Hide()

	w.doctor = blocked
	w.fillTargets()
	w.render(pTarget)
	shot("platform-blocked")
	w.doctor = ready
	w.fillTargets()
	w.render(pTarget)
	shot("platform")

	if err := w.st.Set("scale", "2"); err != nil {
		t.Fatal(err)
	}
	w.render(pOptions)
	w.fillOptions()
	shot("options")

	w.st.Dump = "~/Games/disc.iso"
	w.fillSummary()
	w.render(pSummary)
	shot("summary")
	w.summaryAcc.Open(0)
	shot("summary-details-open")

	w.building = true
	w.cancelBtn.Enable()
	w.bar.SetValue(0.35)
	w.stepLabel.SetText(fmt.Sprintf(buildStep, 2, 5, "setup") + " (llvm-mingw)")
	logLines(w.buildLog, "$ ./"+opt.Cfg.Game.Slug+" package "+w.st.Target+" --plain",
		"step 1/5 import", "step 2/5 setup llvm-mingw", "setup: llvm-mingw 20251104: 48% of 182.4 MB")
	w.render(pBuild)
	shot("building")
	w.buildLog.setOpen(true)
	shot("building-log-open")
	w.buildLog.setOpen(false)
	w.building = false

	w.prog = &runner.Progress{Bundle: filepath.Join(src, "dist", "Game-1.0-"+w.st.Target+".tar")}
	w.buildErr = nil
	w.fillDone()
	w.render(pDone)
	shot("done")
	w.prog = &runner.Progress{Failed: "build", Log: filepath.Join(src, "build-logs", "package-"+w.st.Target+".log")}
	w.buildErr = errors.New("canceled")
	w.fillDone()
	w.render(pDone)
	shot("done-stopped")

	// A row opened by tapping its line: the plain detail over the CLI's
	// own lines, which must read as text, not as disabled.
	w.doctor = ready
	logLines(w.checksLog, append(prepLog[:5:5], "doctor: ./"+opt.Cfg.Game.Slug+" doctor --json", checksDone)...)
	w.showChecks(nil)
	w.render(pChecks)
	test.Tap(w.rowParts[rowTools].line)
	shot("checks-row-open")
}

// The state colours read as text on both backgrounds (WCAG AA, 4.5:1),
// and a failure line in the log pane takes the error colour until the
// next line.
func TestPalette(t *testing.T) {
	lum := func(c color.Color) float64 {
		r, g, b, _ := c.RGBA()
		ch := func(v uint32) float64 {
			f := float64(v) / 0xffff
			if f <= 0.03928 {
				return f / 12.92
			}
			return math.Pow((f+0.055)/1.055, 2.4)
		}
		return 0.2126*ch(r) + 0.7152*ch(g) + 0.0722*ch(b)
	}
	//lint:ignore SA1019 the deprecated fixed variants are what the screenshots use
	for name, base := range map[string]fyne.Theme{"light": theme.LightTheme(), "dark": theme.DarkTheme()} {
		th := Theme(base)
		bg := lum(th.Color(theme.ColorNameBackground, theme.VariantLight))
		for _, n := range []fyne.ThemeColorName{theme.ColorNameSuccess, theme.ColorNameWarning, theme.ColorNameError} {
			fg := lum(th.Color(n, theme.VariantLight))
			if ratio := (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05); ratio < 4.5 {
				t.Errorf("%s %s: contrast %.2f", name, n, ratio)
			}
		}
	}

	p := newLogPane(nil)
	p.after = func(time.Duration, func()) {}
	p.Fail("stopped")
	p.flush()
	if p.last.seg.Style.ColorName != theme.ColorNameError {
		t.Errorf("failure line colour %q", p.last.seg.Style.ColorName)
	}
	p.Append("again")
	p.flush()
	if p.last.seg.Style.ColorName != theme.ColorNamePlaceHolder {
		t.Errorf("next line colour %q", p.last.seg.Style.ColorName)
	}
}

// Every target the CLI knows has its own glyph, a game's unknown target
// falls back to the generic computer, and the Platform page shows one
// choice per buildable platform, exclusive like one radio group.
func TestPlatformIcons(t *testing.T) {
	seen := map[fyne.Resource]string{}
	for _, tg := range []string{"windows", "macos", "steamos", "linux"} {
		r, ok := platformGlyphs[tg]
		if !ok || !strings.Contains(string(r.Content()), "<svg") {
			t.Errorf("%s has no glyph", tg)
		}
		if other, dup := seen[r]; dup && tg != "linux" {
			t.Errorf("%s and %s share a glyph", tg, other)
		}
		seen[r] = tg
	}
	if platformIcon("dreamcast") != genericComputer || genericComputer == platformIcon("macos") {
		t.Error("an unknown target does not fall back to the generic computer")
	}
	if rowIcon(checkRow{Name: targetName("macos"), Target: "macos"}) != platformIcon("macos") {
		t.Error("a platform row does not lead with its glyph")
	}

	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.doctor = loadDoctor(t, "doctor-blocked.json")
	w.st.Target = "steamos"
	w.fillTargets()
	ok := w.okTargets()
	if len(w.targetRadios) != len(ok) {
		t.Fatalf("%d choices for %d buildable platforms", len(w.targetRadios), len(ok))
	}
	w.doctor = loadDoctor(t, "doctor.json")
	w.doctor.Blockers = map[string][]string{"windows": nil, "steamos": nil, "macos": nil}
	w.fillTargets()
	w.targetRadios["macos"].SetSelected(model.TargetLabels["macos"])
	if w.st.Target != "macos" || w.targetRadios["steamos"].Selected != "" || w.targetRadios["windows"].Selected != "" {
		t.Errorf("target %q, steamos %q, windows %q", w.st.Target, w.targetRadios["steamos"].Selected, w.targetRadios["windows"].Selected)
	}
}

// The Options page: a slider for a range, set from the state with its value
// and label beside it, and the config.toml note following the platform.
func TestOptionsPage(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	w := New(a, opt)
	if err := w.st.Set("scale", "3"); err != nil {
		t.Fatal(err)
	}
	w.st.Target = "windows"
	w.render(pOptions)
	w.fillOptions()
	var slider *widget.Slider
	var link *widget.Hyperlink
	var labels []string
	walkTree(w.pages[pOptions].obj, func(o fyne.CanvasObject) {
		switch v := o.(type) {
		case *widget.Hyperlink:
			link = v
		case *widget.Slider:
			slider = v
		case *widget.Label:
			labels = append(labels, v.Text)
		}
	})
	// The slider runs over indexes: scale 3 of 1..4 is index 2.
	if slider == nil || slider.Value != 2 || slider.Min != 0 || slider.Max != 3 || slider.Step != 1 {
		t.Fatalf("scale slider: %+v", slider)
	}
	if _, ok := any(slider).(fyne.Focusable); !ok {
		t.Error("the slider cannot take keyboard focus")
	}
	if !slices.Contains(labels, "3 (1920×1440)") {
		t.Errorf("readout not in %q", labels)
	}
	// The HUD option's link, from the config; not tapped here, since that
	// would open a browser.
	if link == nil || link.Text != "Get PT Sans Narrow (free, Google Fonts)" ||
		link.URL.String() != "https://fonts.google.com/specimen/PT+Sans+Narrow" || link.OnTapped == nil {
		t.Errorf("hint link: %+v", link)
	}
	hud := opt.Cfg.Options[2]
	if h, ok := w.optionHint(hud).(*fyne.Container); !ok || len(h.Objects) != 1 {
		t.Error("a checkbox's hint is not in an indented container")
	}
	if _, ok := w.optionHint(opt.Cfg.Options[0]).(*fyne.Container); !ok {
		t.Error("the slider's hint is missing")
	}
	hud.Hint, hud.HintLink = "", nil
	if w.optionHint(hud) != nil {
		t.Error("an option without a hint has one")
	}
	slider.SetValue(3)
	if got := w.st.Value("scale"); got != "4" {
		t.Errorf("slider at 4 set scale %q", got)
	}
	if got := w.optionsNote.Text(); got != fmt.Sprintf(optionsLater, `%LOCALAPPDATA%\BLiNX2\config\config.toml`) {
		t.Errorf("windows note %q", got)
	}
	w.st.Target = "macos"
	w.fillOptions()
	if got := w.optionsNote.Text(); !strings.HasSuffix(got, "/Library/Application Support/BLiNX2/config/config.toml") {
		t.Errorf("macos note %q", got)
	}
	opt.Cfg.Data = nil
	if got := ConfigPath(opt.Cfg, "steamos"); got != "" {
		t.Errorf("no data folder gave %q", got)
	}
}

// A range whose min is not a multiple of its step: Fyne's slider snaps to
// multiples of Step from 0, so driven by value 3 and 5 would land on 2 and
// 4. Over indexes every value round-trips and a saved 3 shows as 3.
func TestOptionsRangeOffStep(t *testing.T) {
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	lo, hi := 1, 7
	o := &opt.Cfg.Options[0]
	o.Min, o.Max, o.Step, o.Labels, o.Default = &lo, &hi, 2, nil, float64(1)
	o.TOML = map[string]string{"1": "1", "3": "3", "5": "5", "7": "7"}
	if err := opt.Cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	w := New(a, opt)
	if err := w.st.Set("scale", "3"); err != nil {
		t.Fatal(err)
	}
	w.render(pOptions)
	w.fillOptions()
	var slider *widget.Slider
	var readout []string
	look := func() {
		readout = nil
		walkTree(w.pages[pOptions].obj, func(c fyne.CanvasObject) {
			switch v := c.(type) {
			case *widget.Slider:
				slider = v
			case *widget.Label:
				readout = append(readout, v.Text)
			}
		})
	}
	look()
	if slider == nil || slider.Value != 1 || slider.Max != 3 || !slices.Contains(readout, "3") {
		t.Fatalf("saved 3: slider %+v, labels %q", slider, readout)
	}
	for i, want := range []string{"1", "3", "5", "7"} {
		slider.SetValue(float64(i))
		look()
		if got := w.st.Value("scale"); got != want || !slices.Contains(readout, want) {
			t.Errorf("index %d: scale %q, labels %q; want %s", i, got, readout, want)
		}
	}
}

// walkTree calls f on o and every object inside its containers and scrolls.
func walkTree(o fyne.CanvasObject, f func(fyne.CanvasObject)) {
	f(o)
	switch v := o.(type) {
	case *fyne.Container:
		for _, c := range v.Objects {
			walkTree(c, f)
		}
	case *container.Scroll:
		walkTree(v.Content, f)
	}
}

// A web address opens through url.dll on Windows (explorer splits on
// commas), as one argument everywhere.
func TestURLOpener(t *testing.T) {
	u := "https://example.org/a,b?q=1"
	for goos, want := range map[string][]string{
		"windows": {"rundll32", "url.dll,FileProtocolHandler", u},
		"macos":   {"open", u},
		"linux":   {"xdg-open", u},
	} {
		if got := urlOpener(goos, u); !slices.Equal(got, want) {
			t.Errorf("%s: %q, want %q", goos, got, want)
		}
	}
}
