package screens

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/dialog"

	"wizard/internal/hostinfo"
	"wizard/internal/repo"
	"wizard/internal/runner"
)

// The Summary's plan, the build folder line and the disk line (D13).
// What the build will make comes from the CLI's `package --plan --json`; the
// wizard never reads the build folder's bundle records itself, only the
// clone's mark for the Welcome line, before any tool is fetched.

// folderFound is the Welcome page's line for a build folder that already
// holds a marked clone, "" for none.
func (w *Wizard) folderFound() string {
	m := repo.MarkIn(filepath.Join(w.root, "src"))
	if m == nil {
		return ""
	}
	name := w.opt.Cfg.Game.Name
	if s := w.opt.Source; s != nil {
		return fmt.Sprintf(folderFoundLocal, name, short7(m.Commit), short7(s.Commit))
	}
	if m.Origin == "local" {
		return fmt.Sprintf(folderFoundDev, name, short7(m.Commit))
	}
	return fmt.Sprintf(folderFoundRelease, name, m.Ref, short7(m.Commit))
}

func (w *Wizard) showFolderFound() {
	if w.foundLabel == nil {
		return
	}
	if t := w.folderFound(); t != "" {
		w.foundLabel.SetText(t)
		w.foundLabel.Show()
	} else {
		w.foundLabel.SetText("")
		w.foundLabel.Hide()
	}
}

// verText is a version as the player reads it: its date and time, without
// the commits after them.
func verText(v string) string {
	if i := strings.IndexByte(v, '-'); i > 0 {
		return v[:i]
	}
	return v
}

// sizeText is a size in MB under a gigabyte, else in GB.
func sizeText(n int64) string {
	if n < 1e9 {
		return fmt.Sprintf("%d MB", max((n+5e5)/1e6, 1))
	}
	return fmt.Sprintf("%.1f GB", float64(n)/1e9)
}

func sized(n int64) string {
	if n <= 0 {
		return ""
	}
	return " (" + sizeText(n) + ")"
}

// askPlan asks the CLI what the build will make, one plan per platform
// in turn, off the UI thread; an answer a newer ask overtook is dropped.
// The seed is written first, as the build writes it, since the plan
// reads the same options.
func (w *Wizard) askPlan() {
	w.planRun++
	run := w.planRun
	w.plan, w.plans = nil, nil
	w.summaryPlan.Objects = []fyne.CanvasObject{para(summaryAsking)}
	w.summaryPlan.Refresh()
	w.refreshNav()
	if w.env == nil {
		w.showPlan(nil)
		return
	}
	r := w.env.Runner
	seed, body := w.seedArg(), w.st.Seed()
	var args [][]string
	for _, t := range w.st.Builds() {
		args = append(args, w.st.For(t).PlanArgs(seed, w.out))
	}
	w.async(func() {
		ps := make([]*runner.Plan, len(args))
		var err error
		if seed != "" {
			err = os.WriteFile(seed, []byte(body), 0o644)
		}
		for i, a := range args {
			if err != nil {
				break
			}
			var p *runner.Plan
			if p, err = w.planAsk(r, a); err == nil {
				ps[i] = p
			}
		}
		fyne.Do(func() {
			if run != w.planRun {
				return
			}
			if err != nil {
				w.Logf("plan: %v", err)
				ps = []*runner.Plan{nil}
			}
			w.showPlans(ps)
		})
	})
}

// planOf is `package --plan --json` through the game's bootstrap.
func (w *Wizard) planOf(r runner.Runner, args []string) (*runner.Plan, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	return r.Plan(ctx, LogWriter(func(s string) { w.Logf("plan: %s", s) }), args)
}

func (w *Wizard) showPlan(p *runner.Plan) { w.showPlans([]*runner.Plan{p}) }

// showPlans shows the plans of the platforms built, in Builds() order: a
// line each when there are several, the disk warning when any is short.
func (w *Wizard) showPlans(ps []*runner.Plan) {
	w.plans = ps
	p := ps[0]
	w.plan = p
	short := slices.IndexFunc(ps, diskShort)
	ts := w.st.Builds()
	// Each plan counts one platform; together they need more.
	var all *diskVolume
	if len(ps) > 1 {
		all = shortVolume(sumDisk(ts, ps, hostinfo.SameVolume))
	}
	var items []fyne.CanvasObject
	switch {
	case short >= 0:
		items = append(items, para(fmt.Sprintf(summaryDisk, ps[short].Output.Why)))
	case all != nil:
		items = append(items, para(fmt.Sprintf(summaryDiskAll, sizeText(all.Need), all.Path, sizeText(all.Free))))
	case w.st.Update():
		items = w.updateLines(p)
	case len(ps) > 1 && len(ps) == len(ts):
		for i, q := range ps {
			if q != nil {
				items = append(items, para(fmt.Sprintf(summaryFreshFor, platformName[ts[i]], sized(q.FullSize))))
			}
		}
	case p != nil:
		items = append(items, para(fmt.Sprintf(summaryFresh, sized(p.FullSize))))
	}
	if p != nil && p.Xbe != nil && p.Xbe.Verdict == "data" {
		items = append(items, para(fmt.Sprintf(summaryModData, strings.Join(p.Xbe.Sections, ", "))))
	}
	w.summaryPlan.Objects = items
	w.summaryPlan.Refresh()
	w.refreshNav()
	tech := w.techBase
	for i, p := range ps {
		if p == nil {
			continue
		}
		var b strings.Builder
		b.WriteString("\n\n" + techPlan + "\n")
		if len(ps) > 1 && i < len(ts) {
			b.WriteString("  " + platformName[ts[i]] + "\n")
		}
		for _, s := range p.Stages {
			if len(s.Why) > 0 {
				fmt.Fprintf(&b, "  %s %s (%s)\n", s.Action, s.Stage, strings.Join(s.Why, ", "))
			} else {
				fmt.Fprintf(&b, "  %s %s\n", s.Action, s.Stage)
			}
		}
		if o := p.Output; o != nil {
			fmt.Fprintf(&b, "  output: %s: %s\n", o.Kind, o.Why)
		}
		tech += strings.TrimRight(b.String(), "\n")
	}
	w.summaryTech.SetText(tech)
}

// diskVolume is one volume of the platforms' summed disk needs.
type diskVolume struct {
	Path      string
	Free      int64 // -1 when no plan knew it
	Need      int64
	slack     int64
	shared    map[string]int64 // stages all platforms share: the largest
	trees     map[string]int64 // a build tree's compile: the largest
	perTarget int64            // the bundles and staging, one per platform
}

// sumDisk adds up the plans' disk needs, one plan per platform in ts
// order, as one run of them all writes them: the import, setup, generate
// and HUD pack once (each plan counts them, so the largest stands), a
// compile per build tree (macOS's and the shared Windows one), and a
// bundle per platform. same says two paths are on one volume. A plan
// without disk needs (an older CLI) adds nothing: the sum is then low,
// and each run's own disk check still refuses before it writes.
func sumDisk(ts []string, ps []*runner.Plan, same func(a, b string) bool) []*diskVolume {
	var vols []*diskVolume
	for i, p := range ps {
		if p == nil || i >= len(ts) {
			continue
		}
		for _, d := range p.Disk {
			var v *diskVolume
			for _, u := range vols {
				if u.Path == d.Path || same(u.Path, d.Path) {
					v = u
					break
				}
			}
			if v == nil {
				v = &diskVolume{Path: d.Path, Free: -1, shared: map[string]int64{}, trees: map[string]int64{}}
				vols = append(vols, v)
			}
			if d.Free != nil && (v.Free < 0 || *d.Free < v.Free) {
				v.Free = *d.Free // the least seen: the conservative one
			}
			var staged int64
			for stage, n := range d.Stages {
				staged += n
				switch stage {
				case "build":
					v.trees[buildTree(ts[i])] = max(v.trees[buildTree(ts[i])], n)
				case "package":
					v.perTarget += n
				default:
					v.shared[stage] = max(v.shared[stage], n)
				}
			}
			v.slack = max(v.slack, d.Need-staged)
		}
	}
	for _, v := range vols {
		v.Need = v.slack + v.perTarget
		for _, n := range v.shared {
			v.Need += n
		}
		for _, n := range v.trees {
			v.Need += n
		}
	}
	return vols
}

// shortVolume is the first summed volume without the room the run needs.
func shortVolume(vols []*diskVolume) *diskVolume {
	for _, v := range vols {
		if v.Free >= 0 && v.Free < v.Need {
			return v
		}
	}
	return nil
}

// isPatch says whether the CLI's bundle line names an update: package
// names a patch "...-patch-from-<short>" on every target.
func isPatch(bundle string) bool {
	return strings.Contains(filepath.Base(bundle), "-patch-from-")
}

// applyText is the end screen's apply step for what was made.
func applyText(target, bundle string) string {
	if isPatch(bundle) {
		return applyPatchText[target]
	}
	return installText[target]
}

// --- the disk line

// showDisk measures the build folder off the UI thread and shows it with
// Free space.
func (w *Wizard) showDisk() {
	w.diskRun++
	run := w.diskRun
	root := w.root
	w.diskLabel.SetText(diskMeasuring)
	w.async(func() {
		n := w.treeSize(root)
		fyne.Do(func() {
			if run == w.diskRun {
				w.diskLabel.SetText(fmt.Sprintf(diskLine, sizeText(int64(n))))
			}
		})
	})
}

// freeSpace asks first: the next build compiles again.
func (w *Wizard) freeSpace() {
	dialog.ShowConfirm(freeTitle, freeText, func(ok bool) {
		if ok {
			w.runClean()
		}
	}, w.win)
}

func (w *Wizard) runClean() {
	if w.env == nil {
		return
	}
	r := w.env.Runner
	w.freeBtn.Disable()
	w.diskLabel.SetText(diskFreeing)
	w.async(func() {
		n, err := w.cleanRun(r)
		fyne.Do(func() {
			w.freeBtn.Enable()
			if err != nil {
				w.Logf("free space: %v", err)
				w.diskLabel.SetText(diskFreeFailed)
				return
			}
			w.Logf("free space: %d bytes", n)
			w.diskRun++
			run := w.diskRun
			root := w.root
			w.async(func() {
				left := w.treeSize(root)
				fyne.Do(func() {
					if run == w.diskRun {
						w.diskLabel.SetText(fmt.Sprintf(diskFreed, sizeText(n), sizeText(int64(left))))
					}
				})
			})
		})
	})
}

func (w *Wizard) clean(r runner.Runner) (int64, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	return r.Clean(ctx, LogWriter(func(s string) { w.Logf("free space: %s", s) }))
}
