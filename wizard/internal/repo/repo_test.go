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

// upstream is a game repo with tags v1 and v2, served by file:// as a
// release repo would be over https.
type upstream struct {
	dir, url string
	c1, c2   string
}

func newUpstream(t *testing.T) upstream {
	t.Helper()
	src := t.TempDir()
	sh(t, src, "init", "-q", "-b", "main")
	commit := func(msg string) string {
		os.WriteFile(filepath.Join(src, "game.toml"), []byte(msg), 0o644)
		sh(t, src, "add", ".")
		sh(t, src, "commit", "-qm", msg)
		sh(t, src, "tag", "-a", msg, "-m", msg)
		return sh(t, src, "rev-parse", "HEAD")
	}
	u := upstream{dir: src, url: "file://" + filepath.ToSlash(src)}
	u.c1 = commit("v1")
	u.c2 = commit("v2")
	return u
}

func (u upstream) src(tag string) Source {
	c := map[string]string{"v1": u.c1, "v2": u.c2}[tag]
	return Source{Repo: u.url, Tag: tag, Commit: c}
}

func newGit(t *testing.T) (Git, context.Context) {
	return Git{Path: gitOrSkip(t), Env: os.Environ()}, context.Background()
}

func TestEnsureFreshAndReuse(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	dest := filepath.Join(t.TempDir(), "src")
	r, err := g.Ensure(ctx, u.src("v1"), dest)
	if err != nil || r.Commit != u.c1 || r.Before != "" {
		t.Fatalf("fresh: %+v %v", r, err)
	}
	m := g.ReadMark(ctx, dest)
	if m == nil || *m != (Mark{Origin: "repo", Ref: "v1", Commit: u.c1}) {
		t.Fatalf("mark: %+v", m)
	}
	// The mark lives in the git dir: the tree stays clean.
	if st := sh(t, dest, "status", "--porcelain"); st != "" {
		t.Fatalf("dirty after clone: %q", st)
	}
	os.WriteFile(filepath.Join(dest, "build.log"), []byte("x"), 0o644) // untracked: allowed
	r, err = g.Ensure(ctx, u.src("v1"), dest)
	if err != nil || r.Commit != u.c1 || r.Before != u.c1 {
		t.Fatalf("reuse: %+v %v", r, err)
	}
	if _, err := os.Stat(filepath.Join(dest, "build.log")); err != nil {
		t.Fatal("reuse lost an untracked file")
	}
}

func TestEnsureTagBumpAndDowngrade(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	dest := filepath.Join(t.TempDir(), "src")
	if _, err := g.Ensure(ctx, u.src("v1"), dest); err != nil {
		t.Fatal(err)
	}
	keep := filepath.Join(dest, "build", "obj.o")
	os.MkdirAll(filepath.Dir(keep), 0o755)
	os.WriteFile(keep, []byte("x"), 0o644)
	r, err := g.Ensure(ctx, u.src("v2"), dest)
	if err != nil || r.Commit != u.c2 || r.Before != u.c1 {
		t.Fatalf("bump: %+v %v", r, err)
	}
	if got := sh(t, dest, "rev-parse", "HEAD"); got != u.c2 {
		t.Fatal("HEAD", got)
	}
	if _, err := os.Stat(keep); err != nil {
		t.Fatal("the bump lost the build folder")
	}
	r, err = g.Ensure(ctx, u.src("v1"), dest)
	if err != nil || r.Commit != u.c1 {
		t.Fatalf("downgrade: %+v %v", r, err)
	}
	if m := g.ReadMark(ctx, dest); m == nil || m.Ref != "v1" || m.Commit != u.c1 {
		t.Fatalf("mark after downgrade: %+v", m)
	}
}

func TestEnsureLegacyClone(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	dest := filepath.Join(t.TempDir(), "src")
	sh(t, filepath.Dir(dest), CloneArgs(u.url, "v1", dest)...) // what the old Clone made
	r, err := g.Ensure(ctx, u.src("v2"), dest)
	if err != nil || r.Commit != u.c2 || r.Before != u.c1 {
		t.Fatalf("legacy: %+v %v", r, err)
	}
	if m := g.ReadMark(ctx, dest); m == nil || m.Origin != "repo" || m.Ref != "v2" {
		t.Fatalf("mark: %+v", m)
	}
}

func TestEnsureRefuses(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	base := t.TempDir()

	dirty := filepath.Join(base, "dirty")
	if _, err := g.Ensure(ctx, u.src("v1"), dirty); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dirty, "game.toml"), []byte("edited"), 0o644)
	if _, err := g.Ensure(ctx, u.src("v2"), dirty); err == nil || !strings.Contains(err.Error(), "local changes") {
		t.Fatalf("dirty: %v", err)
	}
	if b, _ := os.ReadFile(filepath.Join(dirty, "game.toml")); string(b) != "edited" {
		t.Fatal("the edit was lost")
	}

	// A full clone of another origin, unmarked: a developer's own tree.
	foreign := filepath.Join(base, "foreign")
	sh(t, base, "clone", "-q", u.dir, foreign)
	if _, err := g.Ensure(ctx, u.src("v2"), foreign); err == nil || !strings.Contains(err.Error(), "did not make") {
		t.Fatalf("foreign: %v", err)
	}

	full := filepath.Join(base, "full")
	os.MkdirAll(full, 0o755)
	os.WriteFile(filepath.Join(full, "x"), []byte("x"), 0o644)
	if _, err := g.Ensure(ctx, u.src("v1"), full); err == nil || !strings.Contains(err.Error(), "not a clone") {
		t.Fatalf("non-empty folder: %v", err)
	}
}

func TestEnsureMovedTagAndFailedFetch(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	base := t.TempDir()
	other := filepath.Join(base, "h")
	bad := u.src("v1")
	bad.Commit = strings.Repeat("0", 40)
	if _, err := g.Ensure(ctx, bad, other); err == nil || !strings.Contains(err.Error(), "tag moved") {
		t.Fatalf("moved tag: %v", err)
	}
	if _, err := os.Stat(other + ".partial"); err == nil {
		t.Fatal("left the partial clone")
	}

	dest := filepath.Join(base, "src")
	if _, err := g.Ensure(ctx, u.src("v1"), dest); err != nil {
		t.Fatal(err)
	}
	bad = u.src("v2")
	bad.Commit = strings.Repeat("0", 40)
	if _, err := g.Ensure(ctx, bad, dest); err == nil || !strings.Contains(err.Error(), "tag moved") {
		t.Fatalf("moved tag on update: %v", err)
	}
	gone := Source{Repo: u.url, Tag: "v9", Commit: u.c2}
	if _, err := g.Ensure(ctx, gone, dest); err == nil || !strings.Contains(err.Error(), "unchanged") {
		t.Fatalf("failed fetch: %v", err)
	}
	if got := sh(t, dest, "rev-parse", "HEAD"); got != u.c1 {
		t.Fatal("a failed update moved HEAD to", got)
	}
}

func TestEnsureLocalThenPlayer(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	// The developer's checkout: one commit past v2 on a branch.
	dev := t.TempDir()
	sh(t, dev, "clone", "-q", u.dir, ".")
	os.WriteFile(filepath.Join(dev, "game.toml"), []byte("wip"), 0o644)
	sh(t, dev, "commit", "-qam", "wip")
	wip := sh(t, dev, "rev-parse", "HEAD")

	dest := filepath.Join(t.TempDir(), "src")
	l, err := ParseSource(dev)
	if err != nil || l.Bundle {
		t.Fatalf("parse: %+v %v", l, err)
	}
	if g.Dirty(ctx, l) {
		t.Fatal("clean checkout reported dirty")
	}
	r, err := g.Ensure(ctx, Source{Repo: u.url, Local: l}, dest)
	if err != nil || r.Commit != wip {
		t.Fatalf("local: %+v %v", r, err)
	}
	if m := g.ReadMark(ctx, dest); m == nil || *m != (Mark{Origin: "local", Ref: "HEAD", Commit: wip, Via: "checkout"}) {
		t.Fatalf("mark: %+v", m)
	}
	// A named ref.
	l2, _ := ParseSource(dev + "#v1")
	if r, err := g.Ensure(ctx, Source{Repo: u.url, Local: l2}, dest); err != nil || r.Commit != u.c1 {
		t.Fatalf("local #v1: %+v %v", r, err)
	}
	l3, _ := ParseSource(dev + "#nope")
	if _, err := g.Ensure(ctx, Source{Repo: u.url, Local: l3}, dest); err == nil || !strings.Contains(err.Error(), "no nope") {
		t.Fatalf("bad ref: %v", err)
	}
	// Back to the release: origin is the repo again.
	r, err = g.Ensure(ctx, u.src("v2"), dest)
	if err != nil || r.Commit != u.c2 {
		t.Fatalf("player: %+v %v", r, err)
	}
	if o := sh(t, dest, "config", "--get", "remote.origin.url"); o != u.url {
		t.Fatal("origin", o)
	}
	if m := g.ReadMark(ctx, dest); m == nil || m.Origin != "repo" {
		t.Fatalf("mark: %+v", m)
	}

	os.WriteFile(filepath.Join(dev, "game.toml"), []byte("uncommitted"), 0o644)
	if !g.Dirty(ctx, l) {
		t.Fatal("dirty checkout not reported")
	}
}

func TestEnsureBundle(t *testing.T) {
	g, ctx := newGit(t)
	u := newUpstream(t)
	b := filepath.Join(t.TempDir(), "g.bundle")
	sh(t, u.dir, "bundle", "create", b, "main")
	l, err := ParseSource(b)
	if err != nil || !l.Bundle || l.URL() != b {
		t.Fatalf("parse: %+v %v", l, err)
	}
	dest := filepath.Join(t.TempDir(), "src")
	r, err := g.Ensure(ctx, Source{Local: l}, dest)
	if err != nil || r.Commit != u.c2 {
		t.Fatalf("bundle: %+v %v", r, err)
	}
	if m := g.ReadMark(ctx, dest); m == nil || m.Via != "bundle" || m.Ref != "refs/heads/main" {
		t.Fatalf("mark: %+v", m)
	}
	two := filepath.Join(t.TempDir(), "two.bundle")
	sh(t, u.dir, "branch", "other", "v1")
	sh(t, u.dir, "bundle", "create", two, "main", "other")
	l2, _ := ParseSource(two)
	if _, err := g.Ensure(ctx, Source{Local: l2}, filepath.Join(t.TempDir(), "x")); err == nil || !strings.Contains(err.Error(), "2 heads") {
		t.Fatalf("two heads: %v", err)
	}
	l2, _ = ParseSource(two + "#other")
	if r, err := g.Ensure(ctx, Source{Local: l2}, filepath.Join(t.TempDir(), "y")); err != nil || r.Commit != u.c1 {
		t.Fatalf("two heads #other: %+v %v", r, err)
	}
}

func TestParseSource(t *testing.T) {
	if _, err := ParseSource(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("missing path")
	}
	if _, err := ParseSource(t.TempDir()); err == nil || !strings.Contains(err.Error(), "not a git checkout") {
		t.Fatalf("plain folder: %v", err)
	}
	if _, err := ParseSource(t.TempDir() + "#"); err == nil {
		t.Fatal("empty ref")
	}
}
