"""`<game> public init|export|audit` on throwaway repositories: a game from
`xbr new`, a bare public repo and bare CLI and toolkit repos with release
tags, all over file:// through url.insteadOf, so the ssh and https URLs the
manifest names resolve without a network. Every name, host and address
here is made up; the planted ones are built from pieces so this file does
not trip the audit of this repository.

  uv run pytest tests/test_public.py
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from test_scaffold import TK, game, new, tk  # noqa: F401 (fixtures)

from xboxrecomp_cli import layout, main, manifest, pins, public

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("git") or not shutil.which("sh"),
    reason="needs git and sh",
)

# Stand-ins for the private patterns: a bench host's name and an address.
HOSTNAME = "bench" + "box-7"
PRIVATE_EMAIL = "some" + "one.private@corp.example"
AUTHOR = "Pub Owner <owner@example.invalid>"
URL = "git@github.example:someowner/my-game.git"
TK_URL = "https://git.example/tk/xboxrecomp.git"


def git(repo, *args, check=True, input=None, env=None):
    r = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Private Person",
            "-c",
            "user.email=" + PRIVATE_EMAIL,
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        input=input,
        env=env,
    )
    if check and r.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), r.stderr))
    return r


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def tagged_bare(d, name, tag):
    """A bare repo with one commit and an annotated tag on it: (path, sha)."""
    src = os.path.join(d, name + "-src")
    os.makedirs(src)
    git(src, "init", "-q")
    write(os.path.join(src, "README.md"), name + "\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    git(src, "tag", "-a", tag, "-m", tag)
    bare = os.path.join(d, name + ".git")
    git(d, "clone", "-q", "--bare", src, bare)
    return bare, git(src, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def pub(d, tk, game, monkeypatch):  # noqa: F811
    """A conformed game in d/g with [public], committed under a private
    identity, its public repo d/pub.git (empty), and XBR_PUBLIC_DIR with the
    patterns and one rewrite rule."""
    cli_bare, cli_sha = tagged_bare(d, "cli", "v1.0")
    tk_bare, tk_sha = tagged_bare(d, "tk", "v2.0")
    root = os.path.join(d, "g")
    rc, _, err = new(
        root,
        "--offline",
        "--license",
        "mit",
        "--cli-tag",
        "v1.0",
        "--cli-commit",
        cli_sha,
        "--toolkit-url",
        TK_URL,
        "--toolkit-commit",
        tk_sha,
    )
    assert rc == 0, err
    toml = read(os.path.join(root, "game.toml"))
    toml = toml.replace('branch = "', 'tag = "v2.0"\nbranch = "', 1)
    toml += '\n[public]\nurl = "%s"\nauthor = "%s"\n' % (URL, AUTHOR)
    write(os.path.join(root, "game.toml"), toml)
    # What `finish` writes online.
    write(os.path.join(root, "uv.lock"), "version = 1\n")
    write(os.path.join(root, "config", "setup-pins.json"), "{}\n")
    write(os.path.join(root, "TASKS.md"), "private: the bench is %s\n" % HOSTNAME)
    write(os.path.join(root, "docs", "notes.md"), "Runs on %s under Proton.\n" % HOSTNAME)
    write(
        os.path.join(root, "CHANGELOG.md"),
        read(os.path.join(root, "CHANGELOG.md")) + "\n## [1.0.0] - 2026-10-18\n\n- First.\n",
    )
    git(root, "init", "-q")
    bare = os.path.join(d, "pub.git")
    git(d, "init", "-q", "--bare", bare)
    for real, alias in ((bare, URL), (cli_bare, pins.CLI_URL), (tk_bare, TK_URL)):
        git(root, "config", "--add", "url.file://%s.insteadOf" % real, alias)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "private work on " + HOSTNAME)
    pd = os.path.join(d, "public-inputs")
    write(
        os.path.join(pd, "audit-private"),
        "# made up\n%s\n%s\n" % (HOSTNAME, re.escape(PRIVATE_EMAIL)),
    )
    write(os.path.join(pd, "rewrite"), "# made up\n%s\tthe bench host\n" % HOSTNAME)
    monkeypatch.setenv("XBR_PUBLIC_DIR", pd)
    monkeypatch.delenv("XBR_AUDIT_PRIVATE", raising=False)
    return {"root": root, "bare": bare, "pd": pd}


def xbr(root, *args, capsys=None):
    rc = main.main(["--game", root, *args])
    out = capsys.readouterr() if capsys else None
    return rc, out


def tree_files(repo, rev):
    return git(repo, "ls-tree", "-r", "--name-only", rev).stdout.split()


def test_rules():
    r = public.Rules(
        "# a comment\n\n"
        "(?i)\\bbench(box)\\b\tthe \\1 host\n"
        "!blob a\\nb\tab\n"
        "^(an?) (parable)\t\\1 \\u\\2\n"
        "!fail secret\n"
    )
    assert r.apply("Bench and benchbox\r\n") == "Bench and the box host\r\n"
    assert r.apply("x a\nb y\n") == "x ab y\n"
    assert r.apply("a parable\n") == "a Parable\n"
    assert [s for s, _ in r.fails] == ["secret"]
    with pytest.raises(public.CliError, match="rewrite:1"):
        public.Rules("no tab here\n")
    # ASCII classes, as perl's bytes: \w does not take a non-ASCII letter.
    assert public.Rules("\\w+\tX\n").apply("é1\n") == "éX\n"


def test_changelog_section():
    text = "# C\n\n## [Unreleased]\n\n## [1.0.0] - 2026-10-18\n\n- One.\n\n## [0.9.0]\n- Old.\n"
    assert public.changelog_section(text, "v1.0.0") == "- One."
    assert public.changelog_section(text, "v2") == ""


def test_round_trip(pub, capsys):
    root, bare = pub["root"], pub["bare"]
    rc, out = xbr(root, "public", "init", capsys=capsys)
    assert rc == 0, out
    hook = os.path.join(root, ".git", "hooks", "pre-push")
    assert layout.HOOK_MARK in read(hook) and os.access(hook, os.X_OK)
    assert git(root, "config", "remote.public.tagOpt").stdout.strip() == "--no-tags"
    manifest.use(manifest.load(root))
    assert layout.report(manifest.current())["problems"] == []

    rc, out = xbr(root, "public", "export", "--tag", "v1.0.0", capsys=capsys)
    assert rc == 0, out.out + out.err
    assert "excluded 1:\n    TASKS.md" in out.out and "rewritten 1:\n    docs/notes.md" in out.out
    push = [x.strip() for x in out.out.splitlines() if x.strip().startswith("git push")]
    assert push == [
        "git push public refs/public/main:refs/heads/main refs/public-tags/v1.0.0:refs/tags/v1.0.0"
    ]
    files = tree_files(root, "refs/public/main")
    assert "TASKS.md" not in files and "game.toml" in files
    assert git(root, "show", "refs/public/main:docs/notes.md").stdout == (
        "Runs on the bench host under Proton.\n"
    )
    who = git(root, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", "refs/public/main").stdout
    today = datetime.date.today().isoformat()
    assert who.strip() == "%s|%s|g static recompilation: %s round" % (AUTHOR, AUTHOR, today)
    tag = git(root, "cat-file", "tag", "refs/public-tags/v1.0.0").stdout
    assert "tagger " + AUTHOR in tag and "- First." in tag
    mp = os.path.join(pub["pd"], "g.map")
    pub_sha = git(root, "rev-parse", "refs/public/main").stdout.strip()
    assert read(mp).split()[:2] == [pub_sha, git(root, "rev-parse", "HEAD").stdout.strip()]

    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0 and "clean" in out.out, out.out + out.err

    # The guard: the printed push goes; the private branch does not.
    r = git(root, "push", "-q", "public", "HEAD:refs/heads/main", check=False)
    assert r.returncode and "public guard" in r.stderr
    r = git(root, "push", "-q", "public", "refs/public/main:refs/heads/side", check=False)
    assert r.returncode and "only refs/heads/main" in r.stderr
    r = git(root, *push[0].split()[1:])
    assert git(bare, "rev-parse", "main").stdout.strip() == pub_sha
    assert git(bare, "cat-file", "-t", "v1.0.0").stdout.strip() == "tag"
    r = git(root, "push", "-q", "public", ":refs/heads/main", check=False)
    assert r.returncode and "a delete" in r.stderr

    # Nothing new, nothing to publish; a change is one commit on the tip.
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0 and "nothing to publish" in out.out
    write(os.path.join(root, "docs", "more.md"), "More.\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "more")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err
    assert git(root, "rev-parse", "refs/public/main^").stdout.strip() == pub_sha
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0, out.out + out.err


def commit_file(root, rel, text):
    write(os.path.join(root, rel), text)
    git(root, "add", "-f", rel)
    git(root, "commit", "-qm", "add " + rel)


def test_export_refuses_a_leak_and_moves_no_ref(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    # The rewrite does not cover the address: the pattern catches it.
    commit_file(root, "docs/who.md", "Mail %s.\n" % PRIVATE_EMAIL)
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and "docs/who.md:1:" in out.out and "no ref moved" in out.out
    assert git(root, "rev-parse", "-q", "--verify", "refs/public/main", check=False).returncode


@pytest.mark.parametrize(
    "rel,data,why",
    [
        ("media/intro.xmv", "x", "game data"),
        ("docs/images/shot.png", None, "tEXt"),
        ("runs/frame.txt", "x", "runs/"),
        ("build-win/x.txt", "x", "build tree"),
    ],
)
def test_export_refuses_data(pub, capsys, rel, data, why):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    if data is None:
        # A PNG with a text chunk (ImageMagick writes date:* ones).
        chunk = b"tEXt" + b"date:create\x002026"
        data_b = layout.PNG_SIG + len(chunk[4:]).to_bytes(4, "big") + chunk + b"\0\0\0\0"
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "wb") as f:
            f.write(data_b)
        git(root, "add", "-f", rel)
        git(root, "commit", "-qm", "img")
    else:
        commit_file(root, rel, data)
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and rel in out.out and why in out.out, out.out


def test_a_fail_rule_refuses(pub, capsys):
    root = pub["root"]
    with open(os.path.join(pub["pd"], "rewrite"), "a") as f:
        f.write("!fail \\bproject-x\\b\n")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    commit_file(root, "docs/x.md", "Project-X notes.\n")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and "a !fail check" in out.out


def test_no_patterns_is_exit_2(pub, capsys, monkeypatch):
    os.remove(os.path.join(pub["pd"], "audit-private"))
    rc, out = xbr(pub["root"], "public", "audit", "--all", capsys=capsys)
    assert rc == 2 and "no private patterns" in out.err


def test_public_dir_inside_the_tree_is_refused(pub, capsys, monkeypatch):
    monkeypatch.setenv("XBR_PUBLIC_DIR", os.path.join(pub["root"], "docs"))
    rc, out = xbr(pub["root"], "public", "audit", "--all", capsys=capsys)
    assert rc == 2 and "inside the game's work tree" in out.err


def test_audit_all_and_pins(pub, capsys):
    root = pub["root"]
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 0, out.out + out.err
    # A pin whose tag is not on its URL: a public clone could not set up.
    toml = read(os.path.join(root, "game.toml")).replace('tag = "v2.0"', 'tag = "v9.9"')
    write(os.path.join(root, "game.toml"), toml)
    git(root, "commit", "-qam", "re-pin")
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 1 and "[toolkit] tag v9.9 is not on" in out.out


def test_history_mode_checks_every_commit_and_identity(pub, capsys):
    root = pub["root"]
    toml = read(os.path.join(root, "game.toml")).replace(
        'author = "%s"' % AUTHOR, 'author = "%s"\nmode = "history"' % AUTHOR
    )
    write(os.path.join(root, "game.toml"), toml)
    git(root, "rm", "-q", "TASKS.md")
    git(root, "commit", "-qam", "history mode")
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    # The private identity and the host name in a message, and TASKS.md in
    # the first commit.
    assert rc == 1
    assert "author Private Person" in out.out and "message " in out.out
    assert "TASKS.md" in out.out


def test_init_renames_a_second_remote_and_chains_a_hook(pub, capsys):
    root = pub["root"]
    git(root, "remote", "add", "origin-pub", "https://github.example/SomeOwner/my-game")
    hooks = os.path.join(root, ".git", "hooks")
    write(os.path.join(hooks, "pre-push"), "#!/bin/sh\necho chained >&2\nexit 0\n")
    os.chmod(os.path.join(hooks, "pre-push"), 0o755)
    rc, out = xbr(root, "public", "init", capsys=capsys)
    assert rc == 0, out.out + out.err
    assert git(root, "remote").stdout.split() == ["public"]
    assert os.path.isfile(os.path.join(hooks, "pre-push.chained"))
    # Run again: our own hook is rewritten, the chained one kept.
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert "chained" in read(os.path.join(hooks, "pre-push.chained"))
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    r = git(root, "push", "public", "refs/public/main:refs/heads/main")
    assert "chained" in r.stderr


def test_init_warns_on_hooks_path(pub, capsys):
    git(pub["root"], "config", "core.hooksPath", ".githooks")
    rc, out = xbr(pub["root"], "public", "init", capsys=capsys)
    assert rc == 1 and "core.hooksPath" in out.out


def test_hook_text_has_no_placeholders(pub):
    manifest.use(manifest.load(pub["root"]))
    ctx = public.Ctx(manifest.current(), need_patterns=False)
    text = public.hook_text(ctx)
    assert "@" not in text.replace("$@", "") and "someowner/my-game" in text
    r = subprocess.run(["sh", "-n", "-c", text])
    assert r.returncode == 0
    assert TK not in text


def as_author(name_email):
    name, email = name_email[:-1].split(" <")
    return dict(
        os.environ,
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
    )


def parents(root, rev):
    return git(root, "rev-list", "--parents", "-n1", rev).stdout.split()[1:]


def test_upstream_second_parent_and_a_merge_with_a_leak(pub, capsys):
    """A fork: the upstream commit REF has merged becomes the export's
    second parent, one REF has not merged never does, and the audit reads
    a merge's diff against its first parent (git log -p shows none)."""
    root = pub["root"]
    up_env = as_author("Up Stream <up@example.invalid>")
    tree = git(root, "mktree", input="").stdout.strip()
    blob = git(root, "hash-object", "-w", "--stdin", input="upstream\n").stdout.strip()
    tree = git(root, "mktree", input="100644 blob %s\tUPSTREAM.md\n" % blob).stdout.strip()
    u1 = git(root, "commit-tree", tree, "-m", "upstream one", env=up_env).stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", u1)
    toml = read(os.path.join(root, "game.toml")).replace(
        'author = "%s"' % AUTHOR, 'author = "%s"\nupstream = "origin/main"' % AUTHOR
    )
    write(os.path.join(root, "game.toml"), toml)
    git(root, "commit", "-qam", "fork")
    git(root, "merge", "-q", "--allow-unrelated-histories", "-m", "merge upstream", u1)
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err
    assert parents(root, "refs/public/main") == [u1]
    assert "UPSTREAM.md" in tree_files(root, "refs/public/main")
    git(root, "push", "-q", "public", "refs/public/main:refs/heads/main")
    git(root, "fetch", "-q", "public")
    tip = git(root, "rev-parse", "refs/public/main").stdout.strip()

    # Upstream moves on; REF has not merged it: one parent.
    u2 = git(root, "commit-tree", tree, "-p", u1, "-m", "upstream two", env=up_env).stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", u2)
    commit_file(root, "docs/more.md", "More.\n")
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    assert parents(root, "refs/public/main") == [tip]
    # REF merges it: the second parent again.
    git(root, "merge", "-q", "-m", "merge upstream two", u2)
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    assert parents(root, "refs/public/main")[1:] == [u2]

    # A merge on the public tip that adds a private line: caught.
    leak = git(root, "hash-object", "-w", "--stdin", input="on %s\n" % HOSTNAME).stdout.strip()
    ls = git(root, "ls-tree", "refs/public/main^{tree}").stdout
    ls += "100644 blob %s\tLEAK.md\n" % leak
    bad_tree = git(root, "mktree", input=ls).stdout.strip()
    env = as_author(AUTHOR)
    m = git(root, "commit-tree", bad_tree, "-p", tip, "-p", u2, "-m", "round", env=env)
    git(root, "update-ref", "refs/public/main", m.stdout.strip())
    assert git(root, "log", "-p", "-1", "--format=", "refs/public/main").stdout.strip() == ""
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "LEAK.md:1:" in out.out, out.out


def test_a_binary_blob_is_not_rewritten(pub, capsys):
    root = pub["root"]
    toml = read(os.path.join(root, "game.toml")).replace(
        'author = "%s"' % AUTHOR, 'author = "%s"\nallow = ["docs/*.bin"]' % AUTHOR
    )
    write(os.path.join(root, "game.toml"), toml)
    with open(os.path.join(pub["pd"], "rewrite"), "a", encoding="utf-8") as f:
        f.write("widget\tgadget\n")
    data = b"\0a widget\n"
    with open(os.path.join(root, "docs", "x.bin"), "wb") as f:
        f.write(data)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "a binary")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err
    got = subprocess.run(
        ["git", "-C", root, "cat-file", "blob", "refs/public/main:docs/x.bin"], capture_output=True
    ).stdout
    assert got == data
    # Not rewritten, but still read: the audit diffs with --text, so an
    # allowed binary holding a private name is a hit.
    with open(os.path.join(root, "docs", "x.bin"), "wb") as f:
        f.write(b"\0" + HOSTNAME.encode() + b"\n")
    git(root, "commit", "-qam", "a leaky binary")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and "docs/x.bin:1:" in out.out, out.out


def test_tags_need_the_author_and_a_clean_message(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0, out.out

    def public_tag(name, who, msg):
        env = as_author(who)
        git(root, "tag", "-a", name, "-m", msg, "refs/public/main", env=env)
        sha = git(root, "rev-parse", "refs/tags/" + name).stdout.strip()
        git(root, "update-ref", "refs/public-tags/" + name, sha)
        git(root, "update-ref", "-d", "refs/tags/" + name)

    public_tag("v1", "Some Body <body@example.invalid>", "One.")
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "tag v1: tagger Some Body" in out.out, out.out
    git(root, "update-ref", "-d", "refs/public-tags/v1")
    public_tag("v2", AUTHOR, "Built on %s." % HOSTNAME)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "tag v2:1:" in out.out, out.out


def test_the_guard_refuses_other_urls_raw_shas_and_rewinds(pub, capsys):
    root, bare = pub["root"], pub["bare"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    first = git(root, "rev-parse", "refs/public/main").stdout.strip()
    # The https form of the same repo, not the remote's name: still guarded.
    https = "https://github.example/SomeOwner/My-Game"
    git(root, "config", "--add", "url.file://%s.insteadOf" % bare, https)
    r = git(root, "push", "-q", https, "HEAD:refs/heads/main", check=False)
    assert r.returncode and "public guard" in r.stderr
    # A raw sha that is not the export's commit.
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    r = git(root, "push", "-q", https, "%s:refs/heads/main" % head, check=False)
    assert r.returncode and "public guard" in r.stderr
    git(root, "push", "-q", "public", "refs/public/main:refs/heads/main")
    # A tip that does not descend from the public one: not a fast-forward.
    env = as_author(AUTHOR)
    other = git(root, "commit-tree", "refs/public/main^{tree}", "-m", "x", env=env).stdout.strip()
    git(root, "update-ref", "refs/public/main", other)
    r = git(
        root, "push", "-q", "--force", "public", "refs/public/main:refs/heads/main", check=False
    )
    assert r.returncode and "not a fast-forward" in r.stderr
    assert git(bare, "rev-parse", "main").stdout.strip() == first


def test_a_pin_tag_naming_another_commit(pub, capsys):
    root = pub["root"]
    toml = read(os.path.join(root, "game.toml"))
    m = re.search(r'^commit = "([0-9a-f]{40})"', toml.split("[toolkit]", 1)[1], re.M)
    write(os.path.join(root, "game.toml"), toml.replace(m.group(1), "1" * 40))
    git(root, "commit", "-qam", "re-pin")
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 1 and "not the pinned commit 111111111111" in out.out, out.out


def test_doctor_json_keeps_layout_apart(pub, capsys, monkeypatch):
    from xboxrecomp_cli import doctor

    os.remove(os.path.join(pub["root"], "LICENSE"))
    manifest.use(manifest.load(pub["root"]))
    monkeypatch.setattr(doctor, "PROBLEMS", [])
    rep = json.loads(doctor.report_json(["next: x"], [], {}))
    assert any("missing LICENSE" in p for p in rep["layout"]["problems"])
    assert rep["problems"] == [] and rep["next"] == "x"
    assert "LICENSE" not in json.dumps(rep["blockers"])


# ── review regressions ───────────────────────────────────────────────────

# A home-directory path, one of the built-in checks, in pieces.
HOME_LINE = "see /Us" + "ers/someone/x\n"


def history_mode(root):
    toml = read(os.path.join(root, "game.toml")).replace(
        'author = "%s"' % AUTHOR, 'author = "%s"\nmode = "history"' % AUTHOR
    )
    write(os.path.join(root, "game.toml"), toml)
    git(root, "rm", "-q", "TASKS.md")
    git(root, "commit", "-qam", "history mode")


def test_a_quoted_path_is_still_read(pub, capsys):
    """git C-quotes a name with a quote, a backslash or a control
    character; its added lines must still be checked, and a header that is
    not understood is a hit of its own."""
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    commit_file(root, 'docs/a"b.md', HOME_LINE)
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and 'docs/a"b.md:1:' in out.out, out.out
    # The audit reads it from the diff, whose header quotes the name.
    history_mode(root)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and 'docs/a"b.md:1:' in out.out, out.out
    assert public.unquote_c('"b/a\\"b\\\\c\\tc\\303\\251"') == 'b/a"b\\c\tcé'
    assert public.unquote_c("b/plain") == "b/plain"

    class FakeGit:
        def run(self, *args):
            diff = "diff --git x y\n+++ ???\n@@ -0,0 +1 @@\n+" + HOME_LINE
            return subprocess.CompletedProcess(args, 0, diff.encode(), b"")

    got = public.added_lines(FakeGit(), "a", "b")
    assert list(got) == [public.UNKNOWN_PATH]


@pytest.mark.parametrize("where", ["info", "global"])
def test_a_diff_attribute_hides_nothing(pub, capsys, monkeypatch, where):
    root = pub["root"]
    history_mode(root)
    if where == "info":
        write(os.path.join(root, ".git", "info", "attributes"), "*.md -diff\n")
    else:
        attrs = os.path.join(pub["pd"], "..", "attrs")
        write(attrs, "*.md -diff\n")
        cfg = os.path.join(pub["pd"], "..", "gitconfig")
        write(cfg, "[core]\n\tattributesFile = %s\n" % attrs)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", cfg)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "docs/notes.md:1:" in out.out, out.out


def test_text_that_is_not_utf8_is_refused(pub, capsys):
    root = pub["root"]
    with open(os.path.join(pub["pd"], "rewrite"), "a") as f:
        f.write("!fail \\bproject-x\\b\n")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    with open(os.path.join(root, "docs", "l1.md"), "wb") as f:
        f.write("caf\xe9 Project-X\n".encode("latin-1"))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "l1")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and "refused docs/l1.md: text that is not UTF-8" in out.out, out.out


def test_export_takes_head_only(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    commit_file(root, "docs/more.md", "More.\n")
    rc, out = xbr(root, "public", "export", "HEAD~1", capsys=capsys)
    assert rc == 1 and "is not HEAD" in out.err, out.err


def test_an_unchecked_layout_is_exit_2(pub, capsys, monkeypatch):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0

    def broken(_game, *_strict):
        raise OSError("unreadable")

    monkeypatch.setattr(layout, "_report", broken)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 2 and "layout not checked: unreadable" in out.err, out.err
    monkeypatch.undo()
    monkeypatch.setenv("XBR_PUBLIC_DIR", pub["pd"])
    # A clean tip that is neither HEAD nor an export of it: the working
    # tree's layout does not speak for it.
    env = as_author(AUTHOR)
    tip = git(root, "rev-parse", "refs/public/main").stdout.strip()
    other = git(root, "commit-tree", tip + "^{tree}", "-p", tip, "-m", "x", env=env)
    git(root, "update-ref", "refs/public/main", other.stdout.strip())
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 2 and "neither HEAD nor an export of it" in out.err, out.err


def test_the_rewrite_file_is_named_and_required(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    path = os.path.join(os.path.realpath(pub["pd"]), "rewrite")
    assert rc == 0 and "rewrite: %s (1 rules, 0 checks)" % path in out.out, out.out
    os.remove(path)
    commit_file(root, "docs/more.md", "More.\n")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 2 and "does not exist" in out.err, out.err


def test_a_checkout_without_a_rewrite_file_refuses(pub, capsys, monkeypatch):
    """A main checkout run without XBR_PUBLIC_DIR finds the patterns in
    .git/info but no rewrite file: it must not export or pass an audit with
    no rewrites and no !fail checks."""
    root = pub["root"]
    with open(os.path.join(pub["pd"], "rewrite"), "a") as f:
        f.write("!fail \\bsession session\\b\n")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    info = os.path.join(root, ".git", "info")
    write(os.path.join(info, "audit-private"), read(os.path.join(pub["pd"], "audit-private")))
    monkeypatch.delenv("XBR_PUBLIC_DIR")
    commit_file(root, "docs/more.md", "More.\n")
    for args in (("export",), ("audit", "--all"), ("audit",)):
        rc, out = xbr(root, "public", *args, capsys=capsys)
        assert rc == 2 and "XBR_PUBLIC_DIR" in out.err, (args, out.err)
    # The checkout's own rewrite file serves as well.
    write(os.path.join(info, "xbr-public", "rewrite"), read(os.path.join(pub["pd"], "rewrite")))
    assert xbr(root, "public", "audit", "--all", capsys=capsys)[0] == 0
    # The range audit runs the rewrite file's checks over what is pushed.
    tip = git(root, "rev-parse", "refs/public/main").stdout.strip()
    blob = git(root, "hash-object", "-w", "--stdin", input="the game session session\n")
    entries = git(root, "ls-tree", tip).stdout + "100644 blob %s\textra.md\n" % blob.stdout.strip()
    tree = git(root, "mktree", input=entries).stdout.strip()
    bad = git(root, "commit-tree", tree, "-p", tip, "-m", "x", env=as_author(AUTHOR))
    git(root, "update-ref", "refs/public/main", bad.stdout.strip())
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "(a !fail check)" in out.out + out.err, out.out + out.err


def test_history_mode_checks_a_merge(pub, capsys):
    """A merge whose tree adds a line neither parent has: only its diff
    against the first parent shows it."""
    root = pub["root"]
    history_mode(root)
    env = as_author(AUTHOR)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    side = git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD~1", "-m", "side", env=env)
    blob = git(root, "hash-object", "-w", "--stdin", input=HOME_LINE).stdout.strip()
    ls = git(root, "ls-tree", "HEAD^{tree}").stdout + "100644 blob %s\tEVIL.md\n" % blob
    tree = git(root, "mktree", input=ls).stdout.strip()
    m = git(root, "commit-tree", tree, "-p", head, "-p", side.stdout.strip(), "-m", "m", env=env)
    msha = m.stdout.strip()
    hits = public.history_hits(public.Ctx(manifest.load(root)), [msha, "^" + head])
    assert any(h.startswith("commit %s: EVIL.md:1:" % msha[:12]) for h in hits), hits


def test_the_private_set_in_an_export_tip(pub, capsys):
    root = pub["root"]
    ctx = public.Ctx(manifest.load(root))
    hits = public.tree_hits(ctx, "HEAD")
    assert any(h.startswith("TASKS.md is in the private set") for h in hits), hits


def test_rules_check_their_groups():
    for bad, why in (
        ("(a)\t\\2\n", "\\\\2, but the regex has 1 group"),
        ("a\t\\g<x\n", "\\\\g needs"),
        ("(?P<n>a)\t\\g<m>\n", "names no group"),
        ("a\t\\g<1>\n", "names no group"),
    ):
        with pytest.raises(public.CliError, match="rewrite:1: .*" + why):
            public.Rules(bad)
    public.Rules("(?P<n>a)(b)\t\\g<n>\\2\\\\3\n")


def test_line_numbers_count_newlines_only():
    hits = public.line_hits([("x", re.compile("x"))], "f", "a\x0bb\nx\n")
    assert hits == ["f:2: /x/: x"]


def test_patterns_that_are_not_utf8_are_exit_2(pub, capsys):
    with open(os.path.join(pub["pd"], "audit-private"), "wb") as f:
        f.write(b"caf\xe9\n")
    rc, out = xbr(pub["root"], "public", "audit", "--all", capsys=capsys)
    assert rc == 2 and "is not UTF-8" in out.err, out.err


def test_the_guard_refuses_on_a_git_before_2_31(pub, capsys, d):
    """An older git echoes rev-parse's --path-format back instead of a
    folder: the guard refuses the push rather than guess."""
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", capsys=capsys)[0] == 0
    hook = os.path.join(root, git(root, "rev-parse", "--git-path", "hooks/pre-push").stdout.strip())
    tip = git(root, "rev-parse", "refs/public/main").stdout.strip()
    line = "refs/public/main %s refs/heads/main %s\n" % (tip, "0" * 40)
    url = git(root, "remote", "get-url", "public").stdout.strip()

    def run(path):
        env = dict(os.environ, PATH=path)
        return subprocess.run(
            ["sh", hook, "public", url],
            cwd=root,
            input=line,
            capture_output=True,
            text=True,
            env=env,
        )

    r = run(os.environ["PATH"])
    assert r.returncode == 0, r.stderr
    fake = os.path.join(d, "oldgit")
    os.makedirs(fake)
    write(
        os.path.join(fake, "git"),
        '#!/bin/sh\nfor a; do [ "$a" = --path-format=absolute ] && echo "$a"; done\n'
        'for a; do shift; [ "$a" = --path-format=absolute ] || set -- "$@" "$a"; done\n'
        'exec %s "$@"\n' % shutil.which("git"),
    )
    os.chmod(os.path.join(fake, "git"), 0o755)
    r = run(fake + os.pathsep + os.environ["PATH"])
    assert r.returncode == 1 and "git 2.31 or newer" in r.stderr, r.stderr


def test_the_guard_wants_the_same_tag_name(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    assert xbr(root, "public", "export", "--tag", "v1.0.0", capsys=capsys)[0] == 0
    git(root, "push", "-q", "public", "refs/public/main:refs/heads/main")
    r = git(root, "push", "-q", "public", "refs/public-tags/v1.0.0:refs/tags/v9", check=False)
    assert r.returncode and "not refs/public-tags/v9" in r.stderr, r.stderr
    git(root, "push", "-q", "public", "refs/public-tags/v1.0.0:refs/tags/v1.0.0")


def test_a_signing_config_does_not_sign(pub, capsys):
    root = pub["root"]
    git(root, "config", "commit.gpgSign", "true")
    git(root, "config", "gpg.program", "false")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err


# The cases of Burnout 3's own audit script, which `public audit` replaces
# there: each must still be caught.


def commit_bytes(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    git(root, "add", "-f", "--", rel)
    git(root, "commit", "-qm", "add file")


def png(*chunks):
    body = b"".join(len(d).to_bytes(4, "big") + t + d + b"\0\0\0\0" for t, d in chunks)
    return layout.PNG_SIG + body + (0).to_bytes(4, "big") + b"IEND" + b"\0\0\0\0"


@pytest.mark.parametrize(
    "rel,data",
    [
        ("a.py", "p = '/Us" + "ers/someone/x'\n"),
        ("a.py", "p = '/ho" + "me/someone/x'\n"),
        ("a.py", "p = '/var/ho" + "me/someone'\n"),
        ("a.md", "see C:\\Us" + "ers\\someone\\x\n"),
        ("a.md", "see https://clau" + "de.ai/chat/x\n"),
        # The rewrite rule keeps its case; the pattern matches without.
        ("a.md", "the %s host\n" % HOSTNAME.upper()),
        ("docs/FRAME.BMP", b"BM"),
        ("data/track.Rws", b"x"),
        ("docs/fr\u00e1me.bmp", b"BM"),
        ("docs/shots/x.png", png()),
        ("docs/images/b.png", png((b"eXIf", b"MM\0*"))),
        ("docs/images/c.png", b"not a png"),
        ("docs/images/clip.gif", b"GIF89a\0\1"),
        ("blob.bin", b"\0\1\2\0" * 64),
    ],
)
def test_export_refuses_what_the_fork_audit_did(pub, capsys, rel, data):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    if isinstance(data, str):
        commit_file(root, rel, data)
    else:
        commit_bytes(root, rel, data)
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 1 and rel in out.out, out.out + out.err
    assert git(root, "rev-parse", "-q", "--verify", "refs/public/main", check=False).returncode


def fork_with_screenshot(root):
    """An upstream commit with docs/screenshots/race.png, merged, and
    [public] upstream and upstream_files set for it."""
    up_env = as_author("Up Stream <up@example.invalid>")
    blob = git(root, "hash-object", "-w", "--stdin", input="\x89PNG\0up").stdout.strip()
    sub = git(root, "mktree", input="100644 blob %s\trace.png\n" % blob).stdout.strip()
    docs = git(root, "mktree", input="040000 tree %s\tscreenshots\n" % sub).stdout.strip()
    tree = git(root, "mktree", input="040000 tree %s\tdocs\n" % docs).stdout.strip()
    u1 = git(root, "commit-tree", tree, "-m", "upstream", env=up_env).stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", u1)
    toml = read(os.path.join(root, "game.toml")).replace(
        'author = "%s"' % AUTHOR,
        'author = "%s"\nupstream = "origin/main"\nupstream_files = ["docs/screenshots/*"]' % AUTHOR,
    )
    write(os.path.join(root, "game.toml"), toml)
    git(root, "commit", "-qam", "fork")
    git(root, "merge", "-q", "--allow-unrelated-histories", "-m", "merge upstream", u1)
    return u1


def test_upstream_files_only_while_equal(pub, capsys):
    root = pub["root"]
    fork_with_screenshot(root)
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 0, out.out + out.err
    commit_bytes(root, "docs/screenshots/race.png", b"\x89PNG\0ours")
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 1 and "docs/screenshots/race.png" in out.out, out.out
    git(root, "checkout", "-q", "origin/main", "--", "docs/screenshots/race.png")
    git(root, "commit", "-qm", "upstream's again")
    commit_bytes(root, "docs/screenshots/new.png", b"\x89PNG\0new")
    rc, out = xbr(root, "public", "audit", "--all", capsys=capsys)
    assert rc == 1 and "docs/screenshots/new.png" in out.out, out.out
    assert "race.png" not in out.out


def test_history_mode_skips_upstream_and_reads_the_whole_range(pub, capsys):
    """Upstream's commits keep their own identity; a name that is not the
    author's is a hit even with its address; a screenshot changed and put
    back inside the range was still pushed."""
    root = pub["root"]
    fork_with_screenshot(root)
    history_mode(root)
    # Make the private history public as it stands, so the range starts here.
    bare = pub["bare"]
    git(root, "push", "-q", bare, "HEAD:refs/heads/main")
    git(root, "fetch", "-q", bare, "main:refs/remotes/public/main")
    env = as_author(AUTHOR)
    # Upstream moves on and is merged: its commit is new to the range.
    u1 = git(root, "rev-parse", "origin/main").stdout.strip()
    tree = git(root, "rev-parse", u1 + "^{tree}").stdout.strip()
    up_env = as_author("Up Stream <up@example.invalid>")
    u2 = git(root, "commit-tree", tree, "-p", u1, "-m", "upstream two", env=up_env).stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", u2)
    git(root, "merge", "-q", "-m", "merge upstream two", u2, env=env)
    assert "up@example.invalid" in git(root, "log", "--format=%ae", "public/main..").stdout
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0, out.out + out.err
    name, email = AUTHOR[:-1].split(" <")
    other = as_author("Someone Else <%s>" % email)
    write(os.path.join(root, "a.txt"), "ok\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-qm", "c", env=other)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "Someone Else" in out.out, out.out
    git(root, "reset", "-q", "--hard", "HEAD~1")
    path = os.path.join(root, "docs", "screenshots", "race.png")
    with open(path, "wb") as f:
        f.write(b"\x89PNG\0ours")
    git(root, "commit", "-qam", "ours", env=env)
    git(root, "checkout", "-q", "origin/main", "--", "docs/screenshots/race.png")
    git(root, "commit", "-qm", "upstream's again", env=env)
    write(os.path.join(root, "b.txt"), "ok\n")
    git(root, "add", "b.txt")
    git(root, "commit", "-qm", "from /Us" + "ers/me", env=env)
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1, out.out + out.err
    assert "docs/screenshots/race.png" in out.out and "message " in out.out, out.out


# ── a fresh root, PR branches, home paths ────────────────────────────────


def printed_push(out):
    return [x.strip() for x in out.splitlines() if x.strip().startswith("git push")]


def test_fresh_root_replaces_the_history_once(pub, capsys):
    """An approved rewrite: the new root has no public parent, the guard
    passes exactly the printed push (and the tag deletes it lists), and
    the next plain export takes the allowance away."""
    root, bare = pub["root"], pub["bare"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", "--tag", "v1.0.0", capsys=capsys)
    assert rc == 0, out.err
    git(root, *printed_push(out.out)[0].split()[1:])
    first = git(bare, "rev-parse", "main").stdout.strip()
    tag_obj = git(bare, "rev-parse", "refs/tags/v1.0.0").stdout.strip()
    git(root, "fetch", "-q", "public")
    commit_file(root, "docs/more.md", "More.\n")

    for args, why in (
        (("--drop-tags",), "goes with --fresh-root"),
        (("--fresh-root", "abcdef0"), "name it"),
        (("--fresh-root", first[:6]), "name it"),
    ):
        rc, out = xbr(root, "public", "export", *args, capsys=capsys)
        assert rc == 1 and why in out.err, (args, out.err)
    rc, out = xbr(root, "public", "export", "--fresh-root", first[:8], capsys=capsys)
    assert rc == 0 and "1 public tag still reaches the old history" in out.out, out.out
    rc, out = xbr(root, "public", "export", "--fresh-root", first[:8], "--drop-tags", capsys=capsys)
    assert rc == 0, out.err
    new = git(root, "rev-parse", "refs/public/main").stdout.strip()
    assert parents(root, new) == []
    assert not git(
        root, "rev-parse", "-q", "--verify", "refs/public-tags/v1.0.0", check=False
    ).stdout
    allowance = os.path.join(root, ".git", public.ALLOWANCE)
    assert read(allowance) == "branch main %s %s\ntag v1.0.0 %s\n" % (first, new, tag_obj)
    push = printed_push(out.out)
    assert push == [
        "git push --atomic --force-with-lease=refs/heads/main:%s public"
        " refs/public/main:refs/heads/main :refs/tags/v1.0.0" % first
    ]
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0 and "the approved fresh root" in out.out, out.out + out.err

    # Another rewrite is not the approved one, and a plain force is refused.
    env = as_author(AUTHOR)
    other = git(root, "commit-tree", new + "^{tree}", "-m", "x", env=env).stdout.strip()
    git(root, "update-ref", "refs/public/main", other)
    r = git(
        root, "push", "-q", "--force", "public", "refs/public/main:refs/heads/main", check=False
    )
    assert r.returncode and "not a fast-forward" in r.stderr
    # Nor does the audit read it whole: a stale or rewound tip is refused.
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 1 and "fetch the remote and export again" in out.err, out.out + out.err
    git(root, "update-ref", "refs/public/main", new)
    git(root, *push[0].split()[1:])
    assert git(bare, "rev-parse", "main").stdout.strip() == new
    assert not git(bare, "tag").stdout.strip()

    # Once landed it allows nothing more; the next plain export drops it.
    git(root, "fetch", "-q", "public")
    commit_file(root, "docs/again.md", "Again.\n")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0 and "dropped the rewrite allowance" in out.out, out.out
    assert not os.path.exists(allowance) and parents(root, "refs/public/main") == [new]


def test_nothing_to_publish_drops_the_allowance(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    git(root, *printed_push(out.out)[0].split()[1:])
    git(root, "fetch", "-q", "public")
    allowance = os.path.join(root, ".git", public.ALLOWANCE)
    write(allowance, "branch main 1 2\n")
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0 and "nothing to publish" in out.out, out.out
    assert not os.path.exists(allowance)


def test_fresh_root_needs_a_public_tip(pub, capsys):
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", "--fresh-root", "abcdef0", capsys=capsys)
    assert rc == 1 and "the public branch is empty" in out.err, out.err


def test_the_rules_load_before_the_fetch(pub, capsys):
    """No init, so no remote: a missing rewrite file is still what refuses."""
    os.remove(os.path.join(pub["pd"], "rewrite"))
    rc, out = xbr(pub["root"], "public", "export", capsys=capsys)
    assert rc == 2 and "rewrite" in out.err and "no remote" not in out.err, out.err


def pr_branch(root, d, base):
    """A worktree d/prwt on branch prb at base, for commits on top of upstream."""
    wt = os.path.join(d, "prwt")
    git(root, "worktree", "add", "-q", "-b", "prb", wt, base)
    return wt


def test_pr_branches(pub, capsys, d):
    root, bare = pub["root"], pub["bare"]
    rc, out = xbr(root, "public", "pr", "fix", capsys=capsys)
    assert rc == 1 and "upstream is not set" in out.err, out.err
    u1 = fork_with_screenshot(root)
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "pr", "fix", "origin/main", capsys=capsys)
    assert rc == 1 and "no commits on top" in out.err, out.err
    rc, out = xbr(root, "public", "pr", "..bad", capsys=capsys)
    assert rc == 1 and "not a branch name" in out.err, out.err

    wt = pr_branch(root, d, u1)
    env = as_author(AUTHOR)
    write(os.path.join(wt, "fix.c"), "int fix;\n")
    git(wt, "add", "fix.c")
    git(wt, "commit", "-qm", "fix: one thing")  # the private identity
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "author Private Person" in out.out, out.out
    assert not git(root, "rev-parse", "-q", "--verify", "refs/public-pr/fix", check=False).stdout
    git(wt, "commit", "-q", "--amend", "--reset-author", "-m", "fix: one thing", env=env)
    write(os.path.join(wt, "TASKS.md"), "the plan\n")
    git(wt, "add", "TASKS.md")
    git(wt, "commit", "-qm", "plan", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "TASKS.md is in the private set" in out.out, out.out
    git(wt, "reset", "-q", "--hard", "HEAD~1")
    write(os.path.join(wt, "fix.c"), "int fix; /* " + HOME_LINE.strip() + " */\n")
    git(wt, "commit", "-qam", "more", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "fix.c" in out.out, out.out
    git(wt, "reset", "-q", "--hard", "HEAD~1")

    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 0 and "1 commit on origin/main" in out.out, out.out + out.err
    tip = git(wt, "rev-parse", "HEAD").stdout.strip()
    assert git(root, "rev-parse", "refs/public-pr/fix").stdout.strip() == tip
    push = printed_push(out.out)
    assert push == ["git push --force-with-lease public refs/public-pr/fix:refs/heads/pr/fix"]
    assert (
        "close it: git push public :refs/heads/pr/fix && git update-ref -d refs/public-pr/fix"
        in out.out
    )
    # Only the audited commit reaches pr/fix, and not under another name.
    r = git(root, "push", "-q", "--force", "public", "prb~1:refs/heads/pr/fix", check=False)
    assert r.returncode and "only refs/public-pr/fix" in r.stderr
    r = git(root, "push", "-q", "public", "refs/public-pr/fix:refs/heads/pr/other", check=False)
    assert r.returncode and "only refs/public-pr/other" in r.stderr
    git(root, *push[0].split()[1:])
    assert git(bare, "rev-parse", "pr/fix").stdout.strip() == tip
    # A rebased PR: any old tip, once audited again.
    git(wt, "commit", "-q", "--amend", "-m", "fix: one thing, reworded", env=env)
    assert xbr(root, "public", "pr", "fix", "prb", capsys=capsys)[0] == 0
    git(root, "push", "-q", "--force", "public", "refs/public-pr/fix:refs/heads/pr/fix")
    # Closed: the branch goes; main still cannot.
    git(root, "push", "-q", "public", ":refs/heads/pr/fix")
    git(root, "update-ref", "-d", "refs/public-pr/fix")
    assert not git(bare, "branch", "--list", "pr/*").stdout.strip()
    r = git(root, "push", "-q", "public", ":refs/heads/main", check=False)
    assert r.returncode and "a delete" in r.stderr
    # A pr/ branch no `public pr` made is not ours to delete.
    git(bare, "branch", "pr/theirs", u1)
    r = git(root, "push", "-q", "public", ":refs/heads/pr/theirs", check=False)
    assert r.returncode and "a delete of a branch no" in r.stderr


def test_pr_runs_the_fail_checks_and_reads_each_commit(pub, capsys, d):
    """The rewrite file's !fail checks hold for messages and the branch
    name too, and a private-set file added and removed inside the range is
    still pushed in the commit that added it."""
    root = pub["root"]
    with open(os.path.join(pub["pd"], "rewrite"), "a") as f:
        f.write("!fail \\bsecretword\\b\n")
    u1 = fork_with_screenshot(root)
    wt = pr_branch(root, d, u1)
    env = as_author(AUTHOR)
    write(os.path.join(wt, "fix.c"), "int fix;\n")
    git(wt, "add", "fix.c")
    git(wt, "commit", "-qm", "fix (the secretword)", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "message " in out.out and "secretword" in out.out, out.out
    git(wt, "commit", "-q", "--amend", "-m", "fix", env=env)
    rc, out = xbr(root, "public", "pr", "secretword-fix", "prb", capsys=capsys)
    assert rc == 1 and "branch name" in out.out, out.out
    rc, out = xbr(root, "public", "pr", HOSTNAME + "-fix", "prb", capsys=capsys)
    assert rc == 1 and "branch name" in out.out, out.out
    write(os.path.join(wt, "TASKS.md"), "the plan\n")
    git(wt, "add", "TASKS.md")
    git(wt, "commit", "-qm", "plan", env=env)
    git(wt, "rm", "-q", "TASKS.md")
    git(wt, "commit", "-qm", "unplan", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "TASKS.md is in the private set" in out.out, out.out
    git(wt, "reset", "-q", "--hard", "HEAD~2")
    assert xbr(root, "public", "pr", "fix", "prb", capsys=capsys)[0] == 0
    # A path's name is pushed as its content is: the checks read it too.
    write(os.path.join(wt, "docs", "secretword-notes.md"), "clean\n")
    git(wt, "add", "docs")
    git(wt, "commit", "-qm", "notes", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "path docs/secretword-notes.md: " in out.out, out.out
    assert "(a !fail check)" in out.out, out.out


def test_pr_refuses_what_a_rewrite_rule_would_change(pub, capsys, d):
    """Export rewrites the names the rules hide; nothing rewrites a PR
    branch, so a line, path, message or name a rule would change stays
    out of it."""
    root = pub["root"]
    with open(os.path.join(pub["pd"], "rewrite"), "a") as f:
        f.write("\\bcodename\\b\tthe game\n")
    u1 = fork_with_screenshot(root)
    wt = pr_branch(root, d, u1)
    env = as_author(AUTHOR)
    write(os.path.join(wt, "fix.c"), "int fix; /* the codename build */\n")
    git(wt, "add", "fix.c")
    git(wt, "commit", "-qm", "fix", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "fix.c:1: " + public.REWRITTEN in out.out, out.out
    write(os.path.join(wt, "fix.c"), "int fix;\n")
    git(wt, "commit", "-qam", "fix the codename", "--amend", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "message: " + public.REWRITTEN in out.out, out.out
    git(wt, "commit", "-q", "--amend", "-m", "fix", env=env)
    rc, out = xbr(root, "public", "pr", "codename-fix", "prb", capsys=capsys)
    assert rc == 1 and "branch name pr/codename-fix: " + public.REWRITTEN in out.out, out.out
    git(wt, "mv", "fix.c", "codename.c")
    git(wt, "commit", "-q", "--amend", "-m", "fix", env=env)
    rc, out = xbr(root, "public", "pr", "fix", "prb", capsys=capsys)
    assert rc == 1 and "path codename.c: " + public.REWRITTEN in out.out, out.out
    git(wt, "mv", "codename.c", "fix.c")
    git(wt, "commit", "-q", "--amend", "-m", "fix", env=env)
    assert xbr(root, "public", "pr", "fix", "prb", capsys=capsys)[0] == 0


def test_home_paths_without_case(pub, capsys):
    """A Wine prefix's own drive_c/users/steamuser/ and
    drive_c/users/public/ are no one's home; every other home path is, in
    any case."""
    root = pub["root"]
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    commit_file(
        root,
        "docs/proton.md",
        "Saves: ~/.steam/steam/steamapps/compatdata/1/pfx/drive_c/us" + "ers/steamuser/x\n"
        "Shared: pfx/drive_c/Us" + "ers/Public/x\n",
    )
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err
    rc, out = xbr(root, "public", "audit", capsys=capsys)
    assert rc == 0, out.out + out.err
    for line in (
        HOME_LINE,
        "see /ho" + "me/me/x\n",
        "see /us" + "ers/me/x\n",
        "see /c/us" + "ers/me\n",
        "see c:\\us" + "ers\\me\\x\n",
        "see pfx/drive_c/us" + "ers/me/x\n",
    ):
        commit_file(root, "docs/bad.md", line)
        rc, out = xbr(root, "public", "export", capsys=capsys)
        assert rc == 1 and "docs/bad.md" in out.out, (line, out.out)


def test_golden_known_divergence_is_dropped_whole(pub, capsys):
    """golden.json's known_divergence entries name a private tracker item
    (golden check --host-os): the export drops them whole, leaving valid
    JSON with no such key and no item ref; the rest is as written."""
    root = pub["root"]
    toml = read(os.path.join(root, "game.toml"))
    golden = 'json = "analysis/golden/golden.json"\n'
    toml = (
        toml.replace("[golden]\n", "[golden]\n" + golden, 1)
        if "[golden]\n" in toml
        else toml + "\n[golden]\n" + golden
    )
    write(os.path.join(root, "game.toml"), toml)
    known = {"host": "windows", "item": "TRACK-42", "why": "menu dump timing"}
    g = {
        "about": "made up",
        "scenarios": {
            "s": {
                "frames": [
                    {"name": "menu", "dump": 58, "known_divergence": [known]},
                    {"name": "hub", "dump": 72},
                ]
            }
        },
    }
    rel = "analysis/golden/golden.json"
    write(os.path.join(root, rel), json.dumps(g, indent=2) + "\n")
    other = "docs/known.json"
    write(os.path.join(root, other), '{"known_divergence": 1}\n')
    git(root, "add", "-A")
    git(root, "add", "-f", rel, other)
    git(root, "commit", "-qm", "a known divergence")
    assert xbr(root, "public", "init", capsys=capsys)[0] == 0
    rc, out = xbr(root, "public", "export", capsys=capsys)
    assert rc == 0, out.out + out.err
    text = git(root, "show", "refs/public/main:" + rel).stdout
    assert "known_divergence" not in text and "TRACK-42" not in text, text
    frames = json.loads(text)["scenarios"]["s"]["frames"]
    assert frames == [{"name": "menu", "dump": 58}, {"name": "hub", "dump": 72}]
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
    # Only golden.json: another file keeps the key.
    assert git(root, "show", "refs/public/main:" + other).stdout == '{"known_divergence": 1}\n'
