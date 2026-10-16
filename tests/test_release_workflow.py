"""The game release workflow that `xbr new` writes
(templates/new-game/.github/workflows/release-wizard.yml): its notes step
takes the tag's CHANGELOG.md section or stops the run, and its release
step drafts the release, attaches the assets, then publishes it, with a
-rc. tag as a pre-release; a release a maintainer made by hand keeps its
title, notes and state, and gets the assets only when it has none of them. Each step's `run:` block is taken from the
template and run by bash, the release step against a stand-in gh.

  uv run pytest tests/test_release_workflow.py
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from xboxrecomp_cli import scaffold

WORKFLOW = os.path.join(scaffold.CLI_TEMPLATES, *scaffold.WORKFLOW.split("/"))

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("python3"),
    reason="needs bash and python3",
)

CHANGELOG = """# Changelog

## [Unreleased]

## [2026.10.0-rc.2] - 2026-10-16

### Fixed
- A fix.

## [2026.10.0-rc.1] - 2026-10-15

### Added
- The first release.

[2026.10.0-rc.2]: https://example.invalid/compare/v2026.10.0-rc.1...v2026.10.0-rc.2
"""

# A stand-in gh: logs each call. `release view` fails with $GH_VIEW_ERROR
# when set, else as gh does with no release for the tag ("release not
# found") unless $GH_RELEASE_STATE is set; then it answers the --jq of the
# isDraft,author view with it ("true <login>") and the assets view with
# $GH_ASSETS (one name per line).
FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [ "$1 $2" = "release view" ]; then
  if [ -n "${GH_VIEW_ERROR:-}" ]; then echo "$GH_VIEW_ERROR" >&2; exit 1; fi
  if [ -z "${GH_RELEASE_STATE:-}" ]; then echo "release not found" >&2; exit 1; fi
  case "$*" in
    *"--json isDraft,author"*) echo "$GH_RELEASE_STATE" ;;
    *"--json assets"*) printf '%s' "${GH_ASSETS:-}" ;;
  esac
fi
exit 0
"""
BOT = "github-actions[bot]"


def step_script(name):
    """The `run: |` block of the step called NAME, as bash reads it."""
    with open(WORKFLOW, encoding="utf-8") as f:
        lines = f.read().splitlines()
    (start,) = [i for i, ln in enumerate(lines) if ln.strip() == "- name: " + name]
    i = start + 1
    while lines[i].strip() != "run: |":
        i += 1
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for ln in lines[i + 1 :]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        body.append(ln)
    return textwrap.dedent("\n".join(body)) + "\n"


def run_notes(tmp_path, tag, changelog=CHANGELOG):
    if changelog is not None:
        (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    script = step_script("Take the tag's section of CHANGELOG.md")
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env=dict(os.environ, TAG=tag),
        capture_output=True,
        text=True,
        check=False,
    )


def test_notes_are_the_tags_section(tmp_path):
    r = run_notes(tmp_path, "v2026.10.0-rc.2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "### Fixed\n- A fix.\n"


def test_the_last_section_leaves_out_the_links(tmp_path):
    r = run_notes(tmp_path, "v2026.10.0-rc.1")
    assert r.returncode == 0, r.stdout + r.stderr
    notes = (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert notes == "### Added\n- The first release.\n"


@pytest.mark.parametrize("tag", ["v2026.10.0-rc.3", "v2026.10.0", "vUnreleased"])
def test_a_tag_without_notes_stops_the_run(tmp_path, tag):
    r = run_notes(tmp_path, tag)
    assert r.returncode != 0
    assert "::error file=CHANGELOG.md::" in r.stdout and tag in r.stdout
    assert not (tmp_path / "notes.md").exists()


def test_no_changelog_stops_the_run(tmp_path):
    r = run_notes(tmp_path, "v1.0.0", changelog=None)
    assert r.returncode != 0
    assert "::error::v1.0.0 has no CHANGELOG.md" in r.stdout


def run_release(tmp_path, tag, state="", assets="", out=None, view_error="", ok=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    env = dict(
        os.environ,
        TAG=tag,
        GITHUB_REPOSITORY="owner/game",
        GH_LOG=str(log),
        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
    )
    if view_error:
        env["GH_VIEW_ERROR"] = view_error
    if state:
        env["GH_RELEASE_STATE"] = state
        env["GH_ASSETS"] = assets
    for f in ("assets/a-macos.zip", "assets/a-linux-x64.tar.gz", "assets/SHA256SUMS"):
        (tmp_path / f).parent.mkdir(exist_ok=True)
        (tmp_path / f).write_text("x", encoding="utf-8")
    script = step_script("Draft the release, attach the assets, then publish it")
    r = subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if out is not None:
        out.append(r.stdout + r.stderr)
    return [ln.split(" --repo ")[0] for ln in log.read_text(encoding="utf-8").splitlines()]


def test_a_new_release_is_drafted_filled_then_published(tmp_path):
    calls = run_release(tmp_path, "v1.0.0")
    assert calls == [
        "release view v1.0.0",
        "release create v1.0.0 --draft --verify-tag --title v1.0.0"
        " --notes-file notes/notes.md --prerelease=false",
        "release upload v1.0.0 assets/a-macos.zip assets/a-linux-x64.tar.gz"
        " assets/SHA256SUMS --clobber",
        "release edit v1.0.0 --draft=false",
    ]


def test_an_rc_tag_is_a_prerelease(tmp_path):
    calls = run_release(tmp_path, "v2026.10.0-rc.2")
    assert "--prerelease=true" in calls[1]


def test_the_workflows_own_draft_takes_the_notes_and_is_published(tmp_path):
    """A draft an earlier run left (its author is the Actions bot) takes
    this run's title, notes and pre-release mark, then is published."""
    calls = run_release(tmp_path, "v2026.10.0-rc.1", state="true " + BOT)
    assert calls[1] == (
        "release edit v2026.10.0-rc.1 --title v2026.10.0-rc.1 --notes-file notes/notes.md"
        " --prerelease=true"
    )
    assert calls[2].endswith(" --clobber")
    assert calls[-1] == "release edit v2026.10.0-rc.1 --draft=false"
    assert not any(c.startswith("release create") for c in calls)


HAND_MADE = ["false someone", "true someone", "false " + BOT]
HAND_IDS = ["published-by-hand", "draft-by-hand", "published-by-an-earlier-run"]


@pytest.mark.parametrize("state", HAND_MADE, ids=HAND_IDS)
def test_a_hand_made_release_gets_the_assets_and_keeps_its_text(tmp_path, state):
    """A release this run did not draft (a maintainer's, or one already
    published) keeps its title, notes, pre-release mark and draft state:
    no edit, no create. With none of the files yet it gets all four,
    never clobbering."""
    out = []
    calls = run_release(tmp_path, "v1.0.0", state=state, out=out)
    assert not any(c.startswith(("release edit", "release create")) for c in calls), calls
    assert [c for c in calls if c.startswith("release upload")] == [
        "release upload v1.0.0 assets/a-macos.zip assets/a-linux-x64.tar.gz assets/SHA256SUMS"
    ]
    assert (
        "::notice::v1.0.0 is a release this run did not draft (published, or made by hand)"
        in out[0]
    )


@pytest.mark.parametrize("state", HAND_MADE, ids=HAND_IDS)
def test_a_hand_made_release_with_any_asset_gets_none(tmp_path, state):
    """All or nothing: one name already there and no file goes up, so
    SHA256SUMS never sits beside another build's files; the warning names
    each clash."""
    out = []
    names = "a-macos.zip\nSHA256SUMS\n"
    calls = run_release(tmp_path, "v1.0.0", state=state, assets=names, out=out)
    assert not any(
        c.startswith(("release upload", "release edit", "release create")) for c in calls
    )
    assert "::warning::v1.0.0 already has a-macos.zip SHA256SUMS; no asset uploaded" in out[0]


def test_a_failed_view_is_not_read_as_no_release(tmp_path):
    """gh failing for another reason than "release not found" (network,
    auth) stops the step: nothing is created, edited or uploaded."""
    out = []
    calls = run_release(tmp_path, "v1.0.0", view_error="HTTP 502: Bad Gateway", out=out, ok=False)
    assert calls == ["release view v1.0.0"]
    assert "HTTP 502" in out[0] and "::error::cannot read the release for v1.0.0" in out[0]


def test_only_the_release_job_can_write():
    """The workflow's token reads by default; contents: write is the
    release job's alone, and nothing asks for more."""
    with open(WORKFLOW, encoding="utf-8") as f:
        text = f.read()
    assert "permissions:\n  contents: read\n" in text
    assert text.count("contents: write") == 1
    assert text.index("contents: write") > text.index("\n  release:\n")
    perms = [ln.strip() for ln in text.splitlines() if ln.strip().endswith((": write", ": read"))]
    assert sorted(perms) == ["contents: read", "contents: write"]
