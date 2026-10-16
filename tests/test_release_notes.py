"""scripts/release-notes.py, which prints one version's CHANGELOG.md section
as the body of its GitHub release, and the changelog itself: the version in
the package has a section, so a release commit cannot skip it.

  uv run pytest tests/test_release_notes.py
"""

import os
import re
import subprocess
import sys

import pytest

from xboxrecomp_cli import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "release-notes.py")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

SAMPLE = """# Changelog

Intro text.

## [Unreleased]

## [1.2.0] - 2026-01-02

### Added
- A new thing.

### Fixed
- An old thing.

## [1.1.0] - 2026-01-01

### Changed
- Something else.

[Unreleased]: https://example.invalid/compare/v1.2.0...HEAD
[1.2.0]: https://example.invalid/compare/v1.1.0...v1.2.0
[1.1.0]: https://example.invalid/releases/tag/v1.1.0
"""


def run(*args, changelog=None):
    argv = [sys.executable, SCRIPT, *args]
    if changelog is not None:
        argv += ["--changelog", str(changelog)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_prints_the_section_without_its_heading(sample):
    r = run("1.2.0", changelog=sample)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "### Added\n- A new thing.\n\n### Fixed\n- An old thing.\n"


def test_a_leading_v_is_the_same_version(sample):
    assert run("v1.2.0", changelog=sample).stdout == run("1.2.0", changelog=sample).stdout


def test_the_last_section_leaves_out_the_link_definitions(sample):
    r = run("1.1.0", changelog=sample)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "### Changed\n- Something else.\n"


def test_a_missing_version_fails_with_the_reason(sample):
    r = run("1.3.0", changelog=sample)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "no section for 1.3.0" in r.stderr


def test_a_prefix_is_not_a_match(sample):
    assert run("1.2", changelog=sample).returncode == 1


def test_an_empty_section_fails(sample):
    r = run("Unreleased", changelog=sample)
    assert r.returncode == 1
    assert "empty" in r.stderr


def test_a_missing_changelog_fails(tmp_path):
    r = run("1.2.0", changelog=tmp_path / "nope.md")
    assert r.returncode == 1
    assert "cannot read" in r.stderr


def test_the_package_version_has_a_changelog_section():
    r = run(__version__, changelog=CHANGELOG)
    assert r.returncode == 0, r.stderr


def test_every_section_has_a_compare_link():
    text = open(CHANGELOG, encoding="utf-8").read()
    versions = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    links = set(re.findall(r"^\[([^\]]+)\]: https://", text, re.M))
    assert versions[0] == "Unreleased"
    # Newest first; the newest section may be the release being prepared,
    # ahead of __version__ until its release commit.
    releases = [tuple(int(n) for n in v.split(".")) for v in versions[1:]]
    assert releases == sorted(releases, reverse=True)
    assert __version__ in versions[1:3]
    assert [v for v in versions if v not in links] == []
