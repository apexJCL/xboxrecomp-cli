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
	"math"
	"net/url"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
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
	Key   string `json:"key"`
	Label string `json:"label"`
	Hint  string `json:"hint,omitempty"`
	// HintLink follows the hint: words and an https address, which the
	// page opens in the browser.
	HintLink *Link `json:"hint_link,omitempty"`
	// FontHint follows the hint only while the game's font is not
	// installed, or the wizard cannot tell; with it set, HintLink shows
	// under the same rule (`wizard font --json`).
	FontHint string            `json:"font_hint,omitempty"`
	Kind     string            `json:"kind"`
	Values   []string          `json:"values"`
	Default  any               `json:"default"`
	Config   string            `json:"config"`
	Flag     string            `json:"flag"`
	TOML     map[string]string `json:"toml,omitempty"`
	// Kind range only: whole numbers from Min to Max by Step (1 when
	// unset), and what some of them mean.
	Min    *int              `json:"min,omitempty"`
	Max    *int              `json:"max,omitempty"`
	Step   int               `json:"step,omitempty"`
	Labels map[string]string `json:"labels,omitempty"`
}

// Omitted is one option the CLI left out, and why.
type Omitted struct {
	Key    string `json:"key"`
	Config string `json:"config"`
	Why    string `json:"why"`
}

// ReadsConfig reports whether the build reads config.toml, as the CLI
// read it at the toolkit pin (reads_config). A config from a CLI older than
// the key builds on the fork, which always reads it.
func (c *Config) ReadsConfig() bool { return c.Reads == nil || *c.Reads }

// Link is an option's hint link.
type Link struct {
	Text string `json:"text"`
	URL  string `json:"url"`
}

// httpsURL is an absolute https address with a host and no spaces.
func httpsURL(s string) bool {
	u, err := url.Parse(s)
	return err == nil && u.Scheme == "https" && u.Host != "" && u.Hostname() != "" &&
		!strings.ContainsAny(s, " \t\r\n")
}

// Credit is one open-source project the wizard credits: the CLI's list,
// the game's own entries first (docs/wizard.md, Credits).
type Credit struct {
	Name    string `json:"name"`
	Role    string `json:"role"`
	License string `json:"license"`
	URL     string `json:"url"`
}

// FallbackCredits is the list for a config from a CLI older than the
// credits key: the wizard's own projects, so it still credits them.
var FallbackCredits = []Credit{
	{"xboxrecomp-cli", "this build wizard and the CLI that runs the build", "MIT", "https://github.com/apexJCL/xboxrecomp-cli"},
	{"Go", "the wizard's language and runtime", "BSD-3-Clause", "https://github.com/golang/go"},
	{"Fyne", "the wizard's window toolkit", "BSD-3-Clause", "https://github.com/fyne-io/fyne"},
	{"zenity", "the wizard's file dialogs", "MIT", "https://github.com/ncruces/zenity"},
	{"Tabler Icons", "the wizard's icons", "MIT", "https://github.com/tabler/tabler-icons"},
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
	// Omitted are the game's config options the CLI left out because the
	// toolkit at the pin reads no config.toml (upstream xboxrecomp has no
	// enhancements layer); absent from a CLI older than the key.
	Omitted []Omitted `json:"omitted,omitempty"`
	// Reads is reads_config: false when the toolkit at the pin has no
	// config.toml reader, even with no option left out. ReadsConfig.
	Reads *bool `json:"reads_config,omitempty"`
	// Credits is absent from a CLI older than the key: CreditList.
	Credits []Credit `json:"credits,omitempty"`
}

// CreditList is what the wizard credits: the config's list, or
// FallbackCredits when the CLI that wrote the config had none.
func (c *Config) CreditList() []Credit {
	if c.Credits == nil {
		return FallbackCredits
	}
	return c.Credits
}

// DefaultValue is the option's default as the model keeps every value:
// "true"/"false" for a bool, the value itself for a choice, the number in
// decimal for a range.
func (o Option) DefaultValue() string {
	switch v := o.Default.(type) {
	case bool:
		return fmt.Sprint(v)
	case string:
		return v
	case float64: // JSON's number
		if v == math.Trunc(v) {
			return strconv.Itoa(int(v))
		}
	}
	return ""
}

func isNumber(v any) bool { _, ok := v.(float64); return ok }

// RangeStep is a range's step: 1 when the config leaves it out.
func (o Option) RangeStep() int {
	if o.Step == 0 {
		return 1
	}
	return o.Step
}

// Choices are the values an option takes, in the screen's order.
func (o Option) Choices() []string {
	switch o.Kind {
	case "bool":
		return []string{"false", "true"}
	case "range":
		if o.Min == nil || o.Max == nil || o.RangeStep() < 1 || *o.Max-*o.Min > 10000 {
			return nil
		}
		var out []string
		for v := *o.Min; v <= *o.Max; v += o.RangeStep() {
			out = append(out, strconv.Itoa(v))
		}
		return out
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
	for i, cr := range c.Credits {
		if strings.TrimSpace(cr.Name) == "" || !httpsURL(cr.URL) {
			bad("credits[%d] (%s): a credit has a name and an https address", i, cr.Name)
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
		if o.Kind != "range" && (o.Min != nil || o.Max != nil || o.Step != 0 || len(o.Labels) > 0) {
			bad("%s: min, max, step and labels are only for a range", where)
		}
		if l := o.HintLink; l != nil && (strings.TrimSpace(l.Text) == "" || !httpsURL(l.URL)) {
			bad("%s: a hint link has words and an https address", where)
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
		case "range":
			switch {
			case o.Min == nil || o.Max == nil || *o.Min >= *o.Max:
				bad("%s: a range needs min below max", where)
			case o.Step < 0 || (*o.Max-*o.Min)%o.RangeStep() != 0:
				bad("%s: a range's step divides max - min", where)
			case *o.Max-*o.Min > 10000:
				bad("%s: a range of more than 10000 values", where)
			case !isNumber(o.Default) || !slices.Contains(o.Choices(), o.DefaultValue()):
				bad("%s: a range's default is a whole number from min to max, on a step", where)
			}
			for v := range o.Labels {
				if !slices.Contains(o.Choices(), v) {
					bad("%s: label %q is not a value of the range", where, v)
				}
			}
		default:
			bad("%s: kind %q is not bool, choice or range", where, o.Kind)
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
