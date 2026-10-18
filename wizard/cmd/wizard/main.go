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
	"os/exec"
	"path/filepath"
	"strings"

	"fyne.io/fyne/v2"
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
	// source and patchFrom are the developers' hidden flags (D14): out of
	// the usage text, the UI and the settings.
	source, patchFrom string
}

// hidden are the flags the usage text leaves out. docs/wizard.md's
// developer section lists them.
var hidden = map[string]bool{"source": true, "patch-from": true}

func parse(args []string, out io.Writer) (flags, error) {
	var f flags
	fs := flag.NewFlagSet("wizard", flag.ContinueOnError)
	fs.SetOutput(out)
	fs.StringVar(&f.game, "game", "", "developers: build in this game checkout as it is (no clone); its config comes from its CLI")
	fs.StringVar(&f.config, "config", "", "developers: the wizard config JSON to use (xbr wizard config --json)")
	fs.StringVar(&f.root, "root", "", "the build folder: the clone, the tools and the build (default: the saved one, else the per-OS default)")
	fs.BoolVar(&f.noDialog, "no-dialog", false, "no file dialogs: type the paths")
	fs.BoolVar(&f.dry, "dry", false, "print what a build with the defaults would run, and exit (no window)")
	fs.BoolVar(&f.noUpdate, "no-update-check", false, "do not look for a newer wizard at start")
	fs.BoolVar(&f.ver, "version", false, "print the version and exit")
	fs.StringVar(&f.source, "source", "", "developers: build from this checkout or git bundle at a commit (PATH[#REF])")
	fs.StringVar(&f.patchFrom, "patch-from", "", "developers: the base for a patch, passed to package")
	fs.Usage = func() {
		fmt.Fprintln(fs.Output(), "Usage of wizard:")
		fs.VisitAll(func(fl *flag.Flag) {
			if hidden[fl.Name] {
				return
			}
			name, use := flag.UnquoteUsage(fl)
			if name != "" {
				name = " " + name
			}
			fmt.Fprintf(fs.Output(), "  -%s%s\n    \t%s\n", fl.Name, name, use)
		})
	}
	err := fs.Parse(args)
	if err == nil && f.source != "" && f.game != "" {
		err = errors.New("--source and --game: pick one")
	}
	if err == nil && fs.NArg() > 0 {
		err = fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	return f, err
}

// local is --source, read: the source, its commit and whether the
// checkout has changes the build leaves out.
type local struct {
	src    *repo.Local
	commit string
	dirty  bool
	snap   string // the throwaway clone at commit, until the config is read
}

// loadConfig is the embedded config, else --config, else developer mode's
// `<slug> wizard config --json --dev` in --game DIR, or in a throwaway
// clone of --source at its ref (the config of the tree that is built).
func loadConfig(f flags, l *local) (*gamecfg.Config, error) {
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
	case l != nil:
		return sourceConfig(l)
	}
	cfg, err := gamecfg.Embedded()
	if err == nil && cfg == nil {
		err = errors.New("this is a developer build with no game config: pass --game DIR or --config FILE " +
			"(players: download the wizard from the game's release page)")
	}
	return cfg, err
}

// readSource checks --source and finds its commit.
func readSource(arg string) (*local, error) {
	src, err := repo.ParseSource(arg)
	if err != nil {
		return nil, err
	}
	g, err := sourceGit()
	if err != nil {
		return nil, err
	}
	ctx := context.Background()
	ref, err := g.LocalRef(ctx, src)
	if err != nil {
		return nil, err
	}
	dir, commit, err := g.Snapshot(ctx, src, "")
	if err != nil {
		return nil, fmt.Errorf("--source %s: %w", arg, err)
	}
	src.Ref = ref
	return &local{src: src, commit: commit, dirty: g.Dirty(ctx, src), snap: dir}, nil
}

func sourceGit() (repo.Git, error) {
	p, err := exec.LookPath("git")
	if err != nil {
		return repo.Git{}, errors.New("--source needs git on PATH")
	}
	return repo.Git{Path: p, Env: os.Environ()}, nil
}

func sourceConfig(l *local) (*gamecfg.Config, error) {
	uv := fetch.UVOnPath()
	if uv == "" {
		return nil, errors.New("--source needs uv 0.5.31 or newer on PATH")
	}
	slug, err := runner.SlugOf(l.snap)
	if err != nil {
		return nil, err
	}
	r := runner.Runner{UV: uv, Game: l.snap, Slug: slug, Env: runner.Environ(os.Environ(), "")}
	return r.DevConfig(context.Background(), os.Stderr)
}

func dry(w io.Writer, cfg *gamecfg.Config, pins *fetch.Pins, f flags, l *local) {
	set := hostinfo.LoadSettings(hostinfo.SettingsDir(cfg.Game.Slug))
	root := f.root
	if root == "" {
		if set.Root != "" {
			root = set.Root
		} else {
			root = hostinfo.DefaultRoot(cfg.Game.Slug)
		}
	}
	out, why := hostinfo.ResolveOut(set.Out, root)
	r := cfg.Release
	fmt.Fprintf(w, "%s build wizard %s (release %s %s, %s)\n", cfg.Game.Name, version, r.Tag, r.Commit, fetch.HostKey())
	fmt.Fprintf(w, "build folder: %s\n", root)
	if why != "" {
		fmt.Fprintf(w, "output folder: %s (%s)\n", out, why)
	} else {
		fmt.Fprintf(w, "output folder: %s\n", out)
	}
	if uv := fetch.UVOnPath(); uv != "" {
		fmt.Fprintf(w, "uv: %s (on PATH)\n", uv)
	} else if a, ok := pins.UV.Assets[fetch.HostKey()]; ok {
		fmt.Fprintf(w, "uv: %s %s sha256 %s -> %s\n", pins.UV.Version, a.URL, a.SHA256, filepath.Join(root, "tools", "uv"))
	}
	if hostinfo.OS() == "windows" {
		a := pins.MinGit.Assets[fetch.HostKey()]
		fmt.Fprintf(w, "git: on PATH, else MinGit %s %s sha256 %s\n", pins.MinGit.Version, a.URL, a.SHA256)
	}
	game := filepath.Join(root, "src")
	if l != nil {
		fmt.Fprintf(w, "%s\n", screens.SourceBanner(l.commit, l.dirty, l.src.Path))
		fmt.Fprintf(w, "clone: git clone --no-checkout -- %s %s, then %s at %s\n", l.src.URL(), game, l.src.Ref, l.commit)
	} else if f.game != "" {
		game = f.game
		fmt.Fprintf(w, "game: %s (developer mode)\n", game)
	} else if cfg.Private {
		fmt.Fprintln(w, "clone: none (a private game: run it with --game DIR)")
	} else if r.Tag == "" {
		fmt.Fprintln(w, "clone: none (a developer build with no release tag: run it with --game DIR)")
	} else {
		fmt.Fprintf(w, "clone: git %s\n", strings.Join(repo.CloneArgs(r.Repo, r.Tag, game), " "))
	}
	for _, e := range fetch.ForHost(cfg.Fetch, hostinfo.OS()) {
		fmt.Fprintf(w, "fetch: %s %s sha256 %s -> %s\n", e.Name, e.URL, e.SHA256, e.Dest)
	}
	st := model.New(cfg, hostinfo.NativeTarget())
	st.Dump = "DUMP"
	fmt.Fprintf(w, "targets: %s (default %s)\n", strings.Join(cfg.Targets, ", "), st.Target)
	st.PatchFrom = f.patchFrom
	st.Path = model.PathFresh
	if f.patchFrom != "" {
		// An update; the dry run asks no plan, so it shows the disc's
		// steps as when the build folder lacks the game files.
		st.Path, st.NeedDisc = model.PathUpdate, true
	}
	for _, s := range st.Steps("", out) {
		fmt.Fprintf(w, "run: %s\n", runner.Display(cfg.Game.Slug, s))
	}
	fmt.Fprintln(w, "seed: none (every option at its default)")
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
	var l *local
	if f.source != "" {
		if l, err = readSource(f.source); err != nil {
			fmt.Fprintln(stderr, "wizard:", err)
			return 1
		}
		if l.dirty {
			fmt.Fprintf(stderr, "wizard: uncommitted changes in %s are not built\n", l.src.Path)
		}
	}
	if f.patchFrom != "" {
		// The CLI runs in the clone: a path is made absolute here. A
		// record's sha256 prefix is passed as it is.
		if _, err := os.Stat(f.patchFrom); err == nil {
			f.patchFrom, _ = filepath.Abs(f.patchFrom)
		}
	}
	cfg, err := loadConfig(f, l)
	if l != nil {
		os.RemoveAll(filepath.Dir(l.snap))
	}
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
		dry(stdout, cfg, pins, f, l)
		return 0
	}
	a := app.New()
	// The window's and the taskbar's icon where the desktop asks the app
	// (X11 and Wayland); Windows and macOS also take it from the binary.
	a.SetIcon(fyne.NewStaticResource("icon.png", wizard.IconPNG))
	opt := screens.Options{
		Cfg: cfg, Pins: pins, Version: version, Root: f.root, DevGame: f.game,
		NoDialog: f.noDialog, NoUpdateCheck: f.noUpdate, PatchFrom: f.patchFrom,
	}
	if l != nil {
		opt.Source = &screens.Source{Local: l.src, Commit: l.commit, Dirty: l.dirty}
		opt.Update = updateRelease()
	}
	screens.New(a, opt).Show()
	return 0
}

// updateRelease is what the newer-wizard check compares in local mode:
// the release this wizard binary was built for, when it embeds one, at
// the repo of the game being built. A wizard with no embedded release
// has nothing to compare and skips it, as it does without --source.
func updateRelease() *gamecfg.Release {
	emb, _ := gamecfg.Embedded()
	if emb == nil || emb.Private || emb.Release.Dev {
		return nil
	}
	r := emb.Release
	return &r
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
