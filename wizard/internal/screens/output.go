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
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/hostinfo"
	"wizard/internal/repo"
	"wizard/internal/runner"
)

// The output choice, the build folder line and the disk line (D13). What
// the build will make comes from the CLI's `package --plan --json`; the
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

// outputText is the summary's line for what the build will make; the
// plan's reasons are the CLI's, so only the cases are told apart here.
func outputText(p *runner.Plan, target string, native bool, full bool, patchFrom string) string {
	if p == nil || p.Output == nil {
		return outputUnknown
	}
	o := p.Output
	dev := targetDevice[target]
	if o.Kind == "patch" && o.Base != nil {
		if native && patchFrom == "" {
			return fmt.Sprintf(outputUpdate, sized(o.PatchSize), verText(o.Base.Version))
		}
		return fmt.Sprintf(outputUpdateOn, sized(o.PatchSize), verText(o.Base.Version), dev)
	}
	var why string
	inst := o.Installed
	switch {
	case full:
		why = outputChosen
	case patchFrom != "":
		why = outputRefused
	case inst != nil && slices.Contains(inst.Why, "it is this version already"):
		why = fmt.Sprintf(outputSame, verText(inst.Version))
	case inst != nil:
		why = fmt.Sprintf(outputCannot, verText(inst.Version))
	case native:
		why = outputNoInstall
	case len(p.Bases) > 0:
		why = fmt.Sprintf(outputPick, dev)
	default:
		why = fmt.Sprintf(outputFirst, dev)
	}
	return fmt.Sprintf(outputFull, sized(p.FullSize), why)
}

func (w *Wizard) native() bool { return w.st.Target == hostinfo.NativeTarget() }

// askPlan asks the CLI what the build will make, off the UI thread; an
// answer a newer ask overtook is dropped. The seed is written first, as
// the build writes it, since the plan reads the same options.
func (w *Wizard) askPlan() {
	w.planRun++
	run := w.planRun
	w.plan = nil
	w.outputLine.SetText(outputAsking)
	w.outputChange.Disable()
	if w.env == nil {
		w.showPlan(nil)
		return
	}
	r := w.env.Runner
	seed, body := w.seedArg(), w.st.Seed()
	args := w.st.PlanArgs(seed)
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
	w.outputLine.SetText(outputText(p, w.st.Target, w.native(), w.st.Full, w.st.PatchFrom))
	if p != nil && p.Output != nil {
		w.outputChange.Enable()
	}
	tech := w.techBase
	if p != nil {
		var b strings.Builder
		b.WriteString("\n\n" + techPlan + "\n")
		for _, s := range p.Stages {
			if s.Why != "" {
				fmt.Fprintf(&b, "  %s %s (%s)\n", s.Action, s.Stage, s.Why)
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

// outputForm is the Change… dialog's controls: the kind, for another
// platform the version that computer has, and Rebuild everything.
type outputForm struct {
	kind    *widget.RadioGroup
	has     *widget.Select
	rebuild *widget.Check
	// shas are has's entries' bundle sha256s, in its order.
	shas []string
}

// outputDialog is the Change… dialog over the last plan; body is its
// content, for the tests.
func (w *Wizard) outputDialog() (dialog.Dialog, *outputForm, fyne.CanvasObject) {
	p := w.plan
	f := &outputForm{}
	var opts []string
	native := w.native()
	dev := targetDevice[w.st.Target]
	switch {
	case native && p.Output.Installed != nil && p.Output.Installed.Usable:
		opts = append(opts, fmt.Sprintf(choiceUpdate, verText(p.Output.Installed.Version)))
	case !native && len(p.Bases) > 0:
		opts = append(opts, choiceUpdateOn)
	}
	opts = append(opts, choiceFull)
	f.kind = widget.NewRadioGroup(opts, nil)
	f.kind.Required = true
	items := []fyne.CanvasObject{f.kind}
	if !native && len(p.Bases) > 0 {
		var labels []string
		for _, b := range p.Bases {
			made := b.Made
			if len(made) > 10 {
				made = made[:10]
			}
			labels = append(labels, fmt.Sprintf(choiceHasEntry, verText(b.Version), made, sized(b.PatchSize)))
			f.shas = append(f.shas, b.Sha256)
		}
		f.has = widget.NewSelect(labels, func(string) {
			if f.kind.Selected != choiceUpdateOn {
				f.kind.SetSelected(choiceUpdateOn)
			}
		})
		if i := slices.IndexFunc(f.shas, func(s string) bool {
			return w.st.PatchFrom != "" && strings.HasPrefix(s, w.st.PatchFrom)
		}); i >= 0 {
			f.has.SetSelectedIndex(i)
		}
		items = append(items, container.NewBorder(nil, nil, widget.NewLabel(fmt.Sprintf(choiceHas, upperFirst(dev))), nil, f.has))
	}
	f.rebuild = widget.NewCheck(choiceRebuild, nil)
	f.rebuild.SetChecked(w.st.Clean)
	items = append(items, f.rebuild, hintText(choiceRebuildHint))
	if p.Output.Kind == "patch" && !w.st.Full {
		f.kind.SetSelected(opts[0])
	} else {
		f.kind.SetSelected(choiceFull)
	}
	body := container.NewVBox(items...)
	d := dialog.NewCustomConfirm(choiceTitle, choiceOK, choiceCancel, body, func(ok bool) {
		if ok {
			w.applyChoice(f)
		}
	}, w.win)
	return d, f, body
}

// applyChoice sets the state from the dialog and asks the plan again.
func (w *Wizard) applyChoice(f *outputForm) {
	w.st.Clean = f.rebuild.Checked
	w.st.Full = f.kind.Selected == choiceFull
	switch {
	case w.st.Full:
		w.st.PatchFrom = ""
	case f.has != nil:
		w.st.PatchFrom = ""
		if i := f.has.SelectedIndex(); i >= 0 {
			// The plan's prefix length, which package takes for a record.
			w.st.PatchFrom = f.shas[i][:12]
		} else {
			w.st.Full = true
		}
	default:
		// The installed version, as package picks it; a developer's
		// --patch-from stays.
		w.st.PatchFrom = w.opt.PatchFrom
	}
	w.askPlan()
}

func upperFirst(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
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
