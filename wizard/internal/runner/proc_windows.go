//go:build windows

package runner

import (
	"os/exec"
	"strconv"
	"syscall"
)

const (
	createNoWindow        = 0x08000000
	createNewProcessGroup = 0x00000200
)

// setGroup starts the child without a console window (the wizard is a GUI
// program) in a group of its own.
func setGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow | createNewProcessGroup}
}

// interrupt ends the process tree: a GUI parent has no console to send
// CTRL_BREAK_EVENT through, so taskkill /T is the dependable stop.
func interrupt(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	k := exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid))
	k.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
	return k.Run()
}
