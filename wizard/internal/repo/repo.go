// Package repo keeps the game's clone in the build folder at the commit
// this run builds (ensure.go): the release tag's, checked against the
// commit the wizard was built for, or a developer's local source.
package repo

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os/exec"
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
