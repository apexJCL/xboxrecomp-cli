"""The toolkit: where it is, the Python that runs its tools, its state for
the generation key, and the clone at the manifest's pin."""

import hashlib
import os
import shutil
import subprocess

from . import host
from .host import CliError

# Written into a clone this CLI made at a pin (the toolkit's, and the
# bootstrap's clone of the CLI): a checkout without it is a developer's own.
PIN_MARK = ".xbr-pin"


def legacy_pin_mark():
    """The mark the game's old single-file CLI wrote (.<slug>-pin)."""
    return ".%s-pin" % host.g().slug


def toolkit_dir(env=None):
    """$XBOXRECOMP_DIR, else external/xboxrecomp, else ../xboxrecomp: the
    order README.md, CMakeLists.txt and bench use."""
    env = os.environ if env is None else env
    if env.get("XBOXRECOMP_DIR"):
        return os.path.abspath(env["XBOXRECOMP_DIR"])
    root = host.g().root
    ext = os.path.join(root, "external", "xboxrecomp")
    if os.path.isdir(ext):
        return ext
    return os.path.abspath(os.path.join(root, "..", "xboxrecomp"))


def tool_python(env=None):
    """The interpreter that runs the toolkit's tools: XBOXRECOMP_PYTHON,
    else this project's venv (made by setup), else a toolkit venv a
    developer already has (tools/macos/setup.sh)."""
    env = os.environ if env is None else env
    if env.get("XBOXRECOMP_PYTHON"):
        return env["XBOXRECOMP_PYTHON"]
    for cand in (
        host.venv_python(),
        host.venv_python(os.path.join(toolkit_dir(env), ".venv")),
    ):
        if os.path.isfile(cand):
            return cand
    raise CliError(
        "no Python environment for the toolkit's tools: run '%s setup' "
        "(or set XBOXRECOMP_PYTHON)" % host.prog()
    )


def toolkit_state():
    """The toolkit commit, plus a hash of uncommitted and untracked changes
    under tools/ (runtime-only toolkit edits do not stale gen/)."""
    tk = toolkit_dir()

    def git(*args):
        r = subprocess.run(
            ["git", "-C", tk] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return r.stdout if r.returncode == 0 else b""

    head = git("rev-parse", "HEAD").decode().strip() or "unknown"
    h = hashlib.sha256(git("diff", "HEAD", "--", "tools"))
    for rel in sorted(
        git("ls-files", "--others", "--exclude-standard", "--", "tools").decode().splitlines()
    ):
        p = os.path.join(tk, rel)
        if os.path.isfile(p):
            h.update(rel.encode() + b"\0" + host.sha256_path(p).encode())
    dirty = h.hexdigest()
    empty = hashlib.sha256(b"").hexdigest()
    return head if dirty == empty else "%s-dirty%s" % (head, dirty[:12])


def exclude_pin_mark(tk, mark=PIN_MARK):
    """Keep the pin mark out of `git status` in a clone: as an untracked
    file it made every packaged version '-dirty'."""
    if not os.path.isfile(os.path.join(tk, mark)):
        return
    # Ask git where the exclude file lives: in a worktree (or a clone whose
    # .git is a file) it is under the common dir, not <tk>/.git/info.
    r = subprocess.run(
        ["git", "-C", tk, "rev-parse", "--git-path", "info/exclude"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        return
    excl = os.path.join(tk, r.stdout.decode().strip())
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    try:
        with open(excl) as f:
            if "/" + mark in f.read().split():
                return
    except OSError:
        pass
    with open(excl, "a") as f:
        f.write("\n/%s\n" % mark)


def rename_legacy_mark(d):
    """A clone the old CLI made carries .<slug>-pin: it becomes .xbr-pin."""
    old = os.path.join(d, legacy_pin_mark())
    if os.path.isfile(old) and not os.path.isfile(os.path.join(d, PIN_MARK)):
        os.replace(old, os.path.join(d, PIN_MARK))
        host.say("%s: %s renamed to %s" % (d, legacy_pin_mark(), PIN_MARK))
    exclude_pin_mark(d)


def pin_note(d, pinned):
    """' (pinned)', ' (differs from the pin X)', or '' for a checkout this
    CLI did not clone at a pin (a developer's own)."""
    if not host.read_text(os.path.join(d, PIN_MARK)):
        return ""
    head = host.git_head(d) or "?"
    return " (pinned)" if head == pinned else " (differs from the pin %s)" % pinned[:12]


def checkout_pin(d, commit, what):
    """A marked clone at another commit: the game moved its pin, so fetch
    if needed and check the new one out. A developer's checkout (no mark)
    is left alone."""
    if not host.read_text(os.path.join(d, PIN_MARK)) or host.git_head(d) == commit:
        return
    host.say("%s: the pin moved; checking out %s" % (what, commit[:12]))
    if (
        subprocess.run(
            ["git", "-C", d, "cat-file", "-e", commit + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    ):
        host.run(["git", "-C", d, "fetch", "--quiet", "origin"])
    host.run(["git", "-C", d, "checkout", "--quiet", commit])
    with open(os.path.join(d, PIN_MARK), "w") as f:
        f.write(commit + "\n")


def clone_toolkit():
    pin = host.g().m["toolkit"]
    tk = toolkit_dir()
    if os.path.isdir(os.path.join(tk, "tools")):
        rename_legacy_mark(tk)
        checkout_pin(tk, pin["commit"], "toolkit")
        host.say("toolkit: %s (left as it is)" % tk)
        return tk
    if not shutil.which("git"):
        raise CliError("git is needed to fetch the toolkit")
    dest = os.path.join(host.g().root, "external", "xboxrecomp")
    host.say("toolkit: cloning %s %s into external/xboxrecomp" % (pin["url"], pin["commit"][:12]))
    host.run(["git", "clone", "--branch", pin["branch"], pin["url"], dest])
    host.run(["git", "-C", dest, "checkout", "--quiet", pin["commit"]])
    with open(os.path.join(dest, PIN_MARK), "w") as f:
        f.write(pin["commit"] + "\n")
    exclude_pin_mark(dest)
    return dest
