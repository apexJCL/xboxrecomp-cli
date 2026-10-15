// Package screens is the wizard's window: one page at a time with Back
// and Next, each page reading and writing the model. The work behind the
// pages is the CLI's (work.go runs it); nothing here builds anything.
package screens

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/widget"
	"github.com/ncruces/zenity"

	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/hostinfo"
	"wizard/internal/model"
	"wizard/internal/repo"
	"wizard/internal/runner"
	"wizard/internal/termlaunch"
	"wizard/internal/update"
)

// Options are main's flags and what it loaded.
type Options struct {
	Cfg           *gamecfg.Config
	Pins          *fetch.Pins
	Version       string
	Root          string // --root; "" for the saved or the default
	DevGame       string // --game DIR: build in DIR as it is, no clone
	NoDialog      bool
	NoUpdateCheck bool
}

type page struct {
	title string
	obj   fyne.CanvasObject
	enter func()
	ready func() bool
	next  string
}

// Wizard is the window and its state.
type Wizard struct {
	opt      Options
	app      fyne.App
	win      fyne.Window
	st       *model.State
	setDir   string
	settings hostinfo.Settings
	firstRun bool
	root     string

	pages []*page
	cur   int
	title *widget.Label
	body  *fyne.Container
	back  *widget.Button
	next  *widget.Button

	logMu sync.Mutex

	// welcome
	rootLabel *widget.Label
	notice    *fyne.Container

	// checks
	env       *Env
	doctor    *runner.Doctor
	checksLog *logView
	checkBox  *fyne.Container
	checking  bool

	// dump
	dumpEntry *widget.Entry
	dumpNote  *widget.Label

	// target, options, summary
	targetBox *fyne.Container
	summary   *widget.Entry

	// build and done
	buildLog  *logView
	bar       *widget.ProgressBar
	stepLabel *widget.Label
	cancelBtn *widget.Button
	procMu    sync.Mutex
	proc      *runner.Proc
	canceled  bool
	building  bool
	prog      *runner.Progress
	buildErr  error
	doneBox   *fyne.Container
}

const (
	pWelcome = iota
	pChecks
	pDump
	pTarget
	pOptions
	pSummary
	pBuild
	pDone
)

// New builds the window and every page; Show starts it.
func New(a fyne.App, opt Options) *Wizard {
	w := &Wizard{opt: opt, app: a}
	w.setDir = hostinfo.SettingsDir(opt.Cfg.Game.Slug)
	_, err := os.Stat(filepath.Join(w.setDir, "settings.json"))
	w.firstRun = err != nil
	w.settings = hostinfo.LoadSettings(w.setDir)
	switch {
	case opt.Root != "":
		w.root = opt.Root
	case w.settings.Root != "":
		w.root = w.settings.Root
	default:
		w.root = hostinfo.DefaultRoot(opt.Cfg.Game.Slug)
	}
	w.st = model.New(opt.Cfg, hostinfo.NativeTarget())
	w.win = a.NewWindow(opt.Cfg.Game.Name + " build wizard")
	w.title = widget.NewLabelWithStyle("", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	w.body = container.NewStack()
	w.back = widget.NewButton("Back", func() { w.show(w.cur - 1) })
	w.next = widget.NewButton("Next", w.onNext)
	w.next.Importance = widget.HighImportance
	w.pages = []*page{
		w.welcomePage(), w.checksPage(), w.dumpPage(), w.targetPage(),
		w.optionsPage(), w.summaryPage(), w.buildPage(), w.donePage(),
	}
	nav := container.NewHBox(w.back, widgetSpacer(), w.next)
	w.win.SetContent(container.NewBorder(container.NewPadded(w.title), container.NewPadded(nav), nil, nil,
		container.NewPadded(w.body)))
	w.win.Resize(fyne.NewSize(780, 580))
	w.win.SetCloseIntercept(w.onClose)
	w.render(pWelcome)
	return w
}

func widgetSpacer() fyne.CanvasObject { return container.NewHBox() }

// Show runs the window and the release check.
func (w *Wizard) Show() {
	if !w.opt.NoUpdateCheck && !w.settings.NoUpdateCheck && !w.opt.Cfg.Release.Dev {
		go w.checkUpdate()
	}
	w.win.ShowAndRun()
}

// Logf appends a line to the wizard's own log file (the settings folder).
func (w *Wizard) Logf(format string, a ...any) {
	w.logMu.Lock()
	defer w.logMu.Unlock()
	if os.MkdirAll(w.setDir, 0o755) != nil {
		return
	}
	f, err := os.OpenFile(filepath.Join(w.setDir, "wizard.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s %s\n", time.Now().Format("2006-01-02 15:04:05"), fmt.Sprintf(format, a...))
}

func (w *Wizard) checkUpdate() {
	rel, why := update.Latest(context.Background(), &http.Client{}, w.opt.Cfg.Release.Repo, w.opt.Cfg.Release.Tag)
	w.Logf("release check: %s", why)
	if rel == nil {
		return
	}
	fyne.Do(func() {
		notes := widget.NewLabel(rel.Body)
		notes.Wrapping = fyne.TextWrapWord
		scroll := container.NewVScroll(notes)
		scroll.SetMinSize(fyne.NewSize(0, 90))
		w.notice.Objects = []fyne.CanvasObject{widget.NewCard("A newer wizard is available: "+rel.Tag,
			"This one keeps working; download the new one from the release page if you like.",
			container.NewBorder(nil, widget.NewButton("Open the release page", func() { w.openURL(rel.URL) }), nil, nil, scroll))}
		w.notice.Refresh()
	})
}

func (w *Wizard) openURL(u string) {
	if err := OpenPath(u); err != nil {
		w.errorBox(err)
	}
}

func (w *Wizard) errorBox(err error) { dialog.ShowError(err, w.win) }

// render shows page i without running its enter step (the tests use it).
func (w *Wizard) render(i int) {
	w.cur = i
	p := w.pages[i]
	w.title.SetText(fmt.Sprintf("%s  (%d of %d)", p.title, i+1, len(w.pages)))
	w.body.Objects = []fyne.CanvasObject{p.obj}
	w.body.Refresh()
	w.refreshNav()
}

func (w *Wizard) show(i int) {
	if i < 0 || i >= len(w.pages) {
		return
	}
	w.render(i)
	if p := w.pages[i]; p.enter != nil {
		p.enter()
	}
	w.refreshNav()
}

func (w *Wizard) refreshNav() {
	p := w.pages[w.cur]
	text := p.next
	if text == "" {
		text = "Next"
	}
	w.next.SetText(text)
	if p.ready == nil || p.ready() {
		w.next.Enable()
	} else {
		w.next.Disable()
	}
	if w.cur == pWelcome || w.cur == pDone || w.building || w.checking {
		w.back.Disable()
	} else {
		w.back.Enable()
	}
}

func (w *Wizard) onNext() {
	switch w.cur {
	case pDump:
		if err := w.checkDump(); err != nil {
			w.dumpNote.SetText(err.Error())
			return
		}
	case pDone:
		w.app.Quit()
		return
	}
	w.show(w.cur + 1)
}

func (w *Wizard) onClose() {
	if !w.building {
		w.win.Close()
		return
	}
	dialog.ShowConfirm("A build is running", "Stop the build and quit? The next run picks up from what is done.",
		func(ok bool) {
			if ok {
				w.cancelBuild()
				go func() {
					w.procMu.Lock()
					p := w.proc
					w.procMu.Unlock()
					if p != nil {
						p.Wait()
					}
					fyne.Do(w.win.Close)
				}()
			}
		}, w.win)
}

func para(text string) *widget.Label {
	l := widget.NewLabel(text)
	l.Wrapping = fyne.TextWrapWord
	return l
}

func short(c string) string {
	if len(c) > 12 {
		return c[:12]
	}
	return c
}

// --- welcome

func (w *Wizard) welcomePage() *page {
	c := w.opt.Cfg
	rel := c.Release.Tag
	if c.Release.Dev {
		rel = "developer build of the working tree"
	}
	pins := fmt.Sprintf("Release %s (%s); xboxrecomp-cli %s; toolkit %s", rel, short(c.Release.Commit), c.CLI.Tag, c.Toolkit.Tag)
	intro := para(fmt.Sprintf("This program builds %s for your own computer from your own copy of the game. "+
		"It downloads the free tools it needs into the folder below, asks for your disc image or game folder, "+
		"and runs the game's own build. Nothing is uploaded.", c.Game.Name))
	w.rootLabel = widget.NewLabel(w.root)
	w.rootLabel.Wrapping = fyne.TextWrapBreak
	change := widget.NewButton("Change...", w.pickRoot)
	w.notice = container.NewVBox()
	upd := widget.NewCheck("Check for a newer wizard when it starts", func(on bool) {
		w.settings.NoUpdateCheck = !on
		w.saveSettings()
	})
	upd.SetChecked(!w.settings.NoUpdateCheck)
	items := []fyne.CanvasObject{
		widget.NewLabelWithStyle(c.Game.Name+" build wizard "+w.opt.Version, fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
		widget.NewLabel(pins),
		intro,
		widget.NewLabelWithStyle("Install folder (the tools, the game's source and the build; about 15 GB)", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
		container.NewBorder(nil, nil, nil, change, w.rootLabel),
		upd,
	}
	if w.firstRun {
		items = append(items, para("This wizard is not signed. If your system warned you before it opened, "+
			"that is why; the release page lists the SHA256 of every download."))
	}
	items = append(items, w.notice)
	return &page{title: "Welcome", obj: container.NewVScroll(container.NewVBox(items...)),
		ready: func() bool { return w.root != "" }}
}

func (w *Wizard) saveSettings() {
	if err := hostinfo.SaveSettings(w.setDir, w.settings); err != nil {
		w.Logf("settings: %v", err)
	}
}

func (w *Wizard) pickRoot() {
	set := func(p string) {
		if p == "" {
			return
		}
		w.root = p
		w.settings.Root = p
		w.saveSettings()
		w.rootLabel.SetText(p)
		w.env, w.doctor = nil, nil
		w.refreshNav()
	}
	if !w.opt.NoDialog {
		p, err := zenity.SelectFile(zenity.Directory(), zenity.Title("Install folder"), zenity.Filename(w.root))
		if err == nil || errors.Is(err, zenity.ErrCanceled) {
			set(p)
			return
		}
		w.Logf("folder dialog: %v; using Fyne's", err)
	}
	dialog.ShowFolderOpen(func(u fyne.ListableURI, err error) {
		if u != nil {
			set(u.Path())
		}
	}, w.win)
}

// --- checks

func (w *Wizard) checksPage() *page {
	w.checksLog = newLogView(func(s string) { w.Logf("checks: %s", s) })
	w.checkBox = container.NewVBox()
	obj := container.NewBorder(w.checkBox, nil, nil, nil, w.checksLog.list)
	return &page{title: "Checks", obj: obj, enter: func() {
		if w.doctor == nil && !w.checking {
			w.runChecks()
		}
	}, ready: func() bool { return w.doctor != nil && len(w.okTargets()) > 0 && !w.checking }}
}

func (w *Wizard) okTargets() []string {
	var out []string
	if w.doctor == nil {
		return nil
	}
	for _, t := range w.opt.Cfg.Targets {
		if ok, _ := w.doctor.Target(t); ok {
			out = append(out, t)
		}
	}
	return out
}

func (w *Wizard) runChecks() {
	w.checking = true
	w.doctor = nil
	w.checksLog.Clear()
	w.checkBox.Objects = []fyne.CanvasObject{widget.NewProgressBarInfinite(), para("Getting the tools and the game's source ready. The first run downloads a few hundred MB.")}
	w.checkBox.Refresh()
	w.refreshNav()
	go func() {
		log := w.checksLog.Append
		var d *runner.Doctor
		env, err := Prepare(context.Background(), w.opt.Cfg, w.opt.Pins, w.root, w.opt.DevGame, log)
		if err == nil {
			log("doctor: " + runner.Display(w.opt.Cfg.Game.Slug, []string{"doctor", "--json"}))
			d, err = env.Runner.Doctor(context.Background(), LogWriter(func(s string) { log("  " + s) }))
		}
		fyne.Do(func() {
			w.checking = false
			w.env, w.doctor = env, d
			if err != nil {
				w.Logf("checks failed: %v", err)
			}
			w.showChecks(err)
			w.refreshNav()
		})
	}()
}

func (w *Wizard) showChecks(err error) {
	retry := widget.NewButton("Retry", w.runChecks)
	var rows []fyne.CanvasObject
	if err != nil {
		var need *NeedError
		rows = append(rows, para(err.Error()))
		if errors.As(err, &need) && need.Action.Kind != "" {
			rows = append(rows, w.actionButton(need.Action))
		}
		rows = append(rows, retry)
		w.checkBox.Objects = rows
		w.checkBox.Refresh()
		return
	}
	for _, l := range w.doctor.Lines {
		lab := widget.NewLabel(l)
		lab.TextStyle = fyne.TextStyle{Monospace: true}
		lab.Truncation = fyne.TextTruncateEllipsis
		rows = append(rows, lab)
	}
	var reasons []string
	for _, t := range w.opt.Cfg.Targets {
		ok, why := w.doctor.Target(t)
		mark := "can be built here"
		if !ok {
			mark = "not yet: " + strings.Join(why, "; ")
			reasons = append(reasons, why...)
		}
		rows = append(rows, para(fmt.Sprintf("%s: %s", model.TargetLabels[t], mark)))
	}
	for _, a := range runner.Actions(reasons) {
		rows = append(rows, w.actionButton(a))
	}
	rows = append(rows, para("Everything else the build needs it fetches itself when it starts."), retry)
	w.checkBox.Objects = []fyne.CanvasObject{container.NewVScroll(container.NewVBox(rows...))}
	w.checkBox.Objects[0].(*container.Scroll).SetMinSize(fyne.NewSize(0, 300))
	w.checkBox.Refresh()
}

// actionButton is the fix for a blocker, run where the OS asks for any
// password itself (D11): never by the wizard.
func (w *Wizard) actionButton(a runner.Action) fyne.CanvasObject {
	switch a.Kind {
	case "clt":
		return widget.NewButton("Install Apple's Command Line Tools", func() {
			if err := repo.InstallDeveloperTools(); err != nil {
				w.Logf("xcode-select --install: %v", err)
			}
			w.checksLog.Append("Apple's installer is open; this page checks again when it finishes.")
			go func() {
				for i := 0; i < 720 && !repo.HaveDeveloperTools(); i++ {
					time.Sleep(5 * time.Second)
				}
				fyne.Do(w.runChecks)
			}()
		})
	case "brew":
		if BrewPath() == "" {
			return container.NewVBox(
				para("Homebrew is needed for "+a.Command+". Its installer runs in Terminal and asks for your password there."),
				widget.NewButton("Install Homebrew", w.installHomebrew))
		}
		return widget.NewButton("Run: "+a.Command, func() { w.runBrew(a.Command) })
	case "sudo":
		return widget.NewButton("Run in a terminal: "+a.Command, func() {
			if err := termlaunch.Open(a.Command); err != nil {
				w.errorBox(err)
			}
		})
	}
	return widget.NewLabel(a.Command)
}

func (w *Wizard) installHomebrew() {
	go func() {
		script, err := HomebrewScript(context.Background(), w.opt.Pins, w.root)
		if err == nil {
			err = exec.Command("open", "-a", "Terminal", script).Run()
		}
		if err != nil {
			fyne.Do(func() { w.errorBox(err) })
			return
		}
		w.checksLog.Append("Homebrew's installer is open in Terminal; this page checks again when brew is there.")
		for i := 0; i < 720 && BrewPath() == ""; i++ {
			time.Sleep(5 * time.Second)
		}
		fyne.Do(w.runChecks)
	}()
}

func (w *Wizard) runBrew(command string) {
	brew := BrewPath()
	args := strings.Fields(command)[1:]
	w.checking = true
	w.refreshNav()
	go func() {
		w.checksLog.Append("$ " + command)
		cmd := exec.Command(brew, args...)
		lw := LogWriter(w.checksLog.Append)
		cmd.Stdout, cmd.Stderr = lw, lw
		err := cmd.Run()
		if err != nil {
			w.checksLog.Append(fmt.Sprintf("brew: %v", err))
		}
		fyne.Do(func() { w.checking = false; w.runChecks() })
	}()
}

// --- dump

func (w *Wizard) dumpPage() *page {
	w.dumpEntry = widget.NewEntry()
	w.dumpEntry.SetPlaceHolder("the path of your disc image (.iso) or game folder")
	w.dumpEntry.OnChanged = func(string) { w.dumpNote.SetText(""); w.refreshNav() }
	w.dumpNote = para("")
	items := []fyne.CanvasObject{
		para(fmt.Sprintf("Your own copy of %s: a disc image (.iso, .xiso) or the folder with default.xbe. "+
			"The build checks the title and copies the files into the game's folder; the original is not changed.", w.opt.Cfg.Game.Name)),
	}
	if !w.opt.NoDialog {
		items = append(items, container.NewHBox(
			widget.NewButton("Choose a disc image...", func() { w.pickDump(false) }),
			widget.NewButton("Choose a folder...", func() { w.pickDump(true) })))
	}
	items = append(items, w.dumpEntry, w.dumpNote)
	return &page{title: "Your game", obj: container.NewVBox(items...),
		ready: func() bool { return strings.TrimSpace(w.dumpEntry.Text) != "" }}
}

func (w *Wizard) pickDump(folder bool) {
	opts := []zenity.Option{zenity.Title("Your " + w.opt.Cfg.Game.Name + " dump")}
	if folder {
		opts = append(opts, zenity.Directory())
	} else {
		opts = append(opts, zenity.FileFilters{{Name: "Disc images", Patterns: []string{"*.iso", "*.xiso"}, CaseFold: true}})
	}
	p, err := zenity.SelectFile(opts...)
	if err == nil {
		w.dumpEntry.SetText(p)
		return
	}
	if errors.Is(err, zenity.ErrCanceled) {
		return
	}
	w.Logf("file dialog: %v; using Fyne's", err)
	if folder {
		dialog.ShowFolderOpen(func(u fyne.ListableURI, err error) {
			if u != nil {
				w.dumpEntry.SetText(u.Path())
			}
		}, w.win)
		return
	}
	dialog.ShowFileOpen(func(r fyne.URIReadCloser, err error) {
		if r != nil {
			w.dumpEntry.SetText(r.URI().Path())
			r.Close()
		}
	}, w.win)
}

// checkDump is the path check before the CLI's own (the title, the files).
func (w *Wizard) checkDump() error {
	p := strings.TrimSpace(w.dumpEntry.Text)
	st, err := os.Stat(p)
	if err != nil {
		return fmt.Errorf("%s: no such file or folder", p)
	}
	need := uint64(hostinfo.NeedBytes)
	if st.IsDir() {
		if _, err := os.Stat(filepath.Join(p, "default.xbe")); err != nil {
			return fmt.Errorf("%s has no default.xbe: pick the game's top folder, or the disc image", p)
		}
	} else {
		need += uint64(st.Size())
	}
	if free, err := hostinfo.Free(w.root); err == nil && free < need {
		return fmt.Errorf("the install folder's disk has %.1f GB free; this needs about %.1f GB", float64(free)/1e9, float64(need)/1e9)
	}
	abs, err := filepath.Abs(p)
	if err == nil {
		p = abs
	}
	w.st.Dump = p
	return nil
}

// --- target

func (w *Wizard) targetPage() *page {
	w.targetBox = container.NewVBox()
	return &page{title: "Platform", obj: container.NewVScroll(w.targetBox), enter: w.fillTargets,
		ready: func() bool { return slices.Contains(w.okTargets(), w.st.Target) }}
}

func (w *Wizard) fillTargets() {
	ok := w.okTargets()
	labels := make([]string, len(ok))
	byLabel := map[string]string{}
	for i, t := range ok {
		labels[i] = model.TargetLabels[t]
		byLabel[labels[i]] = t
	}
	radio := widget.NewRadioGroup(labels, func(s string) {
		if t, found := byLabel[s]; found {
			w.st.Target = t
		}
		w.refreshNav()
	})
	if !slices.Contains(ok, w.st.Target) && len(ok) > 0 {
		w.st.Target = ok[0]
	}
	radio.SetSelected(model.TargetLabels[w.st.Target])
	items := []fyne.CanvasObject{para("Which computer is the game for? This one is preselected; " +
		"a bundle for another computer of yours is copied there and installed the same way."), radio}
	if w.doctor != nil {
		for _, t := range w.opt.Cfg.Targets {
			if good, why := w.doctor.Target(t); !good {
				items = append(items, para(model.TargetLabels[t]+": not on this computer yet ("+strings.Join(why, "; ")+")"))
			}
		}
	}
	if hostinfo.IsDeck() {
		items = append(items, para("On a Steam Deck the build is long and needs a lot of memory; a PC is faster."))
	}
	w.targetBox.Objects = items
	w.targetBox.Refresh()
}

// --- options

func (w *Wizard) optionsPage() *page {
	var items []fyne.CanvasObject
	for _, o := range w.opt.Cfg.Options {
		o := o
		if o.Kind == "bool" {
			c := widget.NewCheck(o.Label, func(on bool) { _ = w.st.Set(o.Key, fmt.Sprint(on)) })
			c.SetChecked(w.st.Value(o.Key) == "true")
			items = append(items, c)
			continue
		}
		r := widget.NewRadioGroup(o.Values, func(v string) {
			if v != "" {
				_ = w.st.Set(o.Key, v)
			}
		})
		r.Horizontal = true
		r.Required = true
		r.SetSelected(w.st.Value(o.Key))
		items = append(items, widget.NewLabel(o.Label), r)
	}
	if len(items) == 0 {
		items = append(items, para("This game has no options to choose here."))
	}
	head := para("The defaults are the game as it shipped. Anything else is written to the game's config.toml " +
		"on a first install, where you can change it later.")
	return &page{title: "Options", obj: container.NewBorder(head, nil, nil, nil, container.NewVScroll(container.NewVBox(items...)))}
}

// --- summary

func (w *Wizard) summaryPage() *page {
	w.summary = widget.NewMultiLineEntry()
	w.summary.TextStyle = fyne.TextStyle{Monospace: true}
	w.summary.Wrapping = fyne.TextWrapWord
	return &page{title: "Summary", obj: w.summary, next: "Build", enter: w.fillSummary}
}

func (w *Wizard) seedArg() string {
	if w.st.Seed() == "" {
		return ""
	}
	return SeedPath(w.root)
}

func (w *Wizard) fillSummary() {
	c := w.opt.Cfg
	var b strings.Builder
	fmt.Fprintf(&b, "Game:      %s %s\n", c.Game.Name, c.Release.Tag)
	fmt.Fprintf(&b, "Folder:    %s\n", w.root)
	fmt.Fprintf(&b, "Platform:  %s\n", model.TargetLabels[w.st.Target])
	fmt.Fprintf(&b, "Data:      %s (saves and config/config.toml, on the computer you install it on)\n\n", c.Data[w.st.Target])
	b.WriteString("The build runs, in the game's folder:\n")
	for _, s := range w.st.Steps(w.seedArg()) {
		b.WriteString("  " + runner.Display(c.Game.Slug, s) + "\n")
	}
	if seed := w.st.Seed(); seed != "" {
		b.WriteString("\nconfig.toml for a first install:\n" + seed)
	} else {
		b.WriteString("\nEvery option at its default: no config.toml is written.\n")
	}
	w.summary.SetText(b.String())
	w.summary.Disable()
}

// --- build

func (w *Wizard) buildPage() *page {
	w.buildLog = newLogView(nil)
	w.bar = widget.NewProgressBar()
	w.stepLabel = widget.NewLabel("")
	w.cancelBtn = widget.NewButton("Cancel", func() {
		dialog.ShowConfirm("Cancel the build?", "The next run picks up from what is done.", func(ok bool) {
			if ok {
				w.cancelBuild()
			}
		}, w.win)
	})
	top := container.NewVBox(w.stepLabel, container.NewBorder(nil, nil, nil, w.cancelBtn, w.bar))
	return &page{title: "Building", obj: container.NewBorder(top, nil, nil, nil, w.buildLog.list),
		enter: w.startBuild, ready: func() bool { return !w.building }, next: "Next"}
}

func (w *Wizard) cancelBuild() {
	w.procMu.Lock()
	defer w.procMu.Unlock()
	w.canceled = true
	if w.proc != nil {
		if err := w.proc.Cancel(); err != nil {
			w.Logf("cancel: %v", err)
		}
	}
}

func (w *Wizard) startBuild() {
	if w.building {
		return
	}
	w.building, w.canceled, w.buildErr = true, false, nil
	w.prog = &runner.Progress{}
	w.buildLog.Clear()
	w.bar.SetValue(0)
	w.cancelBtn.Enable()
	w.stepLabel.SetText("Starting")
	w.refreshNav()
	seed := w.seedArg()
	steps := w.st.Steps(seed)
	go func() {
		err := w.runSteps(seed, steps)
		fyne.Do(func() {
			w.building = false
			w.buildErr = err
			w.cancelBtn.Disable()
			w.fillDone()
			w.show(pDone)
		})
	}()
}

func (w *Wizard) runSteps(seed string, steps [][]string) error {
	log := w.buildLog.Append
	if seed != "" {
		if err := os.WriteFile(seed, []byte(w.st.Seed()), 0o644); err != nil {
			return err
		}
	}
	slug := w.opt.Cfg.Game.Slug
	for i, s := range steps {
		w.procMu.Lock()
		if w.canceled {
			w.procMu.Unlock()
			return errors.New("canceled")
		}
		w.procMu.Unlock()
		log("$ " + runner.Display(slug, s))
		if i < len(steps)-1 {
			fyne.Do(func() { w.stepLabel.SetText("Preparing: " + strings.Join(s[:min(2, len(s))], " ")) })
		}
		p, err := w.env.Runner.Start(context.Background(), []string{"CI=1"}, func(line string) {
			log(line)
			if w.prog.Feed(line) {
				f, label := w.prog.Fraction(), w.progressText()
				fyne.Do(func() { w.bar.SetValue(f); w.stepLabel.SetText(label) })
			}
		}, s...)
		if err != nil {
			return err
		}
		w.procMu.Lock()
		w.proc = p
		w.procMu.Unlock()
		err = p.Wait()
		w.procMu.Lock()
		w.proc = nil
		canceled := w.canceled
		w.procMu.Unlock()
		if canceled {
			return errors.New("canceled")
		}
		if err != nil {
			return fmt.Errorf("%s: %w", runner.Display(slug, s), err)
		}
	}
	return nil
}

func (w *Wizard) progressText() string {
	p := w.prog
	if p.Index == 0 {
		return "Planning"
	}
	s := fmt.Sprintf("Step %d of %d: %s", p.Index, p.Total, p.Stage)
	if p.Label != "" {
		s += " (" + p.Label + ")"
	}
	return s
}

// --- done

func (w *Wizard) donePage() *page {
	w.doneBox = container.NewVBox()
	return &page{title: "Done", obj: container.NewVScroll(w.doneBox), next: "Quit"}
}

// installText is docs/packaging.md's install step for each target.
var installText = map[string]string{
	"windows": "Run the setup program on the Windows PC. It installs for your user only and adds a Start menu entry.",
	"macos":   "Open the .dmg and drag the app to Applications. The first time, right-click the app and choose Open (it is not signed).",
	"steamos": "Copy the .tar to the Deck or Linux PC, unpack it and run ./install.sh --steam in a terminal: it installs the game and adds it to Steam.",
}

const bundleRule = "The bundle contains your own copy of the game and code generated from it. " +
	"It is for your own computers only: never share, upload or publish it."

func (w *Wizard) fillDone() {
	c := w.opt.Cfg
	var items []fyne.CanvasObject
	if w.buildErr == nil && w.prog.Bundle != "" {
		bundle := w.prog.Bundle
		w.pages[pDone].title = "Done"
		items = append(items,
			widget.NewLabelWithStyle("The bundle is ready.", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}),
			para(bundle),
			para(installText[w.st.Target]))
		row := []fyne.CanvasObject{widget.NewButton("Show the bundle", func() { w.openURL(filepath.Dir(bundle)) })}
		if w.st.Target == hostinfo.NativeTarget() {
			row = append(row, widget.NewButton("Install now", func() { w.installNow(bundle) }))
		}
		items = append(items, container.NewHBox(row...),
			para("Saves and config/config.toml live in "+c.Data[w.st.Target]+" on the computer you install it on."))
	} else {
		w.pages[pDone].title = "The build stopped"
		msg := "The build did not finish"
		if w.buildErr != nil {
			msg += ": " + w.buildErr.Error()
		}
		items = append(items, widget.NewLabelWithStyle(msg, fyne.TextAlignLeading, fyne.TextStyle{Bold: true}))
		if w.prog.Failed != "" {
			items = append(items, para("The failed step: "+w.prog.Failed))
		}
		logDir := filepath.Join(w.env.Game, "build-logs")
		if w.prog.Log != "" {
			items = append(items, para("Its log: "+w.prog.Log))
			logDir = filepath.Dir(w.prog.Log)
		}
		items = append(items, container.NewHBox(
			widget.NewButton("Open the log folder", func() { w.openURL(logDir) }),
			widget.NewButton("Retry", func() { w.show(pBuild) })),
			para("Retry runs the build again; it skips what is already done."))
	}
	items = append(items, para(bundleRule))
	w.doneBox.Objects = items
	w.doneBox.Refresh()
}

// installNow installs on this computer by the target's own route: the
// setup program, the .dmg, or install.sh in a terminal (it may ask about
// umu there).
func (w *Wizard) installNow(bundle string) {
	var err error
	switch w.st.Target {
	case "windows":
		err = exec.Command(bundle).Start()
	case "macos":
		err = OpenPath(bundle)
	case "steamos":
		dir := strings.TrimSuffix(bundle, ".tar")
		err = fetch.Unpack(bundle, dir)
		if err == nil {
			script := fetch.Find(dir, "install.sh")
			if script == "" {
				err = errors.New("the bundle has no install.sh")
			} else {
				err = termlaunch.Open(`cd "$1" && ./install.sh --steam`, filepath.Dir(script))
			}
		}
	}
	if err != nil {
		w.errorBox(err)
	}
}
