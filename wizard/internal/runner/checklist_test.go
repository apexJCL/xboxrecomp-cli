package runner

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"
)

var t0 = time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)

func at(s int) time.Time { return t0.Add(time.Duration(s) * time.Second) }

// shape is the parts as "name:state" in order.
func shape(c *Checklist) string {
	var out []string
	for _, p := range c.Parts {
		out = append(out, fmt.Sprintf("%s:%d", p.Name, p.State))
	}
	return strings.Join(out, " ")
}

const (
	P = PartPending
	W = PartWorking
	D = PartDone
	F = PartFailed
	S = PartSkipped
	X = PartCanceled
)

func want(t *testing.T, c *Checklist, parts ...any) {
	t.Helper()
	var w []string
	for i := 0; i < len(parts); i += 2 {
		w = append(w, fmt.Sprintf("%s:%d", parts[i], parts[i+1]))
	}
	if got := shape(c); got != strings.Join(w, " ") {
		t.Fatalf("parts %s, want %s", got, strings.Join(w, " "))
	}
}

func TestCommandName(t *testing.T) {
	for _, c := range []struct {
		args []string
		want string
	}{
		{[]string{"import", `D:\games\blinx.iso`}, "import"},
		{[]string{"import", "/d/games/blinx 2.iso"}, "import"},
		{[]string{"enhance", "hud"}, "enhance hud"},
		{[]string{"package", "windows", "--dump", "x"}, "package windows"},
		{nil, ""},
	} {
		if got := CommandName(c.args); got != c.want {
			t.Errorf("%q: %q, want %q", c.args, got, c.want)
		}
	}
}

// The HUD pack's build: two commands, then package's plan replaces its
// placeholder; stages run in order, each with its own lines and clock.
func TestChecklistPlan(t *testing.T) {
	c := NewChecklist([][]string{{"import", "/d.iso"}, {"enhance", "hud"}, {"package", "windows", "--plain"}})
	want(t, c, "import", P, "enhance hud", P, PlanPart, P)
	c.StartCommand(0, at(0))
	c.Feed("$ ./blinx2 import /d.iso", at(0))
	c.Feed("copying 1234 files", at(1))
	c.EndCommand(nil, false, at(10))
	c.StartCommand(1, at(10))
	c.Feed("  upscale: 50% (10/20)", at(12))
	if p := c.Parts[1]; p.Step != "upscale" || p.Pct != 50 || p.Count != "10/20" {
		t.Fatalf("enhance hud: %+v", p)
	}
	c.EndCommand(nil, false, at(70))
	c.StartCommand(2, at(70))
	c.Feed("$ ./blinx2 package windows --plain", at(70))
	if !c.Feed("plan: setup (toolkit, uv), generate (no gen/), build (build-win), package (windows)", at(72)) {
		t.Fatal("the plan did not reshape the list")
	}
	want(t, c, "import", D, "enhance hud", D, "setup", W, "generate", P, "build", P, "package", P)
	// The first stage took the placeholder's clock and lines.
	if s := c.Parts[2]; !s.Start.Equal(at(70)) || len(s.Lines) != 2 || !strings.HasPrefix(s.Lines[1], "plan:") {
		t.Fatalf("setup after the plan: %+v", s)
	}
	if c.Feed("== [1/4] setup ==", at(73)) {
		t.Fatal("the running stage's own line reshaped the list")
	}
	want(t, c, "import", D, "enhance hud", D, "setup", W, "generate", P, "build", P, "package", P)
	c.Feed("== [2/4] generate ==", at(100))
	c.Feed("  recomp ... 4:12, ~7 min left | [1201/26190] Translating sub_1234", at(352))
	g := c.Parts[3]
	if g.State != W || g.Step != "recomp" || g.Count != "1201/26190" || g.Pct != -1 || !g.Start.Equal(at(100)) {
		t.Fatalf("generate: %+v", g)
	}
	if s := c.Parts[2]; s.State != D || s.Took(at(999)) != 30*time.Second {
		t.Fatalf("setup: %+v", s)
	}
	if len(c.Parts[2].Lines) != 3 || len(g.Lines) != 2 {
		t.Fatalf("lines went to the wrong part: %q / %q", c.Parts[2].Lines, g.Lines)
	}
	c.Feed("== [3/4] build ==", at(400))
	c.Feed("== [4/4] package ==", at(900))
	c.Feed("bundle: /out/BLiNX2-1-windows", at(950))
	c.EndCommand(nil, false, at(951))
	want(t, c, "import", D, "enhance hud", D, "setup", D, "generate", D, "build", D, "package", D)
	if f := c.Fraction(); f != 1 {
		t.Fatalf("fraction %v", f)
	}
	if c.Running() != -1 {
		t.Fatal("a part still runs")
	}
}

// What the plan does not foresee: a stage added at run time, a jump over
// a stage, a stage never reached, no plan line at all.
func TestChecklistSurprises(t *testing.T) {
	c := NewChecklist([][]string{{"package", "macos", "--plain"}})
	c.StartCommand(0, at(0))
	c.Feed("plan: import (x), setup (y), build (z), package (macos)", at(1))
	if !c.Feed("== [2/5] fetch ==", at(5)) {
		t.Fatal("a stage outside the plan did not reshape")
	}
	want(t, c, "import", D, "fetch", W, "setup", P, "build", P, "package", P)
	c.Feed("== [4/5] build ==", at(9))
	want(t, c, "import", D, "fetch", D, "setup", S, "build", W, "package", P)
	c.EndCommand(nil, false, at(20))
	want(t, c, "import", D, "fetch", D, "setup", S, "build", D, "package", S)

	// An older CLI or a failure before the plan: the placeholder stands
	// for the command and takes its end.
	c = NewChecklist([][]string{{"package", "windows"}})
	c.StartCommand(0, at(0))
	c.Feed("error: no dump", at(1))
	c.EndCommand(errors.New("exit status 1"), false, at(2))
	want(t, c, PlanPart, F)
	if !slices.Equal(c.Parts[0].Lines, []string{"error: no dump"}) {
		t.Fatalf("lines %q", c.Parts[0].Lines)
	}

	// Stage lines with no plan line: the placeholder becomes the first
	// stage, with its clock and lines, and the next ones are added.
	c = NewChecklist([][]string{{"package", "windows"}})
	c.StartCommand(0, at(0))
	c.Feed("$ ./blinx2 package windows", at(0))
	if !c.Feed("== [1/2] setup ==", at(1)) {
		t.Fatal("the rename did not reshape the list")
	}
	want(t, c, "setup", W)
	if p := c.Parts[0]; !p.Start.Equal(at(0)) || len(p.Lines) != 2 {
		t.Fatalf("the first stage: %+v", p)
	}
	c.Feed("== [2/2] build ==", at(5))
	c.EndCommand(nil, false, at(9))
	want(t, c, "setup", D, "build", D)
}

func TestChecklistFailureAndCancel(t *testing.T) {
	steps := [][]string{{"enhance", "hud"}, {"package", "windows"}}
	c := NewChecklist(steps)
	c.StartCommand(0, at(0))
	c.Feed("Traceback (most recent call last):", at(1))
	c.EndCommand(errors.New("exit status 1"), false, at(3))
	// The failed part keeps its lines; the rest did not run.
	want(t, c, "enhance hud", F, PlanPart, P)
	if c.Parts[0].Took(at(100)) != 3*time.Second {
		t.Fatalf("took %v", c.Parts[0].Took(at(100)))
	}

	c = NewChecklist(steps[1:])
	c.StartCommand(0, at(0))
	c.Feed("plan: setup (a), build (b), package (windows)", at(1))
	c.Feed("== [2/3] build ==", at(5))
	c.Feed("  compile: 40% (400/1000)", at(6))
	if f := c.Fraction(); f < 0.46 || f > 0.47 {
		t.Fatalf("fraction %v, want (1 + 0.4) / 3", f)
	}
	c.EndCommand(errors.New("signal: interrupt"), true, at(7))
	want(t, c, "setup", D, "build", X, "package", P)
	// A stop between two commands.
	c = NewChecklist(steps)
	c.StartCommand(0, at(0))
	c.Stop(true, at(1))
	want(t, c, "enhance hud", X, PlanPart, P)
}

// A part's log is bounded and its sequence moves with every line.
func TestChecklistLines(t *testing.T) {
	c := NewChecklist([][]string{{"enhance", "hud"}})
	if c.Feed("before any command", at(0)) {
		t.Fatal("a line before the start")
	}
	c.StartCommand(0, at(0))
	for i := range maxPartLines + 10 {
		c.Feed(fmt.Sprint("line ", i), at(1))
	}
	p := c.Parts[0]
	if len(p.Lines) != maxPartLines || p.Lines[0] != "line 10" || p.Seq != maxPartLines+10 {
		t.Fatalf("%d lines from %q, seq %d", len(p.Lines), p.Lines[0], p.Seq)
	}
	snap := c.Snapshot(5)
	if len(snap[0].Lines) != 5 || snap[0].Lines[4] != fmt.Sprint("line ", maxPartLines+9) {
		t.Fatalf("snapshot %q", snap[0].Lines)
	}
	snap[0].Lines[0] = "changed"
	if c.Parts[0].Lines[maxPartLines-5] == "changed" {
		t.Fatal("the snapshot shares the lines")
	}
}
