package screens

import (
	"errors"
	"fmt"
	"runtime"
	"slices"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/hostinfo"
	"wizard/internal/runner"
)

// The computer row: absent with an older CLI, ready with enough memory,
// a warning in the player's words when memory or disk is short, and the
// CLI's own lines behind it once (D5).
func TestComputerRow(t *testing.T) {
	cfg := sampleOptions(t).Cfg
	env := &Env{Game: "/r/src", Git: "/usr/bin/git"}
	if rows := checkRows(cfg, env, loadDoctor(t, "doctor.json"), nil, true); slices.ContainsFunc(rows, func(r checkRow) bool { return r.Name == rowComputer }) {
		t.Fatal("a computer row without resources")
	}

	d := loadDoctor(t, "doctor-lowmem.json")
	rows := checkRows(cfg, env, d, nil, true)
	r := rowByName(rows, rowComputer)
	if r.State != stateWarn || r.Status != statusLowRes {
		t.Fatalf("low memory: %+v", r)
	}
	for _, want := range []string{"one file at a time", computerLowMem, computerTool} {
		if !strings.Contains(r.Detail, want) {
			t.Errorf("detail lacks %q:\n%s", want, r.Detail)
		}
	}
	for _, k := range []string{"memory:", "cores:", "disk:", "build jobs:", "warning:    memory is low", "warning:    disasm"} {
		if !slices.ContainsFunc(r.Raw, func(l string) bool { return strings.HasPrefix(l, k) }) {
			t.Errorf("raw lacks %q: %q", k, r.Raw)
		}
	}
	// The resource warnings are the computer's, not the Notes row's.
	if slices.ContainsFunc(rows, func(r checkRow) bool { return r.Name == rowNotes }) {
		t.Errorf("notes repeat the computer's warnings: %+v", rowByName(rows, rowNotes))
	}
	for _, tg := range cfg.Targets {
		if tr := rowByName(rows, targetName(tg)); tr.State != stateOK {
			t.Errorf("low memory blocked %s: %+v", tg, tr)
		}
	}

	res := *d.Resources
	res.Warnings, res.Jobs = nil, 6
	ok := computerRow(&res, nil)
	if ok.State != stateOK || ok.Status != statusEnough || !strings.Contains(ok.Detail, "6 files at a time") {
		t.Errorf("enough: %+v", ok)
	}
	res.Warnings = []string{"disk: 1.1 GB free, a build needs about 9.9 GB: package will refuse to start"}
	if low := computerRow(&res, nil); low.State != stateWarn || !strings.Contains(low.Detail, fmt.Sprintf(computerLowDisk, "42.9 GB", "9.9 GB")) {
		t.Errorf("low disk: %+v", low)
	}
	// Nothing measured is "unknown", not a crash.
	if u := computerRow(&runner.Resources{Jobs: 4}, nil); strings.Count(u.Detail, statusUnknown) != 4 {
		t.Errorf("unknown: %q", u.Detail)
	}
}

func TestNeedBytes(t *testing.T) {
	w := &Wizard{}
	if w.needBytes() != hostinfo.NeedBytes {
		t.Fatal("no doctor")
	}
	small, big := int64(1<<30), int64(40<<30)
	w.doctor = &runner.Doctor{Resources: &runner.Resources{DiskNeed: &small}}
	if w.needBytes() != hostinfo.NeedBytes {
		t.Fatal("a smaller estimate lowered the need")
	}
	w.doctor.Resources.DiskNeed = &big
	if w.needBytes() != uint64(big) {
		t.Fatal("a larger estimate was ignored")
	}
}

func TestJobsChoices(t *testing.T) {
	if got := jobsChoices(3, 4); !slices.Equal(got, []string{fmt.Sprintf(jobsAutoN, 3), "1", "2", "3", "4"}) {
		t.Fatalf("%q", got)
	}
	if got := jobsChoices(0, 0); !slices.Equal(got, []string{jobsAuto, "1"}) {
		t.Fatalf("%q", got)
	}
}

func selectOf(t *testing.T, o fyne.CanvasObject) *widget.Select {
	t.Helper()
	var r *widget.Select
	walkTree(o, func(c fyne.CanvasObject) {
		if s, ok := c.(*widget.Select); ok && strings.HasPrefix(s.Options[0], jobsAuto) {
			r = s
		}
	})
	if r == nil {
		t.Fatalf("no build jobs select in\n%s", shown(o))
	}
	return r
}

// The Summary's build jobs reach package and the settings; Automatic
// passes nothing.
func TestSummaryJobs(t *testing.T) {
	w, _ := pathWizard(t, func([]string) *runner.Plan { return basesPlan() })
	w.st.PatchFrom, w.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	w.show(pSummary)
	sel := selectOf(t, w.pages[pSummary].obj)
	if sel.Selected != sel.Options[0] || slices.Contains(w.st.PackageArgs("", "/o"), "--jobs") {
		t.Fatalf("not Automatic: %q %q", sel.Selected, w.st.PackageArgs("", "/o"))
	}
	sel.SetSelected("2")
	if w.st.Jobs != 2 || w.settings.Jobs != 2 || !strings.Contains(strings.Join(w.st.PackageArgs("", "/o"), " "), "--jobs 2") {
		t.Fatalf("jobs %d settings %d", w.st.Jobs, w.settings.Jobs)
	}
	if got := hostinfo.LoadSettings(w.setDir); got.Jobs != 2 {
		t.Fatalf("saved %+v", got)
	}
	sel.SetSelected(sel.Options[0])
	if w.st.Jobs != 0 || slices.Contains(w.st.PackageArgs("", "/o"), "--jobs") {
		t.Fatal("Automatic still passes --jobs")
	}
}

// Jobs saved above this computer's cores: Automatic, shown and passed.
func TestSummaryJobsOutOfRange(t *testing.T) {
	w, _ := pathWizard(t, func([]string) *runner.Plan { return basesPlan() })
	w.st.PatchFrom, w.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	w.st.Jobs = runtime.NumCPU() + 4
	w.show(pSummary)
	sel := selectOf(t, w.pages[pSummary].obj)
	if sel.Selected != sel.Options[0] || w.st.Jobs != 0 || slices.Contains(w.st.PackageArgs("", "/o"), "--jobs") {
		t.Fatalf("selected %q jobs %d %q", sel.Selected, w.st.Jobs, w.st.PackageArgs("", "/o"))
	}
}

// Short disk is not a refused update: the Summary says to free space and
// the build may start (package refuses itself), and the failure page
// offers Retry, not Fresh (D9).
func TestDiskShort(t *testing.T) {
	w, _ := pathWizard(t, func(args []string) *runner.Plan {
		p := basesPlan()
		if slices.Contains(args, "--patch-from") {
			p.Output = &runner.PlanOutput{Kind: "none", Why: "needs about 9.9 GB, 1.1 GB free",
				WhyCodes: []string{runner.WhyDisk}}
		}
		return p
	})
	w.st.PatchFrom, w.st.Dump = strings.Repeat("a", 12), "/d/game.iso"
	w.show(pSummary)
	txt := shown(w.pages[pSummary].obj)
	if !strings.Contains(txt, fmt.Sprintf(summaryDisk, "needs about 9.9 GB, 1.1 GB free")) || w.cannotUpdate() {
		t.Fatalf("cannot %v:\n%s", w.cannotUpdate(), txt)
	}

	bf := &buildFailure{Step: "package", Err: errors.New("exit status 1"),
		Tail: []string{"blinx2: not enough disk space", "blinx2: refused: disk"}}
	w.prog, w.buildErr = &runner.Progress{}, bf
	w.fillDone()
	done := shown(w.doneBox)
	if !strings.Contains(done, doneDisk) || strings.Contains(done, failedFresh) {
		t.Fatalf("done:\n%s", done)
	}
	if refusedUpdate(bf.Tail) || refusedCode(bf.Tail) != runner.WhyDisk {
		t.Fatal("disk counted as a refused update")
	}
}
