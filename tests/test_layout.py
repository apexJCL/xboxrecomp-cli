"""The standard game repo layout (layout.py, docs/layout.md): what `xbr new`
writes passes it, the baseline test it writes agrees with the CLI's own
list, and each drift is a problem doctor reports without changing its
status. Made-up names only.

  uv run pytest tests/test_layout.py
"""

import os
import runpy
import shutil
import subprocess
import sys

import pytest
from test_scaffold import CLI, TK, game, new, tk  # noqa: F401 (fixtures)

from xboxrecomp_cli import layout, manifest, scaffold, workflow

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


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


@pytest.fixture
def g(d, tk, game):  # noqa: F811
    """A new game as `finish` leaves it online, in a git repo."""
    root = os.path.join(d, "g")
    rc, _, err = new(
        root, "--offline", "--license", "mit", "--cli-commit", CLI, "--toolkit-commit", TK
    )
    assert rc == 0, err
    write(os.path.join(root, "uv.lock"), "version = 1\n")
    write(os.path.join(root, "config", "setup-pins.json"), "{}\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "new")
    return manifest.use(manifest.load(root))


def problems(G):
    return layout.report(G)["problems"]


@needs_git
def test_a_new_game_is_standard(g):
    rep = layout.report(g)
    assert rep["problems"] == [], rep
    for rel in (
        "LICENSE",
        layout.GITIGNORE_TEST,
        layout.CHECKS_WORKFLOW,
        "openspec/config.yaml",
        "openspec/specs/.gitkeep",
        "openspec/changes/.gitkeep",
    ):
        assert os.path.isfile(g.path(rel)), rel
    text = open(g.path(".gitignore"), encoding="utf-8").read()
    assert text.rstrip().endswith(layout.GAME_LINES)
    py = open(g.path("pyproject.toml"), encoding="utf-8").read()
    assert 'testpaths = ["scripts"]' in py and "line-length = 100" in py
    assert "# [public]" in open(g.path("game.toml"), encoding="utf-8").read()


@needs_git
def test_the_baseline_test_runs_and_agrees(g):
    """The written scripts/test_gitignore.py passes in the new game, and its
    samples are the CLI's baseline_samples, for the default folders and for
    others."""
    r = subprocess.run([sys.executable, g.path(layout.GITIGNORE_TEST)], capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr
    mod = runpy.run_path(g.path(layout.GITIGNORE_TEST))
    text = open(g.path("game.toml"), encoding="utf-8").read()
    assert mod["samples"](mod["manifest_values"](text)) == layout.baseline_samples(g)
    assert mod["DATA_EXT"] == layout.DATA_EXT and mod["TRACKED"] == layout.NOT_IGNORED
    other = text.replace('game_files = "game_files"', 'game_files = "dump"').replace(
        'gen = "src/recomp/gen"', 'gen = "gen"'
    )
    write(g.path("game.toml"), other)
    G2 = manifest.load(g.root)
    assert mod["samples"](mod["manifest_values"](other)) == layout.baseline_samples(G2)


@needs_git
def test_drift_is_reported(g):
    os.remove(g.path("LICENSE"))
    with open(g.path(".gitignore"), "a", encoding="utf-8") as f:
        f.write("!*.bak\n")
    write(g.path("runs", "frame_0100.ppm"), "P6")
    git(g.root, "add", "-f", "runs/frame_0100.ppm")
    os.makedirs(g.path("wt", "x"))
    git(g.path("wt", "x"), "init", "-q")
    got = "\n".join(problems(g))
    assert "missing LICENSE" in got
    assert "game.toml.bak is not ignored" in got
    assert "runs/frame_0100.ppm" in got
    assert "wt/x" in got


@needs_git
def test_public_needs_checks_and_openspec(g):
    with open(g.path("game.toml"), "a", encoding="utf-8") as f:
        f.write('\n[public]\nurl = "git@github.example:someone/g.git"\nauthor = "A <a@b.c>"\n')
    G = manifest.load(g.root)
    assert problems(G) == []
    shutil.rmtree(G.path("openspec"))
    os.remove(G.path(layout.CHECKS_WORKFLOW))
    got = "\n".join(problems(G))
    assert "missing .github/workflows/checks.yml" in got and "missing openspec/config.yaml" in got
    # A [wizard] repo that is not [public]'s repo.
    # (The wizard needs [package] to load; the check reads the table alone.)
    G.m["wizard"] = {"repo": "https://github.example/someone/other.git", "icon": ""}
    rep = layout.report(G)
    assert any("is not the repo of [public] url" in p for p in rep["problems"]), rep


@needs_git
def test_layout_main_and_extra(g):
    os.makedirs(g.path("src", "host"))
    os.rename(g.path("src", "main.c"), g.path("src", "host", "main.c"))
    assert "missing src/main.c (the main C source" in "\n".join(problems(g))
    with open(g.path("game.toml"), "a", encoding="utf-8") as f:
        f.write('\n[layout]\nmain = "src/host/main.c"\n')
    G = manifest.load(g.root)
    assert problems(G) == []
    write(G.path("tools", "x.py"), "")
    git(G.root, "add", "tools/x.py")
    assert any("tools is not a standard entry" in n for n in layout.report(G)["notes"])


def test_refused_paths():
    G = manifest.current()
    gen = G.m["pipeline"]["gen"]
    assert layout.refused_path(G, gen + "/recomp_0000.c")
    assert layout.refused_path(G, "game_files/default.xbe")
    assert layout.refused_path(G, "dist/x.zip") and not layout.refused_path(G, "docs/dist.md")
    assert layout.refused_path(G, "build-win/x") and not layout.refused_path(
        G, "openspec/changes/build-resources/design.md"
    )
    assert layout.refused_path(G, "a/runs/x") and layout.refused_path(G, "x/hud-pack.toml")
    assert layout.data_ext("A/B.ISO") == "iso" and layout.data_ext("README.md") == ""
    # Without case (a case-insensitive file system), and a tracked symlink
    # standing for a shared folder.
    assert layout.refused_path(G, "Runs/x") and layout.refused_path(G, "DIST/x.zip")
    assert layout.refused_path(G, "Build-Win/x") and layout.refused_path(G, "bench-logs")
    assert not layout.refused_path(G, "docs/Build.md")


def test_private_set():
    G = manifest.current()
    for p in ("TASKS.md", "RESUME.md", "openspec/changes/x/RESUME-2.md", "_local/n.md"):
        assert layout.private(G, p), p
    for p in ("README.md", "docs/TASKS.md.txt", "openspec/changes/x/design.md"):
        assert not layout.private(G, p), p


def test_png_chunks():
    plain = layout.PNG_SIG + (0).to_bytes(4, "big") + b"IEND" + b"\0" * 4
    assert layout.png_problem(plain) == ""
    text = layout.PNG_SIG + (3).to_bytes(4, "big") + b"tEXtabc" + b"\0" * 4
    assert "tEXt" in layout.png_problem(text)
    assert layout.png_problem(b"GIF89a") == "not a PNG"


@needs_git
def test_workflow_check(g, capsys):
    assert workflow.main(["--check"], g) == 0
    with open(g.path("game.toml"), "a", encoding="utf-8") as f:
        f.write('\n[public]\nurl = "git@github.example:someone/g.git"\nauthor = "A <a@b.c>"\n')
    G = manifest.load(g.root)
    with open(G.path(layout.CHECKS_WORKFLOW), "a", encoding="utf-8") as f:
        f.write("# drift\n")
    assert workflow.main(["--check"], G) == 1
    assert "+# drift" in capsys.readouterr().out
    assert any("checks.yml differs" in p for p in problems(G))


def test_checks_template_pins_actions_by_sha():
    text = open(
        os.path.join(scaffold.CLI_TEMPLATES, ".github", "workflows", "checks.yml"), encoding="utf-8"
    ).read()
    for line in text.splitlines():
        if "uses:" in line:
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40 and "# v" in line, line
    assert "UV_PYTHON:" in text and "contents: read" in text


@needs_git
def test_doctor_prints_the_block_and_keeps_its_status(g, monkeypatch, capsys):
    from xboxrecomp_cli import doctor, main

    os.remove(g.path("LICENSE"))
    monkeypatch.setattr(doctor, "doctor_report", lambda: (["x"], [], {}))
    monkeypatch.setattr(doctor, "PROBLEMS", [])
    rc = main.cmd_doctor(type("A", (), {"json": False})())
    out = capsys.readouterr().out
    assert rc == 0 and "layout:     1 problem" in out and "missing LICENSE" in out
