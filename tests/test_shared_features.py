"""The shared-feature check: a toolkit feature a game lacks fails visibly."""

import contextlib
import io
import os
import re

from test_layout import g, needs_git, write  # noqa: F401
from test_scaffold import game, tk  # noqa: F401

from xboxrecomp_cli import layout, main, manifest, shared_features

ROW = next(r for r in shared_features.SHARED if r["name"] == "present.shader")


def edit(path, fn):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    new = fn(text)
    assert new != text
    write(path, new)


def features(G):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main.main(["--game", G.root, "features"])
    return rc, out.getvalue()


@needs_git
def test_a_new_game_has_every_shared_feature(g):  # noqa: F811
    assert shared_features.problems(g) == []
    rc, out = features(g)
    assert rc == 0 and "present.shader: ok" in out, out


@needs_git
def test_a_missing_readme_row_fails_and_reaches_the_layout_report(g):  # noqa: F811
    edit(g.path("README.md"), lambda s: re.sub(r"^\|[^|\n]*\| Shaders \|.*\n", "", s, flags=re.M))
    rc, out = features(g)
    assert rc == 1 and "MISSING" in out and "Shaders row" in out, out
    assert any("present.shader: no Shaders row" in p for p in layout.report(g)["problems"])


@needs_git
def test_a_row_in_a_comment_is_not_a_row(g):  # noqa: F811
    edit(
        g.path("README.md"),
        lambda s: re.sub(r"^(\|[^|\n]*\| Shaders \|.*)\n", r"<!-- \1 -->\n", s, flags=re.M),
    )
    assert any("Shaders row" in p for p in shared_features.problems(g))


@needs_git
def test_the_config_row_must_sit_in_its_section(g):  # noqa: F811
    edit(g.path("packaging/config.default.toml"), lambda s: re.sub(r"(?m)^# shader =.*\n", "", s))
    assert any("no shader row under [present]" in p for p in shared_features.problems(g))
    write(g.path("packaging/config.default.toml"), '[other]\n# shader = "none"\n')
    assert any("no shader row under [present]" in p for p in shared_features.problems(g))


@needs_git
def test_an_older_toolkit_needs_the_games_call(g, tk):  # noqa: F811
    write(os.path.join(tk, "cmake", "librashader.cmake"), "# no scan yet\n")
    assert any("xboxrecomp_present_shaders(<exe>)" in p for p in shared_features.problems(g))
    # The first scan saw only an exe's own links: an umbrella-only game
    # still needed its call there.
    write(os.path.join(tk, "cmake", "librashader.cmake"), "_xboxrecomp_present_shaders_all\n")
    assert any("xboxrecomp_present_shaders(<exe>)" in p for p in shared_features.problems(g))
    write(g.path("src/game/CMakeLists.txt"), "# xboxrecomp_present_shaders(game)\n")
    assert shared_features.problems(g), "a commented call is no call"
    write(g.path("src/game/CMakeLists.txt"), "    xboxrecomp_present_shaders(game)\n")
    assert shared_features.problems(g) == []


@needs_git
def test_a_toolkit_without_the_code_is_a_gap(g, tk):  # noqa: F811
    os.remove(os.path.join(tk, *ROW["code"].split("/")))
    assert any("the toolkit has no" in p for p in shared_features.problems(g))


@needs_git
def test_no_toolkit_here_yet_tells_nothing_about_it(g, tk):  # noqa: F811
    absent = os.path.join(tk, "absent")
    assert shared_features.problems(g, tk=absent) == []
    lines, ok = shared_features.text_lines(g, tk=absent)
    assert ok and "present.shader: toolkit not found; staging not checked" in lines[0], lines


@needs_git
def test_a_build_without_the_layer_has_nothing_to_check(g, monkeypatch):  # noqa: F811
    monkeypatch.setattr(manifest, "builds_enhance", lambda m: False)
    edit(g.path("README.md"), lambda s: re.sub(r"(?m)^\|[^|\n]*\| Shaders \|.*\n", "", s))
    assert shared_features.report(g) == []
    rc, out = features(g)
    assert rc == 0 and "no shared features" in out, out
