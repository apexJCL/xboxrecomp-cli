//go:build e2e

// The Fresh and Update paths end to end: the wizard's pages, plans and
// build steps against the real CLI in a scratch game checkout, headless
// in Fyne's test driver. Not in check.sh: it builds the game. Run it with
//
//	XBR_E2E_GAME=<scratch checkout> XBR_E2E_CONFIG=<wizard config --json --dev>
//	XBR_E2E_DUMP=<disc> XBR_E2E_OUT=<dir> XBR_E2E_ROOT=<dir> \
//	go test -tags e2e -run TestE2E -timeout 3h ./internal/screens
//
// TestE2EOffline (the same variables) is a fresh tree with no network: the
// toolkit's dsp56300 download fails at the first configure, and its error
// must reach the player's Done page. It deletes the clone's package build
// trees and its dsp56300 cache, so a TestE2E run after it downloads again.
//
// The keep step commits an empty change to XBR_E2E_GAME, so it must be a
// scratch clone: an update needs a new version, and the version comes from
// the commits. The test refuses a checkout without the marker
// .git/xbr-e2e-scratch, which the e2e script writes into its clone, so the
// commit can never land in a real checkout.
package screens

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"fyne.io/fyne/v2/test"

	"wizard"
	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/model"
	"wizard/internal/runner"
)

// e2eMarker, in the clone's .git, marks it as scratch the test may commit
// to. In .git it is no untracked file, so the build's version stays clean.
const e2eMarker = "xbr-e2e-scratch"

func e2eEnv(t *testing.T, key string) string {
	t.Helper()
	v := os.Getenv(key)
	if v == "" {
		t.Skip(key + " is not set")
	}
	return v
}

// e2eRun is one build as the Done page saw it, for the record.
type e2eRun struct {
	Name    string     `json:"name"`
	Path    string     `json:"path"`
	Base    string     `json:"base_id,omitempty"`
	Steps   [][]string `json:"steps"`
	Summary string     `json:"summary"`
	Done    string     `json:"done"`
	Bundle  string     `json:"bundle"`
	ID      string     `json:"update_id"`
	Err     string     `json:"error,omitempty"`
}

// e2eSetup is the wizard both tests drive, over the scratch clone, and its
// config.
func e2eSetup(t *testing.T) (w *Wizard, cfg *gamecfg.Config, game, dump, out string) {
	t.Helper()
	game, cfgPath := e2eEnv(t, "XBR_E2E_GAME"), e2eEnv(t, "XBR_E2E_CONFIG")
	dump, out, root := e2eEnv(t, "XBR_E2E_DUMP"), e2eEnv(t, "XBR_E2E_OUT"), e2eEnv(t, "XBR_E2E_ROOT")
	if _, err := os.Stat(filepath.Join(game, ".git", e2eMarker)); err != nil {
		t.Fatalf("%s is not a scratch clone (no .git/%s): the test commits to it", game, e2eMarker)
	}
	b, err := os.ReadFile(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	cfg, err = gamecfg.Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	pins, err := fetch.LoadPins(wizard.PinsJSON)
	if err != nil {
		t.Fatal(err)
	}
	// The wizard's own settings go to a scratch home; the build and the
	// CLI keep the real one (uv's cache, the toolchains).
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	a := test.NewApp()
	t.Cleanup(a.Quit)
	w = New(a, Options{Cfg: cfg, Pins: pins, Version: "e2e", Root: root, DevGame: game,
		NoDialog: true, NoUpdateCheck: true})
	w.async = func(f func()) { f() }
	env, err := Prepare(t.Context(), cfg, pins, root, game, nil, func(s string) { t.Log(s) }, nil)
	if err != nil {
		t.Fatal(err)
	}
	w.env = env
	w.setOut(out)
	return w, cfg, game, dump, out
}

func TestE2E(t *testing.T) {
	w, cfg, game, dump, out := e2eSetup(t)
	var runs []e2eRun
	defer func() {
		j, _ := json.MarshalIndent(runs, "", "  ")
		if err := os.WriteFile(filepath.Join(out, "e2e-runs.json"), j, 0o644); err != nil {
			t.Error(err)
		}
	}()

	// 7.1: Fresh, no tweaks.
	w.setPath(model.PathFresh)
	w.st.Target, w.st.Dump = "steamos", dump
	w.show(pSummary)
	fresh := e2eBuild(t, w, "fresh", "")
	runs = append(runs, fresh)
	if slices.ContainsFunc(fresh.Steps, func(s []string) bool { return slices.Contains(s, "--patch-from") }) ||
		!slices.Contains(fresh.Steps[len(fresh.Steps)-1], "--full") {
		t.Fatalf("Fresh is not the full installer: %q", fresh.Steps)
	}

	// 7.2: Update with Sharper HUD ticked, on the build Fresh made.
	w.setPath(model.PathUpdate)
	e2eBase(t, w, fresh.ID)
	w.show(pChanges)
	checkOf(t, w.pages[pChanges].obj, hudLabel(t, cfg)).SetChecked(true)
	e2eDisc(w, dump)
	w.show(pSummary)
	hud := e2eBuild(t, w, "update-hud", fresh.ID)
	runs = append(runs, hud)
	if !e2eHas(hud.Steps, "enhance", "hud") || !e2eHas(hud.Steps, "--hud-pack") {
		t.Fatalf("no new pack: %q", hud.Steps)
	}

	// 7.3: a change, then Update with the box unticked: the pack is kept,
	// with no disc and no enhance hud.
	git := exec.Command("git", "-c", "user.name=e2e", "-c", "user.email=e2e@localhost",
		"commit", "-q", "--allow-empty", "-m", "e2e: a new version")
	git.Dir = game
	if o, err := git.CombinedOutput(); err != nil {
		t.Fatalf("git commit: %v\n%s", err, o)
	}
	w.setPath(model.PathUpdate)
	e2eBase(t, w, hud.ID)
	w.show(pChanges)
	if c := checkOf(t, w.pages[pChanges].obj, hudLabel(t, cfg)); c.Checked {
		c.SetChecked(false)
	}
	if w.st.NeedDisc {
		t.Fatalf("keep asks for the disc:\n%s", shown(w.pages[pChanges].obj))
	}
	w.st.Dump = ""
	w.show(pSummary)
	keep := e2eBuild(t, w, "update-keep", hud.ID)
	runs = append(runs, keep)
	if e2eHas(keep.Steps, "enhance", "hud") || e2eHas(keep.Steps, "--dump") || !e2eHas(keep.Steps, "--keep-hud-pack") {
		t.Fatalf("keep: %q", keep.Steps)
	}

	// 7.4: Update with Clear previous tweaks.
	w.setPath(model.PathUpdate)
	e2eBase(t, w, keep.ID)
	w.show(pChanges)
	checkOf(t, w.pages[pChanges].obj, changesClear).SetChecked(true)
	w.show(pSummary)
	clear := e2eBuild(t, w, "update-clear", keep.ID)
	runs = append(runs, clear)
	if !e2eHas(clear.Steps, "--clear-hud-pack") {
		t.Fatalf("clear: %q", clear.Steps)
	}
}

func TestE2EOffline(t *testing.T) {
	w, _, game, dump, out := e2eSetup(t)
	// A fresh tree: no package build tree and no shared download (the CLI's
	// XBOXRECOMP_DSP56300_CACHE). Prepare has run, so only the build steps
	// lose the network: CMake's download and uv both honour the proxy.
	for _, d := range []string{"build-pkg-win", "build-pkg-macos", filepath.Join("third_party", "dsp56300")} {
		if err := os.RemoveAll(filepath.Join(game, d)); err != nil {
			t.Fatal(err)
		}
	}
	dead := "http://127.0.0.1:9"
	w.env.Runner.Env = append(slices.Clone(w.env.Runner.Env), "UV_OFFLINE=1",
		"https_proxy="+dead, "HTTPS_PROXY="+dead, "http_proxy="+dead, "HTTP_PROXY="+dead, "ALL_PROXY="+dead)
	w.setPath(model.PathFresh)
	w.st.Target, w.st.Dump = "steamos", dump
	w.show(pSummary)
	r, err := e2eRunSteps(t, w, "offline", "")
	j, _ := json.MarshalIndent([]e2eRun{r}, "", "  ")
	if werr := os.WriteFile(filepath.Join(out, "e2e-offline.json"), j, 0o644); werr != nil {
		t.Error(werr)
	}
	if err == nil {
		t.Fatalf("offline: the build passed with no network:\n%s", r.Done)
	}
	// The player's error view names the download, the offline copy and
	// the opt-out, as cmake/dsp56300.cmake's error does.
	for _, want := range []string{"dsp56300", "XBOXRECOMP_DSP56300_ARCHIVE", "XBOXRECOMP_APU_DSP=OFF"} {
		if !strings.Contains(r.Done, want) {
			t.Errorf("offline: the Done page does not say %q:\n%s\n%s", want, r.Done, r.Err)
		}
	}
}

func hudLabel(t *testing.T, cfg *gamecfg.Config) string {
	t.Helper()
	for _, o := range cfg.Options {
		if o.Flag == "--hud-pack" {
			return o.Label
		}
	}
	t.Fatal("the game has no --hud-pack option")
	return ""
}

// e2eBase picks, on the Base page, the build with that update ID.
func e2eBase(t *testing.T, w *Wizard, id string) {
	t.Helper()
	w.st.PatchFrom = ""
	w.show(pBase)
	// Rows of one version share a label (a rebuild of the same commits),
	// so the radio is found by its row, the order fillBase made them in.
	for i, r := range w.baseRows() {
		if r.base != nil && r.base.ID == id {
			if !r.usable {
				t.Fatalf("build %s cannot take the update: %s", id, r.detail)
			}
			w.baseRadios[i].SetSelected(r.label)
			if w.st.PatchFrom != r.arg {
				t.Fatalf("picked %q, base %q", r.arg, w.st.PatchFrom)
			}
			return
		}
	}
	t.Fatalf("no build %s on the Base page:\n%s", id, shown(w.pages[pBase].obj))
}

// e2eDisc gives the disc when the Changes page asks for it.
func e2eDisc(w *Wizard, dump string) {
	if w.st.NeedDisc {
		w.st.Dump = dump
	} else {
		w.st.Dump = ""
	}
}

// e2eBuild runs a build that has to pass (e2eRunSteps), then the update
// ID of the bundle made, the newest build the Base page lists.
func e2eBuild(t *testing.T, w *Wizard, name, base string) e2eRun {
	t.Helper()
	r, err := e2eRunSteps(t, w, name, base)
	if err != nil {
		t.Fatalf("%s: %s\n%s", name, r.Err, r.Done)
	}
	newest := (*runner.PlanBase)(nil)
	w.show(pBase)
	if w.basePlan != nil {
		for i := range w.basePlan.Bases {
			if b := &w.basePlan.Bases[i]; newest == nil || b.Made > newest.Made {
				newest = b
			}
		}
	}
	if newest == nil {
		t.Fatalf("%s: no build listed after it", name)
	}
	r.ID = newest.ID
	t.Logf("%s: %s, update ID %s", name, r.Bundle, r.ID)
	return r
}

// e2eRunSteps presses Build on a ready Summary and runs the steps as the
// Build page does, here on the test's goroutine, then fills the Done page.
// The error is the build's.
func e2eRunSteps(t *testing.T, w *Wizard, name, base string) (e2eRun, error) {
	t.Helper()
	if !w.pages[pSummary].ready() {
		t.Fatalf("%s: the Summary is not ready:\n%s", name, shown(w.pages[pSummary].obj))
	}
	r := e2eRun{Name: name, Path: w.st.Path, Base: base, Summary: shown(w.pages[pSummary].obj)}
	w.prog = &runner.Progress{}
	seed := w.seedArg()
	var targets []string
	r.Steps, targets = w.st.StepTargets(seed, w.out)
	w.partsMu.Lock()
	w.parts = runner.NewGroupedChecklist(r.Steps, targets)
	w.partsMu.Unlock()
	t.Logf("%s: %q", name, r.Steps)
	results, err := w.runSteps(seed, r.Steps, targets)
	w.buildErr, w.results = err, results
	w.fillDone()
	r.Done, r.Bundle = shown(w.doneBox), w.prog.Bundle
	if err != nil {
		r.Err = err.Error()
		if f, ok := err.(*buildFailure); ok {
			r.Err += "\n" + strings.Join(f.Tail, "\n")
		}
	}
	return r, err
}

func e2eHas(steps [][]string, words ...string) bool {
	return slices.ContainsFunc(steps, func(s []string) bool {
		for _, w := range words {
			if !slices.Contains(s, w) {
				return false
			}
		}
		return true
	})
}
