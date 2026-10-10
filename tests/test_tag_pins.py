"""Pins by release tag: game.toml's tag keys, resolving a tag (annotated
or lightweight) against a lock, the toolkit clone at a tag, doctor's rows,
`pins refresh`'s release lines, `new`'s [cli] tag, and the bootstrap at a
tag. Every remote is a local bare repository with tags, made in tmp: no
test touches the network.

  uv run pytest tests/test_tag_pins.py
"""

import os
import shutil
import subprocess
import sys

import pytest

from xboxrecomp_cli import cli_dir, gitpin, host, manifest, pins, scaffold, toolkit, wrapper
from xboxrecomp_cli.host import CliError

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="no git")

# The test's own repositories: no user config (signing, hooks) applies.
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@example.invalid",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@example.invalid",
)


def git(*args, cwd=None):
    r = subprocess.run(
        [GIT] + list(args), cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


class Remote:
    """A bare repository at <d>/remote.git, filled from a work tree at
    <d>/src: three commits c1..c3 with tools/ and pyproject.toml (a toolkit
    and a CLI checkout both), annotated v0.1.0 (c1), v0.2.0 (c2) and
    blinx2-v0.1.0 (c2), lightweight v0.0.9 (c1) and v0.3.0-rc1 (c3)."""

    def __init__(self, d):
        self.src = os.path.join(d, "src")
        self.url = os.path.join(d, "remote.git")
        os.makedirs(os.path.join(self.src, "tools"))
        git("init", "-q", "-b", "main", self.src)
        self.c = []
        for i in range(3):
            with open(os.path.join(self.src, "tools", "x.py"), "w") as f:
                f.write("# %d\n" % i)
            with open(os.path.join(self.src, "pyproject.toml"), "w") as f:
                f.write("# %d\n" % i)
            git("add", "-A", cwd=self.src)
            git("commit", "-q", "-m", "c%d" % (i + 1), cwd=self.src)
            self.c.append(git("rev-parse", "HEAD", cwd=self.src))
        self.tag("v0.1.0", 0)
        self.tag("v0.2.0", 1)
        self.tag("blinx2-v0.1.0", 1)
        git("tag", "v0.0.9", self.c[0], cwd=self.src)
        git("tag", "v0.3.0-rc1", self.c[2], cwd=self.src)
        git("clone", "-q", "--bare", self.src, self.url)

    def tag(self, name, i, force=False):
        git("tag", "-a", "-m", name, *(["-f"] if force else []), name, self.c[i], cwd=self.src)

    def push_tags(self):
        git("push", "-q", "--force", self.url, "refs/tags/*:refs/tags/*", cwd=self.src)


@pytest.fixture
def remote(d):
    return Remote(d)


def clone(remote, dest, at=None):
    git("clone", "-q", remote.url, dest)
    if at:
        git("-C", dest, "checkout", "-q", at)
    return dest


# ── manifest ─────────────────────────────────────────────────────────────

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = """schema = 1
[game]
name = "Some Game"
slug = "some-game"
[xbe]
title_id = 0x12345678
[cli]
%s
[toolkit]
%s
[pipeline]
game_name = "some"
gen = "src/gen"
[build]
exe = "some_recomp"
"""


TK = 'url = "https://example.invalid/tk.git"\n'


def toml(cli, tk=TK + 'branch = "main"\ncommit = "%s"' % SHA):
    return BASE % (cli, tk)


def test_manifest_tag_forms():
    m = manifest.parse_text(toml('tag = "v0.2.0"'))
    assert m["cli"] == {"tag": "v0.2.0", "commit": "", "url": ""}
    m = manifest.parse_text(
        toml('tag = "v0.2.0"\ncommit = "%s"' % SHA, TK + 'tag = "blinx2-v0.1.0"')
    )
    assert m["cli"]["commit"] == SHA and m["toolkit"]["tag"] == "blinx2-v0.1.0"
    assert m["toolkit"]["branch"] == "" and m["toolkit"]["commit"] == ""
    # The commit-only pins of schema 1 read as before.
    m = manifest.parse_text(toml('commit = "%s"' % SHA))
    assert m["cli"]["tag"] == "" and m["toolkit"]["branch"] == "main"


@pytest.mark.parametrize(
    "cli, tk, why",
    [
        ('url = "x"', None, "cli: tag or commit required"),
        ('tag = "v1"', 'commit = "%s"' % SHA, "toolkit.branch: required without a tag"),
        ('tag = "v1"', 'branch = "main"', "toolkit.commit: required without a tag"),
        ('tag = "v1^"', None, "cli.tag: 'v1\\^' is not a plain tag name"),
        ('tag = "-x"', None, "cli.tag: '-x' is not a plain tag name"),
        ('tag = "a..b"', None, "cli.tag: 'a..b' is not"),
        ('tag = "v1.lock"', None, "cli.tag: 'v1.lock' is not"),
        ('tag = "v1"', 'tag = "x y"', "toolkit.tag: 'x y' is not"),
        ('tag = "v1"\ncommit = "abc"', None, "cli.commit: a full 40-character"),
        ("tag = 'v1'", None, "cli.tag: write as one plain line"),
        ('tag = "v1"\nurl = "--upload-pack=x"', None, "cli.url: '--upload-pack=x' starts with '-'"),
    ],
)
def test_manifest_tag_errors(cli, tk, why):
    text = toml(cli) if tk is None else toml(cli, TK + tk)
    with pytest.raises(manifest.ManifestError, match=why):
        manifest.parse_text(text)


def test_manifest_refuses_an_option_url():
    text = toml('tag = "v1"', 'url = "-oProxyCommand=x"\ntag = "t1"')
    with pytest.raises(manifest.ManifestError, match="toolkit.url: '-oProxyCommand=x' starts"):
        manifest.parse_text(text)


# ── resolving ────────────────────────────────────────────────────────────


def test_resolve_annotated_and_lightweight(d, remote):
    c = clone(remote, os.path.join(d, "c"))
    assert gitpin.local_tag(c, "v0.1.0") == remote.c[0]  # annotated: peeled
    assert gitpin.local_tag(c, "v0.0.9") == remote.c[0]  # lightweight
    assert gitpin.local_tag(c, "v9") == ""
    assert gitpin.resolve(c, {"tag": "v0.2.0"}, "cli") == remote.c[1]
    assert gitpin.resolve(c, {"tag": "v0.2.0", "commit": remote.c[1]}, "cli") == remote.c[1]
    assert gitpin.resolve(c, {"tag": "", "commit": SHA}, "cli") == SHA


def test_resolve_refuses_a_tag_that_is_not_the_lock(d, remote):
    c = clone(remote, os.path.join(d, "c"))
    with pytest.raises(
        CliError,
        match="cli: tag v0.2.0 is %s in .*, not the pinned commit %s: the "
        "tag moved" % (remote.c[1][:12], remote.c[0][:12]),
    ):
        gitpin.resolve(c, {"tag": "v0.2.0", "commit": remote.c[0]}, "cli")
    # The stale copy is the checkout's own: the way to drop it is named.
    with pytest.raises(CliError, match="run 'git -C %s tag -d v0.2.0' and rerun" % c):
        gitpin.resolve(c, {"tag": "v0.2.0", "commit": remote.c[0]}, "cli")
    # A fresh clone's tags are the remote's: nothing local to drop.
    with pytest.raises(CliError, match="v0.2.0 is %s on origin, not" % remote.c[1][:12]) as e:
        gitpin.resolve(c, {"tag": "v0.2.0", "commit": remote.c[0]}, "cli", fresh=True)
    assert "tag -d" not in str(e.value)


def test_resolve_fetches_a_missing_tag_never_forced(d, remote):
    c = clone(remote, os.path.join(d, "c"))
    remote.tag("v0.4.0", 2)
    remote.push_tags()
    pin = {"tag": "v0.4.0", "commit": remote.c[2], "url": remote.url}
    with pytest.raises(CliError, match="tag v0.4.0 is not in"):
        gitpin.resolve(c, pin, "toolkit", fetch=False)
    assert gitpin.resolve(c, pin, "toolkit") == remote.c[2]
    # A tag moved on the remote: the clone keeps the copy it has (and the
    # lock it matches); a clone without one fetches the moved tag and is
    # refused against the lock.
    remote.tag("v0.1.0", 2, force=True)
    remote.push_tags()
    assert gitpin.resolve(c, {"tag": "v0.1.0", "commit": remote.c[0]}, "cli") == remote.c[0]
    git("-C", c, "tag", "-d", "v0.1.0")
    with pytest.raises(
        CliError, match="is %s on %s, not the pinned" % (remote.c[2][:12], remote.url)
    ):
        gitpin.resolve(c, {"tag": "v0.1.0", "commit": remote.c[0], "url": remote.url}, "cli")
    with pytest.raises(CliError, match="git fetch of tag v7 failed"):
        gitpin.resolve(c, {"tag": "v7"}, "cli")


def test_remote_tags_and_versions(d, remote):
    tags = gitpin.remote_tags(remote.url)
    assert tags["v0.1.0"] == remote.c[0] and tags["blinx2-v0.1.0"] == remote.c[1]
    assert tags["v0.3.0-rc1"] == remote.c[2]
    assert gitpin.remote_tags(os.path.join(d, "nowhere.git")) is None
    assert gitpin.split_version("blinx2-v0.1.0") == ("blinx2-v", (0, 1, 0))
    assert gitpin.split_version("v10") == ("v", (10,))
    assert gitpin.split_version("v0.3.0-rc1") == ("v0.3.0-rc", (1,))  # not a v* release
    assert gitpin.newest_with_prefix(tags, "v") == ("v0.2.0", remote.c[1])
    assert gitpin.newest_with_prefix(tags, "blinx2-v") == ("blinx2-v0.1.0", remote.c[1])
    assert gitpin.newest_with_prefix({"v0.9.0": "a", "v0.10.0": "b"}, "v") == ("v0.10.0", "b")
    assert gitpin.newest_with_prefix(tags, "other-v") is None
    c = clone(remote, os.path.join(d, "c"), remote.c[0])
    assert gitpin.version_tag_at(c) == "v0.1.0"  # v0.0.9 is on c1 too
    git("-C", c, "checkout", "-q", remote.c[2])
    assert gitpin.version_tag_at(c) == ""  # only an rc there


# ── pins refresh ─────────────────────────────────────────────────────────


def test_tag_report(d, remote):
    url = remote.url
    r = pins.tag_report("cli", url, "v0.2.0", remote.c[1])
    assert r == "cli:     pinned v0.2.0 (%s) = newest v* tag" % remote.c[1][:12]
    r = pins.tag_report("cli", url, "v0.1.0", remote.c[0])
    assert r == (
        "cli:     pinned v0.1.0 (%s); newest v* tag is v0.2.0 (%s) (move the pin in "
        "game.toml if you want it)" % (remote.c[0][:12], remote.c[1][:12])
    )
    r = pins.tag_report("toolkit", url, "blinx2-v0.1.0", "")
    assert r == "toolkit: pinned blinx2-v0.1.0 (%s) = newest blinx2-v* tag" % remote.c[1][:12]
    remote.tag("v0.2.0", 2, force=True)
    remote.push_tags()
    r = pins.tag_report("cli", url, "v0.2.0", remote.c[1]).splitlines()
    assert r[1] == "cli:     WARNING pinned v0.2.0 is %s on the remote, not the lock %s" % (
        remote.c[2][:12],
        remote.c[1][:12],
    )
    r = pins.tag_report("cli", url, "v0.9.0", SHA).splitlines()
    assert r[0].startswith("cli:     pinned v0.9.0 (012345678")
    assert r[1] == "cli:     WARNING pinned v0.9.0 is not on %s" % url
    r = pins.tag_report("cli", os.path.join(d, "gone.git"), "v0.2.0", "")
    assert r == "cli:     pinned v0.2.0; %s: not reachable" % os.path.join(d, "gone.git")
    # A commit pin keeps the branch-head report.
    r = pins.pin_report("cli", {"tag": "", "commit": remote.c[2], "url": url}, "main")
    assert r == "cli:     pinned %s = main head" % remote.c[2][:12]


# ── the toolkit clone and doctor ─────────────────────────────────────────


@pytest.fixture
def tag_game(d, remote, monkeypatch):
    """A game at <d>/game whose toolkit pin is the bare remote by tag; no
    toolkit beside it (<d>/xboxrecomp)."""
    root = os.path.join(d, "game")
    os.makedirs(root)
    monkeypatch.delenv("XBOXRECOMP_DIR", raising=False)
    saved = manifest.current()
    text = toml('commit = "%s"' % SHA, 'url = "%s"\ntag = "blinx2-v0.1.0"\ncommit = "%s"')
    g = manifest.use(manifest.Game(root, manifest.parse_text(text % (remote.url, remote.c[1]))))
    yield g
    manifest.use(saved)


def test_clone_toolkit_at_a_tag(d, remote, tag_game, capsys):
    dest = toolkit.clone_toolkit()
    assert dest == os.path.join(tag_game.root, "external", "xboxrecomp")
    assert host.git_head(dest) == remote.c[1]
    with open(os.path.join(dest, toolkit.PIN_MARK)) as f:
        assert f.read() == remote.c[1] + "\n"
    assert not os.path.exists(dest + ".partial")
    assert "blinx2-v0.1.0 (%s)" % remote.c[1][:12] in capsys.readouterr().out
    pin = tag_game.m["toolkit"]
    assert toolkit.pin_note(dest, pin) == " (pinned blinx2-v0.1.0)"
    assert toolkit.drift_warning("toolkit", dest, pin) == ""
    # The game moves its pin to a newer release: the marked clone follows,
    # fetching the tag it lacks.
    remote.tag("blinx2-v0.2.0", 2)
    remote.push_tags()
    pin.update(tag="blinx2-v0.2.0", commit=remote.c[2])
    assert toolkit.pin_note(dest, pin).startswith(" (differs from the pin blinx2-v0.2.0")
    toolkit.clone_toolkit()
    assert host.git_head(dest) == remote.c[2]
    # Tag only, offline (the remote gone): the clone's own tag answers.
    shutil.move(remote.url, remote.url + ".away")
    pin.update(commit="")
    toolkit.clone_toolkit()
    assert toolkit.pin_note(dest, pin) == " (pinned blinx2-v0.2.0)"
    # A local tag that is not the lock: doctor warns.
    pin.update(tag="blinx2-v0.1.0", commit=remote.c[0])
    assert toolkit.drift_warning("toolkit", dest, pin) == (
        "toolkit tag blinx2-v0.1.0 here is %s, not the lock %s"
        % (remote.c[1][:12], remote.c[0][:12])
    )
    pin.update(tag="blinx2-v9.0.0", commit="")
    assert toolkit.pin_note(dest, pin) == " (pin blinx2-v9.0.0: not in this checkout's tags)"


def test_clone_toolkit_refuses_a_moved_tag(d, remote, tag_game):
    tag_game.m["toolkit"]["commit"] = remote.c[0]
    with pytest.raises(CliError, match="toolkit: tag blinx2-v0.1.0 is %s" % remote.c[1][:12]):
        toolkit.clone_toolkit()
    ext = os.path.join(tag_game.root, "external")
    assert not os.path.exists(os.path.join(ext, "xboxrecomp"))
    assert not os.path.exists(os.path.join(ext, "xboxrecomp.partial"))


def test_cli_doctor_row_names_the_tag(remote, tag_game, monkeypatch):
    here = cli_dir.cli_dir()
    head = host.git_head(here)
    if not head or not cli_dir.is_checkout(here):
        pytest.skip("the CLI is not a git checkout here")
    tag_game.m["cli"].update(tag="v0.2.0", commit=head)
    assert cli_dir.doctor_line().endswith("@ %s (pinned v0.2.0)" % head[:12])
    tag_game.m["cli"].update(commit="0" * 40)
    assert cli_dir.doctor_line().endswith("(not the pin v0.2.0 (000000000000))")


# ── new ──────────────────────────────────────────────────────────────────


def test_new_resolves_the_cli_release(d, remote, monkeypatch):
    monkeypatch.setattr(pins, "CLI_URL", remote.url)
    monkeypatch.setattr(cli_dir, "tree_state", lambda: ("", 0))
    monkeypatch.setattr(scaffold, "installed_commit", lambda: "")
    # The remote's newest release (the rc is not one).
    assert scaffold.resolve_cli_pin() == (
        "v0.2.0",
        remote.c[1],
        "%s, the newest release" % remote.url,
    )
    assert scaffold.resolve_cli_pin("v0.1.0") == ("v0.1.0", remote.c[0], "--cli-tag")
    assert scaffold.resolve_cli_pin("v0.1.0", offline=True) == (
        "v0.1.0",
        "",
        "--cli-tag (no commit to lock it)",
    )
    with pytest.raises(CliError, match="--cli-tag v7 is not on"):
        scaffold.resolve_cli_pin("v7")
    with pytest.raises(CliError, match="not a plain tag name"):
        scaffold.resolve_cli_pin("v1^")
    assert scaffold.resolve_cli_pin(commit=SHA) == ("", SHA, "--cli-commit")
    # A checkout at a release pins it; one past it pins its commit alone.
    c = clone(remote, os.path.join(d, "c"), remote.c[1])
    monkeypatch.setattr(cli_dir, "cli_dir", lambda: c)
    monkeypatch.setattr(cli_dir, "tree_state", lambda: (host.git_head(c), 0))
    assert scaffold.resolve_cli_pin() == ("v0.2.0", remote.c[1], "this checkout, release v0.2.0")
    git("-C", c, "checkout", "-q", remote.c[2])
    assert scaffold.resolve_cli_pin() == ("", remote.c[2], "this checkout")
    assert scaffold.cli_pin_lines("v0.2.0", SHA) == 'tag = "v0.2.0"\ncommit = "%s"\n' % SHA
    assert scaffold.cli_pin_lines("v0.2.0", "") == 'tag = "v0.2.0"\n'


# ── the bootstrap ────────────────────────────────────────────────────────

FAKE_UV = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "uv 0.9.0"; exit 0; fi
printf '%s\\n' "$@" > "$UV_LOG"
"""


def boot_game(d, remote, cli_lines):
    """<d>/game: the test game with the bootstrap and [cli] replaced;
    <d>/bin: a fake uv and the real git, alone on PATH."""
    root = os.path.join(d, "game")
    os.makedirs(root)
    with open(os.path.join(GAME, "game.toml"), encoding="utf-8") as f:
        text = f.read()
    start = text.index("[cli]\n")
    end = text.index("[toolkit]\n")
    text = text[:start] + '[cli]\n%s\nurl = "%s"\n\n' % (cli_lines, remote.url) + text[end:]
    with open(os.path.join(root, "game.toml"), "w", encoding="utf-8") as f:
        f.write(text)
    manifest.parse_text(text)  # the CLI would read it too
    shutil.copy(wrapper.TEMPLATE, os.path.join(root, "blinx2.py"))
    b = os.path.join(d, "bin")
    os.makedirs(b, exist_ok=True)
    for name, body in (("uv", FAKE_UV), ("git", '#!/bin/sh\nexec "%s" "$@"\n' % GIT)):
        p = os.path.join(b, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
    return root, b


def set_cli(root, cli_lines, url):
    p = os.path.join(root, "game.toml")
    with open(p, encoding="utf-8") as f:
        text = f.read()
    start, end = text.index("[cli]\n"), text.index("[toolkit]\n")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text[:start] + '[cli]\n%s\nurl = "%s"\n\n' % (cli_lines, url) + text[end:])


def boot(d, root, b):
    log = os.path.join(d, "uv.log")
    if os.path.exists(log):
        os.remove(log)
    env = {"PATH": b, "HOME": os.environ.get("HOME", "/"), "UV_LOG": log}
    env.update({k: v for k, v in GIT_ENV.items() if k.startswith("GIT_CONFIG")})
    r = subprocess.run(
        [sys.executable, os.path.join(root, "blinx2.py"), "doctor"],
        env=env,
        cwd=d,
        capture_output=True,
        text=True,
    )
    return r, os.path.exists(log)


def test_bootstrap_clones_at_a_tag_with_its_lock(d, remote):
    root, b = boot_game(d, remote, 'tag = "v0.1.0"\ncommit = "%s"' % remote.c[0])
    r, ran = boot(d, root, b)
    assert r.returncode == 0 and ran, r.stderr
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    assert "cloning %s v0.1.0 (%s)" % (remote.url, remote.c[0][:12]) in r.stderr
    assert host.git_head(dest) == remote.c[0]
    with open(os.path.join(dest, ".xbr-pin")) as f:
        assert f.read() == remote.c[0] + "\n"
    # At the lock, the next run needs no remote at all.
    shutil.move(remote.url, remote.url + ".away")
    r, ran = boot(d, root, b)
    assert r.returncode == 0 and ran and "cloning" not in r.stderr, r.stderr


def test_bootstrap_refuses_a_moved_tag(d, remote):
    root, b = boot_game(d, remote, 'tag = "v0.2.0"\ncommit = "%s"' % remote.c[0])
    r, ran = boot(d, root, b)
    assert r.returncode == 1 and not ran and "Traceback" not in r.stderr, r.stderr
    assert "tag v0.2.0 is %s on %s" % (remote.c[1][:12], remote.url) in r.stderr, r.stderr
    assert "tag -d" not in r.stderr  # the clone is gone: nothing local to drop
    assert "not the pinned commit %s: the tag moved" % remote.c[0][:12] in r.stderr
    assert not os.listdir(os.path.join(root, "external"))


def test_bootstrap_moves_a_stale_clone_to_a_new_tag(d, remote):
    root, b = boot_game(d, remote, 'tag = "v0.1.0"')
    assert boot(d, root, b)[0].returncode == 0
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    assert host.git_head(dest) == remote.c[0]
    # A release made after the clone: fetched by its tag.
    remote.tag("v0.4.0", 2)
    remote.push_tags()
    set_cli(root, 'tag = "v0.4.0"', remote.url)
    r, ran = boot(d, root, b)
    assert r.returncode == 0 and ran, r.stderr
    assert "the pin moved; checking out v0.4.0 (%s)" % remote.c[2][:12] in r.stderr
    assert host.git_head(dest) == remote.c[2]
    # Tag only and offline: the clone's own tag is enough.
    shutil.move(remote.url, remote.url + ".away")
    r, ran = boot(d, root, b)
    assert r.returncode == 0 and ran and "pin moved" not in r.stderr, r.stderr
    # A tag the clone lacks, offline: refused, the clone left as it was.
    set_cli(root, 'tag = "v0.5.0"', remote.url)
    r, ran = boot(d, root, b)
    assert r.returncode == 1 and not ran and "Traceback" not in r.stderr, r.stderr
    assert "git fetch failed" in r.stderr and "no xboxrecomp-cli: clone it beside" in r.stderr
    assert host.git_head(dest) == remote.c[2]


def test_bootstrap_refuses_an_unmarked_checkout_off_the_tag(d, remote):
    root, b = boot_game(d, remote, 'tag = "v0.2.0"')
    dest = clone(remote, os.path.join(root, "external", "xboxrecomp-cli"), remote.c[0])
    r, ran = boot(d, root, b)
    assert r.returncode == 1 and not ran, r.stderr
    assert "is at %s, not the pin v0.2.0, and this script did not clone it" % remote.c[0][:12] in (
        r.stderr
    )
    assert host.git_head(dest) == remote.c[0]
    # At the tag, the same checkout runs as it is.
    git("-C", dest, "checkout", "-q", "v0.2.0")
    assert boot(d, root, b)[0].returncode == 0


def test_bootstrap_refuses_a_revision_tag_and_an_option_url(d, remote):
    root, b = boot_game(d, remote, 'tag = "v0.2.0"')
    assert boot(d, root, b)[0].returncode == 0
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    # v0.2.0^ is v0.2.0's parent to git: refused before git sees it, not run.
    set_cli(root, 'tag = "v0.2.0^"', remote.url)
    r, ran = boot(d, root, b)
    assert r.returncode == 1 and not ran and "Traceback" not in r.stderr, r.stderr
    assert "game.toml [cli] tag 'v0.2.0^' is not a plain tag name" in r.stderr
    assert host.git_head(dest) == remote.c[1]
    shutil.rmtree(dest)
    set_cli(root, 'tag = "v0.2.0"', "--upload-pack=touch x")
    r, ran = boot(d, root, b)
    assert r.returncode == 1 and not ran, r.stderr
    assert "game.toml [cli] url '--upload-pack=touch x' starts with '-'" in r.stderr
    assert not os.listdir(os.path.join(root, "external"))


def test_checkout_pin_refuses_a_moved_tag_in_a_marked_clone(d, remote, tag_game):
    dest = toolkit.clone_toolkit()
    pin = tag_game.m["toolkit"]
    pin.update(commit=remote.c[0])  # the lock no longer matches the clone's tag
    with pytest.raises(CliError, match="run 'git -C %s tag -d blinx2-v0.1.0'" % dest):
        toolkit.clone_toolkit()
    assert host.git_head(dest) == remote.c[1]


def test_clone_toolkit_refuses_a_full_dest_without_tools(tag_game):
    dest = os.path.join(tag_game.root, "external", "xboxrecomp")
    os.makedirs(dest)
    with open(os.path.join(dest, "notes.txt"), "w") as f:
        f.write("mine\n")
    with pytest.raises(CliError, match="is not empty and has no tools/"):
        toolkit.clone_toolkit()
    assert os.listdir(dest) == ["notes.txt"]
    assert not os.path.exists(dest + ".partial")


def test_doctor_report_shows_the_warning_row(d, remote, tag_game, monkeypatch):
    from xboxrecomp_cli import doctor

    toolkit.clone_toolkit()
    tag_game.m["toolkit"]["commit"] = remote.c[0]
    monkeypatch.setattr(cli_dir, "doctor_line", lambda: "cli:        test")
    monkeypatch.setattr(cli_dir, "drift_warning", lambda: "")
    lines = doctor.doctor_report()[0]
    assert (
        "warning:    toolkit tag blinx2-v0.1.0 here is %s, not the lock %s"
        % (remote.c[1][:12], remote.c[0][:12])
    ) in lines, lines


def test_new_names_an_unreachable_remote_and_a_local_release(d, remote, monkeypatch):
    gone = os.path.join(d, "gone.git")
    monkeypatch.setattr(pins, "CLI_URL", gone)
    monkeypatch.setattr(scaffold, "installed_commit", lambda: "")
    assert scaffold.resolve_cli_pin("v0.1.0") == (
        "v0.1.0",
        "",
        "--cli-tag (%s unreachable; no commit to lock it)" % gone,
    )
    c = clone(remote, os.path.join(d, "c"), remote.c[2])
    git("-C", c, "tag", "-a", "-m", "x", "v0.5.0")
    monkeypatch.setattr(cli_dir, "cli_dir", lambda: c)
    monkeypatch.setattr(cli_dir, "tree_state", lambda: (host.git_head(c), 0))
    how = scaffold.resolve_cli_pin()[2]
    assert how.endswith("release v0.5.0 (%s unreachable: is the tag pushed?)" % gone), how
    monkeypatch.setattr(pins, "CLI_URL", remote.url)
    how = scaffold.resolve_cli_pin()[2]
    assert how.endswith("(local only: push the tag to %s before the game)" % remote.url), how
    assert scaffold.resolve_cli_pin(offline=True)[2].endswith("release v0.5.0")
