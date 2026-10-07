"""This CLI's own checkout: where it runs from, its commit, and whether that
is the commit the game pins (doctor's cli: row)."""

import os
import subprocess

from . import host, toolkit


def cli_dir():
    """The checkout this package runs from (uv runs it from source)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def toplevel(d):
    """The root of the repository d is in, or ''."""
    r = subprocess.run(
        ["git", "-C", d, "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else ""


def is_checkout(d):
    """d is the root of a repository: its own, not an enclosing one. Under
    uvx the package runs from uv's cache, and a $HOME that is a git
    repository (dotfiles) would otherwise lend its HEAD to the CLI."""
    top = toplevel(d)
    return bool(top) and os.path.realpath(top) == os.path.realpath(d)


def tree_state():
    """(commit, dirty path count) of the CLI checkout, ('', 0) outside git
    or inside someone else's repository."""
    d = cli_dir()
    if not is_checkout(d):
        return "", 0
    head = host.git_head(d)
    if not head:
        return "", 0
    r = subprocess.run(
        ["git", "-C", d, "status", "--porcelain"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return head, r.stdout.decode(errors="replace").count("\n")


def doctor_line():
    """cli: <path> @ <sha> (pinned | differs from the pin <sha>); a checkout
    the bootstrap did not clone (no .xbr-pin) is a developer's, and says
    "not the pin" instead: nothing moves it."""
    d = cli_dir()
    pin = host.g().m["cli"]["commit"]
    head = host.git_head(d) if is_checkout(d) else ""
    if not head:
        return "cli:        %s (not a git checkout)" % d
    note = toolkit.pin_note(d, pin)
    if not note:
        note = " (pinned)" if head == pin else " (not the pin %s)" % pin[:12]
    return "cli:        %s @ %s%s" % (d, head[:12], note)


def checkout_pin():
    """setup: a clone the bootstrap made at an older pin moves to the new
    one (takes effect from the next command)."""
    toolkit.checkout_pin(cli_dir(), host.g().m["cli"]["commit"], "xboxrecomp-cli")
