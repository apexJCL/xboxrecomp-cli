package screens

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"

	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/hostinfo"
	"wizard/internal/repo"
	"wizard/internal/runner"
)

// Env is what the checks set up: the tools and the game checkout the
// build runs in.
type Env struct {
	Root   string
	Game   string
	UV     string
	Git    string
	Dirs   []string // the tools folders, first on the children's PATH
	Dev    bool     // --game: the developer's tree as it is, not the release
	Runner runner.Runner
}

// NeedError stops the checks on something the player has to do first;
// Action is the fix the screen offers.
type NeedError struct {
	Msg    string
	Action runner.Action
}

func (e *NeedError) Error() string { return e.Msg }

// OverlapError: the install folder and the game's data folder overlap.
type OverlapError struct{ Root, Data string }

func (e *OverlapError) Error() string {
	return fmt.Sprintf("the install folder %s overlaps the game's data folder %s (saves and settings): pick another folder", e.Root, e.Data)
}

// SpaceError: the install folder's disk is too full for the build.
type SpaceError struct{ Free, Need uint64 }

func (e *SpaceError) Error() string {
	return fmt.Sprintf("the build folder's disk has %.1f GB free; the build needs about %.1f GB: "+
		"free some space or pick another folder", float64(e.Free)/1e9, float64(e.Need)/1e9)
}

func exe(name string) string {
	if hostinfo.OS() == "windows" {
		return name + ".exe"
	}
	return name
}

// lineWriter turns writes into whole lines for a log pane.
type lineWriter struct {
	mu   sync.Mutex
	buf  []byte
	line func(string)
}

func (w *lineWriter) Write(b []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf = append(w.buf, b...)
	for {
		i := strings.IndexAny(string(w.buf), "\r\n")
		if i < 0 {
			break
		}
		if i > 0 {
			w.line(string(w.buf[:i]))
		}
		w.buf = w.buf[i+1:]
	}
	return len(b), nil
}

func downloadNote(log func(string), what string) fetch.Progress {
	last := -1
	return func(done, total int64) {
		if total <= 0 {
			return
		}
		pct := int(100 * done / total)
		if pct/25 != last/25 || done == total && pct != last {
			last = pct
			log(fmt.Sprintf("  %s: %d%% of %.1f MB", what, pct, float64(total)/1e6))
		}
	}
}

// prepareTotal is the number of Prepare's step calls: uv, git, the
// clone (or the --game check) and each of the host's fetch entries.
func prepareTotal(cfg *gamecfg.Config, host string) int {
	return 3 + len(fetch.ForHost(cfg.Fetch, host))
}

// Prepare fetches uv and git when the host has none, clones the game at
// the release (or takes the developer's checkout) and unpacks the
// config's fetch entries. Each step logs one line and, when step is not
// nil, reports how many of prepareTotal steps are done.
func Prepare(ctx context.Context, cfg *gamecfg.Config, pins *fetch.Pins, root, devGame string, log func(string),
	step func(done, total int)) (*Env, error) {
	client := fetch.Client()
	e := &Env{Root: root}
	total, done := prepareTotal(cfg, hostinfo.OS()), 0
	if devGame != "" {
		total = 3
	}
	tick := func() {
		done++
		if step != nil {
			step(done, total)
		}
	}
	// The game's data folder holds saves and settings the wizard must never
	// touch, and deleting the install folder must not take them with it.
	if d := hostinfo.ExpandData(cfg.Data[hostinfo.NativeTarget()]); d != "" && hostinfo.Overlaps(root, d) {
		return nil, &OverlapError{Root: root, Data: d}
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("cannot make the install folder: %w", err)
	}
	tools := filepath.Join(root, "tools")

	if uv := fetch.UVOnPath(); uv != "" {
		e.UV = uv
		log("uv: " + uv)
	} else {
		a, ok := pins.UV.Assets[fetch.HostKey()]
		if !ok {
			return nil, fmt.Errorf("no uv download pinned for %s", fetch.HostKey())
		}
		dir := filepath.Join(tools, "uv")
		log("uv: " + pins.UV.Version + " from " + a.URL)
		if err := fetch.Install(ctx, client, a, dir, downloadNote(log, "uv")); err != nil {
			return nil, err
		}
		e.UV = fetch.Find(dir, exe("uv"))
		if e.UV == "" {
			return nil, errors.New("the uv download holds no uv program")
		}
		e.Dirs = append(e.Dirs, filepath.Dir(e.UV))
		log("uv: " + e.UV)
	}
	tick()

	if g, err := exec.LookPath("git"); err == nil && (hostinfo.OS() != "macos" || repo.HaveDeveloperTools()) {
		e.Git = g
	} else {
		switch hostinfo.OS() {
		case "windows":
			a, ok := pins.MinGit.Assets[fetch.HostKey()]
			if !ok {
				return nil, fmt.Errorf("no MinGit download pinned for %s", fetch.HostKey())
			}
			dir := filepath.Join(tools, "git")
			log("git: MinGit " + pins.MinGit.Version + " from " + a.URL)
			if err := fetch.Install(ctx, client, a, dir, downloadNote(log, "MinGit")); err != nil {
				return nil, err
			}
			e.Git = filepath.Join(dir, "cmd", "git.exe")
			e.Dirs = append(e.Dirs, filepath.Join(dir, "cmd"))
		case "macos":
			return nil, &NeedError{
				Msg:    gitMissingMac,
				Action: runner.Action{Kind: "clt", Command: "xcode-select --install"},
			}
		default:
			hint := hostinfo.GitHint(hostinfo.LinuxFlavour())
			act := runner.Action{}
			if strings.HasPrefix(hint, "sudo ") {
				act = runner.Action{Kind: "sudo", Command: hint}
			}
			return nil, &NeedError{Msg: fmt.Sprintf(gitMissingOther, hint), Action: act}
		}
	}
	log("git: " + e.Git)
	tick()
	env := runner.Environ(os.Environ(), tools, e.Dirs...)
	out := &lineWriter{line: func(s string) { log("  " + s) }}

	if devGame != "" {
		e.Game, e.Dev = devGame, true
		log("game: " + devGame + " (developer mode: the checkout as it is)")
		tick()
	} else {
		if free, err := hostinfo.Free(root); err == nil && free < hostinfo.NeedBytes {
			return nil, &SpaceError{Free: free, Need: hostinfo.NeedBytes}
		}
		r := cfg.Release
		if r.Tag == "" {
			return nil, errors.New("this developer build names no release tag to clone: run it with --game DIR")
		}
		// A short fixed name: Windows paths stop at 260 characters unless
		// long paths are on, and the clone's deepest paths are long already.
		e.Game = filepath.Join(root, "src")
		log(fmt.Sprintf("game: %s %s into %s", r.Repo, r.Tag, e.Game))
		g := repo.Git{Path: e.Git, Env: env, Out: out}
		if err := g.Clone(ctx, r.Repo, r.Tag, r.Commit, e.Game); err != nil {
			return nil, err
		}
		tick()
		for _, f := range fetch.ForHost(cfg.Fetch, hostinfo.OS()) {
			log("fetch: " + f.Name + " into " + f.Dest)
			if err := fetch.Entry(ctx, client, f, e.Game, downloadNote(log, f.Name)); err != nil {
				return nil, err
			}
			tick()
		}
	}
	e.Runner = runner.Runner{UV: e.UV, Game: e.Game, Slug: cfg.Game.Slug, Env: env}
	return e, nil
}

// LogWriter is a lineWriter for the runner's stderr.
func LogWriter(log func(string)) io.Writer {
	return &lineWriter{line: log}
}

// BrewPath is Homebrew's brew, or "" when it is not installed.
func BrewPath() string {
	for _, p := range []string{"/opt/homebrew/bin/brew", "/usr/local/bin/brew"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// HomebrewScript fetches the pinned official installer for Terminal.
func HomebrewScript(ctx context.Context, pins *fetch.Pins, root string) (string, error) {
	dest := filepath.Join(root, "tools", "homebrew-install.sh")
	a := pins.HomebrewInstall
	if err := fetch.Download(ctx, fetch.Client(), fetch.Asset{URL: a.URL, SHA256: a.SHA256, Size: a.Size}, dest, nil); err != nil {
		return "", err
	}
	if err := os.Chmod(dest, 0o755); err != nil {
		return "", err
	}
	// The pinned script stays as fetched; a wrapper beside it turns
	// Homebrew's anonymous analytics off for the install Terminal runs.
	wrap := filepath.Join(root, "tools", "homebrew-install-run.sh")
	body := "#!/bin/bash\nexport HOMEBREW_NO_ANALYTICS=1\nexec /bin/bash \"$(dirname \"$0\")/homebrew-install.sh\"\n"
	if err := os.WriteFile(wrap, []byte(body), 0o755); err != nil {
		return "", err
	}
	// WriteFile keeps a leftover wrapper's mode; Terminal needs the exec bit.
	return wrap, os.Chmod(wrap, 0o755)
}

// brewEnv is the environment brew runs in: Homebrew sends anonymous
// install analytics by default, and the Welcome page says the wizard
// sends nothing of the player's.
func brewEnv() []string { return append(os.Environ(), "HOMEBREW_NO_ANALYTICS=1") }

// OpenPath shows a file or folder with the OS's own program.
func OpenPath(p string) error {
	switch hostinfo.OS() {
	case "macos":
		return exec.Command("open", p).Start()
	case "windows":
		return exec.Command("explorer", p).Start()
	}
	return exec.Command("xdg-open", p).Start()
}

// OpenURL opens a web address in the default browser. On Windows that is
// url.dll's handler, not explorer, which reads its own command line and
// takes a comma for a switch separator. The address is one argument, with
// no shell.
func OpenURL(u string) error {
	argv := urlOpener(hostinfo.OS(), u)
	return exec.Command(argv[0], argv[1:]...).Start()
}

// urlOpener is the argv that opens u on an OS.
func urlOpener(goos, u string) []string {
	switch goos {
	case "macos":
		return []string{"open", u}
	case "windows":
		return []string{"rundll32", "url.dll,FileProtocolHandler", u}
	}
	return []string{"xdg-open", u}
}

// SeedPath is where the build writes the seed: beside the clone, so the
// clone stays clean.
func SeedPath(root string) string { return filepath.Join(root, "wizard-seed.toml") }
