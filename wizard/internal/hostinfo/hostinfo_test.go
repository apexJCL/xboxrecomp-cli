package hostinfo

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
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
	if s := LoadSettings(d); !reflect.DeepEqual(s, Settings{}) {
		t.Fatalf("missing file: %+v", s)
	}
	want := Settings{Root: "/r", NoUpdateCheck: true, Jobs: 3, Targets: Targets{"windows", "steamos"}}
	if err := SaveSettings(d, want); err != nil {
		t.Fatal(err)
	}
	if got := LoadSettings(d); !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
}

// One platform as a string, the Platform page's single choice before it
// took several, reads as a list of one; a value of another kind is no
// choice and keeps the rest.
func TestSettingsTargets(t *testing.T) {
	for text, want := range map[string]Targets{
		`{"targets": "steamos", "jobs": 2}`:            {"steamos"},
		`{"targets": ["windows", "macos"], "jobs": 2}`: {"windows", "macos"},
		`{"targets": 7, "jobs": 2}`:                    nil,
	} {
		d := t.TempDir()
		if err := os.WriteFile(filepath.Join(d, "settings.json"), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		if got := LoadSettings(d); !reflect.DeepEqual(got.Targets, want) || got.Jobs != 2 {
			t.Errorf("%s: %+v", text, got)
		}
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

// The output folder beside the wizard, and the places refused for it.
func TestDefaultOut(t *testing.T) {
	env := map[string]string{"ProgramFiles": `C:\Program Files`, "SystemRoot": `C:\Windows`}
	cases := []struct {
		name, goos, exe, tmp, dir, why string
	}{
		{"a .app", "darwin", "/Volumes/p/Games/Wiz.app/Contents/MacOS/wiz", "/private/var/folders/x/T", "/Volumes/p/Games", ""},
		{"a plain folder", "linux", "/srv/p/Downloads/wiz", "/tmp", "/srv/p/Downloads", ""},
		{"a zip preview", "linux", "/tmp/zip1/wiz", "/tmp", "/tmp/zip1", "/tmp/zip1 is a temporary folder"},
		{"translocated", "darwin", "/private/var/folders/x/AppTranslocation/ABC/d/Wiz.app/Contents/MacOS/wiz", "/elsewhere",
			"/private/var/folders/x/AppTranslocation/ABC/d", "/private/var/folders/x/AppTranslocation/ABC/d is a temporary folder"},
		{"/Applications", "darwin", "/Applications/Wiz.app/Contents/MacOS/wiz", "/tmp", "/Applications", "/Applications is a system folder"},
		{"/opt", "linux", "/opt/wiz/wiz", "/tmp", "/opt/wiz", "/opt/wiz is a system folder"},
	}
	if runtime.GOOS == "windows" {
		cases = append(cases, struct{ name, goos, exe, tmp, dir, why string }{
			"Program Files", "windows", `C:\program files\Wiz\wiz.exe`, `C:\T`, `C:\program files\Wiz`, `C:\program files\Wiz is a system folder`})
	}
	for _, c := range cases {
		if runtime.GOOS == "windows" && c.goos != "windows" {
			continue // the POSIX cases' paths need POSIX separators
		}
		dir, why := outCandidate(c.goos, c.exe, c.tmp, env)
		if dir != c.dir || why != c.why {
			t.Errorf("%s: %q (%q), want %q (%q)", c.name, dir, why, c.dir, c.why)
		}
	}
	// within ignores case only where the file system does.
	if !within(`/Applications/X`, `/applications`, true) || within(`/Opt/x`, `/opt`, false) {
		t.Error("within's case folding")
	}

	// The probe: a writable folder is used, an unwritable one falls back
	// to the build folder's dist with the reason.
	root := t.TempDir()
	ok := t.TempDir()
	if dir, why := defaultOut(root, runtime.GOOS, filepath.Join(ok, "wiz"), "", nil); dir != filepath.Join(ok, "dist") || why != "" {
		t.Errorf("writable: %q (%q)", dir, why)
	}
	if entries, _ := os.ReadDir(ok); len(entries) != 0 {
		t.Errorf("the probe left %v", entries)
	}
	if runtime.GOOS != "windows" && os.Geteuid() != 0 {
		ro := t.TempDir()
		if err := os.Chmod(ro, 0o500); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { os.Chmod(ro, 0o700) })
		dir, why := defaultOut(root, runtime.GOOS, filepath.Join(ro, "wiz"), "", nil)
		if dir != filepath.Join(root, "dist") || why != "cannot write in "+ro {
			t.Errorf("unwritable: %q (%q)", dir, why)
		}
	}
	// A refused place falls back without a probe.
	if dir, why := defaultOut(root, "linux", "/usr/bin/wiz", "", nil); dir != filepath.Join(root, "dist") || why == "" {
		t.Errorf("refused: %q (%q)", dir, why)
	}
}

func TestOutHelpers(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a"), make([]byte, 100), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "s"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "s", "b"), make([]byte, 23), 0o644); err != nil {
		t.Fatal(err)
	}
	if n := TreeSize(dir); n != 123 {
		t.Errorf("TreeSize %d", n)
	}
	if BundleNeed(5) != 5+1<<30 {
		t.Error("BundleNeed")
	}
	if !SameVolume(dir, filepath.Join(dir, "not", "yet")) {
		t.Error("a folder and a path under it are on different volumes")
	}
	// A saved folder that is there is used as it is.
	o := filepath.Join(dir, "o")
	if err := os.Mkdir(o, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, why := ResolveOut(o, dir); out != o || why != "" {
		t.Errorf("ResolveOut %q (%q)", out, why)
	}
	// One that is gone falls back even though its parent is there, as
	// /Volumes is when the drive under it is unplugged; so does a file.
	for _, gone := range []string{filepath.Join(dir, "unplugged"), filepath.Join(dir, "s", "b")} {
		if out, why := ResolveOut(gone, dir); out == gone || !strings.Contains(why, "is not there") {
			t.Errorf("ResolveOut(%s) %q (%q)", gone, out, why)
		}
	}
}

func TestOutClash(t *testing.T) {
	dir := t.TempDir()
	root, data, dump := filepath.Join(dir, "root"), filepath.Join(dir, "data"), filepath.Join(dir, "dump")
	iso := filepath.Join(dir, "isos", "game.iso")
	for _, d := range []string{root, data, dump, filepath.Dir(iso)} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(iso, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ out, dump, want string }{
		{filepath.Join(dir, "dist"), dump, ""},
		{filepath.Join(root, "dist"), dump, ""},
		{data, "", ClashData},
		{filepath.Join(data, "bundles"), "", ClashData},
		{dir, "", ClashData},
		{root, "", ClashSrc},
		{filepath.Join(root, "src", "dist"), "", ClashSrc},
		{dump, dump, ClashDump},
		{filepath.Join(dump, "out"), dump, ClashDump},
		// A folder that holds the disc image is fine.
		{filepath.Dir(iso), iso, ""},
	} {
		if got := OutClash(c.out, root, data, c.dump); got != c.want {
			t.Errorf("OutClash(%s, dump %s) = %q, want %q", c.out, c.dump, got, c.want)
		}
	}
}
