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
	"fyne.io/fyne/v2/canvas"
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
	opt Options
	app fyne.App
	win fyne.Window
	// stopD and creditsD are the stop and credits dialogs while they
	// show: they follow the window's size.
	stopD    dialog.Dialog
	creditsD dialog.Dialog
	st       *model.State
	setDir   string
	settings hostinfo.Settings
	firstRun bool
	root     string
	// out is the output folder; outDefault that it is DefaultOut's, so a
	// new build folder or a moved wizard moves it too.
	out        string
	outDefault bool
	// free and sameVolume are hostinfo's; the tests replace them.
	free       func(string) (uint64, error)
	sameVolume func(a, b string) bool

	pages   []*page
	cur     int
	title   *widget.Label
	step    *quietText
	stepBar *thinBar
	info    *widget.Button // the credits; Welcome lists them and hides it
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
	outLabel  *widget.Label
	// outNote says why the output folder cannot be used (outClash).
	outNote *widget.Label

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
	// the font check: fonts are the options' font lines, fontRun counts
	// checks so a late answer from an older one is dropped, fontCheck
	// asks the CLI (the tests replace it).
	fonts     []*fontLines
	fontRun   int
	fontCheck func(runner.Runner) (*runner.Font, error)
	// openLink opens a hint link; async runs the font check off the UI
	// thread. The tests replace both.
	openLink    func(string)
	async       func(func())
	summaryBox  *fyne.Container
	summaryTech *widget.Label
	summaryAcc  *widget.Accordion

	// build and done
	buildLog *logPane
	bar      *thinBar
	overall  *quietText
	// buildStart and buildEnd are the build's clock, on the UI thread.
	buildStart, buildEnd time.Time
	// parts is the build's checklist, fed from the build's goroutine
	// under partsMu; partsArmed is one refresh pending, partsAfter
	// schedules it (the tests replace it, as the log pane's after).
	partsMu    sync.Mutex
	parts      *runner.Checklist
	partsArmed bool
	partsAfter func(time.Duration, func())
	buildParts *partsView
	doneParts  *partsView
	// clockRun counts builds, on the main thread: a tick queued before
	// its build ended sees a newer count, or building false, and leaves
	// the final clocks alone.
	clockRun  int
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
	w.free, w.sameVolume = hostinfo.Free, hostinfo.SameVolume
	w.openLink = w.openURL
	w.async = func(f func()) { go f() }
	w.fontCheck = w.askFont
	w.partsAfter = uiAfter
	var why string
	w.out, why = hostinfo.ResolveOut(w.settings.Out, w.root)
	w.outDefault = w.settings.Out == "" || why != ""
	if why != "" {
		w.Logf("output folder: %s; using %s", why, w.out)
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
	page := container.NewBorder(w.header(), w.bottom(), nil, nil, container.NewPadded(w.body))
	w.win.SetContent(container.NewStack(newSizeWatch(w.fitDialogs), page))
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
	w.info = widget.NewButtonWithIcon("", infoIcon(), func() { d, _ := w.creditsDialog(); d.Show() })
	w.info.Importance = widget.LowImportance
	line := container.NewBorder(nil, nil, w.title,
		container.NewHBox(container.NewVBox(layout.NewSpacer(), w.step), w.info))
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
	if i == pWelcome {
		w.info.Hide()
	} else {
		w.info.Show()
	}
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
	folderIs.SetText(fmt.Sprintf(buildFolderIs, hostinfo.NeedBytes>>30))
	w.outLabel = widget.NewLabel(w.out)
	w.outLabel.Wrapping = fyne.TextWrapBreak
	w.outNote = para("")
	w.outNote.Importance = widget.DangerImportance
	w.outNote.Hide()
	changeOut := widget.NewButton(changeText, w.pickOut)
	outIs := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	outIs.Wrapping = fyne.TextWrapWord
	outIs.SetText(fmt.Sprintf(outputFolderIs, 4))
	upd := widget.NewCheck(updateCheckText, func(on bool) {
		w.settings.NoUpdateCheck = !on
		w.saveSettings()
	})
	upd.SetChecked(!w.settings.NoUpdateCheck)
	items := []fyne.CanvasObject{
		heading,
		para(welcomeIntro(c.Game.Name, c.Targets)),
		bold(creditsHeading),
		w.welcomeCredits(),
		statement(noGameLead, noGameText(c.Game.Name)),
		statement(downloadsLead, downloadsText),
		statement(sendsLead, sendsText),
		container.NewBorder(nil, nil, bold(buildFolder), nil, folderIs),
		container.NewBorder(nil, nil, nil, change, w.rootLabel),
		container.NewBorder(nil, nil, bold(outputFolder), nil, outIs),
		container.NewBorder(nil, nil, nil, changeOut, w.outLabel),
		w.outNote,
		upd,
	}
	if w.firstRun {
		items = append(items, para(unsignedText))
	}
	w.showOutClash()
	return &page{title: titleWelcome, obj: container.NewVScroll(container.NewVBox(items...)), enter: w.showOutClash,
		ready: func() bool { return w.root != "" && w.out != "" && w.outClash() == "" }}
}

// outClash is the folder the output folder overlaps, or "": the game's
// data folder, the build folder's src or the dump folder (hostinfo.OutClash).
func (w *Wizard) outClash() string {
	data := hostinfo.ExpandData(w.opt.Cfg.Data[hostinfo.NativeTarget()])
	return hostinfo.OutClash(w.out, w.root, data, w.st.Dump)
}

// showOutClash puts the reason under the output folder's row.
func (w *Wizard) showOutClash() {
	if w.outNote == nil {
		return
	}
	if msg := outClashText[w.outClash()]; msg != "" {
		w.outNote.SetText(msg)
		w.outNote.Show()
	} else {
		w.outNote.SetText("")
		w.outNote.Hide()
	}
}

func (w *Wizard) saveSettings() {
	if err := hostinfo.SaveSettings(w.setDir, w.settings); err != nil {
		w.Logf("settings: %v", err)
	}
}

func (w *Wizard) pickRoot() {
	w.pickDir(pickFolderTitle, w.root, w.setRoot)
}

func (w *Wizard) setRoot(p string) {
	if p == "" {
		return
	}
	w.root = p
	w.settings.Root = p
	w.saveSettings()
	w.rootLabel.SetText(p)
	if w.outDefault {
		var why string
		w.out, why = hostinfo.DefaultOut(p)
		if why != "" {
			w.Logf("output folder: %s; using %s", why, w.out)
		}
		w.outLabel.SetText(w.out)
	}
	w.showOutClash()
	w.env, w.doctor = nil, nil
	w.refreshNav()
}

func (w *Wizard) pickOut() {
	w.pickDir(pickOutTitle, w.out, w.setOut)
}

// setOut keeps the player's output folder until it is changed again.
func (w *Wizard) setOut(p string) {
	if p == "" {
		return
	}
	w.out, w.outDefault = p, false
	w.settings.Out = p
	w.saveSettings()
	w.outLabel.SetText(p)
	w.showOutClash()
	w.refreshNav()
}

// pickDir is the system's folder dialog, Fyne's when there is none; set
// gets "" on a cancel.
func (w *Wizard) pickDir(title, start string, set func(string)) {
	if !w.opt.NoDialog {
		p, err := zenity.SelectFile(zenity.Directory(), zenity.Title(title), zenity.Filename(start))
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
	var dumpBytes uint64
	if st.IsDir() {
		if _, err := os.Stat(filepath.Join(p, "default.xbe")); err != nil {
			return playerError(fmt.Sprintf(dumpNoXBE, p))
		}
		dumpBytes = hostinfo.TreeSize(p)
	} else {
		dumpBytes = uint64(st.Size())
		need += dumpBytes
	}
	// The bundle carries the game files again, in the output folder: on
	// the build folder's disk the two needs draw on one free figure.
	bundle := hostinfo.BundleNeed(dumpBytes)
	if w.sameVolume(w.root, w.out) {
		need += bundle
	} else if free, err := w.free(w.out); err == nil && free < bundle {
		return playerError(fmt.Sprintf(dumpNoSpaceOut, float64(free)/1e9, float64(bundle)/1e9))
	}
	if free, err := w.free(w.root); err == nil && free < need {
		return playerError(plainPrepare(&SpaceError{Free: free, Need: need}))
	}
	abs, err := filepath.Abs(p)
	if err == nil {
		p = abs
	}
	if st.IsDir() && hostinfo.Overlaps(w.out, p) {
		return playerError(outClashText[hostinfo.ClashDump])
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
		h, font := w.optionHint(o)
		if h != nil {
			items = append(items, h)
		}
		if font != nil {
			w.fonts = append(w.fonts, font)
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

// fontLines are an option's font sentence and link, shown while the
// game's font is missing or the wizard cannot tell, and the line that
// says it is installed.
type fontLines struct {
	need  *fyne.Container
	found *quietText
}

// set shows the lines for the CLI's answer; nil is "cannot tell".
func (l *fontLines) set(f *runner.Font) {
	if f != nil && f.Found {
		l.found.SetText(fmt.Sprintf(optionFontFound, f.Font))
		l.found.Show()
		l.need.Hide()
		return
	}
	l.found.Hide()
	l.need.Show()
}

func hintText(text string) *quietText {
	h := newQuiet(theme.SizeNameCaptionText, fyne.TextAlignLeading, fyne.TextStyle{})
	h.Wrapping = fyne.TextWrapWord
	h.SetText(text)
	return h
}

// optionHint is an option's hint in quiet text and its link after it,
// lined up with the control's own text: a checkbox's words start after
// its box. With a font_hint, the font sentence and the link go in font,
// which the font check shows or hides. nil when the option has neither.
func (w *Wizard) optionHint(o gamecfg.Option) (obj fyne.CanvasObject, font *fontLines) {
	var parts []fyne.CanvasObject
	if o.Hint != "" {
		parts = append(parts, hintText(o.Hint))
	}
	link := w.hintLink(o.HintLink)
	if o.FontHint != "" {
		need := container.NewVBox(hintText(o.FontHint))
		if link != nil {
			need.Add(link)
		}
		found := hintText("")
		found.SetColor(theme.ColorNameSuccess)
		found.Hide()
		font = &fontLines{need: need, found: found}
		parts = append(parts, need, found)
	} else if link != nil {
		parts = append(parts, link)
	}
	if len(parts) == 0 {
		return nil, nil
	}
	box := container.NewVBox(parts...)
	if o.Kind != "bool" {
		return box, font
	}
	// The check's label starts after the focus square (icon plus inner
	// padding) and two borders; the hint's own inner padding is the rest.
	indent := theme.Size(theme.SizeNameInlineIcon) + 2*theme.Size(theme.SizeNameInputBorder)
	return container.New(layout.NewCustomPaddedLayout(0, 0, indent, 0), box), font
}

// hintLink is a hint's link and the external-link glyph after it, which
// says the browser opens. The glyph is a second tap target for the same
// address but takes no focus: the link is the one control, and its
// words are what a screen reader reads.
func (w *Wizard) hintLink(l *gamecfg.Link) fyne.CanvasObject {
	if l == nil {
		return nil
	}
	// The address comes from the embedded config, the game's own at the
	// release tag; the config loader took only https.
	u, err := url.Parse(l.URL)
	if err != nil {
		return nil
	}
	open := func() { w.openLink(l.URL) }
	link := widget.NewHyperlink(l.Text, u)
	link.OnTapped = open
	img := canvas.NewImageFromResource(externalLinkIcon())
	img.FillMode = canvas.ImageFillContain
	img.SetMinSize(fyne.NewSquareSize(theme.TextSize()))
	// The link's own inner padding already leaves a gap after its words.
	return container.New(layout.NewCustomPaddedHBoxLayout(-theme.InnerPadding()/2), link, newTapRow(img, open))
}

// fillOptions sets the controls from the state and says where the chosen
// platform keeps config.toml, for the settings this page leaves out; the
// platform can change between visits, and the font may have been
// installed since.
func (w *Wizard) fillOptions() {
	for _, sync := range w.optionsSync {
		sync()
	}
	w.checkFont()
	if !w.opt.Cfg.ReadsConfig() {
		// Pointing at config.toml would send the player to a file this
		// build never reads.
		w.optionsNote.SetText(optionsNoConfig)
		return
	}
	if p := ConfigPath(w.opt.Cfg, w.st.Target); p != "" {
		w.optionsNote.SetText(fmt.Sprintf(optionsLater, p))
		return
	}
	w.optionsNote.SetText("")
}

// checkFont asks the CLI whether the game's font is installed, off the UI
// thread. The lines keep what they show until the answer; a check that a
// newer one overtook is dropped.
func (w *Wizard) checkFont() {
	if len(w.fonts) == 0 {
		return
	}
	w.fontRun++
	run := w.fontRun
	if w.env == nil {
		w.Logf("font check: no checks yet")
		w.showFont(nil)
		return
	}
	r := w.env.Runner
	w.async(func() {
		f, err := w.fontCheck(r)
		fyne.Do(func() {
			if run != w.fontRun {
				return
			}
			if err != nil {
				w.Logf("font check: %v", err)
				f = nil
			} else {
				w.Logf("font check: %s found=%v %s", f.Font, f.Found, f.Path)
			}
			w.showFont(f)
		})
	})
}

func (w *Wizard) showFont(f *runner.Font) {
	for _, l := range w.fonts {
		l.set(f)
	}
}

// askFont is `wizard font --json` through the game's bootstrap.
func (w *Wizard) askFont(r runner.Runner) (*runner.Font, error) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	return r.Font(ctx, LogWriter(func(s string) { w.Logf("font check: %s", s) }))
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
		para(fmt.Sprintf(summaryOut, w.out)),
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
	for _, s := range w.st.Steps(w.seedArg(), w.out) {
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
	w.bar = newThinBar(0)
	w.bar.Height = 10
	w.cancelBtn = widget.NewButton(cancelText, func() {
		d, _ := w.stopDialog()
		d.Show()
	})
	w.overall = newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	w.overall.Truncation = fyne.TextTruncateEllipsis
	w.buildParts = newPartsView(w.partLines)
	// The bar is thinner than the button: centred beside it.
	bar := container.NewVBox(layout.NewSpacer(), w.bar, layout.NewSpacer())
	top := container.NewVBox(para(buildIntro), w.overall, container.NewBorder(nil, nil, nil, w.cancelBtn, bar))
	// The parts scroll between the bar and the Details pane, which holds
	// every line of the build together.
	obj := container.NewBorder(top, w.buildLog.obj, nil, nil, container.NewVScroll(w.buildParts.box))
	w.buildLog.relayout = obj.Refresh
	return &page{title: titleBuild, obj: obj, enter: w.startBuild, ready: func() bool { return !w.building }, next: nextText}
}

// stopDialog asks before stopping the build: the buttons at the right,
// the way back first, and no default, so Enter never stops a build. body
// is the dialog's content, for the tests.
func (w *Wizard) stopDialog() (_ dialog.Dialog, body fyne.CanvasObject) {
	var d *dialog.CustomDialog
	back := widget.NewButtonWithIcon(stopBack, theme.NavigateBackIcon(), func() { d.Hide() })
	stop := widget.NewButtonWithIcon(stopConfirm, theme.MediaStopIcon(), func() {
		d.Hide()
		w.cancelBuild()
	})
	stop.Importance = widget.DangerImportance
	body = container.NewVBox(para(stopText), container.NewHBox(layout.NewSpacer(), back, stop))
	d = dialog.NewCustomWithoutButtons(stopTitle, body, w.win)
	d.SetOnClosed(func() {
		if w.stopD == d {
			w.stopD = nil
		}
	})
	w.stopD = d
	w.fitDialogs(w.win.Canvas().Size())
	return d, body
}

// stopWidth is the stop dialog's width in a window this wide.
func stopWidth(win float32) float32 { return max(min(420, win-48), 0) }

// fitDialogs sizes the open dialogs to a window this big, again whenever
// the window is resized: wrapped text asks for little width, so each gets
// its own, never more than the window has.
func (w *Wizard) fitDialogs(win fyne.Size) {
	if w.stopD != nil {
		w.stopD.Resize(fyne.NewSize(stopWidth(win.Width), 0))
	}
	if w.creditsD != nil {
		w.creditsD.Resize(creditsSize(win))
	}
}

// sizeWatch fills the window under its content and reports each new
// size: Fyne tells a window's content, not the app, about a resize.
type sizeWatch struct {
	widget.BaseWidget
	resized func(fyne.Size)
}

func newSizeWatch(resized func(fyne.Size)) *sizeWatch {
	s := &sizeWatch{resized: resized}
	s.ExtendBaseWidget(s)
	return s
}

func (s *sizeWatch) Resize(size fyne.Size) {
	if size != s.Size() {
		s.BaseWidget.Resize(size)
		s.resized(size)
	}
}

func (s *sizeWatch) CreateRenderer() fyne.WidgetRenderer {
	return widget.NewSimpleRenderer(&canvas.Rectangle{})
}

// partLines is every line part i kept, for its Copy button.
func (w *Wizard) partLines(i int) string {
	w.partsMu.Lock()
	defer w.partsMu.Unlock()
	if w.parts == nil || i >= len(w.parts.Parts) {
		return ""
	}
	return strings.Join(w.parts.Parts[i].Lines, "\n")
}

// armParts schedules one refresh of the parts; any goroutine.
func (w *Wizard) armParts() {
	w.partsMu.Lock()
	defer w.partsMu.Unlock()
	if !w.partsArmed {
		w.partsArmed = true
		w.partsAfter(paneDelay, w.flushParts)
	}
}

// snapParts is a copy of the parts and the share done, under the lock.
func (w *Wizard) snapParts() ([]runner.Part, float64) {
	w.partsMu.Lock()
	defer w.partsMu.Unlock()
	w.partsArmed = false
	if w.parts == nil {
		return nil, 0
	}
	return w.parts.Snapshot(partShown), w.parts.Fraction()
}

// flushParts shows the parts, the overall line and the bar, on the UI
// goroutine.
func (w *Wizard) flushParts() {
	snap, frac := w.snapParts()
	now := time.Now()
	if !w.building && !w.buildEnd.IsZero() {
		now = w.buildEnd
	}
	w.buildParts.show(snap, !w.building, now)
	w.overall.SetText(overallText(snap, frac, now.Sub(w.buildStart)))
	if frac > 0 && w.building {
		w.bar.SetValue(frac)
	}
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
	w.bar.StartPulse()
	w.cancelBtn.Enable()
	w.refreshNav()
	seed := w.seedArg()
	steps := w.st.Steps(seed, w.out)
	w.partsMu.Lock()
	w.parts = runner.NewChecklist(steps)
	w.partsMu.Unlock()
	w.buildParts.reset()
	w.doneParts.reset()
	w.buildStart, w.buildEnd = time.Now(), time.Time{}
	w.flushParts()
	done := make(chan struct{})
	w.clockRun++
	go w.tickClock(done, w.clockRun)
	go func() {
		err := w.runSteps(seed, steps)
		close(done)
		w.logBuildEnd(err)
		fyne.Do(func() {
			w.building = false
			w.buildErr = err
			w.buildEnd = time.Now()
			w.bar.StopPulse()
			w.flushParts()
			w.cancelBtn.Disable()
			w.fillDone()
			w.show(pDone)
		})
	}()
}

// tickClock moves the clocks every second until done is closed: the CLI's
// lines come in bursts, minutes apart in a long step.
func (w *Wizard) tickClock(done <-chan struct{}, run int) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			fyne.Do(func() { w.showTick(run) })
		}
	}
}

// showTick moves the clocks, unless the tick's build has ended since: the
// end set the final ones.
func (w *Wizard) showTick(run int) {
	if w.building && run == w.clockRun {
		w.flushParts()
	}
}

// partsDo changes the checklist under its lock and schedules a refresh.
func (w *Wizard) partsDo(f func(c *runner.Checklist)) {
	w.partsMu.Lock()
	if w.parts != nil {
		f(w.parts)
	}
	w.partsMu.Unlock()
	w.armParts()
}

func (w *Wizard) runSteps(seed string, steps [][]string) error {
	log := w.buildLog.Append
	if seed != "" {
		if err := os.WriteFile(seed, []byte(w.st.Seed()), 0o644); err != nil {
			return &buildFailure{Step: buildPrepare, Cmd: seed, Err: err}
		}
	}
	slug := w.opt.Cfg.Game.Slug
	cmd := ""
	for i, s := range steps {
		w.procMu.Lock()
		if w.canceled {
			w.procMu.Unlock()
			w.partsDo(func(c *runner.Checklist) { c.Stop(true, time.Now()) })
			return errCanceled
		}
		w.procMu.Unlock()
		cmd = runner.Display(slug, s)
		log("$ " + cmd)
		w.partsDo(func(c *runner.Checklist) { c.StartCommand(i, time.Now()); c.Feed("$ "+cmd, time.Now()) })
		p, err := w.env.Runner.Start(context.Background(), []string{"CI=1"}, func(line string) {
			log(line)
			w.prog.Feed(line)
			w.partsDo(func(c *runner.Checklist) { c.Feed(line, time.Now()) })
		}, s...)
		if err != nil {
			w.partsDo(func(c *runner.Checklist) { c.EndCommand(err, false, time.Now()) })
			return w.failure(cmd, err)
		}
		w.procMu.Lock()
		w.proc = p
		w.procMu.Unlock()
		err = p.Wait()
		w.procMu.Lock()
		w.proc = nil
		canceled := w.canceled
		w.procMu.Unlock()
		w.partsDo(func(c *runner.Checklist) { c.EndCommand(err, canceled, time.Now()) })
		if canceled {
			return errCanceled
		}
		if err != nil {
			return w.failure(cmd, err)
		}
	}
	if w.prog.Bundle == "" {
		return w.failure(cmd, errNoBundle)
	}
	return nil
}

// failure is the build stopping in cmd: where, in the player's words, and
// the log's last lines.
func (w *Wizard) failure(cmd string, err error) *buildFailure {
	return &buildFailure{Step: failedStep(w.prog), Cmd: cmd, Err: err, Tail: w.buildLog.Tail(tailLines)}
}

// failedStep is where the build stopped: the CLI's FAILED line, else the
// running step inside its stage, else the stage, else before the plan.
func failedStep(p *runner.Progress) string {
	switch {
	case p.Failed != "":
		return p.Failed
	case p.Stage != "" && p.Label != "" && !strings.HasPrefix(p.Label, p.Stage+":"):
		return p.Stage + ": " + p.Label
	case p.Label != "":
		return p.Label
	case p.Stage != "":
		return p.Stage
	}
	return buildPrepare
}

// logBuildEnd is the build's entry in wizard.log: the failure with what
// the build said, the cancel, or the bundle.
func (w *Wizard) logBuildEnd(err error) {
	var f *buildFailure
	switch {
	case err == nil:
		w.Logf("build done: %s", w.prog.Bundle)
	case errors.Is(err, errCanceled):
		w.Logf("build canceled by the player")
	case errors.As(err, &f):
		msg := fmt.Sprintf("build stopped at %s: %s: %v", f.Step, f.Cmd, f.Err)
		if ex := failureExcerpt(f.Tail); len(ex) > 0 {
			msg += "\n  " + strings.Join(ex, "\n  ")
		}
		w.Logf("%s", msg)
	default:
		w.Logf("build: %v", err)
	}
}

// --- done

func (w *Wizard) donePage() *page {
	w.doneBox = container.NewVBox()
	w.doneParts = newPartsView(w.partLines)
	return &page{title: titleDone, obj: container.NewVScroll(w.doneBox), next: doneNext}
}

func (w *Wizard) fillDone() {
	c := w.opt.Cfg
	var items []fyne.CanvasObject
	var f *buildFailure
	retry := []fyne.CanvasObject{widget.NewButton(retryText, func() { w.show(pBuild) })}
	switch {
	case w.buildErr == nil && w.prog.Bundle != "":
		bundle := w.prog.Bundle
		w.pages[pDone].title = titleDone
		items = append(items, bold(doneReady), mono(bundle), para(installText[w.st.Target]))
		row := []fyne.CanvasObject{widget.NewButton(openBundleFolder, func() { w.openPath(filepath.Dir(bundle)) })}
		if w.st.Target == hostinfo.NativeTarget() {
			row = append(row, widget.NewButton(installNowText, func() { w.installNow(bundle) }))
		}
		items = append(items, container.NewHBox(row...), para(fmt.Sprintf(doneData, c.Data[w.st.Target])))
	case errors.Is(w.buildErr, errCanceled):
		w.pages[pDone].title = titleStopped
		items = append(items, bold(doneCanceled))
		items, _ = w.doneChecklist(items)
		items = append(items, container.NewHBox(retry...), para(doneRetry))
	default:
		w.pages[pDone].title = titleStopped
		items = append(items, bold(doneFailed))
		if !errors.As(w.buildErr, &f) {
			f = &buildFailure{Step: failedStep(w.prog), Err: w.buildErr, Tail: w.buildLog.Tail(tailLines)}
		}
		items = append(items, para(fmt.Sprintf(doneStoppedAt, f.Step)))
		var said bool
		items, said = w.doneChecklist(items)
		// Without a failed part (the build stopped before its first
		// command), what the build said stands on its own.
		if ex := failureExcerpt(f.Tail); len(ex) > 0 && !said {
			items = append(items, bold(doneSaid), mono(strings.Join(ex, "\n")))
		}
		var ee *exec.ExitError
		if errors.As(f.Err, &ee) {
			q := newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
			q.SetText(fmt.Sprintf(doneExit, ee.ExitCode()))
			items = append(items, q)
		}
		logDir := ""
		if w.prog.Log != "" {
			logDir = filepath.Dir(w.prog.Log)
		} else if w.env != nil {
			logDir = filepath.Join(w.env.Game, "build-logs")
		}
		row := retry
		if logDir != "" {
			items = append(items, para(fmt.Sprintf(doneLogs, logDir)))
			row = append([]fyne.CanvasObject{widget.NewButton(openLogFolder, func() { w.openPath(logDir) })}, retry...)
		}
		items = append(items, container.NewHBox(row...), para(doneRetry))
	}
	items = append(items, spaced(noticeBox(noticeLead, fmt.Sprintf(noticeBody, c.Game.Name), noticeTail)))
	w.doneBox.Objects = items
	w.doneBox.Refresh()
	if w.cur == pDone {
		w.title.SetText(w.pages[pDone].title)
	}
}

// doneChecklist adds the build's parts as they ended, the failed one open
// with what it said; said is whether a failed part has lines to show.
func (w *Wizard) doneChecklist(items []fyne.CanvasObject) (_ []fyne.CanvasObject, said bool) {
	snap, _ := w.snapParts()
	if len(snap) == 0 {
		return items, false
	}
	end := w.buildEnd
	if end.IsZero() {
		end = time.Now()
	}
	w.doneParts.show(snap, true, end)
	for _, p := range snap {
		said = said || p.State == runner.PartFailed && len(p.Lines) > 0
	}
	return append(items, w.doneParts.box), said
}

// setupIn is the Windows bundle's setup program: the one *-setup.exe in
// the bundle folder. A listing, not a glob: the player's folder name may
// hold glob characters ("D:\Games [2026]").
func setupIn(bundle string) (string, error) {
	ents, err := os.ReadDir(bundle)
	if err != nil {
		return "", err
	}
	var m []string
	for _, e := range ents {
		if e.Type().IsRegular() && strings.HasSuffix(strings.ToLower(e.Name()), "-setup.exe") {
			m = append(m, filepath.Join(bundle, e.Name()))
		}
	}
	if len(m) != 1 {
		return "", fmt.Errorf("the bundle %s has %d setup programs, not one", bundle, len(m))
	}
	return m[0], nil
}

// installNow installs on this computer by the target's own route: the
// setup program, the .dmg, or install.sh in a terminal (it may ask about
// umu there).
func (w *Wizard) installNow(bundle string) {
	var err error
	switch w.st.Target {
	case "windows":
		var setup string
		if setup, err = setupIn(bundle); err == nil {
			err = exec.Command(setup).Start()
		}
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
