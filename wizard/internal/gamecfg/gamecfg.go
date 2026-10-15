// Package gamecfg is the game's wizard config: the [wizard] table of the
// game's game.toml resolved by `xbr wizard config --json` at a release tag
// and embedded at build time (xbr wizard build writes config.json). The
// manifest rules live in the CLI; this package only decodes and refuses
// what the rest of the wizard could not use.
package gamecfg

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// Schema is the config format this binary reads (the CLI's CONFIG_SCHEMA).
const Schema = 1

//go:embed config.json
var embedded []byte

// Targets are the package targets the CLI knows, in its order.
var Targets = []string{"windows", "steamos", "macos"}

// Hosts are the fetch entries' host names (the wizard's own OS names).
var Hosts = []string{"windows", "macos", "linux"}

type Game struct {
	Name    string `json:"name"`
	Slug    string `json:"slug"`
	App     string `json:"app"`
	Product string `json:"product"`
}

type Release struct {
	Repo   string `json:"repo"`
	Tag    string `json:"tag"`
	Commit string `json:"commit"`
	Dev    bool   `json:"dev"`
}

type Pin struct {
	Tag    string `json:"tag"`
	Commit string `json:"commit"`
	URL    string `json:"url,omitempty"`
}

// Fetch is one [[wizard.fetch]] download: an archive unpacked into the
// clone at Dest, or with unpack = false the file itself saved as Dest.
type Fetch struct {
	Name   string   `json:"name"`
	URL    string   `json:"url"`
	SHA256 string   `json:"sha256"`
	Size   int64    `json:"size,omitempty"`
	Dest   string   `json:"dest"`
	Unpack *bool    `json:"unpack,omitempty"`
	Hosts  []string `json:"hosts"`
}

// Unpacks is unpack's value, true when the config leaves it out.
func (f Fetch) Unpacks() bool { return f.Unpack == nil || *f.Unpack }

// LocalDest reports whether dest names a path strictly inside the clone:
// relative, '/'-separated, no empty, "." or ".." segment. "." would be the
// clone itself, which an unpack replaces wholesale.
func LocalDest(dest string) bool {
	if dest == "" || strings.Contains(dest, `\`) {
		return false
	}
	for _, seg := range strings.Split(dest, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return false
		}
	}
	clean := filepath.Clean(filepath.FromSlash(dest))
	return filepath.IsLocal(clean) && clean != "."
}

// Option is one [[wizard.option]]: a package flag (Flag) or a config.toml
// key (Config, with each value's TOML literal in TOML).
type Option struct {
	Key     string            `json:"key"`
	Label   string            `json:"label"`
	Kind    string            `json:"kind"`
	Values  []string          `json:"values"`
	Default any               `json:"default"`
	Config  string            `json:"config"`
	Flag    string            `json:"flag"`
	TOML    map[string]string `json:"toml,omitempty"`
}

type Config struct {
	Schema  int               `json:"schema"`
	Game    Game              `json:"game"`
	Release Release           `json:"release"`
	CLI     Pin               `json:"cli"`
	Toolkit Pin               `json:"toolkit"`
	Targets []string          `json:"targets"`
	Data    map[string]string `json:"data"`
	Fetch   []Fetch           `json:"fetch"`
	Options []Option          `json:"options"`
}

// DefaultValue is the option's default as the model keeps every value:
// "true"/"false" for a bool, the value itself for a choice.
func (o Option) DefaultValue() string {
	switch v := o.Default.(type) {
	case bool:
		return fmt.Sprint(v)
	case string:
		return v
	}
	return ""
}

// Choices are the values an option takes, in the screen's order.
func (o Option) Choices() []string {
	if o.Kind == "bool" {
		return []string{"false", "true"}
	}
	return o.Values
}

// Empty reports whether the binary carries no game: a developer build,
// which takes --game DIR or --config FILE.
func Empty() bool {
	s := strings.TrimSpace(string(embedded))
	return s == "" || s == "{}"
}

// Embedded is the config built into this binary; nil when Empty.
func Embedded() (*Config, error) {
	if Empty() {
		return nil, nil
	}
	return Parse(embedded)
}

// Parse decodes and validates a config.
func Parse(b []byte) (*Config, error) {
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("wizard config: %w", err)
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

var (
	slugRE   = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	sha256RE = regexp.MustCompile(`^[0-9a-f]{64}$`)
	keyRE    = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
	flagRE   = regexp.MustCompile(`^--[a-z][a-z0-9-]*$`)
)

// Validate refuses what the wizard cannot act on. The CLI checked the
// game's rules already; these are the ones a hand-written --config file
// or a newer CLI's output could still break.
func (c *Config) Validate() error {
	var errs []string
	bad := func(f string, a ...any) { errs = append(errs, fmt.Sprintf(f, a...)) }
	if c.Schema != Schema {
		bad("schema is %d, this wizard reads %d", c.Schema, Schema)
	}
	if !slugRE.MatchString(c.Game.Slug) {
		bad("game.slug %q is not a slug", c.Game.Slug)
	}
	if c.Game.Name == "" {
		bad("game.name is empty")
	}
	if !strings.HasPrefix(c.Release.Repo, "https://") {
		bad("release.repo %q is not an https URL", c.Release.Repo)
	}
	if !c.Release.Dev && (c.Release.Tag == "" || c.Release.Commit == "") {
		bad("release has no tag and commit (only a developer config may omit them)")
	}
	if len(c.Targets) == 0 {
		bad("targets is empty")
	}
	for _, t := range c.Targets {
		if !slices.Contains(Targets, t) {
			bad("target %q is not one of %s", t, strings.Join(Targets, ", "))
		}
	}
	for i, f := range c.Fetch {
		where := fmt.Sprintf("fetch[%d] (%s)", i, f.Name)
		if f.Name == "" {
			bad("%s: no name", where)
		}
		if !strings.HasPrefix(f.URL, "https://") {
			bad("%s: url %q is not https", where, f.URL)
		}
		if !sha256RE.MatchString(f.SHA256) {
			bad("%s: no sha256", where)
		}
		if !LocalDest(f.Dest) {
			bad("%s: dest %q is not a path inside the clone", where, f.Dest)
		}
		for _, h := range f.Hosts {
			if !slices.Contains(Hosts, h) {
				bad("%s: host %q is not one of %s", where, h, strings.Join(Hosts, ", "))
			}
		}
	}
	seen := map[string]bool{}
	for i, o := range c.Options {
		where := fmt.Sprintf("option[%d] (%s)", i, o.Key)
		if !keyRE.MatchString(o.Key) || seen[o.Key] {
			bad("%s: key is not a unique lower-case name", where)
		}
		seen[o.Key] = true
		if (o.Config == "") == (o.Flag == "") {
			bad("%s: needs exactly one of config and flag", where)
		}
		if o.Flag != "" && (!flagRE.MatchString(o.Flag) || o.Kind != "bool") {
			bad("%s: a flag option is a bool with a --name flag", where)
		}
		switch o.Kind {
		case "bool":
			if _, ok := o.Default.(bool); !ok {
				bad("%s: a bool's default is true or false", where)
			}
		case "choice":
			d, ok := o.Default.(string)
			if len(o.Values) < 2 || !ok || !slices.Contains(o.Values, d) {
				bad("%s: a choice has two or more values and its default among them", where)
			}
		default:
			bad("%s: kind %q is not bool or choice", where, o.Kind)
		}
		if o.Config != "" {
			for _, v := range o.Choices() {
				if _, ok := o.TOML[v]; !ok {
					bad("%s: no TOML literal for %q", where, v)
				}
			}
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("wizard config:\n  %s", strings.Join(errs, "\n  "))
	}
	return nil
}
