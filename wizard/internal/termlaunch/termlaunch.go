// Package termlaunch opens a command in a terminal window on Linux, where
// sudo asks for the password itself: the wizard never handles one.
package termlaunch

import (
	"errors"
	"os/exec"
)

// Terminals in the order tried: KDE's (the Deck's Desktop Mode), the
// Debian alternative, GNOME's, xterm.
var Terminals = []string{"konsole", "x-terminal-emulator", "gnome-terminal", "xterm"}

// Args is the argument list that runs command in a terminal and keeps the
// window open until Enter. command is a sh script; params reach it as $1,
// $2, ... and are never parsed by the shell, so a path with spaces or
// quotes cannot break or extend the command. have reports whether a
// program is on PATH.
func Args(have func(string) bool, command string, params ...string) ([]string, error) {
	script := command + `; echo; printf 'Done. Press Enter to close this window. '; read _`
	sh := append([]string{"sh", "-c", script, "sh"}, params...)
	for _, t := range Terminals {
		if !have(t) {
			continue
		}
		if t == "gnome-terminal" {
			return append([]string{t, "--"}, sh...), nil
		}
		return append([]string{t, "-e"}, sh...), nil
	}
	return nil, errors.New("no terminal program found (konsole, x-terminal-emulator, gnome-terminal or xterm): run the command yourself")
}

// Open starts the terminal and returns without waiting.
func Open(command string, params ...string) error {
	args, err := Args(func(p string) bool { _, err := exec.LookPath(p); return err == nil }, command, params...)
	if err != nil {
		return err
	}
	return exec.Command(args[0], args[1:]...).Start()
}
