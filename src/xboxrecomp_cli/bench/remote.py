"""The ssh transport. Every host action is a shell script on the standard
input of `ssh -o BatchMode=yes HOST bash -s` (or inside the host's distrobox,
or inside the host's run lock), so quoting stays local, exactly as
scripts/bench.sh sent them. The scripts are files under host/; this module
only adds the prologue and the assignments each one expects.

The controller never takes, waits on or opens the run lock: the host
scripts do (lock_box.sh.in, run_game.sh, hold_lock.sh).
"""

import os
import re
import shutil
import subprocess
import sys

HOST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host")

SSH_HINT = {
    "darwin": "it ships with macOS; check PATH",
    "win32": "Settings > Apps > Optional features > OpenSSH Client",
}


class BenchError(Exception):
    """bench.sh's die: printed as `bench: MSG` and exit 1."""


def ssh_bin():
    s = shutil.which("ssh")
    if not s:
        raise BenchError(
            "no ssh on PATH; install the OpenSSH client (%s)"
            % SSH_HINT.get(sys.platform, "your distribution's openssh-client package")
        )
    return s


def host_text(name):
    """A host script or template, without its header: the #@ lines it
    starts with. A #@ line further down is part of the script."""
    with open(os.path.join(HOST_DIR, name), encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(True)
    i = 0
    while i < len(lines) and lines[i].startswith("#@"):
        i += 1
    return "".join(lines[i:])


def fill(name, **values):
    text = host_text(name)
    for k, v in values.items():
        text = text.replace("@%s@" % k, v)
    left = re.findall(r"@[A-Z_]+@", text)
    assert not left, (name, left)
    return text


# bash's printf %q (sh_backslash_quote): the same bytes for printable text,
# so a script reads the same as bench.sh's. Text with control characters is
# single-quoted instead (bash would use $'...'; the value is the same).
_BACKSLASHED = set(" \t'\"\\|&;()<>!{}*[]?^$`,")


def shell_quote(s):
    if s == "":
        return "''"
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        return "'" + s.replace("'", "'\\''") + "'"
    out = []
    for i, c in enumerate(s):
        if c in _BACKSLASHED:
            out.append("\\" + c)
        elif c == "~" and (i == 0 or s[i - 1] in "=:"):
            out.append("\\~")
        elif c == "#" and i == 0:
            out.append("\\#")
        else:
            out.append(c)
    return "".join(out)


def quote_words(words):
    """`printf '%q ' WORDS`: each word quoted and followed by a space; no
    words give nothing (bash's printf gave "'' ", so a run without game
    arguments passed the game one empty argument)."""
    return "".join(shell_quote(w) + " " for w in words)


KEEPALIVE = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]


class Remote:
    def __init__(self, cfg):
        self.cfg = cfg

    def prologue(self):
        c = self.cfg
        return fill(
            "prologue.sh.in",
            BENCH_DIR=c["BENCH_DIR"],
            REMOTE_GAME=c.remote_game,
            LLVM_MINGW_ROOT=c["LLVM_MINGW_ROOT"],
            BENCH_PREFIX=c["BENCH_PREFIX"],
            BENCH_BOX=c["BENCH_BOX"],
        )

    def ship(self, name, assignments="", prologue=True):
        """prologue + assignments + host/NAME, the script bench.sh piped."""
        return (self.prologue() if prologue else "") + assignments + host_text(name)

    def argv(self, *cmd, batch=True):
        # Keepalives: a connection whose packets stop arriving (seen on the
        # LAN: the host retransmitting into silence for minutes after the
        # remote script had exited) fails in about two minutes, exit 255,
        # instead of holding the bench and the host's sshd forever.
        return (
            [ssh_bin()]
            + (["-o", "BatchMode=yes"] if batch else [])
            + KEEPALIVE
            + [self.cfg.host]
            + list(cmd)
        )

    def _run(self, argv, script, capture):
        sys.stdout.flush()
        sys.stderr.flush()
        data = script.encode() if script is not None else None
        if capture:
            r = subprocess.run(argv, input=data, stdout=subprocess.PIPE)
            return r.returncode, r.stdout.decode(errors="replace")
        if data is None:
            return subprocess.run(argv).returncode, ""
        return subprocess.run(argv, input=data).returncode, ""

    def remote(self, script, capture=False):
        """ssh HOST bash -s"""
        return self._run(self.argv("bash", "-s"), script, capture)

    def in_box(self, script):
        """ssh HOST distrobox enter BOX -- bash -s"""
        return self._run(
            self.argv("distrobox", "enter", self.cfg["BENCH_BOX"], "--", "bash", "-s"),
            script,
            False,
        )

    def in_box_locked(self, script, exclusive=False, what=None):
        """The same, inside the host's run lock, taken by the host: shared
        for builds, exclusive (-x) for tests. Exit 75: waited an hour."""
        cmd = fill(
            "lock_box.sh.in",
            FL="-x" if exclusive else "-s",
            WHAT=what or ("tests" if exclusive else "build"),
            BENCH_BOX=self.cfg["BENCH_BOX"],
        )
        return self._run(self.argv(cmd), script, False)

    def command(self, cmd, stdin=None, capture=False, batch=True, tty=False):
        """ssh [-t] HOST CMD, CMD one string for the host's shell."""
        argv = self.argv(cmd, batch=batch)
        if tty:
            argv.insert(1, "-t")
        return self._run(argv, stdin, capture)
