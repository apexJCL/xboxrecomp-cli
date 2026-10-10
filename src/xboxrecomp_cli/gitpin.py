"""Pins by release tag. A game.toml [cli] or [toolkit] table names a tag, a
commit, or both; with both, the commit locks the tag: a tag that resolves
to another commit (moved, or re-pushed) is refused, never checked out.

A tag resolves with git alone: the checkout's own tags first, then that one
tag fetched from origin (never forced, so a moved remote tag cannot replace
the local one quietly). `^{commit}` peels an annotated tag to its commit.
wrapper/game.py, the bootstrap, repeats these rules without importing this
module (it runs before the CLI exists)."""

import re
import shutil
import subprocess

# manifest imports this module and host imports manifest: CliError is
# looked up when raised.
from . import host

# A subset of git check-ref-format: nothing git or a shell would read as an
# option or a revision expression (v1^, v1~2, -x, a:b).
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# A release tag: a prefix, then a dotted number (v0.1.0, blinx2-v0.1.0).
VERSION_RE = re.compile(r"^(.*?)(\d+(?:\.\d+)*)$")


def tag_ok(tag):
    return (
        bool(TAG_RE.match(tag))
        and ".." not in tag
        and "//" not in tag
        and "/." not in tag
        and not tag.endswith(("/", ".", ".lock"))
    )


def describe(pin):
    """How messages name a pin: 'v0.2.0 (abcdef012345)', the tag alone, or
    the short commit."""
    tag, commit = pin.get("tag", ""), pin.get("commit", "")
    if tag and commit:
        return "%s (%s)" % (tag, commit[:12])
    return tag or commit[:12]


def _git(d, *args):
    try:
        r = subprocess.run(["git", "-C", d] + list(args), capture_output=True)
    except OSError as e:
        return 127, "", str(e)
    return (
        r.returncode,
        r.stdout.decode(errors="replace").strip(),
        r.stderr.decode(errors="replace").strip(),
    )


def local_tag(d, tag):
    """The commit tag names in checkout d, or '' (no such tag, no git)."""
    rc, out, _ = _git(d, "rev-parse", "--verify", "--quiet", "refs/tags/%s^{commit}" % tag)
    return out if rc == 0 else ""


def fetch_tag(d, tag):
    """Fetch the one tag from d's origin into refs/tags/<tag>."""
    ref = "refs/tags/%s" % tag
    rc, _, err = _git(d, "fetch", "--quiet", "--no-tags", "origin", "%s:%s" % (ref, ref))
    if rc != 0:
        lines = err.splitlines()
        raise host.CliError(
            "git fetch of tag %s failed%s" % (tag, ": " + lines[-1] if lines else " (exit %d)" % rc)
        )


def mismatch(what, tag, got, lock, where, d=""):
    """The refusal of a tag that is not its lock. d: the checkout whose own
    copy of the tag is the stale one, for the way to drop it."""
    hint = ""
    if d:
        hint = "; if the remote's tag is right, run 'git -C %s tag -d %s' and rerun" % (d, tag)
    return (
        "%s: tag %s is %s %s, not the pinned commit %s: the tag moved, or game.toml "
        "names the wrong pair%s" % (what, tag, got[:12], where, lock[:12], hint)
    )


def resolve(d, pin, what, fetch=True, fresh=False):
    """The commit pin names, for checkout d: the commit of a commit-only
    pin; else the tag, looked up in d and then (fetch) on its origin,
    checked against the lock. CliError when the tag is nowhere or moved.
    fresh: d was just cloned, so its tags are the remote's."""
    tag, lock = pin.get("tag", ""), pin.get("commit", "")
    if not tag:
        return lock
    where, local = "in %s" % d, d
    if fresh:
        where, local = "on %s" % (pin.get("url") or "origin"), ""
    c = local_tag(d, tag)
    if not c and fetch:
        fetch_tag(d, tag)
        c = local_tag(d, tag)
        where, local = "on %s" % (pin.get("url") or "origin"), ""
    if not c:
        raise host.CliError("%s: tag %s is not %s" % (what, tag, where))
    if lock and c != lock:
        raise host.CliError(mismatch(what, tag, c, lock, where, local))
    return c


def expected(d, pin):
    """doctor's answer, offline: the lock, else the tag in d, else ''."""
    return pin.get("commit") or (local_tag(d, pin["tag"]) if pin.get("tag") else "")


def drift(d, pin):
    """The commit d's own copy of the pinned tag names when it is not the
    lock, else ''."""
    if not (pin.get("tag") and pin.get("commit")):
        return ""
    c = local_tag(d, pin["tag"])
    return c if c and c != pin["commit"] else ""


def remote_tags(url):
    """{tag: commit} on a remote (git ls-remote --tags; an annotated tag's
    peeled ^{} line wins over its tag object), or None when unreachable."""
    if not url or not shutil.which("git"):
        return None
    r = subprocess.run(
        ["git", "ls-remote", "--tags", "--", url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    if r.returncode != 0:
        return None
    tags, peeled = {}, {}
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/") :]
        if name.endswith("^{}"):
            peeled[name[:-3]] = parts[0]
        else:
            tags[name] = parts[0]
    tags.update(peeled)
    return tags


def split_version(tag):
    """(prefix, version tuple) of a tag ending in a dotted number, or None
    (latest). v0.2.0-rc1 splits as ('v0.2.0-rc', (1,)), so it is never a
    v* release."""
    m = VERSION_RE.match(tag)
    if not m:
        return None
    return m.group(1), tuple(int(x) for x in m.group(2).split("."))


def newest_with_prefix(tags, prefix):
    """(tag, commit) of the highest release tag with this prefix, or None."""
    best = None
    for name, commit in tags.items():
        sv = split_version(name)
        if sv and sv[0] == prefix and (best is None or sv[1] > best[0]):
            best = (sv[1], name, commit)
    return (best[1], best[2]) if best else None


def version_tag_at(d, prefix="v"):
    """The highest release tag with this prefix on d's HEAD, or ''."""
    rc, out, _ = _git(d, "tag", "--points-at", "HEAD")
    if rc != 0:
        return ""
    new = newest_with_prefix({t: "" for t in out.split()}, prefix)
    return new[0] if new else ""
