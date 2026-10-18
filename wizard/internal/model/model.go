// Package model is the wizard's state and its two outputs: the CLI
// commands the build runs (the package line with its flags) and the
// config.toml seed. No I/O: the screens read and write a State.
package model

import (
	"fmt"
	"slices"
	"strings"

	"wizard/internal/gamecfg"
)

// TargetLabels name the CLI's targets for a player.
var TargetLabels = map[string]string{
	"windows": "Windows (a setup program)",
	"macos":   "macOS (an app in a .dmg)",
	"steamos": "Steam Deck or Linux PC (a .tar with install.sh)",
}

// Prerequisites are the CLI commands a package flag needs run first, in
// order. --hud-pack packs what `enhance hud` made from the imported dump.
// DUMP stands for the chosen dump. The table is the CLI's own command
// set, so it holds for every game.
var Prerequisites = map[string][][]string{
	"--hud-pack": {{"import", "DUMP"}, {"enhance", "hud"}},
}

// The two paths (D1): a Fresh build is the full installer; an Update
// build is a patch for a device that has a build made with this wizard.
const (
	PathFresh  = "fresh"
	PathUpdate = "update"
)

// Tweak is a package flag whose result an update takes from its base
// when the player leaves it alone (D3): Plan names it in the plan's
// tweaks, Keep and Clear are the CLI's flags that keep or remove the
// base's. The table is the CLI's own flag set, so it holds for every
// game.
type Tweak struct {
	Plan, Keep, Clear string
}

var Tweaks = map[string]Tweak{
	"--hud-pack": {Plan: "hud_pack", Keep: "--keep-hud-pack", Clear: "--clear-hud-pack"},
}

type State struct {
	Cfg    *gamecfg.Config
	Target string
	Dump   string
	// Path is PathFresh or PathUpdate, "" before the player chose; an
	// unchosen path builds as Fresh.
	Path string
	// The update's choices (D2, D3, D6): PatchFrom names the base (a
	// path, or a bundle record's sha256 prefix from the plan's bases);
	// BaseHas the tweaks it carries, by their plan key; Clear removes
	// them all; NeedDisc is the plan's import.needed, which brings the
	// disc page, --dump and the import step back.
	PatchFrom string
	BaseHas   map[string]bool
	Clear     bool
	NeedDisc  bool
	// Clean rebuilds everything (package --clean, never --clean=all).
	Clean bool
	// Jobs is --jobs N for package and its enhance hud step; 0 leaves
	// the count to the CLI, which sizes it to the free memory.
	Jobs   int
	values map[string]string
}

// Update says the chosen path is Update.
func (s *State) Update() bool { return s.Path == PathUpdate }

// usesDisc says the build passes --dump: always on Fresh, on Update only
// when the plan asked for the game files again.
func (s *State) usesDisc() bool { return !s.Update() || s.NeedDisc }

// New is the state with every option at its default and the target the
// host's own when the config offers it.
func New(cfg *gamecfg.Config, native string) *State {
	s := &State{Cfg: cfg, values: map[string]string{}}
	for _, o := range cfg.Options {
		s.values[o.Key] = o.DefaultValue()
	}
	if slices.Contains(cfg.Targets, native) {
		s.Target = native
	} else if len(cfg.Targets) > 0 {
		s.Target = cfg.Targets[0]
	}
	return s
}

func (s *State) option(key string) (gamecfg.Option, bool) {
	for _, o := range s.Cfg.Options {
		if o.Key == key {
			return o, true
		}
	}
	return gamecfg.Option{}, false
}

// Value is an option's current value ("true"/"false" for a bool).
func (s *State) Value(key string) string { return s.values[key] }

// Set changes an option; the value must be one it takes.
func (s *State) Set(key, v string) error {
	o, ok := s.option(key)
	if !ok {
		return fmt.Errorf("no option %q", key)
	}
	if !slices.Contains(o.Choices(), v) {
		return fmt.Errorf("option %s takes %s, not %q", key, strings.Join(o.Choices(), ", "), v)
	}
	s.values[key] = v
	return nil
}

// Flags are the flag options' arguments, in the config's order. On
// Update a tweak is one of three (D3): Clear removes it, a ticked box
// makes it again, and a base that carries it keeps it; a base without it
// and an unticked box pass nothing.
func (s *State) Flags() []string {
	var out []string
	for _, o := range s.Cfg.Options {
		if o.Flag == "" {
			continue
		}
		t, tweak := Tweaks[o.Flag]
		switch {
		case s.Update() && tweak && s.Clear:
			out = append(out, t.Clear)
		case s.values[o.Key] == "true":
			out = append(out, o.Flag)
		case s.Update() && tweak && s.BaseHas[t.Plan]:
			out = append(out, t.Keep)
		}
	}
	return out
}

// Seed is the config.toml the installers copy in on a first install: the
// config keys whose value differs from the declared default, grouped by
// table in the options' order. "" when every key is at its default.
func (s *State) Seed() string {
	type kv struct{ key, lit string }
	var tables []string
	byTable := map[string][]kv{}
	for _, o := range s.Cfg.Options {
		if o.Config == "" || s.values[o.Key] == o.DefaultValue() {
			continue
		}
		i := strings.LastIndex(o.Config, ".")
		table, key := "", o.Config
		if i >= 0 {
			table, key = o.Config[:i], o.Config[i+1:]
		}
		if _, ok := byTable[table]; !ok {
			tables = append(tables, table)
		}
		byTable[table] = append(byTable[table], kv{key, o.TOML[s.values[o.Key]]})
	}
	if len(tables) == 0 {
		return ""
	}
	var b strings.Builder
	fmt.Fprintf(&b, "# Written by the %s build wizard: the options chosen there that differ\n", s.Cfg.Game.Name)
	b.WriteString("# from the defaults. config.default.toml beside the game lists every key.\n")
	for _, t := range tables {
		b.WriteString("\n")
		if t != "" {
			fmt.Fprintf(&b, "[%s]\n", t)
		}
		for _, e := range byTable[t] {
			fmt.Fprintf(&b, "%s = %s\n", e.key, e.lit)
		}
	}
	return b.String()
}

// PackageArgs is the package command (after `xbr`): the target, the dump
// when the build reads it, the flags, the seed file when there is one
// (Fresh only, D5), the output folder when one is set, the path's output
// flags, and --plain for the log pane.
func (s *State) PackageArgs(seedPath, out string) []string {
	args := s.common(seedPath)
	if out != "" {
		args = append(args, "--out", out)
	}
	args = append(args, s.choiceArgs()...)
	args = append(args, s.jobsArgs()...)
	return append(args, "--plain")
}

func (s *State) jobsArgs() []string {
	if s.Jobs > 0 {
		return []string{"--jobs", fmt.Sprint(s.Jobs)}
	}
	return nil
}

// PlanArgs is `package --plan --json` for the same build: what the
// Changes and Summary pages show before anything runs. The output folder
// is the run's, so the disk check (D9) judges the volume the bundle goes to.
func (s *State) PlanArgs(seedPath, out string) []string {
	args := s.common(seedPath)
	if out != "" {
		args = append(args, "--out", out)
	}
	args = append(args, s.choiceArgs()...)
	return append(args, "--plan", "--json")
}

// BasesArgs is the plan the Base page lists from: the folder's records
// and this computer's install, which a plan of a full build reports too,
// without a disc or a base chosen.
func (s *State) BasesArgs() []string {
	return []string{"package", s.Target, "--full", "--plan", "--json"}
}

func (s *State) common(seedPath string) []string {
	args := []string{"package", s.Target}
	if s.usesDisc() {
		args = append(args, "--dump", s.Dump)
	}
	args = append(args, s.Flags()...)
	if seedPath != "" && !s.Update() {
		args = append(args, "--seed-config", seedPath)
	}
	return args
}

// choiceArgs: Fresh is always the full installer (D1); Update names its
// base, so package never picks one the player did not see (D2).
func (s *State) choiceArgs() []string {
	var args []string
	if s.Clean {
		args = append(args, "--clean")
	}
	switch {
	case !s.Update():
		args = append(args, "--full")
	case s.PatchFrom != "":
		args = append(args, "--patch-from", s.PatchFrom)
	}
	return args
}

// Steps are the CLI commands of the build in order: each passed flag's
// prerequisites (once each), then the package line. On Update the import
// runs only when the plan asked for the disc (D6); a kept or cleared
// tweak has none. seedPath is where the caller writes Seed(), or "" when
// the seed is empty; out is the output folder.
func (s *State) Steps(seedPath, out string) [][]string {
	var steps [][]string
	seen := map[string]bool{}
	for _, f := range s.Flags() {
		for _, p := range Prerequisites[f] {
			k := strings.Join(p, " ")
			if seen[k] || slices.Contains(p, "DUMP") && !s.usesDisc() {
				continue
			}
			seen[k] = true
			step := slices.Clone(p)
			for i, a := range step {
				if a == "DUMP" {
					step[i] = s.Dump
				}
			}
			if slices.Equal(p, []string{"enhance", "hud"}) {
				step = append(step, s.jobsArgs()...)
			}
			steps = append(steps, step)
		}
	}
	return append(steps, s.PackageArgs(seedPath, out))
}
