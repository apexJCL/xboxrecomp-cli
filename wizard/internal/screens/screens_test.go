package screens

import (
	"os"
	"strings"
	"testing"

	"fyne.io/fyne/v2/test"

	"wizard"
	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/runner"
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

// Every page builds and renders on the sample config, headless.
func TestPages(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())
	a := test.NewApp()
	defer a.Quit()
	w := New(a, sampleOptions(t))
	b, _ := os.ReadFile("../runner/testdata/doctor.json")
	d, err := runner.ParseDoctor(b)
	if err != nil {
		t.Fatal(err)
	}
	w.doctor = d
	w.env = &Env{Root: w.root, Game: w.root}
	w.showChecks(nil)
	w.fillTargets()
	w.dumpEntry.SetText("/d/b2.iso")
	w.st.Dump = "/d/b2.iso"
	if err := w.st.Set("scale", "2"); err != nil {
		t.Fatal(err)
	}
	w.fillSummary()
	if !strings.Contains(w.summary.Text, "--seed-config") || !strings.Contains(w.summary.Text, "scale = 2") {
		t.Fatalf("summary:\n%s", w.summary.Text)
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
