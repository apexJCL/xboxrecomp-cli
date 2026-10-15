package repo

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func gitOrSkip(t *testing.T) string {
	t.Helper()
	p, err := exec.LookPath("git")
	if err != nil {
		t.Skip("no git")
	}
	return p
}

func sh(t *testing.T, dir string, args ...string) string {
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

func TestCloneArgs(t *testing.T) {
	got := strings.Join(CloneArgs("https://e/g.git", "v1", "/r/g"), " ")
	if got != "clone --branch v1 --depth 1 -- https://e/g.git /r/g" {
		t.Fatal(got)
	}
}

func TestClone(t *testing.T) {
	git := gitOrSkip(t)
	src := t.TempDir()
	sh(t, src, "init", "-q")
	os.WriteFile(filepath.Join(src, "game.toml"), []byte("x"), 0o644)
	sh(t, src, "add", ".")
	sh(t, src, "commit", "-qm", "one")
	sh(t, src, "tag", "-a", "v1", "-m", "v1")
	commit := sh(t, src, "rev-parse", "HEAD")
	g := Git{Path: git, Env: os.Environ()}
	ctx := context.Background()
	url := "file://" + filepath.ToSlash(src)
	dest := filepath.Join(t.TempDir(), "g")
	if err := g.Clone(ctx, url, "v1", commit, dest); err != nil {
		t.Fatal(err)
	}
	if err := g.Clone(ctx, url, "v1", commit, dest); err != nil {
		t.Fatalf("reuse: %v", err)
	}
	if err := g.Clone(ctx, url, "v1", strings.Repeat("0", 40), dest); err == nil || !strings.Contains(err.Error(), "not this wizard's release") {
		t.Fatalf("another commit: %v", err)
	}
	other := filepath.Join(t.TempDir(), "h")
	if err := g.Clone(ctx, url, "v1", strings.Repeat("0", 40), other); err == nil || !strings.Contains(err.Error(), "tag moved") {
		t.Fatalf("moved tag: %v", err)
	}
	if _, err := os.Stat(other + ".partial"); err == nil {
		t.Fatal("left the partial clone")
	}
}
