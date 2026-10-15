// Package runner runs the game's CLI. Every command goes through the game
// repo's own bootstrap (the <slug> script at its root, xboxrecomp-cli's
// wrapper/game.py) run by uv's Python, so the CLI is found, cloned at the
// [cli] pin and locked exactly as on the command line: the wizard has no
// second copy of that logic and no build steps of its own.
package runner

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"

	"wizard/internal/gamecfg"
)

// PythonVersion is the Python uv runs the bootstrap with (uv fetches it,
// checked by its own hashes, when the host has none).
const PythonVersion = "3.12"

type Runner struct {
	UV   string   // the uv executable
	Game string   // the game checkout: the clone, or --game DIR
	Slug string   // the bootstrap's name
	Env  []string // the children's environment (Environ)
}

// Environ is base with dirs put first on PATH, the shell's virtualenv
// dropped (the bootstrap does the same) and Python's output unbuffered
// and UTF-8 so the log pane gets each line as it is printed. tools is the
// install root's tools folder: uv's cache and the Python it installs go
// there, so deleting the root removes everything the wizard fetched. On
// Windows git gets core.longpaths for every clone below (the wizard's and
// the CLI's toolkit clone): long paths are off by default there.
func Environ(base []string, tools string, dirs ...string) []string {
	var out []string
	path := ""
	for _, kv := range base {
		k, v, _ := strings.Cut(kv, "=")
		switch strings.ToUpper(k) {
		case "PATH":
			path = v
			continue
		case "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
			"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_INSTALL_BIN", "UV_NO_MODIFY_PATH":
			continue
		case "GIT_CONFIG_COUNT":
			if runtime.GOOS == "windows" {
				continue
			}
		}
		out = append(out, kv)
	}
	parts := append([]string{}, dirs...)
	if path != "" {
		parts = append(parts, path)
	}
	out = append(out, "PATH="+strings.Join(parts, string(os.PathListSeparator)),
		"PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")
	if tools != "" {
		out = append(out, "UV_CACHE_DIR="+filepath.Join(tools, "uv-cache"),
			"UV_PYTHON_INSTALL_DIR="+filepath.Join(tools, "python"),
			"UV_PYTHON_INSTALL_BIN=0", "UV_NO_MODIFY_PATH=1")
	}
	if runtime.GOOS == "windows" {
		out = append(out, "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=core.longpaths", "GIT_CONFIG_VALUE_0=true")
	}
	return out
}

// Command is the full argument list for `<slug> ARGS`. It runs the
// bootstrap's Python half, <slug>.py: ./<slug> and <slug>.cmd are the
// shell and cmd wrappers around it (xbr new writes all three).
func (r Runner) Command(args ...string) []string {
	return append([]string{r.UV, "run", "--no-project", "--quiet", "--python", PythonVersion,
		"python", filepath.Join(r.Game, r.Slug+".py")}, args...)
}

// Display is the command as the player would type it in the clone.
func Display(slug string, args []string) string {
	q := make([]string, len(args))
	for i, a := range args {
		if a == "" || strings.ContainsAny(a, " \t\"'") {
			a = strconv.Quote(a)
		}
		q[i] = a
	}
	return "./" + slug + " " + strings.Join(q, " ")
}

func (r Runner) cmd(ctx context.Context, args ...string) *exec.Cmd {
	c := r.Command(args...)
	cmd := exec.CommandContext(ctx, c[0], c[1:]...)
	cmd.Dir = r.Game
	cmd.Env = r.Env
	setGroup(cmd)
	return cmd
}

// Output runs a command for its stdout; stderr goes to log.
func (r Runner) Output(ctx context.Context, log io.Writer, args ...string) ([]byte, error) {
	cmd := r.cmd(ctx, args...)
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = io.MultiWriter(&errb, orDiscard(log))
	if err := cmd.Run(); err != nil {
		tail := strings.TrimSpace(errb.String())
		if i := strings.LastIndex(tail, "\n"); i >= 0 && len(tail)-i < 400 {
			tail = tail[i+1:]
		}
		return out.Bytes(), fmt.Errorf("%s: %w: %s", Display(r.Slug, args), err, tail)
	}
	return out.Bytes(), nil
}

func orDiscard(w io.Writer) io.Writer {
	if w == nil {
		return io.Discard
	}
	return w
}

// jsonPart is the output from its first '{': a line the bootstrap or uv
// printed to stdout before the JSON does not break the decode.
func jsonPart(b []byte) []byte {
	if i := bytes.IndexByte(b, '{'); i > 0 {
		return b[i:]
	}
	return b
}

// DevConfig is `<slug> wizard config --json --dev`: developer mode's config
// from the working tree.
func (r Runner) DevConfig(ctx context.Context, log io.Writer) (*gamecfg.Config, error) {
	out, err := r.Output(ctx, log, "wizard", "config", "--json", "--dev")
	if err != nil {
		return nil, err
	}
	var raw json.RawMessage
	if err := json.NewDecoder(bytes.NewReader(jsonPart(out))).Decode(&raw); err != nil {
		return nil, fmt.Errorf("wizard config --json: %w", err)
	}
	return gamecfg.Parse(raw)
}

// Doctor is `doctor --json`.
type Doctor struct {
	Host struct {
		OS     string `json:"os"`
		Arch   string `json:"arch"`
		Python string `json:"python"`
	} `json:"host"`
	Lines    []string            `json:"lines"`
	Targets  []string            `json:"targets"`
	Blocked  map[string]string   `json:"blocked"`
	Blockers map[string][]string `json:"blockers"`
	Problems []string            `json:"problems"`
	Next     string              `json:"next"`
}

func ParseDoctor(b []byte) (*Doctor, error) {
	var d Doctor
	// The first JSON value only: text after it (a stray line on stdout)
	// does not break the decode.
	if err := json.NewDecoder(bytes.NewReader(jsonPart(b))).Decode(&d); err != nil {
		return nil, fmt.Errorf("doctor --json: %w", err)
	}
	if d.Blockers == nil {
		return nil, errors.New("doctor --json has no blockers: the game pins a CLI older than this wizard")
	}
	return &d, nil
}

// Doctor runs `doctor --json`. A CLI that exits non-zero after printing
// the report (one before the report was data) still gave the answer: only
// output that does not parse is a failure.
func (r Runner) Doctor(ctx context.Context, log io.Writer) (*Doctor, error) {
	out, err := r.Output(ctx, log, "doctor", "--json")
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) || ctx.Err() != nil {
			return nil, err
		}
		d, perr := ParseDoctor(out)
		if perr != nil {
			return nil, err
		}
		return d, nil
	}
	return ParseDoctor(out)
}

// Target is whether the build can make t here: what stops it that setup
// cannot install (the package run fetches everything else itself).
func (d *Doctor) Target(t string) (ok bool, reasons []string) {
	b, known := d.Blockers[t]
	if !known {
		return false, []string{"not a target of this game"}
	}
	return len(b) == 0, b
}

// Action is a fix the checks screen can offer for a blocker.
type Action struct {
	Kind    string // "clt": Apple's installer; "brew": runs in the log; "sudo": in a terminal
	Command string
}

var (
	brewRE = regexp.MustCompile(`brew install [A-Za-z0-9@.+_ -]*[A-Za-z0-9@.+_-]`)
	sudoRE = regexp.MustCompile(`sudo (?:apt|dnf|pacman) [A-Za-z0-9 ._+-]*[A-Za-z0-9._+-]`)
)

// Actions are the fixes named in the blockers, once each, in order.
func Actions(reasons []string) []Action {
	var out []Action
	seen := map[string]bool{}
	add := func(a Action) {
		if !seen[a.Command] {
			seen[a.Command] = true
			out = append(out, a)
		}
	}
	for _, r := range reasons {
		switch {
		case strings.Contains(r, "xcode-select --install"):
			add(Action{"clt", "xcode-select --install"})
		case brewRE.MatchString(r):
			add(Action{"brew", brewRE.FindString(r)})
		case sudoRE.MatchString(r):
			add(Action{"sudo", sudoRE.FindString(r)})
		}
	}
	return out
}

// Proc is a running command whose merged output is fed line by line.
type Proc struct {
	cmd  *exec.Cmd
	done chan error
	once sync.Once
	err  error
}

// Start runs args with stdout and stderr merged into lines (a line ends
// at \n or \r, so a tool's in-place counter still shows).
func (r Runner) Start(ctx context.Context, extraEnv []string, lines func(string), args ...string) (*Proc, error) {
	cmd := r.cmd(ctx, args...)
	cmd.Env = append(append([]string{}, r.Env...), extraEnv...)
	cmd.Cancel = func() error { return interrupt(cmd) }
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	p := &Proc{cmd: cmd, done: make(chan error, 1)}
	read := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64<<10), 1<<20)
		sc.Split(scanLines)
		for sc.Scan() {
			lines(sc.Text())
		}
		io.Copy(io.Discard, pr)
		close(read)
	}()
	go func() {
		err := cmd.Wait()
		pw.Close()
		<-read
		p.done <- err
	}()
	return p, nil
}

func scanLines(data []byte, atEOF bool) (int, []byte, error) {
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		return i + 1, bytes.TrimRight(data[:i], "\r"), nil
	}
	if atEOF && len(data) > 0 {
		return len(data), data, nil
	}
	return 0, nil, nil
}

// Wait is the command's end (nil on exit 0).
func (p *Proc) Wait() error {
	p.once.Do(func() { p.err = <-p.done })
	return p.err
}

// Cancel stops the command and its children: SIGINT to the process group
// (the CLI cleans up as on Ctrl+C), the process tree on Windows.
func (p *Proc) Cancel() error { return interrupt(p.cmd) }

// SlugOf reads [game] slug from a developer's game.toml: the bootstrap's
// name. One plain string, as the bootstrap reads [cli]; the CLI reads the
// rest.
func SlugOf(gameDir string) (string, error) {
	b, err := os.ReadFile(filepath.Join(gameDir, "game.toml"))
	if err != nil {
		return "", err
	}
	table := ""
	for _, raw := range strings.Split(string(b), "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") {
			table = strings.Trim(line, "[] ")
			continue
		}
		if table != "game" {
			continue
		}
		if m := slugRE.FindStringSubmatch(line); m != nil {
			return m[1], nil
		}
	}
	return "", fmt.Errorf("%s/game.toml has no [game] slug", gameDir)
}

var slugRE = regexp.MustCompile(`^slug\s*=\s*"([a-z0-9][a-z0-9_-]*)"`)
