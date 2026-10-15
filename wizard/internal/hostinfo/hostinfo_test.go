package hostinfo

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultRoot(t *testing.T) {
	if r := DefaultRoot("blinx2"); filepath.Base(r) != "blinx2-recomp" {
		t.Fatal(r)
	}
}

func TestOverlaps(t *testing.T) {
	base := t.TempDir()
	j := func(p ...string) string { return filepath.Join(append([]string{base}, p...)...) }
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{j("x"), j("x"), true},
		{j("x"), j("x", "y"), true},
		{j("x", "y"), j("x"), true},
		{j("x"), j("xy"), false},
		{j("blinx2-recomp"), j("BLiNX2"), false},
	} {
		if got := Overlaps(c.a, c.b); got != c.want {
			t.Errorf("Overlaps(%q, %q) = %v", c.a, c.b, got)
		}
	}
	if OS() != "linux" && !Overlaps(j("blinx2"), j("BLiNX2")) {
		t.Error("case")
	}
}

func TestExpandData(t *testing.T) {
	t.Setenv("XBR_TEST_DATA", filepath.Join("v", "local"))
	if got := ExpandData(`%XBR_TEST_DATA%\BLiNX2`); got != filepath.Join("v", "local", "BLiNX2") {
		t.Fatal(got)
	}
	if got := ExpandData(`%XBR_NO_SUCH_VAR%\x`); got != "" {
		t.Fatal(got)
	}
	if got := ExpandData("~/Games/X"); !strings.HasSuffix(got, filepath.Join("Games", "X")) || !filepath.IsAbs(got) {
		t.Fatal(got)
	}
}

func TestSettingsRoundTrip(t *testing.T) {
	d := filepath.Join(t.TempDir(), "x-wizard")
	if s := LoadSettings(d); s != (Settings{}) {
		t.Fatalf("missing file: %+v", s)
	}
	want := Settings{Root: "/r", NoUpdateCheck: true}
	if err := SaveSettings(d, want); err != nil {
		t.Fatal(err)
	}
	if got := LoadSettings(d); got != want {
		t.Fatalf("%+v", got)
	}
}

func TestFreeOfMissingPath(t *testing.T) {
	n, err := Free(filepath.Join(t.TempDir(), "not", "yet"))
	if err != nil || n == 0 {
		t.Fatalf("%d %v", n, err)
	}
}

func TestFlavour(t *testing.T) {
	cases := map[string]string{
		"ID=steamos\nID_LIKE=arch\n":      "immutable",
		"ID=fedora\n":                     "fedora",
		"ID=\"ubuntu\"\nID_LIKE=debian\n": "debian",
		"ID=endeavouros\nID_LIKE=arch\n":  "arch",
		"":                                "debian",
	}
	for text, want := range cases {
		if got := Flavour(OSRelease(text), false); got != want {
			t.Errorf("%q: %s, want %s", text, got, want)
		}
	}
	if Flavour(OSRelease("ID=fedora"), true) != "immutable" {
		t.Error("an ostree host is immutable")
	}
	if !strings.HasPrefix(GitHint("fedora"), "sudo dnf") {
		t.Error(GitHint("fedora"))
	}
}
