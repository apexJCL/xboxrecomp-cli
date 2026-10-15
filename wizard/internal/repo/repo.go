// Package repo clones the game repo at the release tag and checks that
// the clone is the commit the wizard was built for.
package repo

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Git runs git with env (the tools folders first on PATH).
type Git struct {
	Path string
	Env  []string
	Out  io.Writer // progress and errors, for the log pane
}

func (g Git) run(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, g.Path, args...)
	cmd.Dir = dir
	cmd.Env = g.Env
	var out bytes.Buffer
	cmd.Stdout = &out
	if g.Out != nil {
		cmd.Stderr = g.Out
	}
	hideWindow(cmd)
	err := cmd.Run()
	if err != nil {
		return "", fmt.Errorf("git %s: %w", strings.Join(args, " "), err)
	}
	return strings.TrimSpace(out.String()), nil
}

// Head is the clone's HEAD commit, or "" when dir is no clone.
func (g Git) Head(ctx context.Context, dir string) string {
	h, err := g.run(ctx, dir, "rev-parse", "HEAD")
	if err != nil {
		return ""
	}
	return h
}

// CloneArgs is the clone command: the release tag only, shallow.
func CloneArgs(url, tag, dest string) []string {
	return []string{"clone", "--branch", tag, "--depth", "1", "--", url, dest}
}

// Clone puts the repo at tag into dest, made aside and moved in place
// once HEAD is the commit. A dest already at the commit is reused; a dest
// at another commit is refused with both named (the player's folder is
// never overwritten).
func (g Git) Clone(ctx context.Context, url, tag, commit, dest string) error {
	if _, err := os.Stat(filepath.Join(dest, ".git")); err == nil {
		h := g.Head(ctx, dest)
		if h == commit {
			return nil
		}
		return fmt.Errorf("%s holds a clone at %s, not this wizard's release %s (%s): "+
			"move it away or pick another install folder", dest, short(h), tag, short(commit))
	} else if _, err := os.Stat(dest); err == nil {
		if ents, _ := os.ReadDir(dest); len(ents) > 0 {
			return fmt.Errorf("%s exists and is not a clone: move it away or pick another install folder", dest)
		}
		os.Remove(dest)
	}
	part := dest + ".partial"
	os.RemoveAll(part)
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	if _, err := g.run(ctx, filepath.Dir(dest), CloneArgs(url, tag, part)...); err != nil {
		os.RemoveAll(part)
		return err
	}
	if h := g.Head(ctx, part); h != commit {
		os.RemoveAll(part)
		return fmt.Errorf("the tag %s of %s is %s, but this wizard was built for %s: "+
			"the tag moved; download the wizard again from the release page", tag, url, short(h), short(commit))
	}
	return os.Rename(part, dest)
}

func short(c string) string {
	if len(c) > 12 {
		return c[:12]
	}
	if c == "" {
		return "no commit"
	}
	return c
}

// CLT is where the Command Line Tools install (the CLI's host.CLT).
const CLT = "/Library/Developer/CommandLineTools"

// HaveDeveloperTools reports a Mac whose git works: the Command Line
// Tools or Xcode selected.
func HaveDeveloperTools() bool {
	return exec.Command("xcode-select", "-p").Run() == nil
}

// InstallDeveloperTools opens Apple's own installer dialog; the caller
// polls HaveDeveloperTools.
func InstallDeveloperTools() error {
	return exec.Command("xcode-select", "--install").Run()
}
