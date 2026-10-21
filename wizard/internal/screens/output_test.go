package screens

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"fyne.io/fyne/v2/test"

	"wizard/internal/repo"
	"wizard/internal/runner"
)

// Summary's sum of the platforms' disk plans: what all platforms share
// counts once, a compile once per build tree, a bundle per platform.
func TestSumDisk(t *testing.T) {
	free := int64(100)
	plan := func(build, pkg int64) *runner.Plan {
		return &runner.Plan{Disk: []runner.PlanDisk{{Path: "/v", Free: &free, Need: 10 + 20 + build + pkg + 5,
			Stages: map[string]int64{"import": 10, "generate": 20, "build": build, "package": pkg}}}}
	}
	same := func(a, b string) bool { return a == b }
	// Windows and the Steam Deck share one tree: the larger compile.
	v := sumDisk([]string{"windows", "steamos"}, []*runner.Plan{plan(30, 7), plan(25, 6)}, same)
	if len(v) != 1 || v[0].Need != 10+20+30+7+6+5 || v[0].Free != 100 {
		t.Errorf("one tree: %+v", v)
	}
	// The Mac's compile is its own tree.
	v = sumDisk([]string{"windows", "macos"}, []*runner.Plan{plan(30, 7), plan(25, 6)}, same)
	if v[0].Need != 10+20+30+25+7+6+5 {
		t.Errorf("two trees: %+v", v)
	}
	v = sumDisk([]string{"windows", "steamos", "macos"}, []*runner.Plan{plan(30, 40), plan(30, 40), plan(30, 40)}, same)
	if shortVolume(v) == nil || v[0].Need != 10+20+60+120+5 {
		t.Errorf("short: %+v", v)
	}
	// An older CLI's plan has no disk list: nothing to add.
	if v = sumDisk([]string{"windows"}, []*runner.Plan{{}}, same); len(v) != 0 {
		t.Errorf("no disk list: %+v", v)
	}
}

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
{"schema": 1, "target": "steamos", "version": "v2", "stages": [{"stage": "build", "action": "run", "why": ["x", "y"], "estimate_s": 3}],
 "estimate_s": 3, "unknown": [], "bases": [{"version": "v1", "made": "m", "sha256": "aa", "size": 10, "on_disk": true, "patch_size": null, "id": "1a2b3c4d",
   "usable": true, "why": [], "tweaks": {"hud_pack": {"sha256": "cc", "scale": 4, "entries": 9}}, "seed_config": false}],
 "full_size": 10, "output": {"kind": "patch", "why": "from the installed v1", "base": {"version": "v1", "sha256": "aa", "origin": "host"},
 "patch_size": 4, "installed": {"version": "v1", "sha256": "aa", "origin": "host", "usable": true, "why": [], "patch_size": 4, "path": "/g/versions/v1"}},
 "import": {"needed": true, "why": "no game files"}, "config_overrides": [{"key": "textures.pack", "value": "none"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Stages) != 1 || strings.Join(p.Stages[0].Why, ",") != "x,y" {
		t.Errorf("stages: %+v", p.Stages)
	}
	if p.Output.Kind != "patch" || p.Output.Base.Version != "v1" || !p.Output.Installed.Usable || p.FullSize != 10 || p.Bases[0].PatchSize != 0 ||
		p.Output.Installed.Path != "/g/versions/v1" || p.Bases[0].ID != "1a2b3c4d" || p.Bases[0].Tweaks.HudPack == nil ||
		p.Bases[0].Tweaks.HudPack.Scale != 4 || !p.Import.Needed || len(p.ConfigOverrides) != 1 || p.ConfigOverrides[0].Value != "none" {
		t.Errorf("%+v", p)
	}
	if _, err := runner.ParsePlan([]byte(`{"schema": 2}`)); err == nil {
		t.Error("schema 2 parsed")
	}
	// A CLI before the output fields: no output, so the build chooses.
	p, err = runner.ParsePlan([]byte(`{"schema": 1, "stages": [], "bases": []}`))
	if err != nil || p.Output != nil || p.Import.Needed {
		t.Errorf("old plan: %+v %v", p, err)
	}
	if sizeText(400_000) != "1 MB" || sizeText(999_000_000) != "999 MB" || sizeText(9_800_000_000) != "9.8 GB" {
		t.Error("sizeText")
	}
}
