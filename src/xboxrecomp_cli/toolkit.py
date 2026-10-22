"""The toolkit: where it is, the Python that runs its tools, its state for
the generation key, and the clone at the manifest's pin."""

import glob
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

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


# What a toolkit can do beyond upstream xboxrecomp's base, each told by the
# file that brings it: the fork (apexJCL/xboxrecomp) has them all, upstream
# v0.14.0 none. A game builds on either; without one, the part that needs
# it is compiled out (the game's CMake guards) or left out here.
FEATURE_FILES = (
    ("env_table", "env table", "src/platform/recomp_env.h"),
    # The enhancements layer is also the config.toml reader: without it no
    # config.toml key is read at all.
    ("enhance", "enhancements", "src/enhance/CMakeLists.txt"),
    ("d3d11", "D3D11", "src/d3d/nv2a_pb_d3d11.c"),
    ("metal", "Metal", "src/d3d/nv2a_pb_metal.m"),
)
# The one feature told by recomp's parser rather than a file.
SPIN_WAITS = ("spin_waits", "spin waits", "--spin-waits")


def recomp_flag_re(flag):
    """The parser definition of a recomp flag, not any mention of it."""
    return re.compile(r"""add_argument\(\s*["']%s["']""" % re.escape(flag))


def recomp_has_flag(tk, flag):
    """Whether tk's recomp parser defines flag, from its source: a file
    read, as gen_key() hashes the commands on every build. A toolkit's sha
    cannot tell (the public fork's shas differ from a developer's)."""
    pat = recomp_flag_re(flag)
    for p in sorted(glob.glob(os.path.join(tk, "tools", "recomp", "*.py"))):
        try:
            with open(p, errors="replace") as f:
                if pat.search(f.read()):
                    return True
        except OSError:
            pass
    return False


def features(tk=None):
    """{name: bool} for the toolkit checkout at tk (default toolkit_dir()),
    from its working tree."""
    tk = tk or toolkit_dir()
    out = {k: os.path.isfile(os.path.join(tk, *rel.split("/"))) for k, _, rel in FEATURE_FILES}
    out[SPIN_WAITS[0]] = recomp_has_flag(tk, SPIN_WAITS[2])
    return out


def feature_labels():
    return dict((k, label) for k, label, _ in FEATURE_FILES + (SPIN_WAITS,))


def _git_ok(d, *args):
    return (
        subprocess.run(
            ["git", "-C", d] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        == 0
    )


def _has_paths(d, commit, rels):
    """{rel: bool}: whether each file is in commit's tree in repo d. The
    trees alone answer (ls-tree), so a blobless fetch fetches no file."""

    def has(rel):
        r = subprocess.run(
            ["git", "-C", d, "ls-tree", "-r", "--name-only", commit, "--", rel],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0 and r.stdout.decode(errors="replace").strip() == rel

    return {rel: has(rel) for rel in rels}


def pin_files(pin, rels):
    """{rel: bool}: whether each file is in the toolkit at the pin, without
    a checkout of it. The pin's commit in the local toolkit's history
    answers; else one commit's trees are fetched (depth 1, no file contents)
    into a scratch repository that is removed after. A tag is checked
    against its lock as a clone would."""
    tk = toolkit_dir()
    if shutil.which("git") is None:
        raise CliError("git is needed to read the toolkit at its pin")
    if os.path.isdir(tk):
        commit = pin.get("commit") or (pin.get("tag") and gitpin.local_tag(tk, pin["tag"]))
        if commit and _git_ok(tk, "cat-file", "-e", commit + "^{commit}"):
            return _has_paths(tk, commit, rels)
    with tempfile.TemporaryDirectory(prefix="xbr-toolkit-pin-") as tmp:
        if not (_git_ok(tmp, "init", "-q") and _git_ok(tmp, "remote", "add", "origin", pin["url"])):
            raise CliError("toolkit: git init failed in %s" % tmp)
        ref = ("refs/tags/" + pin["tag"]) if pin.get("tag") else pin["commit"]
        r = subprocess.run(
            ["git", "-C", tmp, "fetch", "--quiet", "--no-tags", "--depth", "1"]
            + ["--filter=blob:none", "origin", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # A private or missing repository answers 401: fail, never
            # wait on a credential prompt the wizard cannot show.
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip().splitlines()
            raise CliError(
                "toolkit: cannot read %s at %s%s (run '%s setup' to clone it, or check the "
                "network)"
                % (pin["url"], gitpin.describe(pin), ": " + err[-1] if err else "", host.prog())
            )
        r = subprocess.run(
            ["git", "-C", tmp, "rev-parse", "FETCH_HEAD^{commit}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        got = r.stdout.decode().strip()
        if r.returncode != 0 or not got:
            raise CliError(
                "toolkit: %s at %s fetched no commit" % (pin["url"], gitpin.describe(pin))
            )
        if pin.get("tag") and pin.get("commit") and got != pin["commit"]:
            raise CliError(
                gitpin.mismatch("toolkit", pin["tag"], got, pin["commit"], "on " + pin["url"])
            )
        return _has_paths(tmp, got, rels)


# pin_has_enhance's answers in this process, by (toolkit dir, url, tag,
# commit): the wizard and package both ask, and a pin outside the local
# history costs a fetch each time.
_PIN_ENHANCE = {}


def pin_has_enhance(pin):
    """Whether the toolkit at the pin has the enhancements layer (and with
    it the config.toml reader). The one answer for every caller: the
    wizard's options and the bundle's config files and README text."""
    key = (toolkit_dir(), pin["url"], pin.get("tag", ""), pin.get("commit", ""))
    if key not in _PIN_ENHANCE:
        rel = dict((k, rel) for k, _, rel in FEATURE_FILES)["enhance"]
        _PIN_ENHANCE[key] = pin_files(pin, [rel])[rel]
    return _PIN_ENHANCE[key]


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


def off_pin(d, pin):
    """The pin a clone this CLI made is not at ('' when it is, when d is a
    developer's own checkout, or when the pin cannot be told offline). A
    game update moves [toolkit]'s pin; the clone stays where setup left it
    until setup runs again, and a build on it compiles the game's new code
    against the old toolkit."""
    marked = any(host.read_text(os.path.join(d, m)) for m in (PIN_MARK, legacy_pin_mark()))
    if not marked:
        return ""
    want = gitpin.expected(d, pin)
    if not want or host.git_head(d) == want:
        return ""
    return gitpin.describe(pin)


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
        try:
            host.run(["git", "-C", d, "fetch", "--quiet", "origin"])
        except host.CliError:
            raise host.CliError(
                "%s: could not download %s into %s (offline, or the server is "
                "down?): go online and run this again, or delete %s and run "
                "this again to clone it afresh" % (what, commit[:12], d, d)
            ) from None
    try:
        host.run(["git", "-C", d, "checkout", "--quiet", commit])
    except host.CliError:
        # A player's local edit to a file the pin changes blocks the move.
        raise host.CliError(
            "%s: could not check out %s in %s (files there were changed by "
            "hand?): delete %s and run this again to clone it afresh" % (what, commit[:12], d, d)
        ) from None
    with open(os.path.join(d, PIN_MARK), "w") as f:
        f.write(commit + "\n")


def clone_toolkit():
    pin = host.g().m["toolkit"]
    tk = toolkit_dir()
    if os.path.isdir(os.path.join(tk, "tools")):
        rename_legacy_mark(tk)
        before = host.git_head(tk)
        checkout_pin(tk, pin, "toolkit")
        if host.git_head(tk) == before:
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
