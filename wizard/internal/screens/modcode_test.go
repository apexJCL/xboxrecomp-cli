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
