package model

import (
	"fmt"
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
		{"defaults", nil, "", [][]string{{"package", "macos", "--dump", "/d/b2.iso", "--seed-config", "S", "--out", "/o", "--full", "--plain"}}},
		{"scale 2 and fullscreen", map[string]string{"scale": "2", "fullscreen": "true"},
			seedHead + "\n[render]\nscale = 2\n\n[present]\nfullscreen = true\n", nil},
		{"scale at the range's top", map[string]string{"scale": "4"},
			seedHead + "\n[render]\nscale = 4\n", nil},
		{"hud pack", map[string]string{"hud_pack": "true"}, "", [][]string{
			{"import", "/d/b2.iso"},
			{"enhance", "hud"},
			{"package", "macos", "--dump", "/d/b2.iso", "--hud-pack", "--seed-config", "S", "--out", "/o", "--full", "--plain"},
		}},
		{"hud pack at 2 jobs", map[string]string{"hud_pack": "true", "jobs": "2"}, "", [][]string{
			{"import", "/d/b2.iso"},
			{"enhance", "hud", "--jobs", "2"},
			{"package", "macos", "--dump", "/d/b2.iso", "--hud-pack", "--seed-config", "S", "--out", "/o", "--full", "--jobs", "2", "--plain"},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := New(sample(t), "macos")
			s.Dump = "/d/b2.iso"
			for k, v := range tc.set {
				if k == "jobs" {
					s.Jobs = int(v[0] - '0')
					continue
				}
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
	want := []string{"package", "steamos", "--dump", "/d", "--full", "--plain"}
	if got := s.PackageArgs("", ""); !reflect.DeepEqual(got, want) {
		t.Fatalf("%q", got)
	}
}

// Fresh is always the full installer; Update names its base, never
// passes the seed, and reads the disc only when the plan asked (D1, D5,
// D6).
func TestPaths(t *testing.T) {
	s := New(&gamecfg.Config{Targets: []string{"steamos"}}, "steamos")
	s.Dump = "D"
	if got := strings.Join(s.PackageArgs("", "/o"), " "); got != "package steamos --dump D --out /o --full --plain" {
		t.Fatal(got)
	}
	s.Path, s.PatchFrom, s.Clean = PathUpdate, "abc123def456", true
	if got := strings.Join(s.PackageArgs("/s.toml", "/o"), " "); got != "package steamos --out /o --clean --patch-from abc123def456 --plain" {
		t.Fatal(got)
	}
	// The Summary's build jobs reach package and nothing else asks for them.
	s.Jobs = 2
	if got := strings.Join(s.PackageArgs("/s.toml", "/o"), " "); got != "package steamos --out /o --clean --patch-from abc123def456 --jobs 2 --plain" {
		t.Fatal(got)
	}
	if got := strings.Join(s.PlanArgs("/s.toml", "/o"), " "); strings.Contains(got, "--jobs") {
		t.Fatal(got)
	}
	s.Jobs = 0
	s.NeedDisc = true
	if got := strings.Join(s.PlanArgs("/s.toml", ""), " "); got != "package steamos --dump D --clean --patch-from abc123def456 --plan --json" {
		t.Fatal(got)
	}
	// The plan judges the disk the bundle goes to: the run's --out.
	if got := strings.Join(s.PlanArgs("/s.toml", "/o"), " "); got != "package steamos --dump D --out /o --clean --patch-from abc123def456 --plan --json" {
		t.Fatal(got)
	}
	if got := strings.Join(s.BasesArgs(), " "); got != "package steamos --full --plan --json" {
		t.Fatal(got)
	}
}

// The HUD box on Update (D3, D6): ticked makes a new pack after the
// import (only when the disc is needed) and enhance hud; unticked keeps
// the base's or passes nothing; Clear removes it, ticked or not.
func TestUpdateTweaks(t *testing.T) {
	cases := []struct {
		name           string
		tick, has, clr bool
		disc           bool
		steps          []string
	}{
		{"unticked, no pack on the device", false, false, false, false,
			[]string{"package steamos --patch-from P --plain"}},
		{"unticked keeps the device's", false, true, false, false,
			[]string{"package steamos --keep-hud-pack --patch-from P --plain"}},
		{"ticked makes a new one", true, true, false, false,
			[]string{"enhance hud", "package steamos --hud-pack --patch-from P --plain"}},
		{"ticked with the disc", true, false, false, true,
			[]string{"import /d", "enhance hud", "package steamos --dump /d --hud-pack --patch-from P --plain"}},
		{"keep with the disc", false, true, false, true,
			[]string{"package steamos --dump /d --keep-hud-pack --patch-from P --plain"}},
		{"clear", false, true, true, false,
			[]string{"package steamos --clear-hud-pack --patch-from P --plain"}},
		{"clear wins over the box", true, true, true, false,
			[]string{"package steamos --clear-hud-pack --patch-from P --plain"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := New(sample(t), "steamos")
			s.Dump, s.Path, s.PatchFrom = "/d", PathUpdate, "P"
			s.BaseHas = map[string]bool{"hud_pack": tc.has}
			s.Clear, s.NeedDisc = tc.clr, tc.disc
			if err := s.Set("hud_pack", fmt.Sprint(tc.tick)); err != nil {
				t.Fatal(err)
			}
			// Settings an update cannot change stay out of it (D5).
			if err := s.Set("scale", "2"); err != nil {
				t.Fatal(err)
			}
			var got []string
			for _, st := range s.Steps("S", "") {
				got = append(got, strings.Join(st, " "))
			}
			if !reflect.DeepEqual(got, tc.steps) {
				t.Fatalf("steps %q, want %q", got, tc.steps)
			}
		})
	}
}

// Several platforms on Fresh: the config's order, the shared steps once,
// one package line each, and only the first cleans. An Update makes one.
func TestTargets(t *testing.T) {
	s := New(sample(t), "macos")
	s.Dump, s.Clean = "/d/b2.iso", true
	if err := s.Set("hud_pack", "true"); err != nil {
		t.Fatal(err)
	}
	s.SetTargets([]string{"steamos", "nope", "windows", "steamos"})
	if !reflect.DeepEqual(s.Targets, []string{"windows", "steamos"}) || s.Target != "windows" {
		t.Fatalf("targets %q, target %q", s.Targets, s.Target)
	}
	steps, targets := s.StepTargets("", "/o")
	var got []string
	for _, st := range steps {
		got = append(got, strings.Join(st, " "))
	}
	want := []string{
		"import /d/b2.iso",
		"enhance hud",
		"package windows --dump /d/b2.iso --hud-pack --out /o --clean --full --plain",
		"package steamos --dump /d/b2.iso --hud-pack --out /o --full --plain",
	}
	if !reflect.DeepEqual(got, want) || !reflect.DeepEqual(targets, []string{"", "", "windows", "steamos"}) {
		t.Fatalf("steps %q for %q", got, targets)
	}
	s.SetTargets(nil)
	if len(s.Targets) != 2 {
		t.Errorf("no platform cleared the choice: %q", s.Targets)
	}
	s.Path = PathUpdate
	if b := s.Builds(); !reflect.DeepEqual(b, []string{"windows"}) {
		t.Errorf("an update builds %q", b)
	}
}
