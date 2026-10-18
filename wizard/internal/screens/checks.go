package screens

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	"wizard/internal/gamecfg"
	"wizard/internal/runner"
)

// The checks table: doctor --json turned into a handful of rows a player
// can read (D3). No Fyne here; checksView draws the rows.

type rowState int

const (
	stateBusy  rowState = iota // still checking
	stateOK                    // ready
	stateLater                 // missing, but the build fetches it itself
	stateWarn                  // the player can or must do something
	stateFail                  // getting ready stopped
)

type checkRow struct {
	Name, Status string
	Target       string // the config's target key on a platform row
	State        rowState
	Detail       string   // the plain explanation; "" with no Raw and no Actions hides the toggle
	Raw          []string // the CLI's own lines behind the row, for whoever helps the player
	Actions      []runner.Action
	Open         bool // the detail starts open
}

func (r checkRow) hasDetail() bool { return r.Detail != "" || len(r.Raw) > 0 || len(r.Actions) > 0 }

// The doctor keys each row reads. Any other line (the host, gen/, next,
// a key a newer CLI adds) is information only: it stays in the log.
var (
	toolKeys   = []string{"uv", "cmake", "ninja", "tools py", "toolkit", "llvm-mingw", "makensis"}
	sourceKeys = []string{"cli", "lock"}
	toolNames  = map[string]string{"tools py": "Python for the tools"}
)

// doctorLine is one `key: value` report line with its continuation lines
// (those starting with spaces).
type doctorLine struct {
	Key, Value string
	Raw        []string
}

func parseLines(lines []string) []doctorLine {
	var out []doctorLine
	for _, l := range lines {
		if strings.HasPrefix(l, " ") && len(out) > 0 {
			out[len(out)-1].Raw = append(out[len(out)-1].Raw, l)
			continue
		}
		k, v, _ := strings.Cut(l, ":")
		out = append(out, doctorLine{Key: strings.TrimSpace(k), Value: strings.TrimSpace(v), Raw: []string{l}})
	}
	return out
}

func isMissing(v string) bool {
	lv := strings.ToLower(v)
	return strings.HasPrefix(lv, "missing") || strings.HasPrefix(lv, "no ") || strings.Contains(lv, " missing")
}

// checkRows is the table for the doctor report d after Prepare made env
// (D3). d == nil with no prepErr is the table while the checks run;
// prepErr is the one-row table of a failed Prepare. brew says whether
// Homebrew is installed, which changes what its fix button does.
func checkRows(cfg *gamecfg.Config, env *Env, d *runner.Doctor, prepErr error, brew bool) []checkRow {
	if prepErr != nil {
		r := checkRow{Name: rowPrepare, Status: statusFailed, State: stateFail, Detail: plainPrepare(prepErr), Open: true}
		var need *NeedError
		if errors.As(prepErr, &need) && need.Action.Kind != "" {
			r.Actions = []runner.Action{need.Action}
		}
		return []checkRow{r}
	}
	// Only what the build depends on: the game files are the next page's,
	// and the host's own limits arrive as each target's blockers.
	if d == nil {
		rows := []checkRow{{Name: rowTools}, {Name: rowSource}}
		for _, t := range cfg.Targets {
			rows = append(rows, checkRow{Name: targetName(t), Target: t})
		}
		for i := range rows {
			rows[i].Status, rows[i].State = statusChecking, stateBusy
		}
		return rows
	}

	lines := parseLines(d.Lines)
	used := map[int]bool{}
	byKey := func(keys ...string) []doctorLine {
		var out []doctorLine
		for i, l := range lines {
			for _, k := range keys {
				if l.Key == k {
					out = append(out, l)
					used[i] = true
				}
			}
		}
		return out
	}
	var blockers []string
	for _, t := range cfg.Targets {
		_, why := d.Target(t)
		blockers = append(blockers, why...)
	}

	// Build tools: what setup fetches. makensis is a blocker of the
	// windows target where setup cannot fetch it; then its row says so.
	tools := checkRow{Name: rowTools, Status: statusReady, State: stateOK}
	var fetched []string
	for _, l := range byKey(toolKeys...) {
		tools.Raw = append(tools.Raw, l.Raw...)
		if !isMissing(l.Value) || l.Key == "makensis" && mentions(blockers, "makensis") {
			continue
		}
		name := l.Key
		if n, ok := toolNames[name]; ok {
			name = n
		}
		fetched = append(fetched, name)
	}
	if env != nil && env.Git != "" {
		tools.Raw = append(tools.Raw, "git:        "+env.Git)
	}
	if len(fetched) > 0 || len(d.Problems) > len(dumpProblems(d.Problems)) {
		tools.State, tools.Status = stateLater, statusFetched
	}
	if len(fetched) > 0 {
		tools.Detail = fmt.Sprintf(toolsFetched, strings.Join(fetched, ", "))
	} else {
		tools.Detail = toolsAllHere
	}

	// Game source: the clone (or --game), the CLI at its pin, the lock;
	// a warning right after the cli or toolkit line is that pin's drift.
	source := checkRow{Name: rowSource, Status: statusReady, State: stateOK}
	if env != nil && env.Dev {
		source.Detail = fmt.Sprintf(sourceDev, env.Game)
		source.State, source.Status = stateWarn, statusDevMode
	} else if env != nil && env.Local != nil {
		source.Detail = fmt.Sprintf(sourceLocal, env.Local.Path, short(env.Commit), env.Game)
		source.State, source.Status = stateWarn, statusLocal
	} else if env != nil {
		source.Detail = fmt.Sprintf(sourceAt, cfg.Release.Repo, cfg.Release.Tag, env.Game)
	}
	drift := false
	for i, l := range lines {
		switch {
		case l.Key == "cli" && strings.Contains(l.Value, "not the pin"),
			l.Key == "lock" && strings.Contains(l.Value, "failed"):
			drift = true
		case l.Key == "warning" && i > 0 && (lines[i-1].Key == "cli" || lines[i-1].Key == "toolkit"):
			drift = true
			used[i] = true
			source.Raw = append(source.Raw, l.Raw...)
		}
	}
	for _, l := range byKey(sourceKeys...) {
		source.Raw = append(source.Raw, l.Raw...)
	}
	if drift && source.State == stateOK {
		source.State, source.Status = stateWarn, statusNotPinned
	}

	// The game files line stays in the raw log only: the next page
	// chooses and checks the player's copy.
	byKey("game files")

	var notes []string
	for i, l := range lines {
		if l.Key == "warning" && !used[i] {
			notes = append(notes, l.Raw...)
			used[i] = true
		}
	}
	rows := []checkRow{tools, source}
	for _, t := range cfg.Targets {
		r := checkRow{Name: targetName(t), Target: t, Status: statusBuildable, State: stateOK}
		if ok, why := d.Target(t); !ok {
			r.State, r.Status = stateWarn, statusNeeds
			var paras []string
			for _, reason := range why {
				paras = append(paras, plainBlocker(reason, brew))
			}
			r.Detail = strings.Join(paras, "\n\n")
			r.Actions = runner.Actions(why)
		}
		rows = append(rows, r)
	}
	if len(notes) > 0 {
		rows = append(rows, checkRow{Name: rowNotes, Status: statusNotes, State: stateWarn, Raw: notes})
	}
	return rows
}

func targetName(t string) string {
	if n, ok := targetRowName[t]; ok {
		return n
	}
	return t
}

func mentions(list []string, word string) bool {
	for _, s := range list {
		if strings.Contains(s, word) {
			return true
		}
	}
	return false
}

// dumpProblems are the problems about the game files, which the next page
// settles rather than the build.
func dumpProblems(problems []string) []string {
	var out []string
	for _, p := range problems {
		if strings.HasSuffix(p, ".xbe") {
			out = append(out, p)
		}
	}
	return out
}

var brewFormulae = regexp.MustCompile(`brew install (.+)$`)

// plainBlocker is a doctor blocker in a player's words (D3).
func plainBlocker(reason string, brew bool) string {
	switch {
	case strings.Contains(reason, "xcode-select --install"):
		return blockCLT
	case strings.HasPrefix(reason, "Homebrew: brew install"):
		if !brew {
			return blockBrewNone
		}
		m := brewFormulae.FindStringSubmatch(strings.TrimSpace(reason))
		if m == nil {
			return reason
		}
		return fmt.Sprintf(blockBrewHave, strings.Join(strings.Fields(m[1]), ", "))
	case strings.HasPrefix(reason, "makensis:"):
		hint := strings.TrimSpace(strings.TrimPrefix(reason, "makensis:"))
		switch {
		case strings.HasPrefix(hint, "brew install") && !brew:
			return blockNSIS + " " + blockNSISNoBrew
		case strings.HasPrefix(hint, "brew install"):
			return blockNSIS + " " + blockNSISBrew
		case strings.HasPrefix(hint, "sudo "):
			return blockNSIS + " " + fmt.Sprintf(blockNSISSudo, hint)
		}
		return blockNSIS + " " + fmt.Sprintf(blockNSISOther, hint)
	case strings.Contains(reason, "needs a macOS host"):
		return blockMacOnly
	}
	return reason
}

// plainPrepare is a failed Prepare in a player's words: the fixes the
// player can make are reworded, anything else is shown as it is.
func plainPrepare(err error) string {
	var overlap *OverlapError
	var space *SpaceError
	var need *NeedError
	switch {
	case errors.As(err, &need):
		return need.Msg
	case errors.As(err, &overlap):
		return fmt.Sprintf(prepareOverlap, overlap.Root, overlap.Data)
	case errors.As(err, &space):
		return fmt.Sprintf(prepareNoSpace, gib(space.Free), gib(space.Need))
	}
	return err.Error()
}

// gib is a byte count in the GB the Welcome page states: hostinfo.NeedBytes
// is 15 GiB, so "about 15 GB" there and here are the same number.
func gib(b uint64) float64 { return float64(b) / (1 << 30) }
