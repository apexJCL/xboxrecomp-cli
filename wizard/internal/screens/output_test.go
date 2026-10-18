package screens

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"fyne.io/fyne/v2/test"

	"wizard/internal/hostinfo"
	"wizard/internal/repo"
	"wizard/internal/runner"
)

func writeMark(t *testing.T, root string, m string) {
	t.Helper()
	d := filepath.Join(root, "src", ".git", "xbr")
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, repo.MarkName), []byte(m), 0o644); err != nil {
		t.Fatal(err)
	}
}

// The Welcome page names the clone a build folder already holds, from the
// mark alone, and follows a new build folder.
func TestFolderFound(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	opt := sampleOptions(t)
	writeMark(t, opt.Root, `{"origin":"repo","ref":"v20261012","commit":"c1a2b3c4d5e6"}`)
	w := New(a, opt)
	want := fmt.Sprintf(folderFoundRelease, opt.Cfg.Game.Name, "v20261012", "c1a2b3c")
	if !w.foundLabel.Visible() || w.foundLabel.Text != want {
		t.Fatalf("found line %v %q", w.foundLabel.Visible(), w.foundLabel.Text)
	}
	other := t.TempDir()
	w.setRoot(other)
	if w.foundLabel.Visible() {
		t.Fatalf("an empty build folder shows %q", w.foundLabel.Text)
	}
	writeMark(t, other, `{"origin":"local","ref":"HEAD","commit":"0123456789ab","via":"checkout"}`)
	w.setRoot(other)
	if got := w.foundLabel.Text; got != fmt.Sprintf(folderFoundDev, opt.Cfg.Game.Name, "0123456") {
		t.Errorf("a local clone in release mode: %q", got)
	}
	w.opt.Source = &Source{Local: &repo.Local{Path: "/src"}, Commit: "fedcba987654"}
	w.showFolderFound()
	if got := w.foundLabel.Text; got != fmt.Sprintf(folderFoundLocal, opt.Cfg.Game.Name, "0123456", "fedcba9") {
		t.Errorf("local mode: %q", got)
	}
	writeMark(t, other, `{"origin":"elsewhere","commit":"x"}`)
	w.showFolderFound()
	if w.foundLabel.Visible() {
		t.Error("a foreign mark counts")
	}
}

func samplePlan() *runner.Plan {
	return &runner.Plan{
		Schema: 1, Target: "steamos", FullSize: 3_500_000_000,
		Stages: []runner.PlanStage{{Stage: "generate", Action: "reuse"}, {Stage: "build", Action: "run", Why: "toolkit moved"}},
		Bases: []runner.PlanBase{
			{Version: "20261016.1200-cabc-t1-g2", Made: "2026-10-16T12:00:00.000Z", Sha256: strings.Repeat("a", 64), Size: 3_500_000_000},
			{Version: "20261012.0900-cdef-t1-g2", Made: "2026-10-12T09:00:00.000Z", Sha256: strings.Repeat("b", 64), PatchSize: 190_000_000},
		},
		Output: &runner.PlanOutput{Kind: "full", Why: "no install found"},
	}
}

func TestOutputText(t *testing.T) {
	p := samplePlan()
	inst := &runner.PlanInstalled{PlanVersion: runner.PlanVersion{Version: "20261012.0900-cdef-t1-g2"}, Usable: true}
	patch := func(size int64, v string) *runner.Plan {
		q := *p
		q.Output = &runner.PlanOutput{Kind: "patch", Base: &runner.PlanVersion{Version: v}, PatchSize: size, Installed: inst}
		return &q
	}
	full := func(inst *runner.PlanInstalled) *runner.Plan {
		q := *p
		q.Output = &runner.PlanOutput{Kind: "full", Installed: inst}
		return &q
	}
	same := &runner.PlanInstalled{PlanVersion: inst.PlanVersion, Why: []string{"it is this version already"}}
	other := &runner.PlanInstalled{PlanVersion: inst.PlanVersion, Why: []string{"game data layout changed"}}
	for _, c := range []struct {
		p         *runner.Plan
		native    bool
		full      bool
		patchFrom string
		want      string
	}{
		{nil, true, false, "", outputUnknown},
		{&runner.Plan{Schema: 1}, true, false, "", outputUnknown},
		{patch(190_000_000, inst.Version), true, false, "", "Update (190 MB) for the installed 20261012.0900"},
		{patch(0, inst.Version), true, false, "", "Update for the installed 20261012.0900"},
		{patch(190_000_000, inst.Version), false, false, "bbbbbbbbbbbb", "Update (190 MB) for 20261012.0900 on the Steam Deck or Linux PC"},
		{full(nil), true, false, "", "Full installer (3.5 GB): no install found"},
		{full(nil), true, true, "", "Full installer (3.5 GB): you chose it"},
		{full(same), true, false, "", "Full installer (3.5 GB): the installed 20261012.0900 is this build already"},
		{full(other), true, false, "", "Full installer (3.5 GB): the installed 20261012.0900 cannot take an update"},
		{full(nil), false, false, "", "Full installer (3.5 GB): for an update, choose what the Steam Deck or Linux PC has under Change…"},
		{full(nil), false, false, "bbbb", "Full installer (3.5 GB): that version cannot take an update"},
		{&runner.Plan{Schema: 1, Output: &runner.PlanOutput{Kind: "full"}}, false, false, "", "Full installer: the first build for the Steam Deck or Linux PC made here"},
	} {
		if got := outputText(c.p, "steamos", c.native, c.full, c.patchFrom); got != c.want {
			t.Errorf("got  %q\nwant %q", got, c.want)
		}
	}
	if sizeText(400_000) != "1 MB" || sizeText(999_000_000) != "999 MB" || sizeText(9_800_000_000) != "9.8 GB" {
		t.Error("sizeText")
	}
}

// The Summary asks the CLI for the plan with the build's own arguments,
// shows the line, and Change… sets the choice and asks again: the version
// another computer has becomes --patch-from, Rebuild everything --clean,
// the full installer --full. An answer a newer ask overtook is dropped.
func TestOutputChoiceFlow(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	other := "steamos"
	if hostinfo.NativeTarget() == "steamos" {
		other = "windows"
	}
	w.st.Target = other
	w.st.Dump = "/d/game.iso"
	var jobs []func()
	w.async = func(f func()) { jobs = append(jobs, f) }
	var asks [][]string
	answer := samplePlan()
	w.planAsk = func(r runner.Runner, args []string) (*runner.Plan, error) {
		asks = append(asks, args)
		return answer, nil
	}
	w.fillSummary() // no checks yet: no plan
	if len(jobs) != 0 || w.outputLine.Text != outputUnknown || !w.outputChange.Disabled() {
		t.Fatalf("before the checks: %q", w.outputLine.Text)
	}
	w.env = &Env{}
	w.fillSummary()
	if w.outputLine.Text != outputAsking || len(jobs) != 1 {
		t.Fatalf("asking: %q", w.outputLine.Text)
	}
	jobs[0]()
	if len(asks) != 1 || !slices.Contains(asks[0], "--plan") || !slices.Contains(asks[0], "--json") || asks[0][1] != other {
		t.Fatalf("plan args %q", asks)
	}
	if !strings.HasPrefix(w.outputLine.Text, "Full installer (3.5 GB): for an update") || w.outputChange.Disabled() {
		t.Fatalf("line %q", w.outputLine.Text)
	}
	if !strings.Contains(w.summaryTech.Text, "run build (toolkit moved)") || !strings.Contains(w.summaryTech.Text, "reuse generate") {
		t.Errorf("the plan is not in the details:\n%s", w.summaryTech.Text)
	}
	_, f, body := w.outputDialog()
	if f.has == nil || len(f.has.Options) != 2 || f.kind.Selected != choiceFull {
		t.Fatalf("dialog: %+v", f)
	}
	if txt := allText(body); strings.Contains(strings.ToLower(txt), "base") || strings.Contains(txt, "patch") {
		t.Errorf("the dialog says base or patch:\n%s", txt)
	}
	f.has.SetSelectedIndex(1)
	if f.kind.Selected != choiceUpdateOn {
		t.Fatal("choosing a version does not choose the update")
	}
	f.rebuild.SetChecked(true)
	w.applyChoice(f)
	if w.st.PatchFrom != strings.Repeat("b", 12) || w.st.Full || !w.st.Clean {
		t.Fatalf("state %+v", w.st)
	}
	answer = samplePlan()
	answer.Output = &runner.PlanOutput{Kind: "patch", Base: &runner.PlanVersion{Version: answer.Bases[1].Version}, PatchSize: 190_000_000}
	jobs[1]()
	got := strings.Join(asks[1], " ")
	if !strings.Contains(got, "--clean --patch-from bbbbbbbbbbbb --plan --json") {
		t.Fatalf("args %q", got)
	}
	if w.outputLine.Text != "Update (190 MB) for 20261012.0900 on "+targetDevice[other] {
		t.Fatalf("line %q", w.outputLine.Text)
	}
	if !strings.Contains(strings.Join(w.st.PackageArgs("", ""), " "), "--clean --patch-from bbbbbbbbbbbb --plain") {
		t.Error("the build does not get the choice")
	}
	// The full installer clears the version.
	_, f, _ = w.outputDialog()
	if f.kind.Selected != choiceUpdateOn || f.has.SelectedIndex() != 1 {
		t.Fatalf("the dialog does not show the choice: %q %d", f.kind.Selected, f.has.SelectedIndex())
	}
	f.kind.SetSelected(choiceFull)
	f.rebuild.SetChecked(false)
	w.applyChoice(f)
	if !w.st.Full || w.st.PatchFrom != "" || w.st.Clean {
		t.Fatalf("state %+v", w.st)
	}
	// A late answer of an older ask is dropped; a failed one leaves the
	// choice to the build.
	w.planAsk = func(runner.Runner, []string) (*runner.Plan, error) { return nil, errors.New("exit status 2") }
	w.askPlan()
	jobs[3]()
	jobs[2]()
	if w.outputLine.Text != outputUnknown || !w.outputChange.Disabled() {
		t.Errorf("after a failed plan: %q", w.outputLine.Text)
	}
}

// On the host's own platform the update is for the installed version:
// Change… offers it and the full installer, and the update leaves the
// choice of version to package.
func TestOutputChoiceNative(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	w.st.Target = hostinfo.NativeTarget()
	w.plan = samplePlan()
	inst := &runner.PlanInstalled{PlanVersion: runner.PlanVersion{Version: "20261012.0900-x"}, Usable: true}
	w.plan.Output = &runner.PlanOutput{Kind: "patch", Base: &inst.PlanVersion, Installed: inst}
	w.st.Full = true
	_, f, _ := w.outputDialog()
	if f.has != nil || len(f.kind.Options) != 2 || f.kind.Selected != choiceFull {
		t.Fatalf("dialog %q %q", f.kind.Options, f.kind.Selected)
	}
	f.kind.SetSelected(fmt.Sprintf(choiceUpdate, "20261012.0900"))
	w.applyChoice(f)
	if w.st.Full || w.st.PatchFrom != "" {
		t.Fatalf("state %+v", w.st)
	}
	// The ask after the choice had no checks to run with: no plan.
	if w.plan != nil {
		t.Fatal("a plan without the checks")
	}
	w.plan = samplePlan()
	inst.Usable = false
	w.plan.Output = &runner.PlanOutput{Kind: "full", Installed: inst}
	_, f, _ = w.outputDialog()
	if len(f.kind.Options) != 1 {
		t.Errorf("an install that cannot take an update is offered: %q", f.kind.Options)
	}
}

// The end screen tells an update from the full installer by its name, on
// every target, and shows the build folder's size with Free space, which
// runs the CLI's clean and measures again.
func TestDoneUpdateAndDisk(t *testing.T) {
	isolate(t)
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	for target, full := range map[string]string{
		"steamos": "BLiNX2-20261017.0900-c1-t2-g3-steamos.tar",
		"windows": "BLiNX2-20261017.0900-c1-t2-g3-windows",
		"macos":   "BLiNX2-20261017.0900-c1-t2-g3.dmg",
	} {
		patch := map[string]string{
			"steamos": "BLiNX2-20261017.0900-c1-t2-g3-steamos-patch-from-20261012.tar",
			"windows": "BLiNX2-20261017.0900-c1-t2-g3-windows-patch-from-20261012",
			"macos":   "BLiNX2-20261017.0900-c1-t2-g3-patch-from-20261012.dmg",
		}[target]
		if isPatch(full) || !isPatch(patch) {
			t.Errorf("%s: isPatch", target)
		}
		if applyText(target, full) != installText[target] || applyText(target, patch) != applyPatchText[target] {
			t.Errorf("%s: apply text", target)
		}
	}
	var jobs []func()
	w.async = func(f func()) { jobs = append(jobs, f) }
	w.treeSize = func(string) uint64 { return 9_800_000_000 }
	w.st.Target = "steamos"
	w.prog = &runner.Progress{Bundle: "/out/BLiNX2-1-steamos-patch-from-20261012.tar"}
	w.fillDone()
	all := allText(w.doneBox)
	if !strings.Contains(all, applyPatchText["steamos"]) || strings.Contains(all, installText["steamos"]) {
		t.Errorf("done page:\n%s", all)
	}
	if w.diskLabel.Text != diskMeasuring {
		t.Fatalf("disk %q", w.diskLabel.Text)
	}
	jobs[0]()
	if w.diskLabel.Text != "Build folder: 9.8 GB" {
		t.Fatalf("disk %q", w.diskLabel.Text)
	}
	w.env = &Env{}
	cleaned := 0
	w.cleanRun = func(runner.Runner) (int64, error) { cleaned++; return 4_300_000_000, nil }
	w.treeSize = func(string) uint64 { return 5_500_000_000 }
	w.runClean()
	if !w.freeBtn.Disabled() || w.diskLabel.Text != diskFreeing {
		t.Fatalf("freeing: %q", w.diskLabel.Text)
	}
	jobs[1]()
	jobs[2]()
	if cleaned != 1 || w.freeBtn.Disabled() || w.diskLabel.Text != "Freed 4.3 GB. Build folder: 5.5 GB" {
		t.Fatalf("freed: %q", w.diskLabel.Text)
	}
	w.cleanRun = func(runner.Runner) (int64, error) { return 0, errors.New("exit status 1") }
	w.runClean()
	jobs[3]()
	if w.diskLabel.Text != diskFreeFailed {
		t.Errorf("failed clean: %q", w.diskLabel.Text)
	}
}

func TestParsePlanOutput(t *testing.T) {
	p, err := runner.ParsePlan([]byte(`note
{"schema": 1, "target": "steamos", "version": "v2", "stages": [{"stage": "build", "action": "run", "why": "x", "estimate_s": 3}],
 "estimate_s": 3, "unknown": [], "bases": [{"version": "v1", "made": "m", "sha256": "aa", "size": 10, "on_disk": true, "patch_size": null}],
 "full_size": 10, "output": {"kind": "patch", "why": "from the installed v1", "base": {"version": "v1", "sha256": "aa", "origin": "host"},
 "patch_size": 4, "installed": {"version": "v1", "sha256": "aa", "origin": "host", "usable": true, "why": [], "patch_size": 4}}}`))
	if err != nil {
		t.Fatal(err)
	}
	if p.Output.Kind != "patch" || p.Output.Base.Version != "v1" || !p.Output.Installed.Usable || p.FullSize != 10 || p.Bases[0].PatchSize != 0 {
		t.Errorf("%+v", p)
	}
	if _, err := runner.ParsePlan([]byte(`{"schema": 2}`)); err == nil {
		t.Error("schema 2 parsed")
	}
	// A CLI before the output fields: no output, so the build chooses.
	p, err = runner.ParsePlan([]byte(`{"schema": 1, "stages": [], "bases": []}`))
	if err != nil || p.Output != nil || outputText(p, "steamos", true, false, "") != outputUnknown {
		t.Errorf("old plan: %+v %v", p, err)
	}
}
