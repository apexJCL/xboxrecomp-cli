package gamecfg

import (
	"encoding/json"
	"os"
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
	if c.Game.Slug != "blinx2" || len(c.Targets) != 3 || len(c.Options) != 6 {
		t.Fatalf("decoded %+v", c)
	}
	scale := c.Options[1]
	if scale.Key != "scale" || scale.DefaultValue() != "1" || scale.TOML["2"] != "2" {
		t.Fatalf("scale option %+v", scale)
	}
	if c.Options[0].Flag != "--hud-pack" || c.Options[0].DefaultValue() != "false" {
		t.Fatalf("hud option %+v", c.Options[0])
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
			m["options"].([]any)[1].(map[string]any)["default"] = "4"
		}},
		{"bool default", "true or false", func(m map[string]any) {
			m["options"].([]any)[2].(map[string]any)["default"] = "no"
		}},
		{"toml literal", "no TOML literal", func(m map[string]any) {
			delete(m["options"].([]any)[1].(map[string]any)["toml"].(map[string]any), "3")
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

func TestLocalDest(t *testing.T) {
	for _, ok := range []string{"x", "third_party/x", "a/b.c/d"} {
		if !LocalDest(ok) {
			t.Errorf("LocalDest(%q) = false", ok)
		}
	}
}
