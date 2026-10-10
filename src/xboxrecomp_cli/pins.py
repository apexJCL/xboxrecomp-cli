"""pins refresh (maintainers): the download hashes in config/setup-pins.json
and the game's uv.lock, then the newer releases (a tag pin) or heads (a
commit pin) of the toolkit and of the CLI next to what game.toml pins.
game.toml itself is never written: moving a pin is an edit a maintainer
makes and the game's commit reviews."""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

from . import env, fetch, gitpin, host
from .host import CliError

# This CLI's public repository, the branch a commit pin follows (`pins
# refresh` reports its head) and its release tags' prefix (`new` writes
# the newest into [cli]).
CLI_URL = "https://github.com/apexJCL/xboxrecomp-cli.git"
CLI_BRANCH = "main"
CLI_TAG_PREFIX = "v"

# The umu-launcher release a steamos bundle's installer may download for a
# host without umu-run (a stock Steam Deck): `install.sh --fetch-umu`, or a
# yes at its prompt. The installer checks the download against this sha256
# and fetches nothing else. Moving it is an edit reviewed here, with the
# sha256 of the release asset taken when the pin moves.
UMU_LAUNCHER = {
    "version": "1.4.4",
    "url": "https://github.com/Open-Wine-Components/umu-launcher/releases/download/"
    "1.4.4/umu-launcher-1.4.4-zipapp.tar",
    "sha256": "eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851",
}


def _get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "%s-pins" % host.prog(), "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def remote_head(url, branch):
    """The commit a remote branch points at (git ls-remote), or None."""
    if not url or not shutil.which("git"):
        return None
    r = subprocess.run(
        ["git", "ls-remote", "--", url, "refs/heads/" + branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    words = r.stdout.decode().split()
    return words[0] if r.returncode == 0 and words else None


def head_report(what, url, branch, pinned):
    head = remote_head(url, branch)
    if head is None:
        return "%-8s pinned %s; %s %s: not reachable" % (
            what + ":",
            pinned[:12],
            url or "(no url)",
            branch,
        )
    if head == pinned:
        return "%-8s pinned %s = %s head" % (what + ":", pinned[:12], branch)
    return "%-8s pinned %s; %s head is %s (move the pin in game.toml if you want it)" % (
        what + ":",
        pinned[:12],
        branch,
        head[:12],
    )


def tag_report(what, url, tag, lock):
    """A tag pin against the remote's tags: the newest release with the
    pinned tag's prefix, and a WARNING when the pinned tag is gone or no
    longer resolves to the lock."""
    w = "%-8s" % (what + ":")
    tags = gitpin.remote_tags(url)
    if tags is None:
        return "%s pinned %s; %s: not reachable" % (w, tag, url or "(no url)")
    at = tags.get(tag)
    pinned = "%s (%s)" % (tag, (lock or at)[:12]) if (lock or at) else tag
    sv = gitpin.split_version(tag)
    if not sv:
        lines = [
            "%s pinned %s; not a release tag (no trailing version), no newer one to name"
            % (w, pinned)
        ]
    else:
        new = gitpin.newest_with_prefix(tags, sv[0])
        if new is None or new[0] == tag:
            lines = ["%s pinned %s = newest %s* tag" % (w, pinned, sv[0])]
        else:
            lines = [
                "%s pinned %s; newest %s* tag is %s (%s) (move the pin in game.toml if you want it)"
                % (w, pinned, sv[0], new[0], new[1][:12])
            ]
    if at is None:
        lines.append("%s WARNING pinned %s is not on %s" % (w, tag, url))
    elif lock and at != lock:
        lines.append(
            "%s WARNING pinned %s is %s on the remote, not the lock %s"
            % (w, tag, at[:12], lock[:12])
        )
    return "\n".join(lines)


def pin_report(what, pin, branch):
    """tag_report for a tag pin, head_report (the branch) for a commit pin."""
    if pin.get("tag"):
        return tag_report(what, pin.get("url", ""), pin["tag"], pin.get("commit", ""))
    return head_report(what, pin.get("url", ""), branch, pin["commit"])


def pins_refresh():
    """The GitHub and SourceForge pins in setup-pins.json, then the Python
    half: `uv lock --upgrade` re-resolves the game's uv.lock (its
    pyproject.toml keeps capstone and pefile on their exact versions)."""
    g = host.g()
    uv, _ = env.find_uv()
    tag = fetch.mingw_tag()
    rel = _get_json("https://api.github.com/repos/mstorsjo/llvm-mingw/releases/tags/%s" % tag)
    want = sorted(set(v.format(tag=tag) for v in fetch.MINGW_ASSETS.values()))
    assets = {}
    for a in rel["assets"]:
        if a["name"] in want:
            if not (a.get("digest") or "").startswith("sha256:"):
                raise CliError("%s has no sha256 digest in the release API" % a["name"])
            assets[a["name"]] = {
                "url": a["browser_download_url"],
                "sha256": a["digest"][len("sha256:") :],
                "size": a["size"],
            }
    missing = set(want) - set(assets)
    if missing:
        raise CliError("llvm-mingw %s lacks %s" % (tag, ", ".join(sorted(missing))))
    # SourceForge publishes no sha256: hash the download here, once, and
    # pin that (trust on first use by the maintainer; setup then checks it).
    nsis_version = g.m["toolchain"]["nsis"]
    nsis_url = "https://sourceforge.net/projects/nsis/files/NSIS%%203/%s/nsis-%s.zip/download" % (
        nsis_version,
        nsis_version,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nsis.zip")
        with (
            urllib.request.urlopen(
                urllib.request.Request(nsis_url, headers={"User-Agent": "%s-pins" % host.prog()})
            ) as r,
            open(p, "wb") as f,
        ):
            shutil.copyfileobj(r, f)
        nsis = {
            "version": nsis_version,
            "url": nsis_url,
            "sha256": host.sha256_path(p),
            "size": os.path.getsize(p),
        }
    pins = {
        "_comment": "Generated by '%s pins refresh'. setup checks every download against these."
        % host.prog(),
        "llvm_mingw": {"tag": tag, "assets": assets},
        "nsis": nsis,
    }
    with open(g.pins, "w", newline="\n") as f:
        json.dump(pins, f, indent=2, sort_keys=True)
        f.write("\n")
    host.run([uv, "lock", "--upgrade", "--project", g.root], env=env.uv_env())
    host.say("wrote %s, %s" % (os.path.relpath(g.pins, g.root), os.path.relpath(g.uv_lock, g.root)))
    tk, cli = g.m["toolkit"], g.m["cli"]
    host.say(pin_report("toolkit", tk, tk["branch"]))
    host.say(pin_report("cli", cli, CLI_BRANCH))
