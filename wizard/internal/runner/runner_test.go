package runner

import (
	"bufio"
	"context"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestCommandDeveloperMode(t *testing.T) {
	game := t.TempDir()
	os.WriteFile(filepath.Join(game, "game.toml"), []byte("schema = 1\n\n[game]\nname = \"X\"\nslug = \"xgame\"\n\n[cli]\nslug = \"not-this\"\n"), 0o644)
	slug, err := SlugOf(game)
	if err != nil || slug != "xgame" {
		t.Fatalf("%q %v", slug, err)
	}
	r := Runner{UV: "/t/uv", Game: game, Slug: slug}
	got := r.Command("wizard", "config", "--json", "--dev")
	want := []string{"/t/uv", "run", "--no-project", "--quiet", "--python", "3.12", "python",
		filepath.Join(game, "xgame.py"), "wizard", "config", "--json", "--dev"}
	if !slices.Equal(got, want) {
		t.Fatalf("%q", got)
	}
	if d := Display("xgame", []string{"package", "macos", "--dump", "/a b/c.iso"}); d != `./xgame package macos --dump "/a b/c.iso"` {
		t.Fatal(d)
	}
}

func TestEnviron(t *testing.T) {
	tools := filepath.Join("r", "tools")
	env := Environ([]string{"PATH=/usr/bin", "VIRTUAL_ENV=/v", "HOME=/h", "UV_CACHE_DIR=/elsewhere"}, tools, "/r/tools/uv", "/r/tools/git/cmd")
	sep := string(os.PathListSeparator)
	if !slices.Contains(env, "PATH=/r/tools/uv"+sep+"/r/tools/git/cmd"+sep+"/usr/bin") {
		t.Fatal(env)
	}
	if slices.Contains(env, "VIRTUAL_ENV=/v") || !slices.Contains(env, "HOME=/h") || !slices.Contains(env, "PYTHONUNBUFFERED=1") {
		t.Fatal(env)
	}
	// uv's cache and Pythons live under the install folder, so removing
	// that folder removes everything the wizard put on the machine.
	for _, kv := range []string{"UV_CACHE_DIR=" + filepath.Join(tools, "uv-cache"),
		"UV_PYTHON_INSTALL_DIR=" + filepath.Join(tools, "python"), "UV_PYTHON_INSTALL_BIN=0", "UV_NO_MODIFY_PATH=1"} {
		if !slices.Contains(env, kv) {
			t.Fatalf("no %s in %q", kv, env)
		}
	}
	if slices.Contains(env, "UV_CACHE_DIR=/elsewhere") {
		t.Fatal(env)
	}
	if long := slices.Contains(env, "GIT_CONFIG_KEY_0=core.longpaths"); long != (runtime.GOOS == "windows") {
		t.Fatalf("core.longpaths %v on %s", long, runtime.GOOS)
	}
	if env := Environ([]string{"PATH=/usr/bin"}, ""); slices.ContainsFunc(env, func(kv string) bool { return strings.HasPrefix(kv, "UV_CACHE_DIR=") }) {
		t.Fatal(env)
	}
}

// doctor --json exits non-zero on CLIs older than the fix while still
// printing the whole report; the wizard reads the report either way.
func TestDoctorExitOneWithReport(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the fake uv is a shell script")
	}
	dir := t.TempDir()
	sample, err := filepath.Abs("testdata/doctor.json")
	if err != nil {
		t.Fatal(err)
	}
	uv := filepath.Join(dir, "uv")
	script := "#!/bin/sh\ncat '" + sample + "'\necho 'blinx2: not set up' >&2\nexit 1\n"
	if err := os.WriteFile(uv, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	r := Runner{UV: uv, Game: dir, Slug: "xgame", Env: Environ(os.Environ(), "")}
	d, err := r.Doctor(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.Host.OS != "macos" || len(d.Lines) == 0 {
		t.Fatalf("%+v", d)
	}
	// Exit 1 with no report is still an error.
	os.WriteFile(uv, []byte("#!/bin/sh\necho boom >&2\nexit 1\n"), 0o755)
	if _, err := r.Doctor(context.Background(), nil); err == nil || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("got %v", err)
	}
}

func TestFirstJSONValue(t *testing.T) {
	b, err := os.ReadFile("testdata/doctor.json")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseDoctor(append(b, []byte("\n{\"trailing\": 1}\n")...)); err != nil {
		t.Fatalf("a second value after the report broke the decode: %v", err)
	}
}

func TestDoctorSample(t *testing.T) {
	b, err := os.ReadFile("testdata/doctor.json")
	if err != nil {
		t.Fatal(err)
	}
	d, err := ParseDoctor(append([]byte("blinx2: a note on stdout\n"), b...))
	if err != nil {
		t.Fatal(err)
	}
	if d.Host.OS != "macos" || len(d.Lines) == 0 || d.Next == "" {
		t.Fatalf("%+v", d)
	}
	if ok, why := d.Target("macos"); !ok || len(why) != 0 {
		t.Fatalf("macos: %v %v", ok, why)
	}
	if ok, _ := d.Target("ps2"); ok {
		t.Fatal("unknown target")
	}
	if _, err := ParseDoctor([]byte(`{"lines": []}`)); err == nil {
		t.Fatal("a doctor without blockers is an older CLI")
	}
}

func TestActions(t *testing.T) {
	got := Actions([]string{
		"Command Line Tools: xcode-select --install",
		"Homebrew: brew install sdl2 sdl3 openssl libepoxy",
		"makensis: brew install makensis",
		"makensis: sudo apt install nsis",
		"makensis: sudo pacman -S nsis   (or the AUR package)",
		"makensis: in a toolbox or distrobox: distrobox create -n x",
		"Homebrew: brew install sdl2 sdl3 openssl libepoxy",
	})
	want := []Action{
		{"clt", "xcode-select --install"},
		{"brew", "brew install sdl2 sdl3 openssl libepoxy"},
		{"brew", "brew install makensis"},
		{"sudo", "sudo apt install nsis"},
		{"sudo", "sudo pacman -S nsis"},
	}
	if !slices.Equal(got, want) {
		t.Fatalf("%q", got)
	}
}

func feedFile(t *testing.T, name string) (*Progress, []float64) {
	t.Helper()
	f, err := os.Open(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var p Progress
	var bars []float64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if p.Feed(sc.Text()) {
			bars = append(bars, p.Fraction())
		}
	}
	return &p, bars
}

func TestProgressSuccess(t *testing.T) {
	p, bars := feedFile(t, "package-plain.txt")
	if strings.Join(p.Steps, ",") != "import,setup,generate,build,package" || p.Total != 5 {
		t.Fatalf("steps %q", p.Steps)
	}
	if !strings.HasSuffix(p.Bundle, ".dmg") || p.Failed != "" {
		t.Fatalf("%+v", p)
	}
	for i := 1; i < len(bars); i++ {
		if bars[i] < bars[i-1] {
			t.Fatalf("the bar went back at %d: %v", i, bars)
		}
	}
	if bars[len(bars)-1] != 1 {
		t.Fatalf("the bar ends at %v", bars[len(bars)-1])
	}
}

func TestProgressFailure(t *testing.T) {
	p, _ := feedFile(t, "package-failed.txt")
	if p.Failed != "build steamos (build-pkg-steamos)" || !strings.HasSuffix(p.Log, ".log") || p.Bundle != "" {
		t.Fatalf("%+v", p)
	}
}

func TestStartStreamsAndCancels(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("a POSIX shell stands in for the bootstrap")
	}
	dir := t.TempDir()
	// A fake uv: the runner's line ends with the bootstrap path and the args.
	uv := filepath.Join(dir, "uv")
	os.WriteFile(uv, []byte("#!/bin/sh\nfor a; do last=$a; done\necho one\necho two >&2\nprintf 'three\\rfour\\n'\n[ \"$last\" = wait ] && sleep 30\nexit 0\n"), 0o755)
	r := Runner{UV: uv, Game: dir, Slug: "g", Env: os.Environ()}
	var lines []string
	p, err := r.Start(context.Background(), nil, func(s string) { lines = append(lines, s) }, "x")
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Wait(); err != nil {
		t.Fatal(err)
	}
	if strings.Join(lines, "|") != "one|two|three|four" {
		t.Fatalf("%q", lines)
	}
	p, err = r.Start(context.Background(), nil, func(string) {}, "wait")
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	start := time.Now()
	p.Cancel()
	if err := p.Wait(); err == nil || time.Since(start) > 10*time.Second {
		t.Fatalf("cancel: %v after %v", err, time.Since(start))
	}
}
