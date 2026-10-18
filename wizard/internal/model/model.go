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

type State struct {
	Cfg    *gamecfg.Config
	Target string
	Dump   string
	// The output choice (D13): Full makes the full installer even when an
	// update could be made; PatchFrom names the update's base (a path, or
	// a bundle record's sha256 prefix from the plan's bases); Clean
	// rebuilds everything (package --clean, never --clean=all).
	Full      bool
	PatchFrom string
	Clean     bool
	values    map[string]string
}

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

// Flags are the set flag options' arguments, in the config's order.
func (s *State) Flags() []string {
	var out []string
	for _, o := range s.Cfg.Options {
		if o.Flag != "" && s.values[o.Key] == "true" {
			out = append(out, o.Flag)
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

// PackageArgs is the package command (after `xbr`): the target, the dump,
// the flags, the seed file when there is one, the output folder when one
// is set, the output choice, and --plain for the log pane.
func (s *State) PackageArgs(seedPath, out string) []string {
	args := []string{"package", s.Target, "--dump", s.Dump}
	args = append(args, s.Flags()...)
	if seedPath != "" {
		args = append(args, "--seed-config", seedPath)
	}
	if out != "" {
		args = append(args, "--out", out)
	}
	args = append(args, s.choiceArgs()...)
	return append(args, "--plain")
}

// PlanArgs is `package --plan --json` for the same build: what the
// output choice and the plan screen show before anything runs.
func (s *State) PlanArgs(seedPath string) []string {
	args := []string{"package", s.Target, "--dump", s.Dump}
	args = append(args, s.Flags()...)
	if seedPath != "" {
		args = append(args, "--seed-config", seedPath)
	}
	args = append(args, s.choiceArgs()...)
	return append(args, "--plan", "--json")
}

func (s *State) choiceArgs() []string {
	var args []string
	if s.Clean {
		args = append(args, "--clean")
	}
	switch {
	case s.Full:
		args = append(args, "--full")
	case s.PatchFrom != "":
		args = append(args, "--patch-from", s.PatchFrom)
	}
	return args
}

// Steps are the CLI commands of the build in order: each set flag's
// prerequisites (once each), then the package line. seedPath is where the
// caller writes Seed(), or "" when the seed is empty; out is the output
// folder.
func (s *State) Steps(seedPath, out string) [][]string {
	var steps [][]string
	seen := map[string]bool{}
	for _, f := range s.Flags() {
		for _, p := range Prerequisites[f] {
			k := strings.Join(p, " ")
			if seen[k] {
				continue
			}
			seen[k] = true
			step := slices.Clone(p)
			for i, a := range step {
				if a == "DUMP" {
					step[i] = s.Dump
				}
			}
			steps = append(steps, step)
		}
	}
	return append(steps, s.PackageArgs(seedPath, out))
}
