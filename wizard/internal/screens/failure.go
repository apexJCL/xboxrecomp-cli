package screens

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// errCanceled is a build the player stopped: there is nothing to read.
var errCanceled = errors.New("canceled")

// errNoBundle is a build whose commands all succeeded but never named the
// bundle they made.
var errNoBundle = errors.New("the build named no bundle")

// buildFailure is a build that stopped on its own: where (Step, in the
// player's words), which command, how it ended and the build log's last
// lines.
type buildFailure struct {
	Step, Cmd string
	Err       error
	Tail      []string
	Log       string // the failed run's full log, "" before it had one
}

// Error leads with the command when there is one: a failure the Done page
// rebuilds from a bare error has none.
func (f *buildFailure) Error() string {
	if f.Cmd == "" {
		return fmt.Sprint(f.Err)
	}
	return fmt.Sprintf("%s: %v", f.Cmd, f.Err)
}
func (f *buildFailure) Unwrap() error { return f.Err }

const (
	tailLines    = 120 // what a failure keeps of the build log
	excerptPlain = 12  // the excerpt without a traceback: the last non-blank lines
	excerptErrs  = 20  // the most compiler error lines an excerpt leads with
	excerptMax   = 40  // the most an excerpt shows
	excerptWidth = 200 // a line's most characters
	tracebackTop = "Traceback (most recent call last):"
)

// errorLine is a compiler's, linker's or Ninja's error line, as the CLI's
// ERROR_RE (package/progress.py) keeps them: "a.c:1:2: error: ...",
// "fatal error:", "ld.lld: error:", "FAILED: target".
var errorLine = regexp.MustCompile(`(^|[\s:])(fatal )?error:|^\s*FAILED: `)

// failureExcerpt is what the Done page and wizard.log show of a failed
// build's tail: from the last Python traceback when there is one, so it
// is shown whole, else the compiler's error lines (each once) and then the
// last few non-blank lines. A failed compile ends with Ninja's later lines
// and its error count, so the last lines alone miss the errors.
// A traceback longer than the limit keeps its first line and its end,
// where the error is.
func failureExcerpt(tail []string) []string {
	start := -1
	for i, l := range tail {
		if strings.TrimSpace(l) == tracebackTop {
			start = i
		}
	}
	var out []string
	if start >= 0 {
		out = append(out, tail[start:]...)
		if len(out) > excerptMax {
			out = append(out[:1:1], out[len(out)-(excerptMax-1):]...)
		}
	} else {
		var last []string
		for i := len(tail) - 1; i >= 0 && len(last) < excerptPlain; i-- {
			if strings.TrimSpace(tail[i]) != "" {
				last = append([]string{tail[i]}, last...)
			}
		}
		seen := map[string]bool{}
		for _, l := range last {
			seen[strings.TrimSpace(l)] = true
		}
		for _, l := range tail {
			t := strings.TrimSpace(l)
			if len(out) < excerptErrs && errorLine.MatchString(l) && !seen[t] {
				seen[t] = true
				out = append(out, l)
			}
		}
		out = append(out, last...)
	}
	for i, l := range out {
		out[i] = cut(strings.TrimRight(l, " \t\r"), excerptWidth)
	}
	return out
}

// cut keeps the first n characters of s, never splitting one.
func cut(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n-1]) + "…"
}

// fmtClock is a duration as the CLI's fmt_time prints it: m:ss, or
// h:mm:ss from an hour.
func fmtClock(d time.Duration) string {
	s := int(max(d, 0) / time.Second)
	if s >= 3600 {
		return fmt.Sprintf("%d:%02d:%02d", s/3600, s/60%60, s%60)
	}
	return fmt.Sprintf("%d:%02d", s/60, s%60)
}
