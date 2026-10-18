package repo

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// MarkName is the wizard's mark on its clone, in <git dir>/xbr/ beside the
// CLI's build state, so it never dirties the tree. The CLI reads it for the
// manifest's "source" (lib.MARK_NAME).
const MarkName = "wizard.json"

// Mark says how the wizard last set the clone (D4).
type Mark struct {
	Origin string `json:"origin"` // "repo": the release tag; "local": --source
	Ref    string `json:"ref"`    // the tag, or the local source's ref
	Commit string `json:"commit"`
	Via    string `json:"via,omitempty"` // local: "checkout" or "bundle"
}

// Local is a developer's --source PATH[#REF] (D14).
type Local struct {
	Path   string // absolute: a git checkout, or a file from git bundle create
	Ref    string // "" for the default: HEAD, or a bundle's only head
	Bundle bool
}

// ParseSource reads --source PATH[#REF].
func ParseSource(arg string) (*Local, error) {
	path, ref := arg, ""
	if i := strings.LastIndex(arg, "#"); i >= 0 {
		path, ref = arg[:i], arg[i+1:]
		if ref == "" {
			return nil, fmt.Errorf("--source %s: no ref after #", arg)
		}
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("--source %s: %w", arg, err)
	}
	st, err := os.Stat(abs)
	if err != nil {
		return nil, fmt.Errorf("--source %s: %w", arg, err)
	}
	if st.IsDir() {
		if _, err := os.Stat(filepath.Join(abs, ".git")); err != nil {
			return nil, fmt.Errorf("--source %s: not a git checkout (no .git)", arg)
		}
	}
	return &Local{Path: abs, Ref: ref, Bundle: !st.IsDir()}, nil
}

// URL is the clone's origin: a file:// URL for a checkout, so later runs
// fetch from it; a bundle by its path, since git opens a bundle only by
// path and not through a file:// URL.
func (l *Local) URL() string {
	if l.Bundle {
		return l.Path
	}
	p := filepath.ToSlash(l.Path)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p // C:/x is file:///C:/x
	}
	return "file://" + p
}

// Via is the mark's and the manifest's word for the kind of source.
func (l *Local) Via() string {
	if l.Bundle {
		return "bundle"
	}
	return "checkout"
}

// LocalRef is the ref a local source builds: the one given, else HEAD of a
// checkout, else a bundle's only head.
func (g Git) LocalRef(ctx context.Context, l *Local) (string, error) {
	if l.Ref != "" {
		return l.Ref, nil
	}
	if !l.Bundle {
		return "HEAD", nil
	}
	out, err := g.run(ctx, filepath.Dir(l.Path), "bundle", "list-heads", l.Path)
	if err != nil {
		return "", fmt.Errorf("--source %s: not a git bundle (%w)", l.Path, err)
	}
	var heads []string
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) == 2 && f[1] != "HEAD" && !slices.Contains(heads, f[1]) {
			heads = append(heads, f[1])
		}
	}
	if len(heads) != 1 {
		return "", fmt.Errorf("--source %s: the bundle has %d heads (%s): name one as PATH#REF",
			l.Path, len(heads), strings.Join(heads, ", "))
	}
	return heads[0], nil
}

// Dirty reports a checkout with uncommitted changes to tracked files:
// only commits are built, so the wizard says those changes are not in the
// build. Untracked files are left out: byproducts a .gitignore misses
// would warn on every run.
func (g Git) Dirty(ctx context.Context, l *Local) bool {
	if l.Bundle {
		return false
	}
	out, err := g.run(ctx, l.Path, "status", "--porcelain", "--untracked-files=no")
	return err == nil && out != ""
}

// Source is what Ensure puts in the clone: the release (Repo, Tag,
// Commit), or a local source when Local is set. Repo is [wizard] repo
// either way ("" for a private game): a player-mode run sets origin back
// to it, and the legacy check compares against it.
type Source struct {
	Repo, Tag, Commit string
	Local             *Local
}

// Result is the clone's commit after Ensure, and before it ("" for a new
// clone).
type Result struct {
	Commit, Before string
}

// Ensure makes dest the clone this run builds, keeping the build folder
// around it (D4):
//   - no clone: clone, check, mark;
//   - the wizard's clone, clean: fetch the tag (checked against Commit)
//     or the local ref, check it out detached, rewrite the mark;
//   - a clone from before the mark (clean, origin == Repo, shallow, at a
//     tag): marked, then the same;
//   - local changes, or a clone that is not the wizard's: refused, so a
//     developer's edits are never thrown away.
//
// A failed fetch leaves the old checkout as it was.
func (g Git) Ensure(ctx context.Context, src Source, dest string) (Result, error) {
	if src.Local == nil && src.Tag == "" {
		return Result{}, errors.New("no release tag to clone")
	}
	if _, err := os.Stat(filepath.Join(dest, ".git")); err != nil {
		if _, err := os.Stat(dest); err == nil {
			if ents, _ := os.ReadDir(dest); len(ents) > 0 {
				return Result{}, fmt.Errorf("%s exists and is not a clone: move it away or pick another build folder", dest)
			}
			os.Remove(dest)
		}
		c, err := g.fresh(ctx, src, dest)
		return Result{Commit: c}, err
	}
	before := g.Head(ctx, dest)
	if before == "" {
		return Result{}, fmt.Errorf("%s holds a broken clone (no HEAD): move it away or pick another build folder", dest)
	}
	// Untracked files do not count: a checkout never touches them, and a
	// build may leave some a game's .gitignore does not list.
	if st, err := g.run(ctx, dest, "status", "--porcelain", "--untracked-files=no"); err != nil || st != "" {
		return Result{Before: before}, fmt.Errorf("%s has local changes: commit or move them away, "+
			"or pick another build folder (the wizard never overwrites them)", dest)
	}
	mark := g.ReadMark(ctx, dest)
	if mark == nil {
		tag := g.legacyTag(ctx, src.Repo, dest)
		if tag == "" {
			return Result{Before: before}, fmt.Errorf("%s holds a clone the wizard did not make: "+
				"move it away or pick another build folder", dest)
		}
		mark = &Mark{Origin: "repo", Ref: tag, Commit: before}
		if err := g.writeMark(ctx, dest, *mark); err != nil {
			return Result{Before: before}, err
		}
	}
	m, err := g.update(ctx, src, dest, before)
	if err != nil {
		return Result{Commit: before, Before: before}, err
	}
	if err := g.writeMark(ctx, dest, m); err != nil {
		return Result{Commit: m.Commit, Before: before}, err
	}
	return Result{Commit: m.Commit, Before: before}, nil
}

// fresh clones into dest.partial and moves it in place once it is at the
// commit, marked.
func (g Git) fresh(ctx context.Context, src Source, dest string) (string, error) {
	part := dest + ".partial"
	os.RemoveAll(part)
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}
	fail := func(err error) (string, error) {
		os.RemoveAll(part)
		return "", err
	}
	var m Mark
	if l := src.Local; l != nil {
		var err error
		if m, err = g.cloneLocal(ctx, l, part); err != nil {
			return fail(err)
		}
	} else {
		if _, err := g.run(ctx, filepath.Dir(dest), CloneArgs(src.Repo, src.Tag, part)...); err != nil {
			return fail(err)
		}
		if h := g.Head(ctx, part); h != src.Commit {
			return fail(movedTag(src, h))
		}
		m = Mark{Origin: "repo", Ref: src.Tag, Commit: src.Commit}
	}
	if err := g.writeMark(ctx, part, m); err != nil {
		return fail(err)
	}
	if err := os.Rename(part, dest); err != nil {
		return fail(err)
	}
	return m.Commit, nil
}

// cloneLocal clones a local source into dir (which must not exist) and
// checks out its ref detached.
func (g Git) cloneLocal(ctx context.Context, l *Local, dir string) (Mark, error) {
	ref, err := g.LocalRef(ctx, l)
	if err != nil {
		return Mark{}, err
	}
	// A file:// URL clones through git's transport (as --no-local does),
	// never by hard links into the developer's own object store.
	if _, err := g.run(ctx, filepath.Dir(dir), "clone", "-q", "--no-checkout", "--", l.URL(), dir); err != nil {
		return Mark{}, err
	}
	c, err := g.fetchLocal(ctx, dir, ref)
	if err != nil {
		return Mark{}, err
	}
	if _, err := g.run(ctx, dir, "checkout", "-q", "--detach", c); err != nil {
		return Mark{}, err
	}
	return Mark{Origin: "local", Ref: ref, Commit: c, Via: l.Via()}, nil
}

// Snapshot is a throwaway clone of a local source at its ref, in a new
// folder under tmp: the tree the wizard reads the game's config from
// before it knows the build folder. The caller removes the folder.
func (g Git) Snapshot(ctx context.Context, l *Local, tmp string) (dir, commit string, err error) {
	base, err := os.MkdirTemp(tmp, "xbr-source-")
	if err != nil {
		return "", "", err
	}
	dir = filepath.Join(base, "src")
	m, err := g.cloneLocal(ctx, l, dir)
	if err != nil {
		os.RemoveAll(base)
		return "", "", err
	}
	return dir, m.Commit, nil
}

// update moves a clean clone of the wizard's to the run's commit. origin
// is set first: back to the repo on a player-mode run after a local one.
func (g Git) update(ctx context.Context, src Source, dest, before string) (Mark, error) {
	if l := src.Local; l != nil {
		ref, err := g.LocalRef(ctx, l)
		if err != nil {
			return Mark{}, err
		}
		if _, err := g.run(ctx, dest, "remote", "set-url", "origin", l.URL()); err != nil {
			return Mark{}, err
		}
		c, err := g.fetchLocal(ctx, dest, ref)
		if err != nil {
			return Mark{}, err
		}
		if c != before {
			if _, err := g.run(ctx, dest, "checkout", "-q", "--detach", c); err != nil {
				return Mark{}, err
			}
		}
		return Mark{Origin: "local", Ref: ref, Commit: c, Via: l.Via()}, nil
	}
	if src.Repo != "" {
		if _, err := g.run(ctx, dest, "remote", "set-url", "origin", src.Repo); err != nil {
			return Mark{}, err
		}
	}
	if before != src.Commit {
		if _, err := g.run(ctx, dest, "fetch", "-q", "--depth", "1", "origin", "tag", src.Tag); err != nil {
			return Mark{}, fmt.Errorf("cannot fetch %s %s (the build folder is unchanged): %w", src.Repo, src.Tag, err)
		}
		got, _ := g.run(ctx, dest, "rev-parse", "--verify", "-q", "refs/tags/"+src.Tag+"^{commit}")
		if got != src.Commit {
			return Mark{}, movedTag(src, got)
		}
		if _, err := g.run(ctx, dest, "checkout", "-q", "--detach", src.Commit); err != nil {
			return Mark{}, err
		}
	}
	return Mark{Origin: "repo", Ref: src.Tag, Commit: src.Commit}, nil
}

// fetchLocal fetches ref from origin and returns its commit. A full
// commit id that origin does not advertise is still found when the clone
// already has it.
func (g Git) fetchLocal(ctx context.Context, dir, ref string) (string, error) {
	if _, err := g.run(ctx, dir, "fetch", "-q", "--no-tags", "origin", ref); err != nil {
		if c, e := g.run(ctx, dir, "rev-parse", "--verify", "-q", ref+"^{commit}"); e == nil && c != "" {
			return c, nil
		}
		return "", fmt.Errorf("the local source has no %s: %w", ref, err)
	}
	return g.run(ctx, dir, "rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}")
}

func movedTag(src Source, got string) error {
	return fmt.Errorf("the tag %s of %s is %s, but this wizard was built for %s: "+
		"the tag moved; download the wizard again from the release page", src.Tag, src.Repo, short(got), short(src.Commit))
}

// legacyTag is the tag a clone made before the mark existed is at, or ""
// when it is not such a clone: origin is the repo, it is shallow, and HEAD
// is a tag's commit (what CloneArgs makes).
func (g Git) legacyTag(ctx context.Context, repoURL, dir string) string {
	if repoURL == "" {
		return ""
	}
	if o, _ := g.run(ctx, dir, "config", "--get", "remote.origin.url"); o != repoURL {
		return ""
	}
	if s, _ := g.run(ctx, dir, "rev-parse", "--is-shallow-repository"); s != "true" {
		return ""
	}
	tags, _ := g.run(ctx, dir, "tag", "--points-at", "HEAD")
	if tags == "" {
		return ""
	}
	return strings.SplitN(tags, "\n", 2)[0]
}

func (g Git) markPath(ctx context.Context, dir string) (string, error) {
	gd, err := g.run(ctx, dir, "rev-parse", "--absolute-git-dir")
	if err != nil {
		return "", err
	}
	return filepath.Join(gd, "xbr", MarkName), nil
}

// ReadMark is the clone's mark, or nil when it has none (or an unreadable
// one, which then takes the legacy check).
func (g Git) ReadMark(ctx context.Context, dir string) *Mark {
	p, err := g.markPath(ctx, dir)
	if err != nil {
		return nil
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil
	}
	var m Mark
	if json.Unmarshal(b, &m) != nil || (m.Origin != "repo" && m.Origin != "local") {
		return nil
	}
	return &m
}

func (g Git) writeMark(ctx context.Context, dir string, m Mark) error {
	p, err := g.markPath(ctx, dir)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(m, "", "  ")
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// MarkIn is the mark of the clone at dir, read without git: the Welcome
// page names the build folder's clone before any tool is fetched (D13).
// nil when dir is no marked clone.
func MarkIn(dir string) *Mark {
	b, err := os.ReadFile(filepath.Join(dir, ".git", "xbr", MarkName))
	if err != nil {
		return nil
	}
	var m Mark
	if json.Unmarshal(b, &m) != nil || (m.Origin != "repo" && m.Origin != "local") || m.Commit == "" {
		return nil
	}
	return &m
}
