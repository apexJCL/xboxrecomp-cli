"""The toolkit: where it is, the Python that runs its tools, its state for
the generation key, and the clone at the manifest's pin."""

import hashlib
import os
import shutil
import subprocess

from . import gitpin, host
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


# What gen/ is made from and written against: the lifter (tools/) and the
# runtime header templates its output includes (recomp_types.h and its
# macros). A toolkit commit that touches neither leaves gen/ fresh.
GEN_PATHS = ("tools", "templates/runtime")


def toolkit_state():
    """The tree ids of tools/ and templates/runtime/ at the toolkit's HEAD
    (`-` for a path the checkout lacks), plus a hash of uncommitted and
    untracked changes under both: runtime-only toolkit commits and edits do
    not stale gen/."""
    tk = toolkit_dir()

    def git(*args):
        r = subprocess.run(
            ["git", "-C", tk] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return r.stdout if r.returncode == 0 else b""

    if not git("rev-parse", "HEAD").strip():
        return "unknown"
    ids = [git("rev-parse", "HEAD:" + p).decode().strip() or "-" for p in GEN_PATHS]
    head = "+".join(ids)
    h = hashlib.sha256(git("diff", "HEAD", "--", *GEN_PATHS))
    for rel in sorted(
        git("ls-files", "--others", "--exclude-standard", "--", *GEN_PATHS).decode().splitlines()
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


def pin_note(d, pin):
    """' (pinned v0.2.0)', ' (differs from the pin X)', or '' for a checkout
    this CLI did not clone at a pin (a developer's own). Offline: a tag
    pin is answered from the lock, else from the checkout's own tags."""
    if not host.read_text(os.path.join(d, PIN_MARK)):
        return ""
    head = host.git_head(d) or "?"
    want = gitpin.expected(d, pin)
    if not want:
        return " (pin %s: not in this checkout's tags)" % pin["tag"]
    if head == want:
        return " (pinned%s)" % (" " + pin["tag"] if pin.get("tag") else "")
    return " (differs from the pin %s)" % gitpin.describe(pin)


def drift_warning(what, d, pin):
    """doctor's warning when d's own copy of the pinned tag is not the
    lock (fetching refuses it on its own), else ''."""
    c = gitpin.drift(d, pin)
    if not c:
        return ""
    return "%s tag %s here is %s, not the lock %s" % (what, pin["tag"], c[:12], pin["commit"][:12])


def checkout_pin(d, pin, what):
    """A marked clone not at the pin: the game moved it, so resolve the pin
    (a tag from the clone's tags, else fetched; checked against the lock),
    fetch the commit if needed, and check it out. A developer's checkout
    (no mark) is left alone, and a clone at the lock needs no tag lookup."""
    if not host.read_text(os.path.join(d, PIN_MARK)):
        return
    head = host.git_head(d)
    if pin.get("commit") and head == pin["commit"]:
        return
    commit = gitpin.resolve(d, pin, what)
    if head == commit:
        return
    host.say(
        "%s: the pin moved; checking out %s"
        % (what, gitpin.describe({"tag": pin.get("tag", ""), "commit": commit}))
    )
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
        checkout_pin(tk, pin, "toolkit")
        host.say("toolkit: %s (left as it is)" % tk)
        return tk
    if not shutil.which("git"):
        raise CliError("git is needed to fetch the toolkit")
    dest = os.path.join(host.g().root, "external", "xboxrecomp")
    if os.path.isdir(dest) and os.listdir(dest):
        raise CliError(
            "toolkit: %s is not empty and has no tools/: move it away, or set "
            "XBOXRECOMP_DIR to a toolkit checkout" % dest
        )
    # Cloned aside and moved into place only at the pin: a moved tag or a
    # failed checkout leaves nothing for the next setup to take as a clone.
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    host.say("toolkit: cloning %s %s into external/xboxrecomp" % (pin["url"], gitpin.describe(pin)))
    try:
        if pin["tag"]:
            host.run(["git", "clone", "--no-checkout", "--", pin["url"], tmp])
            commit = gitpin.resolve(tmp, pin, "toolkit", fresh=True)
        else:
            host.run(["git", "clone", "--branch", pin["branch"], "--", pin["url"], tmp])
            commit = pin["commit"]
        host.run(["git", "-C", tmp, "checkout", "--quiet", commit])
        if host.git_head(tmp) != commit:
            raise CliError("toolkit: the clone is not at the pin %s" % commit[:12])
        with open(os.path.join(tmp, PIN_MARK), "w") as f:
            f.write(commit + "\n")
        exclude_pin_mark(tmp)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest
