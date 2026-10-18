package model

import (
	"os"
	"reflect"
	"strings"
	"testing"

	"wizard/internal/gamecfg"
)

func sample(t *testing.T) *gamecfg.Config {
	t.Helper()
	b, err := os.ReadFile("../gamecfg/testdata/blinx2.json")
	if err != nil {
		t.Fatal(err)
	}
	c, err := gamecfg.Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestModel(t *testing.T) {
	const seedHead = "# Written by the BLiNX 2 build wizard: the options chosen there that differ\n" +
		"# from the defaults. config.default.toml beside the game lists every key.\n"
	cases := []struct {
		name  string
		set   map[string]string
		seed  string
		steps [][]string
	}{
		{"defaults", nil, "", [][]string{{"package", "macos", "--dump", "/d/b2.iso", "--seed-config", "S", "--out", "/o", "--plain"}}},
		{"scale 2 and fullscreen", map[string]string{"scale": "2", "fullscreen": "true"},
			seedHead + "\n[render]\nscale = 2\n\n[present]\nfullscreen = true\n", nil},
		{"scale at the range's top", map[string]string{"scale": "4"},
			seedHead + "\n[render]\nscale = 4\n", nil},
		{"hud pack", map[string]string{"hud_pack": "true"}, "", [][]string{
			{"import", "/d/b2.iso"},
			{"enhance", "hud"},
			{"package", "macos", "--dump", "/d/b2.iso", "--hud-pack", "--seed-config", "S", "--out", "/o", "--plain"},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := New(sample(t), "macos")
			s.Dump = "/d/b2.iso"
			for k, v := range tc.set {
				if err := s.Set(k, v); err != nil {
					t.Fatal(err)
				}
			}
			if got := s.Seed(); got != tc.seed {
				t.Fatalf("seed:\n%s\nwant:\n%s", got, tc.seed)
			}
			if tc.steps != nil {
				if got := s.Steps("S", "/o"); !reflect.DeepEqual(got, tc.steps) {
					t.Fatalf("steps %q, want %q", got, tc.steps)
				}
			}
		})
	}
}

func TestSetRefuses(t *testing.T) {
	s := New(sample(t), "linux")
	if s.Target != "windows" {
		t.Fatalf("a host target the config lacks falls back to the first: %q", s.Target)
	}
	if s.Set("scale", "5") == nil || s.Set("nope", "1") == nil || s.Set("fullscreen", "yes") == nil {
		t.Fatal("Set took a value the option does not take")
	}
}

func TestNoSeedFile(t *testing.T) {
	s := New(sample(t), "steamos")
	s.Dump = "/d"
	want := []string{"package", "steamos", "--dump", "/d", "--plain"}
	if got := s.PackageArgs("", ""); !reflect.DeepEqual(got, want) {
		t.Fatalf("%q", got)
	}
}

func TestOutputChoice(t *testing.T) {
	s := New(&gamecfg.Config{Targets: []string{"steamos"}}, "steamos")
	s.Dump = "D"
	got := strings.Join(s.PackageArgs("", "/o"), " ")
	if got != "package steamos --dump D --out /o --plain" {
		t.Fatal(got)
	}
	s.PatchFrom, s.Clean = "abc123def456", true
	if got := strings.Join(s.PackageArgs("", "/o"), " "); got != "package steamos --dump D --out /o --clean --patch-from abc123def456 --plain" {
		t.Fatal(got)
	}
	// --full and --patch-from are exclusive in the CLI: Full wins.
	s.Full = true
	if got := strings.Join(s.PlanArgs("/s.toml"), " "); got != "package steamos --dump D --seed-config /s.toml --clean --full --plan --json" {
		t.Fatal(got)
	}
}
