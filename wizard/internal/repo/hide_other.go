//go:build !windows

package repo

import "os/exec"

func hideWindow(*exec.Cmd) {}
