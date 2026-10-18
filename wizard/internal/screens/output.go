package screens

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/dialog"

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

// askPlan asks the CLI what the build will make, off the UI thread; an
// answer a newer ask overtook is dropped. The seed is written first, as
// the build writes it, since the plan reads the same options.
func (w *Wizard) askPlan() {
	w.planRun++
	run := w.planRun
	w.plan = nil
	w.summaryPlan.Objects = []fyne.CanvasObject{para(summaryAsking)}
	w.summaryPlan.Refresh()
	w.refreshNav()
	if w.env == nil {
		w.showPlan(nil)
		return
	}
	r := w.env.Runner
	seed, body := w.seedArg(), w.st.Seed()
	args := w.st.PlanArgs(seed, w.out)
	w.async(func() {
		var p *runner.Plan
		var err error
		if seed != "" {
			err = os.WriteFile(seed, []byte(body), 0o644)
		}
		if err == nil {
			p, err = w.planAsk(r, args)
		}
		fyne.Do(func() {
			if run != w.planRun {
				return
			}
			if err != nil {
				w.Logf("plan: %v", err)
				p = nil
			}
			w.showPlan(p)
		})
	})
}

// planOf is `package --plan --json` through the game's bootstrap.
func (w *Wizard) planOf(r runner.Runner, args []string) (*runner.Plan, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	return r.Plan(ctx, LogWriter(func(s string) { w.Logf("plan: %s", s) }), args)
}

func (w *Wizard) showPlan(p *runner.Plan) {
	w.plan = p
	var items []fyne.CanvasObject
	switch {
	case diskShort(p):
		items = append(items, para(fmt.Sprintf(summaryDisk, p.Output.Why)))
	case w.st.Update():
		items = w.updateLines(p)
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
	if p != nil {
		var b strings.Builder
		b.WriteString("\n\n" + techPlan + "\n")
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
