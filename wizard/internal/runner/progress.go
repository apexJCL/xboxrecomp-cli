package runner

import (
	"regexp"
	"strconv"
	"strings"
)

// Progress follows what `package --plain` prints (the CLI's
// package/progress.py plain mode): the plan line, a "== [n/m] step =="
// line per stage, a percentage line every 10% at most, the end marks,
// the bundle and the failed step's log. Nothing else is parsed.
type Progress struct {
	Steps  []string // the plan's step names
	Index  int      // the running stage, 1-based; 0 before the first
	Total  int
	Stage  string
	Label  string // the running step inside the stage
	Pct    int    // the running step's percentage, -1 when it prints none
	Bundle string
	Failed string
	Log    string
	floor  float64 // the bar so far: a stage's next step restarts its percentage
}

var (
	planRE   = regexp.MustCompile(`^plan: (.+)$`)
	stageRE  = regexp.MustCompile(`^== \[(\d+)/(\d+)\] (.+) ==$`)
	pctRE    = regexp.MustCompile(`^  (.+): (\d+)% \(\d+/\d+\)$`)
	beginRE  = regexp.MustCompile(`^  (.+) \.\.\.$`)
	failRE   = regexp.MustCompile(`^  FAILED (.+?)  \S+$`)
	bundleRE = regexp.MustCompile(`^bundle: (.+)$`)
	logRE    = regexp.MustCompile(`^\s*full log: (.+)$`)
)

// planSteps splits "plan: import (why), setup (a, b), build (x), package (macos)"
// into the step names; commas inside the parentheses do not split.
func planSteps(s string) []string {
	var out []string
	depth, start := 0, 0
	for i, c := range s + "," {
		switch {
		case c == '(':
			depth++
		case c == ')':
			depth--
		case c == ',' && depth == 0:
			name, _, _ := strings.Cut(strings.TrimSpace(s[start:i]), " (")
			if name != "" {
				out = append(out, name)
			}
			start = i + 1
		}
	}
	return out
}

// Feed takes one output line; it reports whether the state changed.
func (p *Progress) Feed(line string) bool {
	if !p.feed(line) {
		return false
	}
	p.floor = p.Fraction()
	return true
}

func (p *Progress) feed(line string) bool {
	if m := planRE.FindStringSubmatch(line); m != nil {
		p.Steps = planSteps(m[1])
		p.Total = len(p.Steps)
		return true
	}
	if m := stageRE.FindStringSubmatch(line); m != nil {
		p.Index, _ = strconv.Atoi(m[1])
		p.Total, _ = strconv.Atoi(m[2])
		p.Stage, p.Label, p.Pct = m[3], "", -1
		return true
	}
	if m := pctRE.FindStringSubmatch(line); m != nil {
		p.Label = m[1]
		p.Pct, _ = strconv.Atoi(m[2])
		return true
	}
	if m := failRE.FindStringSubmatch(line); m != nil {
		p.Failed = m[1]
		return true
	}
	if m := beginRE.FindStringSubmatch(line); m != nil {
		p.Label, p.Pct = m[1], -1
		return true
	}
	if m := bundleRE.FindStringSubmatch(line); m != nil {
		p.Bundle = strings.TrimSpace(m[1])
		return true
	}
	if m := logRE.FindStringSubmatch(line); m != nil {
		p.Log = strings.TrimSpace(m[1])
		return true
	}
	return false
}

// Fraction is the bar: the stages done plus the running step's share,
// never less than it showed before.
func (p *Progress) Fraction() float64 {
	if p.Total == 0 || p.Index == 0 {
		return p.floor
	}
	f := float64(p.Index - 1)
	if p.Pct > 0 {
		f += float64(p.Pct) / 100
	}
	if p.Bundle != "" {
		f = float64(p.Total)
	}
	return max(f/float64(p.Total), p.floor)
}
