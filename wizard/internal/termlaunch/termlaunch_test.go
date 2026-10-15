package termlaunch

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestArgs(t *testing.T) {
	only := func(names ...string) func(string) bool {
		return func(p string) bool {
			for _, n := range names {
				if n == p {
					return true
				}
			}
			return false
		}
	}
	got, err := Args(only("konsole", "xterm"), "sudo apt install nsis")
	if err != nil || strings.Join(got[:4], " ") != "konsole -e sh -c" || !strings.HasPrefix(got[4], "sudo apt install nsis; ") {
		t.Fatalf("%q %v", got, err)
	}
	got, _ = Args(only("gnome-terminal"), "x")
	if got[1] != "--" {
		t.Fatalf("%q", got)
	}
	got, _ = Args(only("x-terminal-emulator"), "x")
	if got[0] != "x-terminal-emulator" || got[1] != "-e" {
		t.Fatalf("%q", got)
	}
	if _, err := Args(only(), "x"); err == nil {
		t.Fatal("no terminal")
	}
}

// A folder name with spaces and a quote reaches the script as $1 intact.
func TestParamsAreNotParsed(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh")
	}
	dir := filepath.Join(t.TempDir(), "it's a folder; touch pwned")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	args, err := Args(func(p string) bool { return p == "xterm" }, `cd "$1" && pwd -P > "$1/where"`, dir)
	if err != nil {
		t.Fatal(err)
	}
	if args[len(args)-1] != dir {
		t.Fatalf("%q", args)
	}
	cmd := exec.Command(args[2], args[3:]...) // sh -c SCRIPT sh DIR, without the terminal
	cmd.Dir = t.TempDir()
	cmd.Run() // read _ fails on no stdin; the script ran by then
	got, err := os.ReadFile(filepath.Join(dir, "where"))
	if err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(dir)
	if strings.TrimSpace(string(got)) != want {
		t.Fatalf("ran in %q, want %q", got, want)
	}
	if _, err := os.Stat(filepath.Join(cmd.Dir, "pwned")); err == nil {
		t.Fatal("the folder name ran as a command")
	}
}
