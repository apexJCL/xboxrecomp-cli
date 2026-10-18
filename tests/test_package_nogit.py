"""package on a game folder with no commit to name: outside git,
and in a repo before its first commit. The version names a digest of the
tree's files, nothing of git's reaches the terminal, and a normal repo
keeps the version it had. Made-up names only.

  uv run pytest tests/test_package_nogit.py
"""

import os
import re
import shutil
import subprocess

import pytest
from test_scaffold import CLI, TK, game, new, tk  # noqa: F401 (fixtures)

from xboxrecomp_cli import cli_dir, host, layout, manifest
from xboxrecomp_cli.package import incr, patch
from xboxrecomp_cli.package import lib as pl

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def fresh(tree):
    """tree_state as a new run sees it: the digest is memoised per run."""
    pl.CONTENT_MEMO.clear()
    return pl.tree_state(tree)


def committed(d):
    """A small repo with one commit: the toolkit beside the game."""
    write(os.path.join(d, "tools", "a.py"), "x = 1\n")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "x")
    return d


@pytest.fixture
def plain(d, tk, game):  # noqa: F811
    """A new game as `xbr new` leaves it, never put in git, with the
    folders package fills beside it."""
    root = os.path.join(d, "g")
    rc, _, err = new(root, "--offline", "--cli-commit", CLI, "--toolkit-commit", TK)
    assert rc == 0, err
    G = manifest.load(root)
    for rel in ("game_files/default.xbe", "src/recomp/gen/recomp_0000.c", "build/x.o"):
        write(G.path(rel), "data")
    write(G.path(".xbr", "build-state.json"), "{}")
    write(G.path(".venv", "bin", "python"), "")
    write(G.path("external", "xboxrecomp", "x"), "")
    pl.CONTENT_SKIP[os.path.realpath(root)] = layout.content_skip(G)
    yield G
    pl.CONTENT_SKIP.pop(os.path.realpath(root), None)


def test_modes(d, plain):
    assert pl.git_mode(plain.root) == pl.NOGIT
    tree = committed(os.path.join(d, "tk"))
    assert pl.git_mode(tree) == ""
    git(plain.root, "init", "-q")
    assert pl.git_mode(plain.root) == pl.NOCOMMIT


def test_outside_git(plain, capfd):
    s = fresh(plain.root)
    assert s["commit"] == "none" and s["branch"] == "none" and s["mode"] == pl.NOGIT
    assert s["dirty"] and s["_dirt"] == s["content"] and len(s["content"]) == 64
    names = pl.walk_files(plain.root)
    assert "game.toml" in names and ".gitignore" in names
    # The dump, generated code, builds, records and the toolchain are not
    # the source.
    for rel in names:
        assert not rel.startswith(("game_files/", "src/recomp/gen/", "build/", ".xbr/")), rel
        assert not rel.startswith((".venv/", "external/")), rel
    assert s["dirty_paths"] == len(names)
    # A source edit moves the digest; a new build output does not.
    write(plain.path("build", "y.o"), "more")
    write(plain.path("game_files", "media", "x.xpr"), "more")
    assert fresh(plain.root)["content"] == s["content"]
    write(plain.path("src", "main.c"), "int main(void) { return 1; }\n")
    assert fresh(plain.root)["content"] != s["content"]
    assert capfd.readouterr().err == ""


def test_outside_git_reads_the_gitignore(plain):
    write(plain.path("notes", "scratch.log"), "x")
    before = fresh(plain.root)["content"]
    with open(plain.path(".gitignore"), "a", encoding="utf-8") as f:
        f.write("notes/\n")
    names = pl.walk_files(plain.root)
    assert "notes/scratch.log" not in names
    # The .gitignore itself changed, so the digest does too; the ignored
    # file no longer counts.
    after = fresh(plain.root)["content"]
    write(plain.path("notes", "scratch.log"), "y")
    assert fresh(plain.root)["content"] == after != before


def test_no_commits_yet(plain, capfd):
    git(plain.root, "init", "-q")
    s = fresh(plain.root)
    assert s["commit"] == "none" and s["mode"] == pl.NOCOMMIT and s["dirty"]
    # git's own list: what the first commit would hold.
    listed = git(plain.root, "ls-files", "--cached", "--others", "--exclude-standard")
    assert s["dirty_paths"] == len(listed.split())
    # The scaffold's .gitignore now holds .xbr/: an old record folder
    # does not count.
    write(plain.path(".xbr", "build-state.json"), '{"x": 1}')
    assert fresh(plain.root)["content"] == s["content"]
    write(plain.path("src", "main.c"), "int main(void) { return 2; }\n")
    assert fresh(plain.root)["content"] != s["content"]
    assert pl.commit_time(plain.root) is None
    assert pl.read_mark(plain.root) is None
    assert capfd.readouterr().err == ""


def test_a_normal_repo_is_unchanged(d):
    tree = committed(os.path.join(d, "tk"))
    s = pl.tree_state(tree)
    head = git(tree, "rev-parse", "HEAD").strip()
    assert s == {
        "commit": head,
        "branch": git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "dirty": False,
        "dirty_paths": 0,
        "_dirt": "",
    }
    assert pl.commit_label(s) == head[:7] and pl.source_label(s) == head[:7]


def test_labels_and_patch_names(d, plain):
    s = pl.tree_state(plain.root)
    label = pl.commit_label(s)
    assert label == "nogit" + s["content"][:6]
    assert pl.source_label(s) == label + " (no git repository)"
    tk_state = pl.tree_state(committed(os.path.join(d, "tk")))
    import datetime

    t = datetime.datetime(2026, 10, 18, 23, 10, tzinfo=datetime.UTC)
    v = pl.format_version(t, s, tk_state, "4e521f24" + "f" * 56)
    assert re.fullmatch(
        r"20261018\.2310-cnogit[0-9a-f]{6}-t[0-9a-f]{7}-g4e521f24-dirty[0-9a-f]{8}", v
    ), v
    assert patch.short(v) == "c%s-g4e521f24" % label
    git(plain.root, "init", "-q")
    s2 = pl.tree_state(plain.root)
    assert pl.source_label(s2) == "nocommit%s (no commits yet)" % s2["content"][:6]
    v2 = pl.format_version(t, s2, tk_state, "4e521f24" + "f" * 56)
    assert v2.startswith("20261018.2310-cnocommit") and "-dirty" in v2
    assert patch.short(v2) == "cnocommit%s-g4e521f24" % s2["content"][:6]


def test_compute_version_and_stage_inputs(d, plain, capfd):
    """The package path's git reads end to end without a build: the
    version, the stage's tree inputs and the manifest's sources."""
    tkd = committed(os.path.join(d, "tk"))
    gen = plain.path("src", "recomp", "gen")
    exe = plain.path("build", "g")
    write(exe, "exe")
    v1 = pl.compute_version(plain.root, tkd, gen, exe, tkd)
    assert re.search(r"-cnogit[0-9a-f]{6}-t", v1) and "-dirty" in v1, v1
    assert pl.compute_version(plain.root, tkd, gen, exe, tkd) == v1
    ti = incr.tree_input(plain.root, pl)
    assert ti == {"tree": "none", "dirt": pl.tree_state(plain.root)["content"]}
    git(plain.root, "init", "-q")
    v2 = pl.compute_version(plain.root, tkd, gen, exe, tkd)
    assert "-cnocommit" in v2 and v2 != v1
    assert incr.tree_input(plain.root, pl)["tree"] == "none"
    # The first commit: back to the sha and the clean version.
    git(plain.root, "add", "-A")
    git(plain.root, "commit", "-qm", "first")
    head = git(plain.root, "rev-parse", "HEAD").strip()
    v3 = pl.compute_version(plain.root, tkd, gen, exe, tkd)
    assert "-c%s-t" % head[:7] in v3 and "-dirty" not in v3, v3
    assert incr.tree_input(plain.root, pl) == {
        "tree": git(plain.root, "rev-parse", "HEAD^{tree}").strip(),
        "dirt": "",
    }
    assert capfd.readouterr().err == ""


def test_manifest_sources_name_the_content(d, plain):
    s = pl.tree_state(plain.root)
    tk_state = pl.tree_state(committed(os.path.join(d, "tk")))
    root = os.path.join(d, "payload")
    write(os.path.join(root, "x.txt"), "x")
    man = pl.build_manifest(root, pl.TARGETS[2], "v", s, tk_state, "0" * 64, 1, {}, "cc", [], None)
    src = man["sources"]
    assert src[pl.SOURCE_NAME]["commit"] == "none"
    assert src[pl.SOURCE_NAME]["content"] == s["content"]
    assert "content" not in src["toolkit"] and "mode" not in src[pl.SOURCE_NAME]
    assert man["source"] == {"kind": "dev", "commit": "none"}


def test_doctor_names_the_mode(plain):
    G = manifest.use(manifest.load(plain.root))
    notes = "\n".join(layout.report(G)["notes"])
    assert "not a git repository" in notes and "cnogit" in notes
    git(plain.root, "init", "-q")
    notes = "\n".join(layout.report(G)["notes"])
    assert "no commits yet" in notes and "cnocommit" in notes
    git(plain.root, "add", "-A")
    git(plain.root, "commit", "-qm", "first")
    assert "no commits yet" not in "\n".join(layout.report(G)["notes"])


def test_the_digest_is_memoised_per_run(plain):
    s = pl.tree_state(plain.root)
    write(plain.path("src", "main.c"), "int main(void) { return 3; }\n")
    assert pl.tree_state(plain.root)["content"] == s["content"]
    assert fresh(plain.root)["content"] != s["content"]


def test_a_game_inside_another_repo(d, plain):
    """A game folder inside a parent repo is not named by that repo's
    commit: the parent's HEAD says nothing of the game's files."""
    git(d, "init", "-q")
    write(os.path.join(d, "README"), "parent\n")
    git(d, "add", "README")
    git(d, "commit", "-qm", "parent")
    assert pl.git_mode(plain.root) == pl.NOGIT
    s = pl.tree_state(plain.root)
    assert s["commit"] == "none" and pl.commit_label(s).startswith("nogit")


def test_git_errors_surface(d, plain, monkeypatch):
    tree = committed(os.path.join(d, "tk"))
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    with pytest.raises(ValueError, match="dubious ownership"):
        pl.git_mode(tree)
    with pytest.raises(host.CliError, match="dubious ownership"):
        incr.tree_input(tree, pl)
    with pytest.raises(pl.GitError, match="dubious ownership"):
        pl.git(tree, "status")
    monkeypatch.delenv("GIT_TEST_ASSUME_DIFFERENT_OWNER")
    # A branch whose commit is gone is a broken repo, not one before its
    # first commit.
    head = git(tree, "rev-parse", "HEAD").strip()
    os.remove(os.path.join(tree, ".git", "objects", head[:2], head[2:]))
    with pytest.raises(ValueError, match="bad ref"):
        pl.tree_state(tree)
    gen = plain.path("src", "recomp", "gen")
    exe = plain.path("build", "g")
    write(exe, "exe")
    with pytest.raises(ValueError, match="bad ref"):
        pl.compute_version(plain.root, tree, gen, exe)


def test_symlinks(plain):
    os.symlink("src", plain.path("linked"))
    os.symlink("game.toml", plain.path("toml-link"))
    names = pl.walk_files(plain.root)
    # The folder link is an entry of its own, never walked into.
    assert "linked" in names and "toml-link" in names
    assert not any(n.startswith("linked/") for n in names)
    before = fresh(plain.root)["content"]
    os.remove(plain.path("linked"))
    os.symlink("docs", plain.path("linked"))
    assert fresh(plain.root)["content"] != before
    git(plain.root, "init", "-q")
    s = fresh(plain.root)
    os.remove(plain.path("toml-link"))
    os.symlink(".gitignore", plain.path("toml-link"))
    assert fresh(plain.root)["content"] != s["content"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_file(d, plain):
    p = plain.path("src", "secret.c")
    write(p, "x")
    os.chmod(p, 0)
    try:
        with pytest.raises(ValueError, match="secret.c"):
            pl.tree_state(plain.root)
        tkd = committed(os.path.join(d, "tk"))
        gen = plain.path("src", "recomp", "gen")
        exe = plain.path("build", "g")
        write(exe, "exe")
        with pytest.raises(ValueError, match="secret.c"):
            pl.compute_version(plain.root, tkd, gen, exe)
        with pytest.raises(host.CliError, match="secret.c"):
            incr.tree_input(plain.root, pl)
        git(plain.root, "init", "-q")
        with pytest.raises(host.CliError, match="secret.c"):
            incr.tree_input(plain.root, pl)
    finally:
        os.chmod(p, 0o644)


def test_nested_setup_names_are_source(plain):
    """external/, third_party/, dist/ and node_modules/ are skipped at the
    top only: a game tracks src/external/ and the like."""
    for rel in (
        "src/external/lib.c",
        "src/third_party/x.h",
        "tools/dist/a.py",
        "web/node_modules/m.js",
    ):
        write(plain.path(*rel.split("/")), "x")
    write(plain.path("src", "__pycache__", "a.pyc"), "x")
    names = pl.walk_files(plain.root)
    for rel in (
        "src/external/lib.c",
        "src/third_party/x.h",
        "tools/dist/a.py",
        "web/node_modules/m.js",
    ):
        assert rel in names, rel
    assert "src/__pycache__/a.pyc" not in names
    assert not any(n.startswith(("external/", ".venv/", ".xbr/")) for n in names)
    # The top-level node_modules/ is not in the scaffold's .gitignore: the
    # skip leaves it out of the no-commit list too.
    write(plain.path("node_modules", "m.js"), "x")
    assert "node_modules/m.js" not in pl.walk_files(plain.root)
    git(plain.root, "init", "-q")
    assert fresh(plain.root)["dirty_paths"] == len([n for n in pl.walk_files(plain.root)])


def test_default_skip_is_top_level():
    assert pl._default_skip("external", True) and pl._default_skip(".venv-x", True)
    assert pl._default_skip("a/b/.git", True) and pl._default_skip("a/__pycache__", True)
    assert not pl._default_skip("src/external", True)
    assert not pl._default_skip("src/dist", True)
    assert not pl._default_skip("a/.venv", True)


def test_source_dir_for_an_installed_layout(d, monkeypatch):
    pkg = os.path.dirname(os.path.abspath(cli_dir.__file__))
    monkeypatch.setattr(cli_dir, "cli_dir", lambda: d)
    assert cli_dir.source_dir() == pkg
    write(os.path.join(d, "pyproject.toml"), "")
    assert cli_dir.source_dir() == d
