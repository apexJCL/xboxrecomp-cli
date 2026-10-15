package screens

import (
	"fmt"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/runner"
)

const (
	// partShown is what a part's open log shows: a selectable label is
	// laid out whole on every refresh, so it holds the tail; Copy takes
	// every line the part kept.
	partShown = 500
	// partLogHeight is an open part log's height: the rows around it stay
	// in view.
	partLogHeight = 160
)

// partIcons are the Tabler glyphs of the parts the wizard knows, by the
// CLI's name; any other part gets the checklist glyph.
var partIcons = map[string]string{
	"import":      "download",
	"enhance hud": "photo",
	"setup":       "settings",
	"generate":    "code",
	"build":       "hammer",
	"package":     "package",
}

// partName is a part in the player's words, or the CLI's name with its
// first letter in upper case for a part the wizard does not know.
func partName(cli string) string {
	if n := partNames[cli]; n != "" {
		return n
	}
	r, n := utf8.DecodeRuneInString(cli)
	return string(unicode.ToUpper(r)) + cli[n:]
}

func partIcon(cli string) fyne.Resource {
	f := partIcons[cli]
	if f == "" {
		f = "list-check"
	}
	return tablerIcon(f, theme.ColorNameForeground)
}

// partState is a state's glyph, colour and words: colour is never the
// only cue. The running part has the activity spinner instead of a glyph.
func partStateIcon(s runner.PartState) fyne.Resource {
	switch s {
	case runner.PartDone:
		return tablerIcon("circle-check", partColor(s))
	case runner.PartFailed:
		return tablerIcon("circle-x", partColor(s))
	case runner.PartSkipped:
		return tablerIcon("circle-minus", partColor(s))
	case runner.PartCanceled:
		return tablerIcon("player-stop", partColor(s))
	}
	return tablerIcon("circle-dashed", partColor(s))
}

func partColor(s runner.PartState) fyne.ThemeColorName {
	switch s {
	case runner.PartWorking:
		return theme.ColorNamePrimary
	case runner.PartDone:
		return theme.ColorNameSuccess
	case runner.PartFailed:
		return theme.ColorNameError
	case runner.PartCanceled:
		return theme.ColorNameWarning
	}
	return theme.ColorNamePlaceHolder
}

// partStatus is a row's status: the running part's step, its percentage
// or count and its clock; a finished part's outcome and time. final is a
// build that has ended: its pending parts were not run.
func partStatus(p runner.Part, final bool, now time.Time) string {
	took := fmtClock(p.Took(now))
	switch p.State {
	case runner.PartWorking:
		var s []string
		if p.Step != "" {
			s = append(s, p.Step)
		}
		switch {
		case p.Pct >= 0:
			s = append(s, fmt.Sprintf(buildPct, p.Pct))
		case p.Count != "":
			s = append(s, "["+p.Count+"]")
		}
		return strings.Join(append(s, took), buildSep)
	case runner.PartDone:
		return fmt.Sprintf(partDone, took)
	case runner.PartFailed:
		return fmt.Sprintf(partFailed, took)
	case runner.PartSkipped:
		return partSkipped
	case runner.PartCanceled:
		return fmt.Sprintf(partStopped, took)
	}
	if final {
		return partNotRun
	}
	return partWaiting
}

// overallText is the line above the parts: which part of how many, the
// share done once it moved, and the build's whole time. The part is the
// running one, else the last that ran.
func overallText(parts []runner.Part, frac float64, all time.Duration) string {
	var s []string
	at := -1
	for i, p := range parts {
		if p.State == runner.PartWorking {
			at = i
			break
		}
		if p.State != runner.PartPending && p.State != runner.PartSkipped {
			at = i
		}
	}
	if at >= 0 {
		s = append(s, fmt.Sprintf(buildPart, at+1, len(parts)))
	}
	if pct := int(frac * 100); pct > 0 {
		s = append(s, fmt.Sprintf(buildPct, pct))
	}
	return strings.Join(append(s, fmt.Sprintf(buildAll, fmtClock(all))), buildSep)
}

// partsView is the build's checklist: a row per part with its glyph, its
// name, its state and status, a thin bar under the running part, and the
// part's own log opened in place. The Building page and the Done page
// each have one; it is touched on the UI goroutine only.
type partsView struct {
	box  *fyne.Container
	rows []*partRow
	// names is the shape the rows were built for; open keeps a row's
	// log open across a rebuild, by name (the plan's first stage takes
	// over the placeholder's).
	names []string
	open  map[string]bool
	// copyPart is every line part i kept, for Copy.
	copyPart func(i int) string
}

type partRow struct {
	obj    *fyne.Container
	state  *fyne.Container // the state glyph or the spinner
	act    *widget.Activity
	shown  runner.PartState
	fresh  bool // shown is not set yet
	status *quietText
	bar    *thinBar
	toggle *widget.Button
	detail *fyne.Container
	said   *fyne.Container // the failure's excerpt, a failed part only
	saidT  *widget.Label   // nil until the part fails
	saySeq int             // the Seq the excerpt was taken at
	log    *widget.Label
	scroll *container.Scroll
	seq    int
	// lines are the last snapshot's; a closed log is filled from them
	// when it opens (stale), not on every refresh.
	lines []string
	stale bool
	auto  bool // the failure opened the log once already
	name  string
	nameL *widget.Label
}

func newPartsView(copyPart func(int) string) *partsView {
	return &partsView{box: container.NewVBox(), open: map[string]bool{}, copyPart: copyPart}
}

// show puts the parts on the rows: rebuilt when the parts changed shape,
// else updated in place.
func (v *partsView) show(parts []runner.Part, final bool, now time.Time) {
	names := make([]string, len(parts))
	for i, p := range parts {
		names[i] = p.Name
	}
	if v.rows == nil || !slices.Equal(names, v.names) {
		v.carryPlan(names)
		v.build(parts)
	}
	for i, p := range parts {
		v.rows[i].update(v, p, final, now)
	}
}

// reset drops the rows and what was open: the next show builds them
// afresh, for a new build.
func (v *partsView) reset() {
	v.stop()
	v.rows, v.names = nil, nil
	v.open = map[string]bool{}
	v.box.Objects = nil
	v.box.Refresh()
}

// carryPlan gives the part that replaced the plan placeholder, at the
// placeholder's place, the placeholder's open log: it is the same output,
// now under its stage's name.
func (v *partsView) carryPlan(names []string) {
	i := slices.Index(v.names, runner.PlanPart)
	if i < 0 || i >= len(names) || names[i] == runner.PlanPart {
		return
	}
	if v.open[runner.PlanPart] {
		v.open[names[i]] = true
	}
	delete(v.open, runner.PlanPart)
}

func (v *partsView) build(parts []runner.Part) {
	v.stop()
	v.names = nil
	v.rows = nil
	// The names in one column, so the statuses line up as in a table.
	var nameW float32
	for _, p := range parts {
		nameW = max(nameW, widget.NewLabel(partName(p.Name)).MinSize().Width)
	}
	var objs []fyne.CanvasObject
	for i, p := range parts {
		r := v.newRow(i, p.Name, nameW)
		v.rows = append(v.rows, r)
		v.names = append(v.names, p.Name)
		objs = append(objs, r.obj)
	}
	v.box.Objects = objs
	v.box.Refresh()
}

// stop ends the spinners: a dropped row's would run on.
func (v *partsView) stop() {
	for _, r := range v.rows {
		if r.act != nil {
			r.act.Stop()
		}
		r.bar.StopPulse()
	}
}

func (v *partsView) newRow(i int, cli string, nameW float32) *partRow {
	r := &partRow{name: cli, fresh: true, saySeq: -1}
	name := widget.NewLabel(partName(cli))
	r.nameL = name
	lead := container.NewHBox(widget.NewIcon(partIcon(cli)),
		container.NewGridWrap(fyne.NewSize(nameW, name.MinSize().Height), name))
	r.state = container.NewGridWrap(fyne.NewSquareSize(theme.IconInlineSize()))
	r.status = newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{})
	r.status.Truncation = fyne.TextTruncateEllipsis
	line := container.NewBorder(nil, nil, lead, nil, container.NewBorder(nil, nil, r.state, nil, r.status))
	r.toggle = widget.NewButtonWithIcon(partLog, theme.MenuDropDownIcon(), nil)
	r.toggle.Importance = widget.LowImportance
	r.toggle.IconPlacement = widget.ButtonIconTrailingText
	r.bar = newThinBar(0)
	r.bar.Hide()

	// Filled for a failed part only, so no other row carries the words.
	r.said = container.NewVBox()
	r.log = widget.NewLabel("")
	r.log.TextStyle = fyne.TextStyle{Monospace: true}
	r.log.Selectable = true
	r.scroll = container.NewScroll(r.log)
	r.scroll.SetMinSize(fyne.NewSize(0, partLogHeight))
	cp := widget.NewButtonWithIcon(copyText, theme.ContentCopyIcon(), func() {
		fyne.CurrentApp().Clipboard().SetContent(v.copyPart(i))
	})
	cp.Importance = widget.LowImportance
	indent := theme.IconInlineSize() + 2*theme.Padding()
	r.detail = container.New(layout.NewCustomPaddedLayout(0, theme.Padding(), indent, 0),
		container.NewVBox(r.said, container.NewBorder(nil, container.NewHBox(layout.NewSpacer(), cp), nil, nil, r.scroll)))
	r.detail.Hide()

	r.toggle.OnTapped = func() { r.setOpen(v, !r.detail.Visible()) }
	tap := newTapRow(line, r.toggle.OnTapped)
	r.obj = container.NewVBox(container.NewBorder(nil, nil, nil, r.toggle, tap), r.bar, r.detail, widget.NewSeparator())
	if v.open[cli] {
		r.setOpen(v, true)
	}
	return r
}

func (r *partRow) setOpen(v *partsView, open bool) {
	v.open[r.name] = open
	if open {
		r.fill()
		r.detail.Show()
		r.toggle.SetText(partHideLog)
		r.toggle.SetIcon(theme.MenuDropUpIcon())
		r.scroll.ScrollToBottom()
	} else {
		r.detail.Hide()
		r.toggle.SetText(partLog)
		r.toggle.SetIcon(theme.MenuDropDownIcon())
	}
	r.obj.Refresh()
	v.box.Refresh()
}

func (r *partRow) update(v *partsView, p runner.Part, final bool, now time.Time) {
	if r.fresh || p.State != r.shown {
		r.setState(p.State)
	}
	r.status.SetText(partStatus(p, final, now))
	if p.State == runner.PartWorking {
		r.bar.Show()
		if p.Pct >= 0 {
			r.bar.StopPulse()
			r.bar.SetValue(float64(p.Pct) / 100)
		} else if !r.bar.Pulse {
			r.bar.SetValue(0)
			r.bar.StartPulse()
		}
	} else if r.bar.Visible() {
		r.bar.StopPulse()
		r.bar.Hide()
	}
	if p.Seq > 0 {
		r.toggle.Enable()
	} else {
		r.toggle.Disable()
	}
	if p.Seq != r.seq {
		r.seq, r.lines, r.stale = p.Seq, p.Lines, true
		if r.detail.Visible() {
			r.fill()
			r.scroll.ScrollToBottom()
		}
	}
	// The part fails when its command ends, often with no line after.
	if p.State == runner.PartFailed && r.saySeq != p.Seq {
		r.saySeq = p.Seq
		if ex := failureExcerpt(p.Lines[max(len(p.Lines)-tailLines, 0):]); len(ex) > 0 {
			if r.saidT == nil {
				r.saidT = mono("")
				r.said.Objects = []fyne.CanvasObject{bold(doneSaid), r.saidT}
			}
			r.saidT.SetText(strings.Join(ex, "\n"))
			r.said.Refresh()
		}
	}
	// A failure opens its part once, with what the build said above the
	// log; the player can close it again.
	if p.State == runner.PartFailed && !r.auto {
		r.auto = true
		r.setOpen(v, true)
	}
}

// fill puts the kept lines in the log when they moved since it last
// showed them: a selectable label lays out all its text on each SetText.
func (r *partRow) fill() {
	if r.stale {
		r.log.SetText(strings.Join(r.lines[max(len(r.lines)-partShown, 0):], "\n"))
		r.stale = false
	}
}

func (r *partRow) setState(s runner.PartState) {
	if r.act != nil {
		r.act.Stop()
		r.act = nil
	}
	var o fyne.CanvasObject
	if s == runner.PartWorking {
		r.act = widget.NewActivity()
		r.act.Start()
		o = r.act
	} else {
		o = widget.NewIcon(partStateIcon(s))
	}
	r.state.Objects = []fyne.CanvasObject{o}
	r.state.Refresh()
	r.status.SetColor(partColor(s))
	r.shown, r.fresh = s, false
}
