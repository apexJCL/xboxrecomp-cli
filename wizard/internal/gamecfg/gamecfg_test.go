package gamecfg

import (
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"
)

// Sample loads testdata/blinx2.json: `xbr wizard config --json` on cat's
// game.toml, with a release tag filled in.
func sample(t *testing.T) map[string]any {
	t.Helper()
	b, err := os.ReadFile("testdata/blinx2.json")
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func parse(t *testing.T, m map[string]any) (*Config, error) {
	t.Helper()
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return Parse(b)
}

func TestSample(t *testing.T) {
	c, err := parse(t, sample(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.Game.Slug != "blinx2" || len(c.Targets) != 3 || len(c.Options) != 3 {
		t.Fatalf("decoded %+v", c)
	}
	scale := c.Options[0]
	if scale.Key != "scale" || scale.Kind != "range" || scale.DefaultValue() != "1" || scale.TOML["2"] != "2" ||
		!slices.Equal(scale.Choices(), []string{"1", "2", "3", "4"}) || scale.Labels["1"] == "" {
		t.Fatalf("scale option %+v", scale)
	}
	if c.Options[2].Flag != "--hud-pack" || c.Options[2].DefaultValue() != "false" || c.Options[2].Hint == "" {
		t.Fatalf("hud option %+v", c.Options[2])
	}
	// The font sentence and its link wait for the font; the other options
	// have no font_hint.
	if !strings.Contains(c.Options[2].FontHint, "PT Sans Narrow Bold") || c.Options[2].HintLink == nil ||
		c.Options[0].FontHint != "" {
		t.Fatalf("font hint: %+v", c.Options)
	}
}

// Omitted: the options a CLI left out for a toolkit that reads no
// config.toml. A config without the key reads it.
func TestOmitted(t *testing.T) {
	c, err := parse(t, sample(t))
	if err != nil {
		t.Fatal(err)
	}
	if !c.ReadsConfig() || c.Omitted != nil {
		t.Fatalf("sample: %+v", c.Omitted)
	}
	m := sample(t)
	m["omitted"] = []any{map[string]any{"key": "scale", "config": "render.scale", "why": "no enhancements layer"}}
	m["reads_config"] = false
	if c, err = parse(t, m); err != nil {
		t.Fatal(err)
	}
	if c.ReadsConfig() || len(c.Omitted) != 1 || c.Omitted[0].Config != "render.scale" || c.Omitted[0].Why == "" {
		t.Fatalf("omitted: %+v", c.Omitted)
	}
	// Flag options only: nothing left out, and still no config.toml.
	delete(m, "omitted")
	if c, err = parse(t, m); err != nil {
		t.Fatal(err)
	}
	if c.ReadsConfig() || c.Omitted != nil {
		t.Fatalf("flags only: %+v", c)
	}
	m["reads_config"] = true
	if c, err = parse(t, m); err != nil || !c.ReadsConfig() {
		t.Fatalf("reads_config true: %v", err)
	}
}

func TestCredits(t *testing.T) {
	c, err := parse(t, sample(t))
	if err != nil {
		t.Fatal(err)
	}
	// Our repos (the game's own, then the CLI's), then the third party,
	// the game's font first: the CLI's order.
	got := c.CreditList()
	if len(got) < 5 || got[0].Name != "blinx2-recomp" || got[1].Name != "xboxrecomp-cli" ||
		got[4].Name != "PT Sans Narrow" ||
		got[0].License != "MIT" || !strings.HasPrefix(got[4].URL, "https://") {
		t.Fatalf("credits %+v", got)
	}
	// A CLI older than the key: the wizard's own projects.
	m := sample(t)
	delete(m, "credits")
	if c, err = parse(t, m); err != nil || !slices.Equal(c.CreditList(), FallbackCredits) {
		t.Fatalf("fallback: %v %+v", err, c)
	}
	for _, f := range FallbackCredits {
		if f.Name == "" || f.Role == "" || f.License == "" || !httpsURL(f.URL) {
			t.Fatalf("fallback entry %+v", f)
		}
	}
	if FallbackCredits[0].Name != "xboxrecomp-cli" {
		t.Fatalf("the generic list starts with the CLI: %+v", FallbackCredits[0])
	}
}

func TestPlaceholderIsEmpty(t *testing.T) {
	// The committed config.json is the developer placeholder.
	if !Empty() {
		t.Skip("a game's config is embedded (a release build's staged module)")
	}
	c, err := Embedded()
	if c != nil || err != nil {
		t.Fatalf("Embedded() = %v, %v", c, err)
	}
}

func TestRefusals(t *testing.T) {
	cases := []struct {
		name, want string
		edit       func(m map[string]any)
	}{
		{"schema", "schema", func(m map[string]any) { m["schema"] = 2 }},
		{"target", "not one of", func(m map[string]any) { m["targets"] = []any{"windows", "ps2"} }},
		{"no targets", "targets is empty", func(m map[string]any) { m["targets"] = []any{} }},
		{"no repo", "only a private game", func(m map[string]any) { delete(m["release"].(map[string]any), "repo") }},
		{"http repo", "not an https URL", func(m map[string]any) {
			m["release"].(map[string]any)["repo"] = "http://example.org/g"
			m["private"] = true
		}},
		{"untagged", "no tag", func(m map[string]any) { m["release"].(map[string]any)["tag"] = "" }},
		{"neither", "exactly one", func(m map[string]any) {
			o := m["options"].([]any)[1].(map[string]any)
			o["config"] = ""
		}},
		{"both", "exactly one", func(m map[string]any) {
			o := m["options"].([]any)[1].(map[string]any)
			o["flag"] = "--x"
		}},
		{"choice default", "default among", func(m map[string]any) {
			m["options"] = append(m["options"].([]any), map[string]any{"key": "pace", "label": "Pace", "kind": "choice",
				"values": []any{"a", "b"}, "default": "c", "config": "p.q", "toml": map[string]any{"a": `"a"`, "b": `"b"`}})
		}},
		{"bool default", "true or false", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["default"] = "no"
		}},
		{"toml literal", "no TOML literal", func(m map[string]any) {
			delete(m["options"].([]any)[0].(map[string]any)["toml"].(map[string]any), "3")
		}},
		{"link http", "hint link", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["hint_link"] = map[string]any{"text": "Get", "url": "http://fonts.example/f"}
		}},
		{"link no host", "hint link", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["hint_link"] = map[string]any{"text": "Get", "url": "https:///f"}
		}},
		{"link relative", "hint link", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["hint_link"] = map[string]any{"text": "Get", "url": "fonts.example/f"}
		}},
		{"link space", "hint link", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["hint_link"] = map[string]any{"text": "Get", "url": "https://a.example/a b"}
		}},
		{"link no text", "hint link", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["hint_link"] = map[string]any{"text": " ", "url": "https://fonts.example/f"}
		}},
		{"range min above max", "min below max", func(m map[string]any) {
			o := m["options"].([]any)[0].(map[string]any)
			o["min"], o["max"] = 5, 1
		}},
		{"range min equals max", "min below max", func(m map[string]any) {
			o := m["options"].([]any)[0].(map[string]any)
			o["min"], o["max"] = 1, 1
		}},
		{"range no max", "min below max", func(m map[string]any) {
			delete(m["options"].([]any)[0].(map[string]any), "max")
		}},
		{"range step", "step divides", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["step"] = 2
		}},
		{"range negative step", "step divides", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["step"] = -1
		}},
		{"range default out", "on a step", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["default"] = 5
		}},
		{"range default fraction", "on a step", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["default"] = 1.5
		}},
		{"range default string", "on a step", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["default"] = "1"
		}},
		{"range label outside", "label \"9\"", func(m map[string]any) {
			m["options"].([]any)[0].(map[string]any)["labels"].(map[string]any)["9"] = "nine"
		}},
		{"range keys on a bool", "only for a range", func(m map[string]any) {
			m["options"].([]any)[1].(map[string]any)["min"] = 0
		}},
		{"credit http", "https address", func(m map[string]any) {
			m["credits"].([]any)[1].(map[string]any)["url"] = "http://fonts.example/f"
		}},
		{"credit no name", "https address", func(m map[string]any) {
			m["credits"].([]any)[0].(map[string]any)["name"] = " "
		}},
		{"fetch sha", "no sha256", func(m map[string]any) {
			m["fetch"] = []any{map[string]any{"name": "x", "url": "https://e/x.zip", "dest": "third_party/x", "hosts": []any{}}}
		}},
	}
	for _, dest := range []string{"", ".", "..", "../x", "a/../b", "a//b", "a/./b", "/abs", `a\b`, "a/"} {
		cases = append(cases, struct {
			name, want string
			edit       func(map[string]any)
		}{"fetch dest " + dest, "inside the clone", func(m map[string]any) {
			m["fetch"] = []any{map[string]any{"name": "x", "url": "https://e/x.zip", "sha256": strings.Repeat("a", 64), "dest": dest}}
		}})
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := sample(t)
			tc.edit(m)
			_, err := parse(t, m)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want an error with %q", err, tc.want)
			}
		})
	}
}

// A private game's config carries no repo: it parses, and says so.
func TestPrivate(t *testing.T) {
	m := sample(t)
	delete(m["release"].(map[string]any), "repo")
	m["private"] = true
	c, err := parse(t, m)
	if err != nil {
		t.Fatal(err)
	}
	if !c.Private || c.Release.Repo != "" {
		t.Fatalf("decoded %+v", c)
	}
}

func TestLocalDest(t *testing.T) {
	for _, ok := range []string{"x", "third_party/x", "a/b.c/d"} {
		if !LocalDest(ok) {
			t.Errorf("LocalDest(%q) = false", ok)
		}
	}
}
