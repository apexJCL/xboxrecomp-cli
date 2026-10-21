package screens

import (
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
	"github.com/ncruces/zenity"

	"wizard/internal/hostinfo"
	"wizard/internal/model"
	"wizard/internal/runner"
)

// The two paths (the wizard-paths design): Fresh makes the full
// installer, Update a patch for a build the player picks. The pages
// after Checks depend on the path (D1); the Base page lists what the
// device can have from the CLI's plan (D2); the Changes page sets the
// tweaks (D3, D9); a build that cannot be updated offers Fresh (D7).

// order is the chosen path's pages. An unchosen path shows Fresh's, as
// it builds.
func (w *Wizard) order() []int {
	if !w.st.Update() {
		return []int{pWelcome, pChecks, pDump, pTarget, pOptions, pSummary, pBuild, pDone}
	}
	o := []int{pWelcome, pChecks, pTarget, pBase, pChanges}
	if w.st.NeedDisc {
		o = append(o, pDump)
	}
	return append(o, pSummary, pBuild, pDone)
}

// move goes d pages along the path's order.
func (w *Wizard) move(d int) {
	o := w.order()
	if k := slices.Index(o, w.cur) + d; k >= 0 && k < len(o) {
		w.show(o[k])
	}
}

// --- the choice on Welcome

// pathChoice is Fresh and Update as two large tiles in a row, an icon
// over each label, and under both the chosen one's sentence (the user,
// 2026-10-19). They act as radios: one is chosen at a time, and none
// until the player picks or the saved settings say.
func (w *Wizard) pathChoice() fyne.CanvasObject {
	w.pathTiles = map[string]*pathTile{}
	row := container.NewGridWithColumns(2)
	// A gap past the grid's own padding, so the chosen tile's frame does
	// not touch its neighbour.
	gap := theme.Padding()
	for i, c := range []struct {
		path, label string
		icon        fyne.Resource
	}{
		{model.PathFresh, pathFresh, theme.FileApplicationIcon()},
		{model.PathUpdate, pathUpdate, theme.ViewRefreshIcon()},
	} {
		t := newPathTile(c.icon, c.label, func() { w.setPath(c.path) })
		w.pathTiles[c.path] = t
		left, right := float32(0), gap
		if i == 1 {
			left, right = gap, 0
		}
		row.Add(container.New(layout.NewCustomPaddedLayout(0, 0, left, right), t))
	}
	w.pathHelp = hintText("")
	w.syncPath()
	return container.NewVBox(bold(pathHeading), row, w.pathHelp)
}

// pathHints are the sentences under the tiles, by path.
var pathHints = map[string]string{model.PathFresh: pathFreshHint, model.PathUpdate: pathUpdateHint}

// syncPath shows the state's path on the tiles and its sentence under
// them.
func (w *Wizard) syncPath() {
	for p, t := range w.pathTiles {
		t.setSelected(p == w.st.Path)
	}
	if w.pathHelp != nil {
		help, ok := pathHints[w.st.Path]
		if !ok {
			help = pathPickHint
		}
		w.pathHelp.SetText(help)
	}
}

// setPath chooses a path and saves it, so the wizard opens on it next
// time (D1). The target and the folders stay.
func (w *Wizard) setPath(p string) {
	if w.st.Path != p {
		w.st.Path = p
		w.settings.Path = p
		w.saveSettings()
	}
	w.syncPath()
	if w.pages != nil {
		w.refreshNav()
	}
}

// toFresh is "Make a Fresh build instead": the target, the folders and
// the disc stay, and the next page is the first Fresh needs that the
// player has not seen.
func (w *Wizard) toFresh() {
	w.setPath(model.PathFresh)
	if w.st.Dump == "" {
		w.show(pDump)
		return
	}
	w.show(pOptions)
}

// under is a choice's sentence lined up with the choice's own words,
// which start after its box (as optionHint's).
func under(o fyne.CanvasObject) fyne.CanvasObject {
	indent := theme.Size(theme.SizeNameInlineIcon) + 2*theme.Size(theme.SizeNameInputBorder)
	return container.New(layout.NewCustomPaddedLayout(0, 0, indent, 0), o)
}

func freshButton(w *Wizard) *widget.Button {
	b := widget.NewButton(freshInstead, w.toFresh)
	b.Importance = widget.HighImportance
	return b
}

// --- what the device has now (D2)

// baseRow is one build the update can be for.
type baseRow struct {
	label  string
	detail string
	arg    string // what --patch-from takes for it
	usable bool
	base   *runner.PlanBase
}

func (w *Wizard) basePage() *page {
	w.baseBox = container.NewVBox()
	return &page{title: titleHas, obj: container.NewVScroll(w.baseBox), enter: w.askBases,
		ready: func() bool { return w.st.PatchFrom != "" }}
}

// askBases asks the CLI for the builds made here and this computer's
// install, off the UI thread; an answer a newer ask overtook is dropped.
func (w *Wizard) askBases() {
	if w.baseTarget != w.st.Target {
		// Another platform's build cannot take this one's update.
		if w.baseTarget != "" {
			w.st.PatchFrom, w.baseFile, w.st.BaseHas = "", "", nil
			w.resetChanges()
		}
		w.baseTarget = w.st.Target
	}
	w.baseRun++
	run := w.baseRun
	w.basePlan, w.baseAsked = nil, false
	w.fillBase()
	if w.env == nil {
		w.baseAsked = true
		w.fillBase()
		return
	}
	r := w.env.Runner
	args := w.st.BasesArgs()
	w.async(func() {
		p, err := w.planAsk(r, args)
		fyne.Do(func() {
			if run != w.baseRun {
				return
			}
			if err != nil {
				w.Logf("builds: %v", err)
				p = nil
			}
			w.basePlan, w.baseAsked = p, true
			w.fillBase()
		})
	})
}

// baseRows are the plan's candidates: this computer's install on its own
// platform, then the builds made here, newest first.
func (w *Wizard) baseRows() []baseRow {
	p := w.basePlan
	if p == nil {
		return nil
	}
	var rows []baseRow
	if o := p.Output; o != nil && o.Installed != nil && w.native() {
		b := o.Installed
		r := rowOf(fmt.Sprintf(hasInstalled, verText(b.Version)), b, b.Path)
		if b.Path == "" {
			r.usable, r.detail = false, fmt.Sprintf(hasCannot, b.Origin)
		}
		rows = append(rows, r)
	}
	for i := range p.Bases {
		b := &p.Bases[i]
		made := b.Made
		if len(made) > 10 {
			made = made[:10]
		}
		rows = append(rows, rowOf(fmt.Sprintf(hasBuild, verText(b.Version), made), b, short(b.Sha256)))
	}
	return rows
}

func rowOf(label string, b *runner.PlanBase, arg string) baseRow {
	r := baseRow{label: label, arg: arg, usable: b.Usable && arg != "", base: b}
	switch {
	case b.ID == "":
		r.detail = hasUnreadable
	case onlySame(b.WhyCodes) && arg != "":
		// The Base page's plan has no tweaks yet: a tweak picked on the
		// Changes page makes a new version of the same code, and the
		// Summary's plan says when nothing changed after all.
		r.usable = true
		r.detail = fmt.Sprintf(hasDetail, b.ID, tweaksText(b.Tweaks)) + " · " + hasSameCode
	case !b.Usable:
		r.detail = cannotText(b.WhyCodes, b.Why)
	default:
		r.detail = fmt.Sprintf(hasDetail, b.ID, tweaksText(b.Tweaks)) + sizedAbout(b.PatchSize)
	}
	return r
}

func sizedAbout(n int64) string {
	if n <= 0 {
		return ""
	}
	return fmt.Sprintf(hasSize, sizeText(n))
}

// tweaksText is a build's tweaks as the player reads them.
func tweaksText(t runner.PlanTweaks) string {
	if t.HudPack == nil {
		return hasNoTweaks
	}
	s := tweakTexts["hud_pack"].name
	if t.HudPack.Scale > 0 {
		s += fmt.Sprintf(", %dx", t.HudPack.Scale)
	}
	return s
}

func baseHas(t runner.PlanTweaks) map[string]bool {
	return map[string]bool{"hud_pack": t.HudPack != nil}
}

var sharePct = regexp.MustCompile(`would be (\d+%)`)

// cannotText says in the player's words why a build cannot take this
// update (D7): the plan's reason codes pick the text; the CLI's reasons
// are shown as they are only for a code the wizard does not know.
func cannotText(codes, why []string) string {
	j := strings.Join(why, "; ")
	for _, c := range codes {
		switch c {
		case runner.WhyLayout:
			return cannotLayout
		case runner.WhySame:
			return cannotSame
		case runner.WhyShare:
			if m := sharePct.FindStringSubmatch(j); m != nil {
				return fmt.Sprintf(cannotShare, m[1])
			}
			return cannotShareAny
		case runner.WhyHudKeep:
			return cannotKeep
		case runner.WhyHudGuard:
			return cannotGuard
		case runner.WhyGameFiles:
			return cannotGameFiles
		case runner.WhyGameDeletes:
			return cannotDeletes
		}
	}
	if j != "" {
		return fmt.Sprintf(hasCannot, j)
	}
	return cannotOther
}

// onlySame says the one reason a build cannot take the update is that
// it is this version already.
func onlySame(codes []string) bool {
	return len(codes) > 0 && !slices.ContainsFunc(codes, func(c string) bool { return c != runner.WhySame })
}

// preselect is the row D2 chooses for the player: this computer's
// install on its own platform, else the one build made here; a choice
// already made (Back, or the developers' --patch-from) stays.
func (w *Wizard) preselect(rows []baseRow) {
	if w.st.PatchFrom != "" {
		return
	}
	switch {
	case len(rows) > 0 && w.native() && rows[0].base == w.installed() && rows[0].usable:
		w.chooseBase(rows[0])
	case !w.native() && len(rows) == 1 && rows[0].usable:
		w.chooseBase(rows[0])
	}
}

func (w *Wizard) installed() *runner.PlanBase {
	if w.basePlan == nil || w.basePlan.Output == nil {
		return nil
	}
	return w.basePlan.Output.Installed
}

func (w *Wizard) chooseBase(r baseRow) {
	w.st.PatchFrom, w.baseFile = r.arg, ""
	w.st.BaseHas = baseHas(r.base.Tweaks)
	w.resetChanges()
}

// resetChanges drops what the Changes page knew of the last build picked.
func (w *Wizard) resetChanges() {
	w.st.NeedDisc, w.changesPlan = false, nil
	if !w.st.BaseHas["hud_pack"] {
		w.st.Clear = false
	}
}

func (w *Wizard) fillBase() {
	dev := targetDevice[w.st.Target]
	items := []fyne.CanvasObject{para(fmt.Sprintf(hasIntro, dev))}
	rows := w.baseRows()
	switch {
	case !w.baseAsked:
		items = append(items, para(hasAsking))
	case w.basePlan == nil:
		items = append(items, para(hasNoPlan))
	case len(rows) == 0:
		items = append(items, para(fmt.Sprintf(hasNone, dev)))
	}
	if w.baseAsked {
		w.preselect(rows)
	}
	w.baseRadios = nil
	for _, r := range rows {
		items = append(items, w.baseRadio(r.label, r.arg, r.usable, func() { w.chooseBase(r) }),
			under(hintText(r.detail)))
	}
	file := w.baseRadio(hasFile, w.baseFile, w.baseFile != "", nil)
	if w.baseFile != "" {
		file = w.baseRadio(fmt.Sprintf(hasFileChosen, w.baseFile), w.baseFile, true, func() { w.chooseFile(w.baseFile) })
	}
	where := fmt.Sprintf(manifestWhere[w.st.Target], strings.TrimRight(w.opt.Cfg.Data[w.st.Target], `/\`), w.opt.Cfg.Game.App)
	items = append(items, file, container.NewHBox(widget.NewButton(changeText, w.pickManifest)),
		hintText(fmt.Sprintf(hasFileWhere, where, dev)))
	if w.st.Target == "steamos" && !w.native() {
		items = append(items, hintText(hasStatus))
	}
	if w.baseAsked && len(rows) == 0 {
		items = append(items, container.NewHBox(freshButton(w)))
	}
	w.baseBox.Objects = items
	w.baseBox.Refresh()
	w.refreshNav()
}

// baseRadio is one candidate as a single-choice group, so each carries
// its detail under it; choosing one clears the others. An unusable one
// is shown disabled, with its reason under it.
func (w *Wizard) baseRadio(label, arg string, usable bool, choose func()) *widget.RadioGroup {
	r := widget.NewRadioGroup([]string{label}, nil)
	r.Required = true
	if arg != "" && arg == w.st.PatchFrom {
		r.SetSelected(label)
	}
	r.OnChanged = func(s string) {
		if s == "" || choose == nil {
			return
		}
		choose()
		for _, o := range w.baseRadios {
			if o != r {
				o.SetSelected("")
			}
		}
		w.refreshNav()
	}
	if !usable {
		r.Disable()
	}
	w.baseRadios = append(w.baseRadios, r)
	return r
}

func (w *Wizard) chooseFile(p string) {
	w.st.PatchFrom, w.baseFile = p, p
	// The file's tweaks come with the Changes page's plan.
	w.st.BaseHas = nil
	w.resetChanges()
}

// pickManifest is the system's file dialog for the device's
// manifest.json, Fyne's when there is none.
func (w *Wizard) pickManifest() {
	set := func(p string) {
		if p != "" {
			w.chooseFile(p)
			w.fillBase()
		}
	}
	if !w.opt.NoDialog {
		p, err := zenity.SelectFile(zenity.Title(pickManifest),
			zenity.FileFilters{{Name: pickManifest, Patterns: []string{"*.json"}, CaseFold: true}})
		if err == nil || errors.Is(err, zenity.ErrCanceled) {
			set(p)
			return
		}
		w.Logf("file dialog: %v; using Fyne's", err)
	}
	dialog.ShowFileOpen(func(r fyne.URIReadCloser, err error) {
		if r != nil {
			set(r.URI().Path())
			r.Close()
		}
	}, w.win)
}

// --- the changes (D9)

func (w *Wizard) changesPage() *page {
	w.changesBox = container.NewVBox()
	return &page{title: titleChanges, obj: container.NewVScroll(w.changesBox), enter: w.askChanges,
		ready: func() bool { return w.changesAsked }}
}

// askChanges asks the plan of this update without the disc: whether it
// needs the game files again (D6), the build's tweaks when it is a
// file, and this computer's config.toml lines (D8). A failed ask asks
// for the disc, which is never wrong.
func (w *Wizard) askChanges() {
	w.changesRun++
	run := w.changesRun
	w.changesAsked = false
	w.st.NeedDisc = false
	w.fillChanges()
	if w.env == nil {
		w.changesAsked, w.st.NeedDisc = true, true
		w.fillChanges()
		return
	}
	r := w.env.Runner
	args := w.st.PlanArgs("", w.out)
	w.async(func() {
		p, err := w.planAsk(r, args)
		fyne.Do(func() {
			if run != w.changesRun {
				return
			}
			if err != nil {
				w.Logf("changes: %v", err)
				p = nil
			}
			w.changesPlan, w.changesAsked = p, true
			w.st.NeedDisc = p == nil || p.Import.Needed
			if b := w.planBase(p); b != nil && w.st.BaseHas == nil {
				// A file's tweaks: known now; the flags follow them.
				w.st.BaseHas = baseHas(b.Tweaks)
				w.askChanges()
				return
			}
			w.fillChanges()
		})
	})
}

// planBase is the build the update is for, as a plan read it.
func (w *Wizard) planBase(p *runner.Plan) *runner.PlanBase {
	if p == nil || p.Output == nil {
		return nil
	}
	return p.Output.Base
}

// changesBase is the build the update is for: the Changes plan's, else
// the Base page's row.
func (w *Wizard) changesBase() *runner.PlanBase {
	if b := w.planBase(w.changesPlan); b != nil {
		return b
	}
	for _, r := range w.baseRows() {
		if r.arg == w.st.PatchFrom {
			return r.base
		}
	}
	return nil
}

func (w *Wizard) fillChanges() {
	b := w.changesBase()
	var items []fyne.CanvasObject
	if b != nil {
		now := hasNoTweaks
		if h := b.Tweaks.HudPack; h != nil {
			now = fmt.Sprintf("%s (%dx, %d entries)", tweakTexts["hud_pack"].name, h.Scale, h.Entries)
		}
		items = append(items, para(fmt.Sprintf(changesNow, now)))
	}
	has := w.st.BaseHas["hud_pack"]
	for _, o := range w.opt.Cfg.Options {
		t, ok := model.Tweaks[o.Flag]
		tt, words := tweakTexts[t.Plan]
		if !ok || !words {
			continue
		}
		hint := hintText("")
		c := widget.NewCheck(o.Label, nil)
		show := func() {
			switch {
			case c.Checked:
				hint.SetText(tt.made)
			case w.st.BaseHas[t.Plan]:
				hint.SetText(tt.keeps)
			default:
				hint.SetText(tt.none)
			}
		}
		c.SetChecked(w.st.Value(o.Key) == "true" && !w.st.Clear)
		c.OnChanged = func(on bool) {
			if err := w.st.Set(o.Key, fmt.Sprint(on)); err != nil {
				w.Logf("option %s: %v", o.Key, err)
			}
			show()
			w.askChanges()
		}
		if w.st.Clear {
			c.Disable()
		}
		show()
		items = append(items, c, under(hint))
	}
	if has {
		clear := widget.NewCheck(changesClear, nil)
		clear.SetChecked(w.st.Clear)
		clear.OnChanged = func(on bool) {
			w.st.Clear = on
			// A new tweak replaces the old one anyway: Clear stands alone.
			for _, o := range w.opt.Cfg.Options {
				if _, ok := model.Tweaks[o.Flag]; ok && on {
					w.st.Set(o.Key, "false")
				}
			}
			w.askChanges()
		}
		items = append(items, clear, under(hintText(tweakTexts["hud_pack"].clear)))
	}
	if p := ConfigPath(w.opt.Cfg, w.hereTarget()); p != "" && w.opt.Cfg.ReadsConfig() {
		items = append(items, para(fmt.Sprintf(changesConfig, p)))
		if note := w.overrideNote(w.changesPlan); note != "" {
			items = append(items, para(note))
		}
	}
	switch {
	case !w.changesAsked:
		items = append(items, para(changesAsking))
	case w.st.NeedDisc:
		items = append(items, para(changesDisc))
	default:
		items = append(items, hintText(changesNoDisc))
	}
	w.changesBox.Objects = items
	w.changesBox.Refresh()
	w.refreshNav()
}

// overrideNote is D8's line: on this computer, the config.toml line the
// plan found; for another device, the static note when the build ships a
// pack; "" otherwise.
func (w *Wizard) overrideNote(p *runner.Plan) string {
	if w.native() {
		if p == nil {
			return ""
		}
		for _, o := range p.ConfigOverrides {
			return fmt.Sprintf(changesOverride, ConfigPath(w.opt.Cfg, w.hereTarget()), o.Key, o.Value)
		}
		return ""
	}
	for _, f := range w.st.Flags() {
		if f == "--hud-pack" || f == "--keep-hud-pack" {
			return changesStatic
		}
	}
	return ""
}

// --- the Summary's update lines

// updateLines are the Summary's lines for an update: the build it is
// for, what it keeps and changes, its size, and when the plan says no
// update can be made, the reason and the way to Fresh (D7).
func (w *Wizard) updateLines(p *runner.Plan) []fyne.CanvasObject {
	var items []fyne.CanvasObject
	if b := w.changesBase(); b != nil {
		items = append(items, para(fmt.Sprintf(summaryUpdates, verText(b.Version), b.ID)))
	} else if w.baseFile != "" {
		items = append(items, para(fmt.Sprintf(hasFileChosen, w.baseFile)))
	}
	keeps, changes := w.tweakChanges()
	switch {
	case keeps == "" && changes == "":
		items = append(items, para(summaryNothing))
	default:
		if keeps != "" {
			items = append(items, para(fmt.Sprintf(summaryKeeps, keeps)))
		}
		if changes != "" {
			items = append(items, para(fmt.Sprintf(summaryChanges, changes)))
		}
	}
	if p != nil && p.Output != nil {
		o := p.Output
		switch o.Kind {
		case "patch":
			// Only a patch removes anything: a full installer replaces the
			// build, and "none" makes nothing.
			if n := removedGameFiles(o.Base, w.changesBase()); n > 0 {
				items = append(items, para(removesText(n)))
			}
			if o.PatchSize > 0 {
				items = append(items, para(fmt.Sprintf(summaryUpdSize, sizeText(o.PatchSize))))
			}
		case "none":
			items = append(items, blockedLine(cannotText(o.WhyCodes, []string{o.Why})), container.NewHBox(freshButton(w)))
		}
	}
	return items
}

func removesText(n int) string {
	if n == 1 {
		return summaryRemove1
	}
	return fmt.Sprintf(summaryRemoves, n)
}

// removedGameFiles is how many game files the update removes from the
// build it is for: the Summary plan's base, else the one picked.
func removedGameFiles(bases ...*runner.PlanBase) int {
	for _, b := range bases {
		if b != nil && b.GameFiles != nil {
			return b.GameFiles.Deleted
		}
	}
	return 0
}

// tweakChanges are what the update keeps and changes of the device's
// tweaks, in the player's words.
func (w *Wizard) tweakChanges() (keeps, changes string) {
	tt := tweakTexts["hud_pack"]
	for _, f := range w.st.Flags() {
		switch f {
		case "--keep-hud-pack":
			keeps = tt.name
		case "--hud-pack":
			changes = "a new " + strings.ToLower(tt.name)
		case "--clear-hud-pack":
			changes = tt.removes
		}
	}
	return keeps, changes
}

// cannotUpdate says the last Summary plan refused this update. Short
// disk space is not a refusal of the update: the page says so, and the
// build's own refusal offers Retry once space is free.
func (w *Wizard) cannotUpdate() bool {
	return w.st.Update() && w.plan != nil && w.plan.Output != nil && w.plan.Output.Kind == "none" &&
		!diskShort(w.plan)
}

// diskShort says the plan found too little free space (D9).
func diskShort(p *runner.Plan) bool {
	return p != nil && p.Output != nil && slices.Contains(p.Output.WhyCodes, runner.WhyDisk)
}

var refusedLine = regexp.MustCompile(`: refused: ([a-z_]+)$`)

// refusedUpdate says a failed run's lines are package's refusal of the
// update (D7): its "refused: CODE" line, whatever the message says. The
// plan's estimate was wrong, and Fresh is the way on.
func refusedUpdate(tail []string) bool {
	c := refusedCode(tail)
	return c != "" && c != runner.WhyDisk
}

// refusedCode is the code of a failed run's "refused: CODE" line, or "".
func refusedCode(tail []string) string {
	for _, l := range tail {
		if m := refusedLine.FindStringSubmatch(strings.TrimRight(l, "\r")); m != nil {
			return m[1]
		}
	}
	return ""
}

// native says this computer's own platform is among those built.
func (w *Wizard) native() bool { return slices.Contains(w.st.Builds(), hostinfo.NativeTarget()) }

// hereTarget is the platform the "this computer" notes are about: this
// computer's own when it is built, else the first one built.
func (w *Wizard) hereTarget() string {
	if w.native() {
		return hostinfo.NativeTarget()
	}
	return w.st.Target
}
