package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestDry(t *testing.T) {
	var out, errb bytes.Buffer
	t.Setenv("HOME", t.TempDir())
	rc := run([]string{"--dry", "--config", "../../internal/gamecfg/testdata/blinx2.json", "--root", "/r"}, &out, &errb)
	if rc != 0 {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	s := out.String()
	for _, want := range []string{"build folder: /r", "output folder: ", "--out ", "--branch v0.0.0-sample", "run: ./blinx2 package", "--dump DUMP --out ", "--plain"} {
		if !strings.Contains(s, want) {
			t.Errorf("no %q in:\n%s", want, s)
		}
	}
}

func TestNoConfig(t *testing.T) {
	var out, errb bytes.Buffer
	if rc := run([]string{"--dry"}, &out, &errb); rc != 1 || !strings.Contains(errb.String(), "developer build") {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	if rc := run([]string{"--version"}, &out, &errb); rc != 0 || !strings.Contains(out.String(), "dev") {
		t.Fatal(out.String())
	}
	if rc := run([]string{"stray"}, &out, &errb); rc != 2 {
		t.Fatal("a stray argument")
	}
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@e", "GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@e")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// checkout is a game checkout with one commit, and that commit.
func checkout(t *testing.T) (string, string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	d := t.TempDir()
	git(t, d, "init", "-q", "-b", "main")
	os.WriteFile(filepath.Join(d, "game.toml"), []byte("[game]\nslug = \"blinx2\"\n"), 0o644)
	git(t, d, "add", ".")
	git(t, d, "commit", "-qm", "one")
	return d, git(t, d, "rev-parse", "HEAD")
}

const sampleCfg = "../../internal/gamecfg/testdata/blinx2.json"

// --source and --patch-from are developers' flags: out of the usage text.
func TestHiddenFlags(t *testing.T) {
	var out, errb bytes.Buffer
	if rc := run([]string{"--help"}, &out, &errb); rc != 0 {
		t.Fatal(rc)
	}
	u := errb.String()
	if !strings.Contains(u, "-game") || strings.Contains(u, "source") || strings.Contains(u, "patch-from") {
		t.Fatal(u)
	}
	errb.Reset()
	if rc := run([]string{"--source", "/x", "--game", "/y"}, &out, &errb); rc != 2 || !strings.Contains(errb.String(), "pick one") {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
}

func TestDrySource(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	src, commit := checkout(t)
	var out, errb bytes.Buffer
	rc := run([]string{"--dry", "--config", sampleCfg, "--root", "/r", "--source", src}, &out, &errb)
	if rc != 0 {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	s := out.String()
	for _, want := range []string{
		"Local source: " + commit[:7] + ", not a release; nothing is published\n",
		"clone: git clone --no-checkout -- file://" + filepath.ToSlash(src) + " " + filepath.Join("/r", "src") + ", then HEAD at " + commit,
	} {
		if !strings.Contains(s, want) {
			t.Errorf("no %q in:\n%s", want, s)
		}
	}
	if errb.Len() != 0 {
		t.Errorf("a clean checkout warned: %s", errb.String())
	}

	// Uncommitted changes are not built: said on stderr and on the banner.
	os.WriteFile(filepath.Join(src, "game.toml"), []byte("edited"), 0o644)
	out.Reset()
	if rc := run([]string{"--dry", "--config", sampleCfg, "--root", "/r", "--source", src}, &out, &errb); rc != 0 {
		t.Fatal(rc)
	}
	if !strings.Contains(errb.String(), "uncommitted changes in "+src+" are not built") ||
		!strings.Contains(out.String(), "; uncommitted changes in "+src) {
		t.Fatalf("dirty:\n%s\n%s", errb.String(), out.String())
	}

	errb.Reset()
	if rc := run([]string{"--dry", "--config", sampleCfg, "--source", src + "#nope"}, &out, &errb); rc != 1 || !strings.Contains(errb.String(), "no nope") {
		t.Fatalf("bad ref: rc %d: %s", rc, errb.String())
	}
}

func TestDryBundleAndPatchFrom(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	src, commit := checkout(t)
	b := filepath.Join(t.TempDir(), "g.bundle")
	git(t, src, "bundle", "create", b, "main")
	base := filepath.Join(t.TempDir(), "manifest.json")
	os.WriteFile(base, []byte("{}"), 0o644)
	var out, errb bytes.Buffer
	rc := run([]string{"--dry", "--config", sampleCfg, "--root", "/r", "--source", b, "--patch-from", base}, &out, &errb)
	if rc != 0 {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	s := out.String()
	for _, want := range []string{"-- " + b + " ", "then refs/heads/main at " + commit, "--patch-from " + base + " --plain"} {
		if !strings.Contains(s, want) {
			t.Errorf("no %q in:\n%s", want, s)
		}
	}
}
