// The player build wizard (docs/wizard.md). The game it builds is the
// config embedded by `xbr wizard build`; a developer build with no config
// takes --game DIR or --config FILE.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"fyne.io/fyne/v2/app"

	"wizard"
	"wizard/internal/fetch"
	"wizard/internal/gamecfg"
	"wizard/internal/hostinfo"
	"wizard/internal/model"
	"wizard/internal/repo"
	"wizard/internal/runner"
	"wizard/internal/screens"
)

// version is the release tag, set by the build (-X main.version).
var version = "dev"

type flags struct {
	game, config, root           string
	noDialog, dry, noUpdate, ver bool
}

func parse(args []string, out io.Writer) (flags, error) {
	var f flags
	fs := flag.NewFlagSet("wizard", flag.ContinueOnError)
	fs.SetOutput(out)
	fs.StringVar(&f.game, "game", "", "developers: build in this game checkout as it is (no clone); its config comes from its CLI")
	fs.StringVar(&f.config, "config", "", "developers: the wizard config JSON to use (xbr wizard config --json)")
	fs.StringVar(&f.root, "root", "", "the install folder (default: the saved one, else the per-OS default)")
	fs.BoolVar(&f.noDialog, "no-dialog", false, "no file dialogs: type the paths")
	fs.BoolVar(&f.dry, "dry", false, "print what a build with the defaults would run, and exit (no window)")
	fs.BoolVar(&f.noUpdate, "no-update-check", false, "do not look for a newer wizard at start")
	fs.BoolVar(&f.ver, "version", false, "print the version and exit")
	err := fs.Parse(args)
	if err == nil && fs.NArg() > 0 {
		err = fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	return f, err
}

// loadConfig is the embedded config, else --config, else developer mode's
// `<slug> wizard config --json --dev` in --game DIR.
func loadConfig(f flags) (*gamecfg.Config, error) {
	switch {
	case f.config != "":
		b, err := os.ReadFile(f.config)
		if err != nil {
			return nil, err
		}
		return gamecfg.Parse(b)
	case f.game != "":
		slug, err := runner.SlugOf(f.game)
		if err != nil {
			return nil, err
		}
		uv := fetch.UVOnPath()
		if uv == "" {
			return nil, errors.New("developer mode needs uv 0.5.31 or newer on PATH")
		}
		r := runner.Runner{UV: uv, Game: f.game, Slug: slug, Env: runner.Environ(os.Environ(), "")}
		return r.DevConfig(context.Background(), os.Stderr)
	}
	cfg, err := gamecfg.Embedded()
	if err == nil && cfg == nil {
		err = errors.New("this is a developer build with no game config: pass --game DIR or --config FILE " +
			"(players: download the wizard from the game's release page)")
	}
	return cfg, err
}

func dry(out io.Writer, cfg *gamecfg.Config, pins *fetch.Pins, f flags) {
	root := f.root
	if root == "" {
		if s := hostinfo.LoadSettings(hostinfo.SettingsDir(cfg.Game.Slug)); s.Root != "" {
			root = s.Root
		} else {
			root = hostinfo.DefaultRoot(cfg.Game.Slug)
		}
	}
	r := cfg.Release
	fmt.Fprintf(out, "%s build wizard %s (release %s %s, %s)\n", cfg.Game.Name, version, r.Tag, r.Commit, fetch.HostKey())
	fmt.Fprintf(out, "install folder: %s\n", root)
	if uv := fetch.UVOnPath(); uv != "" {
		fmt.Fprintf(out, "uv: %s (on PATH)\n", uv)
	} else if a, ok := pins.UV.Assets[fetch.HostKey()]; ok {
		fmt.Fprintf(out, "uv: %s %s sha256 %s -> %s\n", pins.UV.Version, a.URL, a.SHA256, filepath.Join(root, "tools", "uv"))
	}
	if hostinfo.OS() == "windows" {
		a := pins.MinGit.Assets[fetch.HostKey()]
		fmt.Fprintf(out, "git: on PATH, else MinGit %s %s sha256 %s\n", pins.MinGit.Version, a.URL, a.SHA256)
	}
	game := filepath.Join(root, "src")
	if f.game != "" {
		game = f.game
		fmt.Fprintf(out, "game: %s (developer mode)\n", game)
	} else if r.Tag == "" {
		fmt.Fprintln(out, "clone: none (a developer build with no release tag: run it with --game DIR)")
	} else {
		fmt.Fprintf(out, "clone: git %s\n", strings.Join(repo.CloneArgs(r.Repo, r.Tag, game), " "))
	}
	for _, e := range fetch.ForHost(cfg.Fetch, hostinfo.OS()) {
		fmt.Fprintf(out, "fetch: %s %s sha256 %s -> %s\n", e.Name, e.URL, e.SHA256, e.Dest)
	}
	st := model.New(cfg, hostinfo.NativeTarget())
	st.Dump = "DUMP"
	fmt.Fprintf(out, "targets: %s (default %s)\n", strings.Join(cfg.Targets, ", "), st.Target)
	for _, s := range st.Steps("") {
		fmt.Fprintf(out, "run: %s\n", runner.Display(cfg.Game.Slug, s))
	}
	fmt.Fprintln(out, "seed: none (every option at its default)")
}

func run(args []string, stdout, stderr io.Writer) int {
	f, err := parse(args, stderr)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintln(stderr, "wizard:", err)
		return 2
	}
	if f.ver {
		fmt.Fprintln(stdout, version)
		return 0
	}
	pins, err := fetch.LoadPins(wizard.PinsJSON)
	if err != nil {
		fmt.Fprintln(stderr, "wizard:", err)
		return 1
	}
	cfg, err := loadConfig(f)
	if err != nil {
		fmt.Fprintln(stderr, "wizard:", err)
		return 1
	}
	if f.game != "" {
		if abs, err := filepath.Abs(f.game); err == nil {
			f.game = abs
		}
	}
	if f.dry {
		dry(stdout, cfg, pins, f)
		return 0
	}
	a := app.New()
	screens.New(a, screens.Options{
		Cfg: cfg, Pins: pins, Version: version, Root: f.root, DevGame: f.game,
		NoDialog: f.noDialog, NoUpdateCheck: f.noUpdate,
	}).Show()
	return 0
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
