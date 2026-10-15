// Package screens is the wizard's window: one page at a time with Back
// and Next, each page reading and writing the model. The work behind the
// pages is the CLI's (work.go runs it); nothing here builds anything.
package screens

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/url"
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
	"fyne.io/fyne/v2/driver/desktop"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
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

	pages   []*page
	cur     int
	title   *widget.Label
	step    *quietText
	stepBar *thinBar
	footer  *quietText
	body    *fyne.Container
	back    *widget.Button
	next    *widget.Button

	logMu sync.Mutex

	// the newer-wizard check: latest is the request (the tests replace it),
	// updateAsked that this run has sent it.
	latest      func() (*update.Release, string)
	updateAsked bool

	// welcome
	rootLabel *widget.Label

	// checks
	env        *Env
	doctor     *runner.Doctor
	checksLog  *logPane
	checksRows *fyne.Container
	checksView *container.Scroll
	checksBar  *thinBar
	retry      *widget.Button
	activities []*widget.Activity
	rowParts   map[string]rowParts // by row name: the rows with a detail
	checking   bool

	// dump
	dumpEntry *widget.Entry
	dumpNote  *widget.Label

	// target, options, summary
	targetBox    *fyne.Container
	targetRadios map[string]*widget.RadioGroup
	optionsNote  *quietText
	optionsSync  []func()
	summaryBox   *fyne.Container
	summaryTech  *widget.Label
	summaryAcc   *widget.Accordion

	// build and done
	buildLog  *logPane
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
	a.Settings().SetTheme(Theme(theme.DefaultTheme()))
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
	w.latest = func() (*update.Release, string) {
		return update.Latest(context.Background(), &http.Client{}, opt.Cfg.Release.Repo, opt.Cfg.Release.Tag)
	}
	w.win = a.NewWindow(opt.Cfg.Game.Name + " build wizard")
	w.body = container.NewStack()
	w.back = widget.NewButton(backText, func() { w.show(w.cur - 1) })
	w.next = widget.NewButton(nextText, w.onNext)
	w.next.Importance = widget.HighImportance
	w.pages = []*page{
		w.welcomePage(), w.checksPage(), w.dumpPage(), w.targetPage(),
		w.optionsPage(), w.summaryPage(), w.buildPage(), w.donePage(),
	}
	w.win.SetContent(container.NewBorder(w.header(), w.bottom(), nil, nil, container.NewPadded(w.body)))
	w.win.Resize(fyne.NewSize(780, 580))
	w.win.SetCloseIntercept(w.onClose)
	w.render(pWelcome)
	return w
}

// header is the page title, "Step n of 8" and the step bar (D1): the
// text carries the step for a reader who cannot see the bar's fill.
func (w *Wizard) header() fyne.CanvasObject {
	w.title = widget.NewLabelWithStyle("", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	w.title.SizeName = theme.SizeNameSubHeadingText
	w.step = newQuiet(theme.SizeNameCaptionText, fyne.TextAlignTrailing, fyne.TextStyle{})
	w.stepBar = newThinBar(len(w.pages))
	line := container.NewBorder(nil, nil, w.title, container.NewVBox(layout.NewSpacer(), w.step))
	return container.NewPadded(container.NewVBox(line, w.stepBar))
}

// bottom is Back and Next, then the version: small, low contrast, at the
// bottom right of every page (the user's 2026-10-15 request). The pins a
// bug report wants are in the Summary's technical details.
func (w *Wizard) bottom() fyne.CanvasObject {
	nav := container.NewHBox(w.back, layout.NewSpacer(), w.next)
	f := footerText
	if w.opt.Cfg.Release.Dev {
		f = footerDev
	}
	w.footer = newQuiet(theme.SizeNameCaptionText, fyne.TextAlignTrailing, fyne.TextStyle{})
	w.footer.Truncation = fyne.TextTruncateEllipsis
	w.footer.SetText(fmt.Sprintf(f, w.opt.Version))
	pad := theme.Padding()
	return container.NewVBox(container.NewPadded(nav),
		container.New(layout.NewCustomPaddedLayout(-2*pad, 0, 0, pad), w.footer))
}

// pinsText names the exact sources of this wizard, for a bug report.
func (w *Wizard) pinsText() string {
	c := w.opt.Cfg
	rel := c.Release.Tag
	if c.Release.Dev {
		rel = devRelease
	}
	return fmt.Sprintf(techPins, w.opt.Version, rel, short(c.Release.Commit), c.CLI.Tag, c.Toolkit.Tag)
}

// Show runs the window. The release check waits for leaveWelcome.
func (w *Wizard) Show() { w.win.ShowAndRun() }

// leaveWelcome sends the newer-wizard check, once per run, when the player
// leaves the Welcome page with its box ticked: nothing goes out before the
// player has seen the box (the user's 2026-10-15 decision).
func (w *Wizard) leaveWelcome() {
	if w.updateAsked || w.opt.NoUpdateCheck || w.settings.NoUpdateCheck || w.opt.Cfg.Release.Dev {
		return
	}
	w.updateAsked = true
	go w.checkUpdate()
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
	rel, why := w.latest()
	w.Logf("release check: %s", why)
	if rel == nil {
		return
	}
	fyne.Do(func() {
		notes := widget.NewLabel(rel.Body)
		notes.Wrapping = fyne.TextWrapWord
		scroll := container.NewVScroll(notes)
		scroll.SetMinSize(fyne.NewSize(480, 120))
		body := container.NewBorder(para(newerKeepsText),
			container.NewHBox(widget.NewButton(openReleaseText, func() { w.openURL(rel.URL) })), nil, nil, scroll)
		dialog.ShowCustom(newerTitle(rel.Tag), closeText, body, w.win)
	})
}

func (w *Wizard) openURL(u string) {
	if err := OpenURL(u); err != nil {
		w.errorBox(err)
	}
}

func (w *Wizard) openPath(p string) {
	if err := OpenPath(p); err != nil {
		w.errorBox(err)
	}
}

func (w *Wizard) errorBox(err error) { dialog.ShowError(err, w.win) }

// render shows page i without running its enter step (the tests use it).
func (w *Wizard) render(i int) {
	w.cur = i
	p := w.pages[i]
	w.title.SetText(p.title)
	w.step.SetText(fmt.Sprintf(stepText, i+1, len(w.pages)))
	// The current page counts as reached: step 2 of 8 fills two segments.
	w.stepBar.SetValue(float64(i+1) / float64(len(w.pages)))
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
		text = nextText
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
	if w.checking {
		w.retry.Disable()
	} else {
		w.retry.Enable()
	}
}

func (w *Wizard) onNext() {
	switch w.cur {
	case pWelcome:
		w.leaveWelcome()
	case pDump:
		if err := w.checkDump(); err != nil {
			w.dumpNote.SetText(err.Error())
			w.dumpNote.Show()
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
	dialog.ShowConfirm(quitTitle, quitText,
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

// blockedLine is a warning in the Checks page's palette: the warning icon
// and the text in the warning colour.
func blockedLine(text string) fyne.CanvasObject {
	t := widget.NewRichText(&widget.TextSegment{Text: text, Style: widget.RichTextStyle{ColorName: theme.ColorNameWarning}})
	t.Wrapping = fyne.TextWrapWord
	return container.NewBorder(nil, nil, widget.NewIcon(theme.NewWarningThemedResource(theme.WarningIcon())), nil, t)
}

func bold(text string) *widget.Label {
	return widget.NewLabelWithStyle(text, fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
}

// mono is read-only text the player can select and copy: a disabled
// Entry cannot be selected, a Label with Selectable can.
func mono(text string) *widget.Label {
	l := widget.NewLabel(text)
	l.TextStyle = fyne.TextStyle{Monospace: true}
	l.Selectable = true
	// Word wrap keeps tags and flags whole; a path longer than the line
	// still breaks.
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

// statement is one "Before you start" row: an icon, the lead in bold and
// the sentence after it.
func statement(lead, text string) fyne.CanvasObject {
	rt := widget.NewRichText(
		&widget.TextSegment{Style: widget.RichTextStyleStrong, Text: lead + " "},
		&widget.TextSegment{Style: widget.RichTextStyleInline, Text: text})
	rt.Wrapping = fyne.TextWrapWord
	icon := widget.NewIcon(theme.InfoIcon())
	return container.NewBorder(nil, nil, container.NewVBox(container.NewPadded(icon)), nil, rt)
}

func (w *Wizard) welcomePage() *page {
	c := w.opt.Cfg
	heading := bold(welcomeHeading(c.Game.Name))
	heading.SizeName = theme.SizeNameSubHeadingText
	w.rootLabel = widget.NewLabel(w.root)
	w.rootLabel.Wrapping = fyne.TextWrapBreak
	change := widget.NewButton(changeText, w.pickRoot)
	folderIs := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	folderIs.Wrapping = fyne.TextWrapWord
	folderIs.SetText(fmt.Sprintf(installFolderIs, hostinfo.NeedBytes>>30))
	upd := widget.NewCheck(updateCheckText, func(on bool) {
		w.settings.NoUpdateCheck = !on
		w.saveSettings()
	})
	upd.SetChecked(!w.settings.NoUpdateCheck)
	items := []fyne.CanvasObject{
		heading,
		para(welcomeIntro(c.Game.Name, c.Targets)),
		statement(noGameLead, noGameText(c.Game.Name)),
		statement(downloadsLead, downloadsText),
		statement(sendsLead, sendsText),
		container.NewBorder(nil, nil, bold(installFolder), nil, folderIs),
		container.NewBorder(nil, nil, nil, change, w.rootLabel),
		upd,
	}
	if w.firstRun {
		items = append(items, para(unsignedText))
	}
	return &page{title: titleWelcome, obj: container.NewVScroll(container.NewVBox(items...)),
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
		p, err := zenity.SelectFile(zenity.Directory(), zenity.Title(pickFolderTitle), zenity.Filename(w.root))
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
	w.checksLog = newLogPane(func(s string) { w.Logf("checks: %s", s) })
	w.checksRows = container.NewVBox()
	w.checksBar = newThinBar(0)
	w.retry = widget.NewButtonWithIcon(retryText, theme.ViewRefreshIcon(), w.runChecks)
	// The bar, Retry and the log stay in view while the table scrolls.
	bottom := container.NewVBox(container.NewBorder(nil, nil, nil, w.retry, w.checksBar), w.checksLog.obj)
	w.checksView = container.NewVScroll(w.checksRows)
	// No sentence above the table: the rows' icons, colours and statuses
	// and the log's last line say how the checks went (the user's
	// 2026-10-15 decision).
	obj := container.NewBorder(nil, bottom, nil, nil, w.checksView)
	w.checksLog.relayout = obj.Refresh
	w.setRows(checkRows(w.opt.Cfg, nil, nil, nil, false))
	return &page{title: titleChecks, obj: obj, enter: func() {
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
	w.checksBar.SetValue(0)
	w.setRows(checkRows(w.opt.Cfg, nil, nil, nil, false))
	w.refreshNav()
	go func() {
		log := w.checksLog.Append
		var d *runner.Doctor
		// doctor is one more step after Prepare's: the bar is full only
		// when its report has parsed.
		step := func(done, total int) {
			fyne.Do(func() { w.checksBar.SetValue(float64(done) / float64(total+1)) })
		}
		env, err := Prepare(context.Background(), w.opt.Cfg, w.opt.Pins, w.root, w.opt.DevGame, log, step)
		if err == nil {
			log("doctor: " + runner.Display(w.opt.Cfg.Game.Slug, []string{"doctor", "--json"}))
			d, err = env.Runner.Doctor(context.Background(), LogWriter(func(s string) { log("  " + s) }))
		}
		// The pane's one visible line ends on the outcome in plain words.
		if err != nil {
			w.checksLog.Fail(fmt.Sprintf(checksStopped, lowerFirst(plainPrepare(err))))
		} else {
			log(checksDone)
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

// showChecks fills the table from the report (or the failure).
func (w *Wizard) showChecks(err error) {
	if err != nil {
		w.checksBar.SetValue(0)
	} else {
		w.checksBar.SetValue(1)
	}
	w.setRows(checkRows(w.opt.Cfg, w.env, w.doctor, err, BrewPath() != ""))
}

// setRows draws the table: per row an icon for what it is about, the
// name, the state's icon and the status in plain words and, when there is more to say, a toggle that opens the detail
// and the fix under the row. The icon and the status take the state's
// colour, but the icon and the words carry the state alone too (D6).
func (w *Wizard) setRows(rows []checkRow) {
	for _, a := range w.activities {
		a.Stop()
	}
	w.activities = nil
	w.rowParts = map[string]rowParts{}
	// The names in one column, so the statuses line up as in a table.
	var nameW float32
	for _, r := range rows {
		nameW = max(nameW, widget.NewLabel(r.Name).MinSize().Width)
	}
	var objs []fyne.CanvasObject
	for _, r := range rows {
		objs = append(objs, w.rowView(r, nameW))
	}
	w.checksRows.Objects = objs
	w.checksRows.Refresh()
}

type rowParts struct {
	line   *tapRow
	toggle *widget.Button
	detail *fyne.Container
}

// tapRow makes a row's icon, name and status one tap target for its
// detail; the Details button beside it is the keyboard's way in.
type tapRow struct {
	widget.BaseWidget
	content fyne.CanvasObject
	onTap   func()
}

func newTapRow(content fyne.CanvasObject, onTap func()) *tapRow {
	r := &tapRow{content: content, onTap: onTap}
	r.ExtendBaseWidget(r)
	return r
}

func (r *tapRow) CreateRenderer() fyne.WidgetRenderer { return widget.NewSimpleRenderer(r.content) }
func (r *tapRow) Tapped(*fyne.PointEvent)             { r.onTap() }
func (r *tapRow) Cursor() desktop.Cursor              { return desktop.PointerCursor }

func (w *Wizard) rowView(r checkRow, nameW float32) fyne.CanvasObject {
	var icon fyne.CanvasObject
	switch r.State {
	case stateBusy:
		a := widget.NewActivity()
		a.Start()
		w.activities = append(w.activities, a)
		icon = container.NewGridWrap(fyne.NewSquareSize(theme.IconInlineSize()), a)
	default:
		icon = widget.NewIcon(stateIcon(r.State))
	}
	name := widget.NewLabel(r.Name)
	status := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	status.Truncation = fyne.TextTruncateEllipsis
	status.SetText(r.Status)
	status.SetColor(stateColor(r.State))
	// What the row is about (a platform's glyph) leads the name; the
	// state's icon and colour sit with the status.
	lead := container.NewHBox(widget.NewIcon(rowIcon(r)), container.NewGridWrap(fyne.NewSize(nameW, name.MinSize().Height), name))
	line := container.NewBorder(nil, nil, lead, nil, container.NewBorder(nil, nil, icon, nil, status))
	if !r.hasDetail() {
		return container.NewVBox(line, widget.NewSeparator())
	}

	var inner []fyne.CanvasObject
	if r.Detail != "" {
		inner = append(inner, para(r.Detail))
	}
	for _, a := range r.Actions {
		inner = append(inner, container.NewHBox(w.actionButton(a)))
	}
	if len(r.Raw) > 0 {
		inner = append(inner, mono(strings.Join(r.Raw, "\n")))
	}
	indent := theme.IconInlineSize() + 2*theme.Padding()
	detail := container.New(layout.NewCustomPaddedLayout(0, theme.Padding(), indent, 0), container.NewVBox(inner...))
	toggle := widget.NewButtonWithIcon(detailsText, theme.MenuDropDownIcon(), nil)
	toggle.Importance = widget.LowImportance
	toggle.IconPlacement = widget.ButtonIconTrailingText
	tap := newTapRow(line, nil)
	box := container.NewVBox(container.NewBorder(nil, nil, nil, toggle, tap), detail, widget.NewSeparator())
	set := func(open bool) {
		if open {
			detail.Show()
			toggle.SetText(hideDetailsText)
			toggle.SetIcon(theme.MenuDropUpIcon())
		} else {
			detail.Hide()
			toggle.SetText(detailsText)
			toggle.SetIcon(theme.MenuDropDownIcon())
		}
		box.Refresh()
		w.checksRows.Refresh()
		if open {
			w.reveal(box)
		}
	}
	toggle.OnTapped = func() { set(!detail.Visible()) }
	tap.onTap = toggle.OnTapped
	set(r.Open)
	w.rowParts[r.Name] = rowParts{line: tap, toggle: toggle, detail: detail}
	return box
}

// reveal scrolls the checks table so an opened row and its detail are in
// view: the last rows open below the fold.
func (w *Wizard) reveal(row fyne.CanvasObject) {
	view := w.checksView.Size().Height
	top, bottom := row.Position().Y, row.Position().Y+row.Size().Height
	off := w.checksView.Offset.Y
	switch {
	case view <= 0:
		return
	case bottom-off > view:
		off = min(top, bottom-view)
	case top < off:
		off = top
	}
	w.checksView.ScrollToOffset(fyne.NewPos(0, off))
}

// actionButton is the fix for a blocker, run where the OS asks for any
// password itself (D11): never by the wizard.
func (w *Wizard) actionButton(a runner.Action) fyne.CanvasObject {
	switch a.Kind {
	case "clt":
		return widget.NewButton(installCLT, func() {
			if err := repo.InstallDeveloperTools(); err != nil {
				w.Logf("xcode-select --install: %v", err)
			}
			w.checksLog.Append(cltOpened)
			go func() {
				for i := 0; i < 720 && !repo.HaveDeveloperTools(); i++ {
					time.Sleep(5 * time.Second)
				}
				fyne.Do(w.runChecks)
			}()
		})
	case "brew":
		if BrewPath() == "" {
			return widget.NewButton(installHomebrew, w.installHomebrew)
		}
		return widget.NewButton(fmt.Sprintf(installBrew, strings.TrimPrefix(a.Command, "brew install ")),
			func() { w.runBrew(a.Command) })
	case "sudo":
		return widget.NewButton(fmt.Sprintf(runInTerminal, a.Command), func() {
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
		w.checksLog.Append(homebrewOpened)
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
		cmd.Env = brewEnv()
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
	w.dumpEntry.SetPlaceHolder(dumpPlaceholder)
	w.dumpEntry.OnChanged = func(string) { w.dumpNote.SetText(""); w.dumpNote.Hide(); w.refreshNav() }
	w.dumpNote = para("")
	w.dumpNote.Importance = widget.DangerImportance
	w.dumpNote.Hide()
	items := []fyne.CanvasObject{para(dumpIntro(w.opt.Cfg.Game.Name))}
	if !w.opt.NoDialog {
		items = append(items, container.NewHBox(
			widget.NewButton(chooseImage, func() { w.pickDump(false) }),
			widget.NewButton(chooseFolder, func() { w.pickDump(true) })))
	}
	items = append(items, w.dumpEntry, w.dumpNote)
	return &page{title: titleDump, obj: container.NewVBox(items...),
		ready: func() bool { return strings.TrimSpace(w.dumpEntry.Text) != "" }}
}

func (w *Wizard) pickDump(folder bool) {
	opts := []zenity.Option{zenity.Title(dumpDialogTitle(w.opt.Cfg.Game.Name))}
	if folder {
		opts = append(opts, zenity.Directory())
	} else {
		opts = append(opts, zenity.FileFilters{{Name: disksTitle, Patterns: []string{"*.iso", "*.xiso"}, CaseFold: true}})
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

// playerError is an error whose text is a sentence for the player, shown
// as it is under the path; Go's error-string style does not apply to it.
type playerError string

func (e playerError) Error() string { return string(e) }

// checkDump is the path check before the CLI's own (the title, the files).
func (w *Wizard) checkDump() error {
	p := strings.TrimSpace(w.dumpEntry.Text)
	st, err := os.Stat(p)
	if err != nil {
		return playerError(fmt.Sprintf(dumpNothing, p))
	}
	need := uint64(hostinfo.NeedBytes)
	if st.IsDir() {
		if _, err := os.Stat(filepath.Join(p, "default.xbe")); err != nil {
			return playerError(fmt.Sprintf(dumpNoXBE, p))
		}
	} else {
		need += uint64(st.Size())
	}
	if free, err := hostinfo.Free(w.root); err == nil && free < need {
		return playerError(plainPrepare(&SpaceError{Free: free, Need: need}))
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
	return &page{title: titleTarget, obj: container.NewVScroll(w.targetBox), enter: w.fillTargets,
		ready: func() bool { return slices.Contains(w.okTargets(), w.st.Target) }}
}

func (w *Wizard) fillTargets() {
	ok := w.okTargets()
	if !slices.Contains(ok, w.st.Target) && len(ok) > 0 {
		w.st.Target = ok[0]
	}
	// One single-choice group per platform, so each can carry its glyph;
	// choosing one clears the others, as one group would.
	w.targetRadios = map[string]*widget.RadioGroup{}
	items := []fyne.CanvasObject{para(targetIntro)}
	for _, t := range ok {
		t := t
		r := widget.NewRadioGroup([]string{model.TargetLabels[t]}, nil)
		r.Required = true
		if t == w.st.Target {
			r.SetSelected(model.TargetLabels[t])
		}
		r.OnChanged = func(s string) {
			if s == "" {
				return
			}
			w.st.Target = t
			for other, g := range w.targetRadios {
				if other != t {
					g.SetSelected("")
				}
			}
			w.refreshNav()
		}
		w.targetRadios[t] = r
		items = append(items, container.NewHBox(widget.NewIcon(platformIcon(t)), r))
	}
	if w.doctor != nil {
		for _, t := range w.opt.Cfg.Targets {
			if good, _ := w.doctor.Target(t); !good {
				items = append(items, container.NewBorder(nil, nil, widget.NewIcon(platformIcon(t)), nil,
					blockedLine(fmt.Sprintf(targetBlocked, model.TargetLabels[t]))))
			}
		}
	}
	if hostinfo.IsDeck() {
		items = append(items, para(targetDeck))
	}
	w.targetBox.Objects = items
	w.targetBox.Refresh()
}

// --- options

func (w *Wizard) optionsPage() *page {
	var items []fyne.CanvasObject
	for _, o := range w.opt.Cfg.Options {
		objs, sync := w.optionControl(o)
		items = append(items, objs...)
		w.optionsSync = append(w.optionsSync, sync)
		if h := w.optionHint(o); h != nil {
			items = append(items, h)
		}
	}
	if len(items) == 0 {
		items = append(items, para(optionsNone))
	} else {
		items = append([]fyne.CanvasObject{para(optionsIntro)}, items...)
	}
	w.optionsNote = newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	w.optionsNote.Wrapping = fyne.TextWrapBreak
	return &page{title: titleOptions, obj: container.NewBorder(nil, w.optionsNote, nil, nil,
		container.NewVScroll(container.NewVBox(items...))), enter: w.fillOptions}
}

// optionControl is one option's control: a checkbox for a bool, radio
// buttons for a short choice and a drop-down for a longer one, a slider
// with its value beside it for a range. sync sets the control from the
// state, which a saved seed or the tests may have changed since.
func (w *Wizard) optionControl(o gamecfg.Option) (objs []fyne.CanvasObject, sync func()) {
	// A value the state refuses is a config the loader should not have
	// let through: say so rather than show one value and build another.
	set := func(v string) {
		if v == "" {
			return
		}
		if err := w.st.Set(o.Key, v); err != nil {
			w.Logf("option %s: %v", o.Key, err)
			w.errorBox(err)
		}
	}
	switch o.Kind {
	case "bool":
		c := widget.NewCheck(o.Label, func(on bool) { set(fmt.Sprint(on)) })
		sync = func() { c.SetChecked(w.st.Value(o.Key) == "true") }
		sync()
		return []fyne.CanvasObject{c}, sync
	case "range":
		// The slider runs over the values' indexes, 0 to n-1 by 1: Fyne
		// snaps to multiples of Step counted from 0, not from Min, so a
		// range from 1 by 2 driven directly would land on 2 and 4.
		vals := o.Choices()
		s := widget.NewSlider(0, float64(max(len(vals)-1, 1)))
		s.Step = 1
		readout := widget.NewLabel("")
		show := func() { readout.SetText(optionText(o, w.st.Value(o.Key))) }
		s.OnChanged = func(f float64) {
			if i := int(math.Round(f)); i >= 0 && i < len(vals) {
				set(vals[i])
			}
			show()
		}
		sync = func() {
			if i := slices.Index(vals, w.st.Value(o.Key)); i >= 0 {
				s.SetValue(float64(i))
			}
			show()
		}
		sync()
		return []fyne.CanvasObject{widget.NewLabel(o.Label),
			container.NewBorder(nil, nil, nil, readout, s)}, sync
	}
	if len(o.Values) > 3 {
		sel := widget.NewSelect(o.Values, set)
		sync = func() { sel.SetSelected(w.st.Value(o.Key)) }
		sync()
		return []fyne.CanvasObject{widget.NewLabel(o.Label), sel}, sync
	}
	r := widget.NewRadioGroup(o.Values, set)
	r.Horizontal = true
	r.Required = true
	sync = func() { r.SetSelected(w.st.Value(o.Key)) }
	sync()
	return []fyne.CanvasObject{widget.NewLabel(o.Label), r}, sync
}

// optionHint is an option's hint in quiet text and its link after it,
// lined up with the control's own text: a checkbox's words start after
// its box. nil when the option has neither.
func (w *Wizard) optionHint(o gamecfg.Option) fyne.CanvasObject {
	var parts []fyne.CanvasObject
	if o.Hint != "" {
		h := newQuiet(theme.SizeNameCaptionText, fyne.TextAlignLeading, fyne.TextStyle{})
		h.Wrapping = fyne.TextWrapWord
		h.SetText(o.Hint)
		parts = append(parts, h)
	}
	if l := o.HintLink; l != nil {
		// The address comes from the embedded config, the game's own at
		// the release tag; the config loader took only https.
		if u, err := url.Parse(l.URL); err == nil {
			link := widget.NewHyperlink(l.Text, u)
			link.OnTapped = func() { w.openURL(l.URL) }
			parts = append(parts, link)
		}
	}
	if len(parts) == 0 {
		return nil
	}
	box := container.NewVBox(parts...)
	if o.Kind != "bool" {
		return box
	}
	// The check's label starts after the focus square (icon plus inner
	// padding) and two borders; the hint's own inner padding is the rest.
	indent := theme.Size(theme.SizeNameInlineIcon) + 2*theme.Size(theme.SizeNameInputBorder)
	return container.New(layout.NewCustomPaddedLayout(0, 0, indent, 0), box)
}

// fillOptions sets the controls from the state and says where the chosen
// platform keeps config.toml, for the settings this page leaves out; the
// platform can change between visits.
func (w *Wizard) fillOptions() {
	for _, sync := range w.optionsSync {
		sync()
	}
	if p := ConfigPath(w.opt.Cfg, w.st.Target); p != "" {
		w.optionsNote.SetText(fmt.Sprintf(optionsLater, p))
		return
	}
	w.optionsNote.SetText("")
}

// ConfigPath is the player's config.toml on a target, as that target
// writes it: the game's data folder and config/config.toml, in the
// target's own separator. "" when the game names no data folder there.
func ConfigPath(cfg *gamecfg.Config, target string) string {
	d := cfg.Data[target]
	if d == "" {
		return ""
	}
	sep := "/"
	if target == "windows" {
		sep = `\`
	}
	return strings.TrimRight(d, `/\`) + sep + "config" + sep + "config.toml"
}

// --- summary

func (w *Wizard) summaryPage() *page {
	w.summaryBox = container.NewVBox()
	w.summaryTech = mono("")
	w.summaryAcc = widget.NewAccordion(widget.NewAccordionItem(technicalDetails, w.summaryTech))
	return &page{title: titleSummary, obj: container.NewVScroll(container.NewVBox(w.summaryBox, w.summaryAcc)),
		next: summaryNext, enter: w.fillSummary}
}

func (w *Wizard) seedArg() string {
	if w.st.Seed() == "" {
		return ""
	}
	return SeedPath(w.root)
}

// optionText is an option's value as the summary shows it.
func optionText(o gamecfg.Option, v string) string {
	if l := o.Labels[v]; l != "" {
		return fmt.Sprintf(optionLabelled, v, l)
	}
	if o.Kind == "bool" {
		if v == "true" {
			return "on"
		}
		return "off"
	}
	return v
}

func (w *Wizard) fillSummary() {
	c := w.opt.Cfg
	items := []fyne.CanvasObject{
		para(fmt.Sprintf(summaryGame, c.Game.Name, c.Release.Tag)),
		para(fmt.Sprintf(summaryFolder, w.root)),
		para(fmt.Sprintf(summaryPlatform, model.TargetLabels[w.st.Target])),
	}
	var changed []fyne.CanvasObject
	for _, o := range c.Options {
		if v := w.st.Value(o.Key); v != o.DefaultValue() {
			changed = append(changed, para(fmt.Sprintf(summarySetting, o.Label, optionText(o, v))))
		}
	}
	if len(changed) == 0 {
		items = append(items, para(summaryShipped))
	} else {
		items = append(items, para(summarySettings),
			container.New(layout.NewCustomPaddedLayout(0, 0, 2*theme.Padding(), 0), container.NewVBox(changed...)))
	}
	items = append(items, para(fmt.Sprintf(summaryData, c.Data[w.st.Target])))
	w.summaryBox.Objects = items
	w.summaryBox.Refresh()

	// The technical details: what someone helping the player asks for.
	var b strings.Builder
	b.WriteString(w.pinsText() + "\n\n")
	b.WriteString(techSteps + "\n")
	for _, s := range w.st.Steps(w.seedArg()) {
		b.WriteString("  " + runner.Display(c.Game.Slug, s) + "\n")
	}
	if seed := w.st.Seed(); seed != "" {
		b.WriteString("\n" + techSeed + "\n" + seed)
	} else {
		b.WriteString("\n" + techNoSeed + "\n")
	}
	w.summaryTech.SetText(strings.TrimRight(b.String(), "\n"))
}

// --- build

func (w *Wizard) buildPage() *page {
	w.buildLog = newLogPane(nil)
	w.bar = widget.NewProgressBar()
	w.stepLabel = widget.NewLabel("")
	w.cancelBtn = widget.NewButton(cancelText, func() {
		dialog.ShowConfirm(stopTitle, stopText, func(ok bool) {
			if ok {
				w.cancelBuild()
			}
		}, w.win)
	})
	top := container.NewVBox(para(buildIntro), w.stepLabel, container.NewBorder(nil, nil, nil, w.cancelBtn, w.bar))
	// Collapsed, the pane sits at the bottom; expanded, it takes the space
	// under the bar instead of leaving a gap above it.
	holder := container.NewStack()
	place := func() {
		if w.buildLog.open {
			holder.Objects = []fyne.CanvasObject{w.buildLog.obj}
		} else {
			holder.Objects = []fyne.CanvasObject{container.NewBorder(nil, w.buildLog.obj, nil, nil)}
		}
		holder.Refresh()
	}
	place()
	obj := container.NewBorder(top, nil, nil, nil, holder)
	w.buildLog.relayout = place
	return &page{title: titleBuild, obj: obj, enter: w.startBuild, ready: func() bool { return !w.building }, next: nextText}
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
	w.stepLabel.SetText(buildStarting)
	w.refreshNav()
	seed := w.seedArg()
	steps := w.st.Steps(seed)
	go func() {
		err := w.runSteps(seed, steps)
		if err != nil {
			w.Logf("build: %v", err)
		}
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
			fyne.Do(func() { w.stepLabel.SetText(buildPrepare) })
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
		return buildPlanning
	}
	s := fmt.Sprintf(buildStep, p.Index, p.Total, p.Stage)
	if p.Label != "" {
		s += " (" + p.Label + ")"
	}
	return s
}

// --- done

func (w *Wizard) donePage() *page {
	w.doneBox = container.NewVBox()
	return &page{title: titleDone, obj: container.NewVScroll(w.doneBox), next: doneNext}
}

func (w *Wizard) fillDone() {
	c := w.opt.Cfg
	var items []fyne.CanvasObject
	if w.buildErr == nil && w.prog.Bundle != "" {
		bundle := w.prog.Bundle
		w.pages[pDone].title = titleDone
		items = append(items, bold(doneReady), mono(bundle), para(installText[w.st.Target]))
		row := []fyne.CanvasObject{widget.NewButton(showBundle, func() { w.openPath(filepath.Dir(bundle)) })}
		if w.st.Target == hostinfo.NativeTarget() {
			row = append(row, widget.NewButton(installNowText, func() { w.installNow(bundle) }))
		}
		items = append(items, container.NewHBox(row...), para(fmt.Sprintf(doneData, c.Data[w.st.Target])))
	} else {
		w.pages[pDone].title = titleStopped
		items = append(items, bold(doneFailed))
		if w.prog.Failed != "" {
			items = append(items, para(fmt.Sprintf(doneStoppedAt, w.prog.Failed)))
		}
		logDir := filepath.Join(w.env.Game, "build-logs")
		if w.prog.Log != "" {
			items = append(items, para(fmt.Sprintf(doneLog, w.prog.Log)))
			logDir = filepath.Dir(w.prog.Log)
		}
		items = append(items, container.NewHBox(
			widget.NewButton(openLogFolder, func() { w.openPath(logDir) }),
			widget.NewButton(retryText, func() { w.show(pBuild) })),
			para(doneRetry))
	}
	items = append(items, para(bundleRule(c.Game.Name)))
	w.doneBox.Objects = items
	w.doneBox.Refresh()
	if w.cur == pDone {
		w.title.SetText(w.pages[pDone].title)
	}
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
