//go:build !windows

package runner

import (
	"os/exec"
	"syscall"
)

// setGroup puts the child in its own process group so a cancel reaches
// the CLI and every tool it started.
func setGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func interrupt(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
}
