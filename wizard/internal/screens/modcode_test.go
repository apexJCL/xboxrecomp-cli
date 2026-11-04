package screens

import (
	"fmt"
	"strings"
	"testing"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/widget"

	"wizard/internal/runner"
)

func xbePlan(verdict string, sections ...string) func([]string) *runner.Plan {
	return func([]string) *runner.Plan {
		p := basesPlan()
		p.Xbe = &runner.PlanXbe{Sha256: strings.Repeat("e", 64), Verdict: verdict, Sections: sections}
		return p
	}
}

func dialogButtons(body fyne.CanvasObject) (texts []string, buttons []*widget.Button) {
	walkTree(body, func(o fyne.CanvasObject) {
		switch v := o.(type) {
		case *widget.Label:
			texts = append(texts, v.Text)
		case *widget.Button:
			buttons = append(buttons, v)
		}
	})
	return texts, buttons
}

// Modded code: Build asks once, Cancel stays on the Summary, and "Build
// anyway" goes on to the build with no extra flag.
func TestModdedCodeAsks(t *testing.T) {
	w, _ := pathWizard(t, xbePlan("code", ".text", "D3D"))
	w.pages[pBuild].enter = func() {}
	w.show(pSummary)
	before := strings.Join(w.st.PackageArgs("", "/o"), " ")
	w.onNext()
	if w.cur != pSummary {
		t.Fatalf("Build went on without asking: page %d", w.cur)
	}
	d, body := w.modCodeDialog()
	texts, buttons := dialogButtons(body)
	if len(texts) != 1 || texts[0] != fmt.Sprintf(modCodeText, ".text, D3D") {
		t.Errorf("texts %q", texts)
	}
	if len(buttons) != 2 || buttons[0].Text != "Cancel" || buttons[1].Text != "Build anyway" {
		t.Fatalf("buttons %v", buttons)
	}
	if buttons[1].Importance != widget.DangerImportance || buttons[0].Importance == widget.HighImportance {
		t.Error("Build anyway is not red, or Cancel is the default")
	}
	d.Show()
	test.Tap(buttons[0])
	if w.cur != pSummary {
		t.Fatalf("Cancel moved to page %d", w.cur)
	}
	d, body = w.modCodeDialog()
	d.Show()
	_, buttons = dialogButtons(body)
	test.Tap(buttons[1])
	if w.cur != pBuild {
		t.Fatalf("Build anyway: page %d", w.cur)
	}
	if after := strings.Join(w.st.PackageArgs("", "/o"), " "); after != before {
		t.Errorf("the build's arguments changed: %q, then %q", before, after)
	}
}

// Modded data: a Summary line and no question; a known dump: neither.
func TestModdedDataSummary(t *testing.T) {
	w, _ := pathWizard(t, xbePlan("data", ".rdata"))
	w.pages[pBuild].enter = func() {}
	w.show(pSummary)
	if txt := shown(w.pages[pSummary].obj); !strings.Contains(txt, fmt.Sprintf(summaryModData, ".rdata")) {
		t.Fatal(txt)
	}
	w.onNext()
	if w.cur != pBuild {
		t.Fatalf("modded data asked: page %d", w.cur)
	}
	k, _ := pathWizard(t, xbePlan("known"))
	k.show(pSummary)
	if txt := shown(k.pages[pSummary].obj); strings.Contains(txt, "modded") {
		t.Fatal(txt)
	}
}

func releasePlan(verdict, why string, other bool) func([]string) *runner.Plan {
	return func([]string) *runner.Plan {
		p := basesPlan()
		p.Xbe = &runner.PlanXbe{Sha256: strings.Repeat("e", 64), Verdict: verdict, Why: why,
			Release: "PAL, version 2", ReferenceRelease: "NTSC-U, version 6", OtherRelease: other}
		return p
	}
}

// Another release, unknown files and a cut-short copy: each gets its
// Summary line and asks before the build, with its own words, the way
// back first and no default.
func TestOtherReleaseAndUnknownAsk(t *testing.T) {
	release := fmt.Sprintf(releaseText, "PAL, version 2", "NTSC-U, version 6")
	for _, c := range []struct {
		name               string
		plan               func([]string) *runner.Plan
		summary, title, qn string
	}{
		{"release code", releasePlan("code", "", true),
			fmt.Sprintf(releaseText, "PAL, version 2", "NTSC-U, version 6"), releaseTitle, release},
		{"release data", releasePlan("data", "", true),
			fmt.Sprintf(releaseText, "PAL, version 2", "NTSC-U, version 6"), releaseTitle, release},
		{"release unknown", releasePlan("unknown", "", true),
			fmt.Sprintf(releaseText, "PAL, version 2", "NTSC-U, version 6"), releaseTitle, release},
		{"unknown", releasePlan("unknown", "", false), summaryUnknown, unknownTitle, unknownText},
		{"truncated", releasePlan("unknown", "truncated", true), summaryTruncated, unknownTitle, truncatedText},
	} {
		t.Run(c.name, func(t *testing.T) {
			w, _ := pathWizard(t, c.plan)
			w.pages[pBuild].enter = func() {}
			w.show(pSummary)
			if txt := shown(w.pages[pSummary].obj); !strings.Contains(txt, c.summary) {
				t.Fatalf("summary %q", txt)
			}
			if strings.Contains(shown(w.pages[pSummary].obj), "modded") {
				t.Error("another release or unknown files called modded")
			}
			w.onNext()
			if w.cur != pSummary {
				t.Fatalf("Build went on without asking: page %d", w.cur)
			}
			if title, _ := xbeQuestion(w.plan); title != c.title {
				t.Errorf("title %q", title)
			}
			d, body := w.modCodeDialog()
			texts, buttons := dialogButtons(body)
			if len(texts) != 1 || texts[0] != c.qn {
				t.Errorf("texts %q", texts)
			}
			if len(buttons) != 2 || buttons[0].Text != modCodeBack || buttons[1].Text != modCodeConfirm {
				t.Fatalf("buttons %v", buttons)
			}
			if buttons[1].Importance != widget.DangerImportance || buttons[0].Importance == widget.HighImportance {
				t.Error("Build anyway is not red, or Cancel is the default")
			}
			d.Show()
			test.Tap(buttons[0])
			if w.cur != pSummary {
				t.Fatalf("Cancel moved to page %d", w.cur)
			}
			d, body = w.modCodeDialog()
			d.Show()
			_, buttons = dialogButtons(body)
			test.Tap(buttons[1])
			if w.cur != pBuild {
				t.Fatalf("Build anyway: page %d", w.cur)
			}
		})
	}
}

// Another release whose sections all match builds as the known dump:
// no line, no question.
func TestOtherReleaseSameCodeIsQuiet(t *testing.T) {
	w, _ := pathWizard(t, releasePlan("header", "", true))
	w.pages[pBuild].enter = func() {}
	w.show(pSummary)
	if txt := shown(w.pages[pSummary].obj); strings.Contains(txt, "different release") {
		t.Fatal(txt)
	}
	w.onNext()
	if w.cur != pBuild {
		t.Fatalf("asked: page %d", w.cur)
	}
}
