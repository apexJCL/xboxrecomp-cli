package runner

import (
	"regexp"
	"strconv"
	"strings"
	"time"
)

// PartState is where a part of the build stands.
type PartState int

const (
	PartPending PartState = iota
	PartWorking
	PartDone
	PartFailed
	PartSkipped
	PartCanceled
)

// PlanPart is the name of the row that stands for `package` until its
// plan line names the stages. A CLI that prints no plan line renames it to
// its first stage, or leaves it standing for the whole command.
const PlanPart = "plan"

// maxPartLines bounds one part's own log; the full output is in the
// CLI's build-logs/ and the page's Details pane.
const maxPartLines = 2000

// Part is one row of the build's checklist: a command the wizard runs
// before package (Name is its words, "enhance hud"), a stage of
// package's plan ("setup"), or PlanPart before the plan is known.
type Part struct {
	Name string
	// Group is the platform of the command the part belongs to, "" for a
	// command every platform shares: one run may build several.
	Group string
	State PartState
	Step  string // the running step inside the part (the CLI's label)
	Pct   int    // the step's percentage, -1 when it printed none
	Count string // "i/n" from the step's percentage line or the tool's line
	Start time.Time
	End   time.Time
	Lines []string
	// Seq changes whenever Lines does: the page refreshes a row's log
	// only when it moved.
	Seq int
	cmd int // the index of the command the part belongs to
}

// Took is the part's time: start to end, or to now while it runs; 0
// when it never started.
func (p Part) Took(now time.Time) time.Duration {
	switch {
	case p.Start.IsZero():
		return 0
	case p.End.IsZero():
		return now.Sub(p.Start)
	}
	return p.End.Sub(p.Start)
}

// Checklist follows the build's parts from the commands the wizard runs
// and what they print (the same plain-mode lines Progress reads). It is
// not safe for concurrent use: the page holds a lock around it.
type Checklist struct {
	Parts []*Part
	cur   int // the part lines go to; -1 before the first command
	cmd   int // the running command
	floor float64
}

var (
	// "[1201/26190] Translating ..." in a tool's line, and the "(i/n)" of
	// a percentage line.
	countRE    = regexp.MustCompile(`\[(\d+)/(\d+)\]`)
	pctCountRE = regexp.MustCompile(`\((\d+)/(\d+)\)$`)
)

// NewChecklist is the checklist of the build's commands: one part per
// command before package (named by its leading words), and PlanPart for
// package, whose plan line replaces it with its stages.
func NewChecklist(steps [][]string) *Checklist { return NewGroupedChecklist(steps, nil) }

// NewGroupedChecklist is NewChecklist with each command's group (its
// platform, StepTargets' second list); its parts, the plan's stages
// included, carry it.
func NewGroupedChecklist(steps [][]string, groups []string) *Checklist {
	c := &Checklist{cur: -1, cmd: -1}
	for i, s := range steps {
		name := PlanPart
		if len(s) == 0 || s[0] != "package" {
			name = CommandName(s)
		}
		g := ""
		if i < len(groups) {
			g = groups[i]
		}
		c.Parts = append(c.Parts, &Part{Name: name, Group: g, Pct: -1, cmd: i})
	}
	return c
}

var wordRE = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)

// CommandName is a command's leading words, the subcommand without its
// arguments: "import" for `import D:\game.iso`, "enhance hud".
func CommandName(args []string) string {
	var words []string
	for _, a := range args {
		if !wordRE.MatchString(a) {
			break
		}
		words = append(words, a)
	}
	return strings.Join(words, " ")
}

// StartCommand starts command i: its first part runs.
func (c *Checklist) StartCommand(i int, now time.Time) {
	c.cmd = i
	for j, p := range c.Parts {
		if p.cmd == i {
			c.start(j, now)
			return
		}
	}
}

func (c *Checklist) start(j int, now time.Time) {
	p := c.Parts[j]
	p.State, p.Start, p.End = PartWorking, now, time.Time{}
	p.Step, p.Pct, p.Count = "", -1, ""
	c.cur = j
}

func (c *Checklist) finish(j int, s PartState, now time.Time) {
	p := c.Parts[j]
	p.State, p.End = s, now
	p.Pct = -1
}

// Feed takes one output line of the running command: it goes to the
// running part's log, and the plan, stage and step lines move the
// parts. It reports whether the list of parts changed shape (rows added
// or removed), which a page redraws rather than updates.
func (c *Checklist) Feed(line string, now time.Time) (reshaped bool) {
	if c.cur < 0 {
		return false
	}
	if m := planRE.FindStringSubmatch(line); m != nil && c.Parts[c.cur].Name == PlanPart {
		c.add(line)
		c.replacePlan(planSteps(m[1]))
		return true
	}
	if m := stageRE.FindStringSubmatch(line); m != nil {
		reshaped = c.stage(m[3], now)
		c.add(line)
		return reshaped
	}
	c.add(line)
	p := c.Parts[c.cur]
	switch {
	case heartRE.MatchString(line):
		m := heartRE.FindStringSubmatch(line)
		if p.Step == "" {
			p.Step = m[1]
		}
		if cm := countRE.FindStringSubmatch(m[4]); cm != nil && p.Pct < 0 {
			p.Count = cm[1] + "/" + cm[2]
		}
	case pctRE.MatchString(line):
		m := pctRE.FindStringSubmatch(line)
		p.Step = m[1]
		p.Pct, _ = strconv.Atoi(m[2])
		if cm := pctCountRE.FindStringSubmatch(line); cm != nil {
			p.Count = cm[1] + "/" + cm[2]
		}
	case failRE.MatchString(line):
	case beginRE.MatchString(line):
		p.Step, p.Pct, p.Count = beginRE.FindStringSubmatch(line)[1], -1, ""
	}
	return false
}

func (c *Checklist) add(line string) {
	p := c.Parts[c.cur]
	p.Lines = append(p.Lines, line)
	if len(p.Lines) > maxPartLines {
		p.Lines = p.Lines[len(p.Lines)-maxPartLines:]
	}
	p.Seq++
}

// replacePlan puts the plan's stages where the plan part was. The first
// stage takes over the plan part's clock and lines and runs at once: the
// time between the plan and its first stage line is that stage's.
func (c *Checklist) replacePlan(stages []string) {
	if len(stages) == 0 {
		return
	}
	old := c.Parts[c.cur]
	var parts []*Part
	for i, s := range stages {
		p := &Part{Name: s, Group: old.Group, Pct: -1, cmd: old.cmd}
		if i == 0 {
			p.State, p.Start, p.Lines, p.Seq = PartWorking, old.Start, old.Lines, old.Seq
		}
		parts = append(parts, p)
	}
	c.Parts = append(c.Parts[:c.cur], append(parts, c.Parts[c.cur+1:]...)...)
}

// stage moves the command to its stage name: the running part is done,
// pending parts it jumps over are skipped, and a name the list does not
// have is a part added after the running one.
func (c *Checklist) stage(name string, now time.Time) (reshaped bool) {
	cur := c.Parts[c.cur]
	if cur.Name == name && cur.State == PartWorking {
		return false // the first stage, already running since the plan
	}
	if cur.Name == PlanPart && cur.State == PartWorking {
		// No plan line came (an older CLI): the placeholder is the first
		// stage, with its clock and lines; later stages are added after.
		cur.Name = name
		return true
	}
	at := -1
	for j := c.cur + 1; j < len(c.Parts) && c.Parts[j].cmd == c.cmd; j++ {
		if c.Parts[j].Name == name && c.Parts[j].State == PartPending {
			at = j
			break
		}
	}
	if cur.State == PartWorking {
		c.finish(c.cur, PartDone, now)
	}
	if at < 0 {
		at = c.cur + 1
		p := &Part{Name: name, Group: cur.Group, Pct: -1, cmd: c.cmd}
		c.Parts = append(c.Parts[:at], append([]*Part{p}, c.Parts[at:]...)...)
		reshaped = true
	}
	for j := c.cur + 1; j < at; j++ {
		if c.Parts[j].State == PartPending {
			c.Parts[j].State = PartSkipped
		}
	}
	c.start(at, now)
	return reshaped
}

// EndCommand ends the running command: its running part is done, failed
// or cancelled. After a success its parts that never ran were not
// needed (skipped); after a failure or a cancel they, and every later
// command's, stay pending: not run.
func (c *Checklist) EndCommand(err error, canceled bool, now time.Time) {
	if c.cur < 0 {
		return
	}
	state := PartDone
	switch {
	case canceled:
		state = PartCanceled
	case err != nil:
		state = PartFailed
	}
	if c.Parts[c.cur].State == PartWorking {
		c.finish(c.cur, state, now)
	}
	if state != PartDone {
		return
	}
	for _, p := range c.Parts {
		if p.cmd == c.cmd && p.State == PartPending {
			p.State = PartSkipped
		}
	}
}

// Stop is a build that ended before its next command started (the
// player stopped it between two): nothing is running any more.
func (c *Checklist) Stop(canceled bool, now time.Time) {
	if c.cur >= 0 && c.Parts[c.cur].State == PartWorking {
		s := PartFailed
		if canceled {
			s = PartCanceled
		}
		c.finish(c.cur, s, now)
	}
}

// Running is the running part's index, -1 when none runs.
func (c *Checklist) Running() int {
	if c.cur >= 0 && c.Parts[c.cur].State == PartWorking {
		return c.cur
	}
	return -1
}

// Fraction is the share of the build done: the parts finished plus the
// running one's percentage, over the parts; never less than before (a
// plan that adds parts would otherwise move it back).
func (c *Checklist) Fraction() float64 {
	if len(c.Parts) == 0 {
		return c.floor
	}
	var f float64
	for _, p := range c.Parts {
		switch p.State {
		case PartDone, PartSkipped:
			f++
		case PartWorking:
			if p.Pct > 0 {
				f += float64(p.Pct) / 100
			}
		}
	}
	c.floor = max(c.floor, f/float64(len(c.Parts)))
	return c.floor
}

// Snapshot is a copy of the parts the page can read without the lock;
// each part keeps at most tail of its lines.
func (c *Checklist) Snapshot(tail int) []Part {
	out := make([]Part, len(c.Parts))
	for i, p := range c.Parts {
		out[i] = *p
		out[i].Lines = append([]string(nil), p.Lines[max(len(p.Lines)-tail, 0):]...)
	}
	return out
}
