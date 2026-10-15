//go:build windows

package repo

import (
	"os/exec"
	"syscall"
)

// The wizard is a GUI program: a console child would flash a window.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}
