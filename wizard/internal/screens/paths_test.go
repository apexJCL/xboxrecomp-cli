package screens

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/hostinfo"
	"wizard/internal/model"
	"wizard/internal/runner"
)

// shown is every word a page shows, the choices' own labels too.
func shown(o fyne.CanvasObject) string {
	var b strings.Builder
	walkTree(o, func(c fyne.CanvasObject) {
		switch v := c.(type) {
		case *widget.Label:
			b.WriteString(v.Text + "\n")
		case *widget.RichText:
			b.WriteString(v.String() + "\n")
		case *quietText:
			b.WriteString(v.Text() + "\n")
		case *widget.Button:
			b.WriteString(v.Text + "\n")
		case *widget.Check:
			b.WriteString(v.Text + "\n")
		case *widget.RadioGroup:
			b.WriteString(strings.Join(v.Options, "\n") + "\n")
		}
	})
	return b.String()
}

func radioOf(t *testing.T, o fyne.CanvasObject, label string) *widget.RadioGroup {
	t.Helper()
	var r *widget.RadioGroup
	walkTree(o, func(c fyne.CanvasObject) {
		if g, ok := c.(*widget.RadioGroup); ok && slices.Contains(g.Options, label) {
			r = g
		}
	})
	if r == nil {
		t.Fatalf("no choice %q in\n%s", label, shown(o))
	}
	return r
}

func checkOf(t *testing.T, o fyne.CanvasObject, label string) *widget.Check {
	t.Helper()
	var r *widget.Check
	walkTree(o, func(c fyne.CanvasObject) {
		if g, ok := c.(*widget.Check); ok && g.Text == label {
			r = g
		}
	})
	if r == nil {
		t.Fatalf("no box %q in\n%s", label, shown(o))
	}
	return r
}

func buttonOf(t *testing.T, o fyne.CanvasObject, label string) *widget.Button {
	t.Helper()
	var r *widget.Button
	walkTree(o, func(c fyne.CanvasObject) {
		if g, ok := c.(*widget.Button); ok && g.Text == label {
			r = g
		}
	})
	if r == nil {
		t.Fatalf("no button %q in\n%s", label, shown(o))
	}
	return r
}

func otherTarget() string {
	if hostinfo.NativeTarget() == "steamos" {
		return "windows"
	}
	return "steamos"
}

// pathWizard is a wizard on the Update path for another platform, past
// its checks, whose plans are answered at once by answer.
func pathWizard(t *testing.T, answer func(args []string) *runner.Plan) (*Wizard, *[][]string) {
	t.Helper()
	isolate(t)
	a := test.NewApp()
	t.Cleanup(a.Quit)
	w := New(a, sampleOptions(t))
	inline(w)
	var asks [][]string
	w.planAsk = func(_ runner.Runner, args []string) (*runner.Plan, error) {
		asks = append(asks, args)
		if p := answer(args); p != nil {
			return p, nil
		}
		return nil, errors.New("exit status 2")
	}
	w.env = &Env{}
	w.st.Target = otherTarget()
	w.setPath(model.PathUpdate)
	return w, &asks
}

const hudSha = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

func basesPlan() *runner.Plan {
	hud := &runner.PlanPack{Sha256: hudSha, Scale: 4, Entries: 9}
	return &runner.Plan{Schema: 1, FullSize: 3_500_000_000,
		Bases: []runner.PlanBase{
			{Version: "20261016.1200-c1-t1-g2", Made: "2026-10-16T12:00:00Z", Sha256: strings.Repeat("a", 64), ID: "1a2b3c4d",
				Usable: true, PatchSize: 190_000_000, Tweaks: runner.PlanTweaks{HudPack: hud}},
			{Version: "20261012.0900-c0-t1-g2", Made: "2026-10-12T09:00:00Z", Sha256: strings.Repeat("b", 64), ID: "5e6f7a8b",
				Why: []string{"game data layout changed"}, WhyCodes: []string{runner.WhyLayout}},
			{Version: "20261010.0900-c0-t1-g2", Made: "2026-10-10T09:00:00Z", Sha256: strings.Repeat("d", 64), ID: "9c0d1e2f",
				Why: []string{"a patch would be 81% of the full bundle"}, WhyCodes: []string{runner.WhyShare}},
		},
		Output: &runner.PlanOutput{Kind: "full", Why: "--full"}}
}

// The page order follows the path, the step counts its pages, and the
// path chosen is kept for the next run (D1).
func TestPathOrder(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	if w.st.Path != "" || w.pages[pWelcome].ready() {
		t.Fatal("a path is chosen before the player chose one")
	}
	w.render(pChecks)
	if w.step.Text() != fmt.Sprintf(stepText, 2, 8) {
		t.Fatalf("step %q", w.step.Text())
	}
	radioOf(t, w.pages[pWelcome].obj, pathUpdate).SetSelected(pathUpdate)
	if w.st.Path != model.PathUpdate || radioOf(t, w.pages[pWelcome].obj, pathFresh).Selected != "" {
		t.Fatalf("path %q", w.st.Path)
	}
	want := []int{pWelcome, pChecks, pTarget, pBase, pChanges, pSummary, pBuild, pDone}
	if got := w.order(); !slices.Equal(got, want) {
		t.Fatalf("update order %v", got)
	}
	w.render(pBase)
	if w.step.Text() != fmt.Sprintf(stepText, 4, 8) || w.stepBar.Segments != 8 {
		t.Fatalf("step %q", w.step.Text())
	}
	w.st.NeedDisc = true
	if got := w.order(); !slices.Equal(got[:7], []int{pWelcome, pChecks, pTarget, pBase, pChanges, pDump, pSummary}) {
		t.Fatalf("with the disc %v", got)
	}
	if hostinfo.LoadSettings(w.setDir).Path != model.PathUpdate {
		t.Error("the path is not saved")
	}
	w2 := New(a, sampleOptions(t))
	if w2.st.Path != model.PathUpdate || radioOf(t, w2.pages[pWelcome].obj, pathUpdate).Selected != pathUpdate {
		t.Error("the saved path is not chosen again")
	}
	// The developers' --patch-from is an update.
	isolate(t)
	opt := sampleOptions(t)
	opt.PatchFrom = "/m.json"
	if w3 := New(a, opt); w3.st.Path != model.PathUpdate {
		t.Error("--patch-from is not an update")
	}
}

// The Base page lists the builds made here from the plan, each with its
// update ID and tweaks; one that cannot take the update is disabled with
// the reason in the player's words. Choosing one sets --patch-from and
// what the build has, so its tweaks are kept unless changed (D2, D3).
func TestBaseRows(t *testing.T) {
	w, asks := pathWizard(t, func([]string) *runner.Plan { return basesPlan() })
	w.show(pBase)
	if len(*asks) != 1 || strings.Join((*asks)[0], " ") != strings.Join(w.st.BasesArgs(), " ") {
		t.Fatalf("asks %q", *asks)
	}
	txt := shown(w.pages[pBase].obj)
	for _, s := range []string{
		"20261016.1200, made 2026-10-16", "Update ID 1a2b3c4d · Sharper HUD, 4x · update about 190 MB",
		cannotLayout, fmt.Sprintf(cannotShare, "81%"), hasFile,
	} {
		if !strings.Contains(txt, s) {
			t.Errorf("no %q in\n%s", s, txt)
		}
	}
	if (w.st.Target == "steamos") != strings.Contains(txt, hasStatus) {
		t.Error("install.sh status is for the Steam Deck or Linux PC only")
	}
	if strings.Contains(txt, freshInstead) || w.pages[pBase].ready() {
		t.Fatal("ready before a choice, or Fresh offered with builds listed")
	}
	if !radioOf(t, w.pages[pBase].obj, "20261012.0900, made 2026-10-12").Disabled() {
		t.Error("a build that cannot take the update can be chosen")
	}
	radioOf(t, w.pages[pBase].obj, "20261016.1200, made 2026-10-16").SetSelected("20261016.1200, made 2026-10-16")
	if w.st.PatchFrom != strings.Repeat("a", 12) || !w.st.BaseHas["hud_pack"] || !w.pages[pBase].ready() {
		t.Fatalf("state %+v", w.st)
	}
	if !slices.Contains(w.st.Flags(), "--keep-hud-pack") {
		t.Errorf("an unticked HUD on a build with one is not kept: %q", w.st.Flags())
	}
	// Another platform drops the choice.
	w.st.Target = hostinfo.NativeTarget()
	w.show(pBase)
	if w.st.PatchFrom != "" {
		t.Error("the choice survives a new platform")
	}
}

// One build made here is chosen for the player; none offers Fresh.
func TestBaseOneOrNone(t *testing.T) {
	one := basesPlan()
	one.Bases = one.Bases[:1]
	w, _ := pathWizard(t, func([]string) *runner.Plan { return one })
	w.show(pBase)
	if w.st.PatchFrom != strings.Repeat("a", 12) || !w.pages[pBase].ready() {
		t.Fatalf("the one build is not chosen: %q", w.st.PatchFrom)
	}
	none := basesPlan()
	none.Bases = nil
	w, _ = pathWizard(t, func([]string) *runner.Plan { return none })
	w.show(pBase)
	if !strings.Contains(shown(w.pages[pBase].obj), fmt.Sprintf(hasNone, targetDevice[w.st.Target])) {
		t.Fatal(shown(w.pages[pBase].obj))
	}
	test.Tap(buttonOf(t, w.pages[pBase].obj, freshInstead))
	if w.st.Path != model.PathFresh || w.cur != pDump {
		t.Fatalf("Fresh: path %q page %d", w.st.Path, w.cur)
	}
}

// The Changes page asks the plan without the disc; the disc page comes
// only when the build folder lacks the game files (D6). Clear stands
// alone and the asks follow every box (D9).
func TestChangesDisc(t *testing.T) {
	need := true
	w, asks := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		if slices.Contains(args, "--patch-from") {
			b := p.Bases[0]
			p.Output = &runner.PlanOutput{Kind: "patch", Base: &b, PatchSize: 190_000_000}
			p.Import.Needed = need
		}
		return p
	})
	w.show(pBase)
	w.chooseBase(w.baseRows()[0])
	w.move(1)
	if w.cur != pChanges || !w.st.NeedDisc || !strings.Contains(shown(w.pages[pChanges].obj), changesDisc) {
		t.Fatalf("page %d disc %v", w.cur, w.st.NeedDisc)
	}
	last := strings.Join((*asks)[len(*asks)-1], " ")
	if strings.Contains(last, "--dump") || !strings.Contains(last, "--keep-hud-pack --patch-from aaaaaaaaaaaa --plan --json") {
		t.Fatalf("ask %q", last)
	}
	if txt := shown(w.pages[pChanges].obj); !strings.Contains(txt, "Sharper HUD (4x, 9 entries)") || !strings.Contains(txt, changesStatic) {
		t.Errorf("changes:\n%s", txt)
	}
	need = false
	checkOf(t, w.pages[pChanges].obj, changesClear).SetChecked(true)
	last = strings.Join((*asks)[len(*asks)-1], " ")
	if !w.st.Clear || w.st.NeedDisc || !strings.Contains(last, "--clear-hud-pack --patch-from") || strings.Contains(last, "keep") {
		t.Fatalf("clear: %v %v %q", w.st.Clear, w.st.NeedDisc, last)
	}
	if !strings.Contains(shown(w.pages[pChanges].obj), changesNoDisc) {
		t.Error("no line for the disc not needed")
	}
	w.move(1)
	if w.cur != pSummary {
		t.Fatalf("without the disc, Next goes to %d", w.cur)
	}
	if txt := shown(w.pages[pSummary].obj); !strings.Contains(txt, "Updates: 20261016.1200 (update ID 1a2b3c4d)") ||
		!strings.Contains(txt, "Changes: removes the sharper HUD") || !strings.Contains(txt, "Update size: about 190 MB") {
		t.Errorf("summary:\n%s", txt)
	}
}

// A file from the device: its tweaks come with the Changes plan, which
// is asked again with the flags they call for.
func TestFileBaseTweaks(t *testing.T) {
	w, asks := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		if slices.Contains(args, "--patch-from") {
			b := p.Bases[0]
			p.Output = &runner.PlanOutput{Kind: "patch", Base: &b}
		}
		return p
	})
	w.show(pBase)
	w.chooseFile("/x/manifest.json")
	w.fillBase()
	if !strings.Contains(shown(w.pages[pBase].obj), fmt.Sprintf(hasFileChosen, "/x/manifest.json")) {
		t.Fatal(shown(w.pages[pBase].obj))
	}
	n := len(*asks)
	w.show(pChanges)
	if len(*asks) != n+2 || !w.st.BaseHas["hud_pack"] {
		t.Fatalf("asks %q", (*asks)[n:])
	}
	if a := strings.Join((*asks)[n], " "); strings.Contains(a, "keep") {
		t.Errorf("first ask %q", a)
	}
	if a := strings.Join((*asks)[n+1], " "); !strings.Contains(a, "--keep-hud-pack --patch-from /x/manifest.json") {
		t.Errorf("second ask %q", a)
	}
}

// When the Summary's plan makes no update, it says why, Build waits, and
// Fresh keeps the platform and the disc (D7).
func TestSummaryNone(t *testing.T) {
	w, _ := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		if slices.Contains(args, "--patch-from") {
			p.Output = &runner.PlanOutput{Kind: "none", Why: "a patch would be 90% of the full bundle",
				WhyCodes: []string{runner.WhyShare}}
		}
		return p
	})
	w.st.PatchFrom, w.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	w.show(pSummary)
	txt := shown(w.pages[pSummary].obj)
	if !strings.Contains(txt, fmt.Sprintf(cannotShare, "90%")) || w.pages[pSummary].ready() {
		t.Fatalf("summary ready %v:\n%s", w.pages[pSummary].ready(), txt)
	}
	test.Tap(buttonOf(t, w.pages[pSummary].obj, freshInstead))
	if w.st.Path != model.PathFresh || w.cur != pOptions || w.st.Target != otherTarget() {
		t.Fatalf("Fresh: path %q page %d target %q", w.st.Path, w.cur, w.st.Target)
	}
	w.show(pSummary)
	if !w.pages[pSummary].ready() || !strings.Contains(strings.Join(w.st.PackageArgs("", "/o"), " "), "--full") {
		t.Error("Fresh is not a full build")
	}
}

// A refused update offers Fresh on the failure page; a made one says
// what happens on another build (D7).
func TestDoneUpdate(t *testing.T) {
	w, _ := pathWizard(t, func([]string) *runner.Plan { return nil })
	w.prog = &runner.Progress{}
	bf := &buildFailure{Step: "package", Err: errors.New("exit status 1"),
		Tail: []string{"blinx2: the base 1 carries a HUD pack: pass --keep-hud-pack to keep it"}}
	w.buildErr = bf
	w.fillDone()
	if strings.Contains(shown(w.doneBox), failedFresh) {
		t.Fatalf("Fresh offered on a failure with no refused line:\n%s", shown(w.doneBox))
	}
	bf.Tail = append(bf.Tail, "blinx2: refused: hud_guard")
	w.fillDone()
	if !strings.Contains(shown(w.doneBox), failedFresh) {
		t.Fatalf("no Fresh offer:\n%s", shown(w.doneBox))
	}
	w.setPath(model.PathFresh)
	w.fillDone()
	if strings.Contains(shown(w.doneBox), failedFresh) {
		t.Error("Fresh offered on a Fresh build")
	}
	w.setPath(model.PathUpdate)
	w.buildErr = nil
	w.prog = &runner.Progress{Bundle: "/out/BLiNX2-1-steamos-patch-from-20261012.tar"}
	w.fillDone()
	if !strings.Contains(shown(w.doneBox), doneWrongBuild) {
		t.Errorf("done:\n%s", shown(w.doneBox))
	}
	// The codes choose the text, whatever the CLI's words.
	if cannotText(nil, []string{"x"}) != fmt.Sprintf(hasCannot, "x") || cannotText(nil, nil) != cannotOther ||
		cannotText([]string{runner.WhySame}, []string{"reworded"}) != cannotSame ||
		cannotText([]string{runner.WhyGameFiles}, nil) != cannotGameFiles ||
		cannotText([]string{runner.WhyShare}, nil) != cannotShareAny ||
		cannotText([]string{"new_code"}, []string{"y"}) != fmt.Sprintf(hasCannot, "y") {
		t.Error("cannotText")
	}
}

// An update that removes game files says how many on the Summary; one
// that removes none says nothing of it.
func TestSummaryRemoves(t *testing.T) {
	deleted := 3
	w, _ := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		if slices.Contains(args, "--patch-from") {
			b := p.Bases[0]
			b.GameFiles = &runner.PlanGameFiles{Changed: 1, Deleted: deleted}
			p.Output = &runner.PlanOutput{Kind: "patch", PatchSize: 1_000_000, Base: &b}
		}
		return p
	})
	w.st.PatchFrom, w.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	w.show(pSummary)
	if txt := shown(w.pages[pSummary].obj); !strings.Contains(txt, fmt.Sprintf(summaryRemoves, 3)) {
		t.Fatalf("no removes line:\n%s", txt)
	}
	deleted = 1
	w.show(pSummary)
	if txt := shown(w.pages[pSummary].obj); !strings.Contains(txt, summaryRemove1) {
		t.Fatalf("no singular removes line:\n%s", txt)
	}
	deleted = 0
	w.show(pSummary)
	if txt := shown(w.pages[pSummary].obj); strings.Contains(txt, "from that build") {
		t.Fatalf("removes line with none removed:\n%s", txt)
	}
	// A full installer replaces the build: no removes line, whatever the
	// base would lose.
	full, _ := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		b := p.Bases[0]
		b.GameFiles = &runner.PlanGameFiles{Deleted: 3}
		p.Output = &runner.PlanOutput{Kind: "full", Base: &b}
		return p
	})
	full.st.PatchFrom, full.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	full.show(pSummary)
	if txt := shown(full.pages[pSummary].obj); strings.Contains(txt, "from that build") {
		t.Fatalf("removes line on a full installer:\n%s", txt)
	}
}

// A build that is this version already stays pickable on the Base page:
// its update can still change a tweak, which the Summary's plan judges.
func TestBaseSameVersion(t *testing.T) {
	b := &runner.PlanBase{ID: "1a2b3c4d", Why: []string{"it is this version already"},
		WhyCodes: []string{runner.WhySame}}
	r := rowOf("x", b, "aaaa")
	if !r.usable || !strings.Contains(r.detail, hasSameCode) {
		t.Fatalf("same version: %+v", r)
	}
	b.WhyCodes = []string{runner.WhySame, runner.WhyLayout}
	if r := rowOf("x", b, "aaaa"); r.usable {
		t.Fatal("a layout change is pickable")
	}
}
