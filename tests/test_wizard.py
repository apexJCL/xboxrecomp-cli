"""xbr wizard and what it relies on: the [wizard] table, the config the Go
binary embeds (read at a release tag), the build's command lines and
archives, pins refresh --wizard, doctor --json, package --seed-config and
the installers that copy the seed, and the workflow `new` writes.

  uv run pytest tests/test_wizard.py
"""

import json
import os
import shutil
import subprocess
import tempfile
import zipfile

import pytest

from xboxrecomp_cli import credits, doctor, manifest, pins, scaffold, wizard, wizard_build
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.host import CliError

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
SHA = "ab" * 32

WIZARD = """
[wizard]
repo = "https://github.com/someone/blinx2-recomp.git"
icon = "packaging/wizard-icon.png"

[[wizard.option]]
key = "hud_pack"
label = "HD HUD"
kind = "bool"
default = false
flag = "--hud-pack"

[[wizard.option]]
key = "scale"
label = "Render scale"
kind = "choice"
values = ["1", "2", "3"]
default = "1"
config = "render.scale"

[[wizard.option]]
key = "fullscreen"
label = "Fullscreen"
kind = "bool"
default = false
config = "present.fullscreen"
"""
DEFAULTS = "[render]\nscale = 1\n\n[present]\nfullscreen = false\n"


def base_text():
    with open(os.path.join(GAME, "game.toml")) as f:
        return f.read()


def git(root, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-C",
            root,
            *args,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


@pytest.fixture
def wgame(d, monkeypatch):
    """The test game with a [wizard] table, its icon and defaults file,
    committed and tagged v1.0.0; current while the test runs. Its toolkit
    pin has the enhancements layer (read with no network)."""
    monkeypatch.setattr(wizard.toolkit, "pin_has_enhance", lambda pin: True)
    root = os.path.join(d, "game")
    shutil.copytree(os.path.join(GAME, "config"), os.path.join(root, "config"))
    write(os.path.join(root, "game.toml"), base_text() + WIZARD)
    write(os.path.join(root, "packaging", "wizard-icon.png"), PNG)
    write(os.path.join(root, "packaging", "config.default.toml"), DEFAULTS)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "game")
    git(root, "tag", "v1.0.0")
    saved = manifest.current()
    G = manifest.load(root)
    manifest.use(G)
    try:
        yield G
    finally:
        manifest.use(saved)


# ── the [wizard] table ───────────────────────────────────────────────────


def test_manifest_wizard_defaults():
    m = manifest.parse_text(base_text() + WIZARD)
    w = m["wizard"]
    assert w["targets"] == m["package"]["targets"]  # derived
    assert w["fetch"] == []
    hud, scale, fs = w["option"]
    assert hud["flag"] == "--hud-pack" and hud["config"] == ""
    assert scale["values"] == ["1", "2", "3"] and fs["values"] == []
    assert "wizard" not in manifest.parse_text(base_text())


@pytest.mark.parametrize(
    "change, error",
    [
        (('repo = "https://github', 'repo = "http://github'), "wizard.repo"),
        (
            ('icon = "packaging/wizard-icon.png"', 'icon = "i.png"\ntargets = ["xbox"]'),
            "wizard.targets: 'xbox'",
        ),
        (('flag = "--hud-pack"', 'flag = "--hud-pack"\nconfig = "a.b"'), "exactly one"),
        (('default = "1"', 'default = "4"'), r"option\[1\].default: one of values"),
        (('flag = "--hud-pack"', 'flag = "hud"'), "a long option"),
        (('config = "render.scale"', 'config = "scale"'), "a dotted key"),
        (('key = "fullscreen"', 'key = "scale"'), "'scale' twice"),
        (('kind = "bool"\ndefault = false\nflag', 'kind = "int"\ndefault = false\nflag'), "kind"),
        (('label = "HD HUD"', 'label = "HD HUD"\ncolour = 1'), "unknown key"),
    ],
)
def test_manifest_wizard_refusals(change, error):
    old, new = change
    text = base_text() + WIZARD
    assert old in text
    with pytest.raises(manifest.ManifestError, match=error):
        manifest.parse_text(text.replace(old, new, 1))


@pytest.mark.parametrize(
    "fetch, error",
    [
        ('url = "http://x.invalid/a.zip"', "url: an https"),
        ('sha256 = "abc"', "not a sha256"),
        ('dest = "../out"', "a path inside the clone"),
        ('dest = ""', "a path inside the clone"),
        ('dest = "."', "a path inside the clone"),
        ('dest = ".."', "a path inside the clone"),
        ('dest = "a/./b"', "a path inside the clone"),
        ('dest = "a//b"', "a path inside the clone"),
        ('dest = "a/"', "a path inside the clone"),
        ('dest = "/abs"', "a path inside the clone"),
        ('dest = "~/x"', "a path inside the clone"),
        ("dest = 'a\\b'", "a path inside the clone"),
        ('hosts = ["dos"]', "'dos' is not one of"),
    ],
)
def test_manifest_wizard_fetch(fetch, error):
    entry = {
        "name": '"tool"',
        "url": '"https://x.invalid/a.zip"',
        "sha256": '"%s"' % SHA,
        "dest": '"third_party/tool"',
    }
    ok = base_text() + WIZARD + "\n[[wizard.fetch]]\n"
    ok += "".join("%s = %s\n" % kv for kv in entry.items())
    f = manifest.parse_text(ok)["wizard"]["fetch"][0]
    assert f["unpack"] is True and f["hosts"] == ["windows", "macos", "linux"]
    key = fetch.split(" ")[0]
    bad = base_text() + WIZARD + "\n[[wizard.fetch]]\n"
    bad += "".join("%s = %s\n" % kv for kv in entry.items() if kv[0] != key) + fetch + "\n"
    with pytest.raises(manifest.ManifestError, match=error):
        manifest.parse_text(bad)


def test_manifest_wizard_needs_package():
    text = base_text()
    text = text[: text.index("[package]")] + text[text.index("[bench]") :]
    with pytest.raises(manifest.ManifestError, match=r"needs a \[package\]"):
        manifest.parse_text(text + WIZARD)


RANGE = WIZARD.replace(
    'kind = "choice"\nvalues = ["1", "2", "3"]\ndefault = "1"',
    'hint = "Sharper on a big screen"\nkind = "range"\nmin = 1\nmax = 6\ndefault = 1\n'
    'labels = { "1" = "640x480", "6" = "about 4K" }',
)


def test_manifest_wizard_range():
    assert RANGE != WIZARD
    scale = manifest.parse_text(base_text() + RANGE)["wizard"]["option"][1]
    assert scale["kind"] == "range" and (scale["min"], scale["max"]) == (1, 6)
    assert "step" not in scale and manifest.range_values(scale) == [1, 2, 3, 4, 5, 6]
    assert scale["hint"] == "Sharper on a big screen"
    stepped = RANGE.replace("max = 6\ndefault = 1", "max = 7\nstep = 2\ndefault = 3")
    stepped = stepped.replace('"6" = "about 4K"', '"7" = "most"')
    assert manifest.range_values(
        manifest.parse_text(base_text() + stepped)["wizard"]["option"][1]
    ) == [1, 3, 5, 7]
    # The other kinds carry no range keys.
    hud = manifest.parse_text(base_text() + RANGE)["wizard"]["option"][0]
    assert not any(k in hud for k in manifest.RANGE_KEYS) and hud["hint"] == ""


@pytest.mark.parametrize(
    "old, new, error",
    [
        ("min = 1\nmax = 6", "min = 6\nmax = 1", r"max: above min"),
        ("min = 1\nmax = 6", "min = 3\nmax = 3", r"max: above min"),
        ("max = 6\n", "", r"max: required"),
        ("max = 6\n", "max = 6\nstep = 0\n", r"step: 1 or more"),
        ("max = 6\n", "max = 6\nstep = 2\n", r"step: 2 does not divide"),
        ("default = 1\nlabels", "default = 7\nlabels", r"default: a whole number"),
        ("default = 1\nlabels", "default = 0\nlabels", r"default: a whole number"),
        ("default = 1\nlabels", 'default = "1"\nlabels', r"default: a whole number"),
        ("default = 1\nlabels", "default = true\nlabels", r"default: a whole number"),
        ("max = 6\n", "max = 7\nstep = 2\n", r"default: a whole number|labels: '6'"),
        ('"6" = "about 4K"', '"9" = "too far"', r"labels: '9' is not a value"),
        ('"6" = "about 4K"', '"x" = "not a number"', r"labels: 'x' is not a value"),
        ('"6" = "about 4K"', '"06" = "padded"', r"labels: '06' is not a value"),
        ("min = 1\nmax = 6", "min = 0\nmax = 10001", r"max: max - min above 10000"),
        ("max = 6\n", "max = 6.5\n", r"max: must be a"),
        ('kind = "range"', 'kind = "range"\nvalues = ["1", "2"]', r"values: only for kind choice"),
    ],
)
def test_manifest_wizard_range_refusals(old, new, error):
    assert old in RANGE
    with pytest.raises(manifest.ManifestError, match=error):
        manifest.parse_text(base_text() + RANGE.replace(old, new, 1))


def test_manifest_wizard_range_keys_only_on_range():
    bad = WIZARD.replace('label = "Fullscreen"', 'label = "Fullscreen"\nmin = 0')
    with pytest.raises(manifest.ManifestError, match=r"option\[2\].min: only for kind range"):
        manifest.parse_text(base_text() + bad)


LINK = 'flag = "--hud-pack"\nhint_link = { text = "Get the font", url = "https://fonts.example/f" }'


def test_manifest_wizard_hint_link():
    text = WIZARD.replace('flag = "--hud-pack"', LINK, 1)
    hud = manifest.parse_text(base_text() + text)["wizard"]["option"][0]
    assert hud["hint_link"] == {"text": "Get the font", "url": "https://fonts.example/f"}
    assert "hint_link" not in manifest.parse_text(base_text() + WIZARD)["wizard"]["option"][0]


@pytest.mark.parametrize(
    "link, error",
    [
        ('{ text = "Get", url = "http://fonts.example/f" }', r"hint_link.url: an https"),
        ('{ text = "Get", url = "fonts.example/f" }', r"hint_link.url: an https"),
        ('{ text = "Get", url = "https:///f" }', r"hint_link.url: an https"),
        ('{ text = "Get", url = "https://a.example/a b" }', r"hint_link.url: an https"),
        ('{ text = " ", url = "https://fonts.example/f" }', r"hint_link.text: words"),
        ('{ url = "https://fonts.example/f" }', r"hint_link: exactly text and url"),
        ('{ text = "Get", url = "https://f.example", x = "y" }', r"exactly text and url"),
        ('{ text = "Get", url = 1 }', r"hint_link"),
        ('{ text = "Get", url = "https://[::1" }', r"hint_link.url: an https"),
        ('{ text = "Get", url = "https://a.example/\\u0000" }', r"hint_link.url: an https"),
        ('{ text = "Get", url = "https://a.example/\\u007f" }', r"hint_link.url: an https"),
    ],
)
def test_manifest_wizard_hint_link_refusals(link, error):
    text = WIZARD.replace('flag = "--hud-pack"', 'flag = "--hud-pack"\nhint_link = ' + link, 1)
    with pytest.raises(manifest.ManifestError, match=error):
        manifest.parse_text(base_text() + text)


FONT_HINT = 'flag = "--hud-pack"\nfont_hint = "It needs the free font."'
HUD_FONT = '\n[enhance.hud]\nfont_atlas = "a.dds"\nfont_rows = ["AB"]\nfont = "Some Sans Bold"\n'


def test_manifest_wizard_font_hint():
    text = WIZARD.replace('flag = "--hud-pack"', FONT_HINT, 1)
    hud = manifest.parse_text(base_text() + HUD_FONT + text)["wizard"]["option"][0]
    assert hud["font_hint"] == "It needs the free font."
    assert manifest.parse_text(base_text() + WIZARD)["wizard"]["option"][0]["font_hint"] == ""
    # The words wait for [enhance.hud] font: without it they would never go.
    with pytest.raises(
        manifest.ManifestError, match=r"option\[0\].font_hint: needs \[enhance.hud\] font"
    ):
        manifest.parse_text(base_text() + text)
    no_font = HUD_FONT.replace('font = "Some Sans Bold"\n', "")
    with pytest.raises(manifest.ManifestError, match=r"font_hint: needs"):
        manifest.parse_text(base_text() + no_font + text)


CREDIT = """
[[wizard.credit]]
name = "blinx2-recomp"
role = "this game's recompilation"
license = "MIT"
url = "https://github.com/someone/blinx2-recomp"
own = true

[[wizard.credit]]
name = "Some Sans"
role = "the HUD pack's font"
license = "OFL-1.1"
url = "https://fonts.example/some-sans"
"""


def test_manifest_wizard_credit():
    w = manifest.parse_text(base_text() + WIZARD + CREDIT)["wizard"]
    assert [c["name"] for c in w["credit"]] == ["blinx2-recomp", "Some Sans"]
    assert [c["own"] for c in w["credit"]] == [True, False]
    assert manifest.parse_text(base_text() + WIZARD)["wizard"]["credit"] == []


@pytest.mark.parametrize(
    "old, new, error",
    [
        ('license = "MIT"', 'license = "MIT or Apache"', r"credit\[0\].license: an SPDX"),
        ('license = "MIT"', 'license = " MIT"', r"credit\[0\].license: an SPDX"),
        ('url = "https://github.com/someone/blinx2-recomp"', 'url = "http://x.example/a"',
         r"credit\[0\].url: an https"),
        ('role = "this game\'s recompilation"', 'role = " "', r"credit\[0\].role: words"),
        ('name = "blinx2-recomp"', 'name = "x "', r"credit\[0\].name: words"),
        ('name = "blinx2-recomp"\n', "", r"credit\[0\].name: required"),
        ('license = "MIT"', 'license = "MIT"\nyear = 1', r"credit\[0\].year: unknown key"),
        ('license = "MIT"', "license = 1", r"credit\[0\].license: must be"),
        ("own = true", 'own = "yes"', r"credit\[0\].own: must be"),
        ('url = "https://fonts.example/some-sans"', 'url = "https://GitHub.com/someone/blinx2-recomp/"',
         r"credit\[1\].url: another entry has this address"),
    ],
)  # fmt: skip
def test_manifest_wizard_credit_refusals(old, new, error):
    text = CREDIT.replace(old, new, 1)
    with pytest.raises(manifest.ManifestError, match=error):
        manifest.parse_text(base_text() + WIZARD + text)


def test_credits_lists_pass_the_game_rules():
    """The CLI's own entries keep the rules a game's must: four fields,
    an SPDX licence, an https link, no url twice."""
    for i, c in enumerate(credits.OURS + credits.THIRD_PARTY):
        assert set(c) == {"name", "role", "license", "url"}
        assert manifest.credit_errors("credits[%d]" % i, c) == [], c
    urls = [c["url"] for c in credits.OURS + credits.THIRD_PARTY]
    assert len(set(urls)) == len(urls)
    assert all(u.startswith("https://github.com/") for u in urls if "nsis" not in u)


def test_credits_generic_order():
    """No game entries: the generic wizard, the CLI first, then the
    toolkit, its upstream and the third-party projects."""
    names = [c["name"] for c in credits.merged()]
    assert names[:3] == ["xboxrecomp-cli", "xboxrecomp", "xboxrecomp (upstream)"]
    assert names[3:7] == ["Go", "Fyne", "zenity", "Tabler Icons"]
    assert len(names) == len(credits.OURS) + len(credits.THIRD_PARTY)


def test_credits_game_order():
    """A game's wizard: our repos first (the game's own, marked own,
    wherever it is listed, then the CLI's), then the third-party projects
    (the game's other entries, then the CLI's). An entry the game repeats
    (by url) is listed once, the game's."""
    game = [
        credits._c("Some Sans", "the HUD font", "OFL-1.1", "https://fonts.example/some-sans"),
        dict(
            credits._c(
                "blinx2-recomp", "the game", "MIT", "https://github.com/someone/blinx2-recomp"
            ),
            own=True,
        ),
        credits._c("SDL 2", "its window", "Zlib", "https://github.com/libsdl-org/SDL/"),
    ]
    out = credits.merged(game)
    names = [c["name"] for c in out]
    assert names[:7] == [
        "blinx2-recomp",
        "xboxrecomp-cli",
        "xboxrecomp",
        "xboxrecomp (upstream)",
        "Some Sans",
        "SDL 2",
        "Go",
    ]
    assert "SDL" not in names and len(out) == len(credits.merged()) + 2
    assert all(set(c) == {"name", "role", "license", "url"} for c in out)  # own is not shown
    out[0]["name"] = "changed"
    assert credits.merged()[0]["name"] == "xboxrecomp-cli"  # copies, not the module's


def test_config_credits(wgame):
    cfg, _ = wizard.resolve(wgame)
    assert cfg["credits"] == credits.merged()
    write(os.path.join(wgame.root, "game.toml"), base_text() + WIZARD + CREDIT)
    cfg, _ = wizard.resolve(manifest.load(wgame.root), dev=True)
    names = [c["name"] for c in cfg["credits"]]
    assert names[:5] == [
        "blinx2-recomp",
        "xboxrecomp-cli",
        "xboxrecomp",
        "xboxrecomp (upstream)",
        "Some Sans",
    ]


def test_font_report(monkeypatch):
    from xboxrecomp_cli.enhance import font

    seen = []

    def find(name, dirs=None):
        seen.append(name)
        return "/fonts/some.ttf" if name == "Some Sans Bold" else None

    monkeypatch.setattr(font, "find", find)
    m = manifest.parse_text(base_text() + HUD_FONT + WIZARD)
    assert wizard.font_report(m) == {
        "font": "Some Sans Bold",
        "found": True,
        "path": "/fonts/some.ttf",
    }
    assert wizard.font_report(m, "Nope Sans") == {"font": "Nope Sans", "found": False, "path": None}
    assert seen == ["Some Sans Bold", "Nope Sans"]
    # No font named: nothing to look for.
    assert wizard.font_report(manifest.parse_text(base_text() + WIZARD)) == {
        "font": "",
        "found": False,
        "path": None,
    }
    assert len(seen) == 2


def test_font_report_real_lookup(d, monkeypatch):
    """The report goes through the build's own lookup: a face installed
    under the font folders by its full name, case and spaces aside."""
    import test_enhance

    from xboxrecomp_cli.enhance import font

    write(os.path.join(d, "fonts", "x.ttf"), test_enhance.ttf(name="Some Sans Bold"))
    monkeypatch.setattr(font, "font_dirs", lambda: [os.path.join(d, "fonts")])
    m = manifest.parse_text(base_text() + HUD_FONT + WIZARD)
    assert wizard.font_report(m)["path"] == os.path.join(d, "fonts", "x.ttf")
    assert wizard.font_report(m, "some  sans BOLD")["found"]
    assert not wizard.font_report(m, "Some Sans")["found"]


def test_wizard_font_json(wgame, capsys, monkeypatch):
    import argparse

    from xboxrecomp_cli.enhance import font

    monkeypatch.setattr(font, "find", lambda name, dirs=None: None)
    assert wizard.run(argparse.Namespace(action="font", json=True, name="Nope Sans")) == 0
    assert json.loads(capsys.readouterr().out) == {
        "font": "Nope Sans",
        "found": False,
        "path": None,
    }
    assert wizard.run(argparse.Namespace(action="font", json=False, name="")) == 0
    assert "names no font" in capsys.readouterr().out


def test_scaffold_wizard_example():
    """`new`'s commented [wizard] example, uncommented, is a valid table
    whose scale is a range."""
    from xboxrecomp_cli import scaffold

    text = scaffold.GAME_TOML
    block = text[text.index("# [wizard]\n") :].split('"""')[0]
    lines = [ln[2:] for ln in block.splitlines() if ln.startswith("# ")]
    example = "\n" + "\n".join(lines).replace("@SLUG@", "blinx2") + "\n"
    scale = manifest.parse_text(base_text() + example)["wizard"]["option"][0]
    assert scale["kind"] == "range" and (scale["min"], scale["max"], scale["default"]) == (1, 4, 1)


# ── wizard config ────────────────────────────────────────────────────────


def test_config_at_the_tag(wgame):
    cfg, icon = wizard.resolve(wgame)
    assert icon == PNG
    assert cfg["schema"] == wizard.CONFIG_SCHEMA
    assert cfg["game"] == {"name": "BLiNX 2", "slug": "blinx2", "app": "BLiNX2",
                           "product": "blinx2-recomp"}  # fmt: skip
    rel = cfg["release"]
    assert rel["tag"] == "v1.0.0" and rel["dev"] is False and len(rel["commit"]) == 40
    assert rel["repo"] == "https://github.com/someone/blinx2-recomp.git"
    assert cfg["targets"] == ["windows", "steamos", "macos"]
    hud, scale, fs = cfg["options"]
    assert "toml" not in hud
    assert scale["toml"] == {"1": "1", "2": "2", "3": "3"}
    assert fs["toml"] == {"true": "true", "false": "false"}
    # Stable bytes: the binary embeds them and the double build compares.
    assert wizard.config_json(cfg) == wizard.config_json(wizard.resolve(wgame)[0])


def test_config_without_enhance(wgame, monkeypatch):
    """A toolkit pin with no enhancements layer (upstream xboxrecomp) reads
    no config.toml: the config options are left out, with why, and flag
    options stay. The toolkit is asked at the tag's pin."""
    asked = []

    def has(pin):
        asked.append(pin)
        return False

    monkeypatch.setattr(wizard.toolkit, "pin_has_enhance", has)
    cfg, _ = wizard.resolve(wgame)
    assert asked == [wgame.m["toolkit"]]
    assert [o["key"] for o in cfg["options"]] == ["hud_pack"]
    assert [(o["key"], o["config"]) for o in cfg["omitted"]] == [
        ("scale", "render.scale"),
        ("fullscreen", "present.fullscreen"),
    ]
    assert all("no enhancements layer (src/enhance/)" in o["why"] for o in cfg["omitted"])
    assert '"omitted"' in wizard.config_json(cfg)
    # Flag options alone need no toolkit read; a toolkit with the layer
    # leaves the config as it was, with no omitted key.
    assert cfg["reads_config"] is False
    # Flag options alone and no data dir: nothing to ask the toolkit. With
    # a data dir the pin is read, as the page would name config.toml there.
    asked.clear()
    m = dict(wgame.m, data=dict(wgame.m["data"], windows="", steamos="", macos=""))
    assert wizard.reads_config(m, cfg["options"]) and asked == []
    assert not wizard.reads_config(wgame.m, cfg["options"]) and asked == [wgame.m["toolkit"]]
    assert wizard.without_enhance(cfg["options"], False, wgame.m["toolkit"]) == (cfg["options"], [])
    # A toolkit with the layer leaves the config as it was, with no
    # omitted key.
    monkeypatch.setattr(wizard.toolkit, "pin_has_enhance", lambda pin: True)
    cfg, _ = wizard.resolve(wgame)
    assert "omitted" not in cfg and len(cfg["options"]) == 3 and cfg["reads_config"] is True


def _toolkit_repo(d):
    """A toolkit repository: commit 'fork' has src/enhance/CMakeLists.txt,
    the next, 'upstream' (tagged v0.14.0), has none. (fork, upstream) shas."""
    tk = os.path.join(d, "tk-remote")
    write(os.path.join(tk, "tools", "recomp", "__main__.py"), "#\n")
    write(os.path.join(tk, "src", "enhance", "CMakeLists.txt"), "#\n")
    git(tk, "init", "-q")
    git(tk, "add", "-A")
    git(tk, "commit", "-qm", "fork")
    git(tk, "rm", "-rq", "src/enhance")
    git(tk, "commit", "-qm", "upstream")
    git(tk, "tag", "-a", "-m", "v0.14.0", "v0.14.0")
    # As GitHub serves it: without these the file:// fetch ignores
    # --filter and refuses a bare commit, and the blobless path is untested.
    git(tk, "config", "uploadpack.allowFilter", "true")
    git(tk, "config", "uploadpack.allowAnySHA1InWant", "true")

    def sha(rev):
        r = subprocess.run(["git", "-C", tk, "rev-parse", rev], capture_output=True, check=True)
        return r.stdout.decode().strip()

    return tk, sha("HEAD~1"), sha("HEAD")


def test_pin_has_enhance(wgame, d, monkeypatch, pin_stub):
    """The toolkit is read at the pin: from the local checkout's history when
    it has the commit (whatever its working tree), else by a blobless
    fetch of that one commit or tag; a tag that is not its lock is refused."""
    monkeypatch.undo()  # the real lookup, not the fixtures' stand-ins
    pin_stub.undo()
    from xboxrecomp_cli import toolkit

    remote, fork, upstream = _toolkit_repo(d)
    url = "file://" + remote
    # The local checkout has both commits; its working tree is upstream's.
    monkeypatch.setenv("XBOXRECOMP_DIR", remote)
    assert toolkit.pin_has_enhance({"url": url, "tag": "", "commit": fork})
    assert not toolkit.pin_has_enhance({"url": url, "tag": "", "commit": upstream})
    assert not toolkit.pin_has_enhance({"url": url, "tag": "v0.14.0", "commit": ""})
    # No local checkout: fetched from the url, into a scratch repository
    # that is gone after every call, passing or failing.
    monkeypatch.setenv("XBOXRECOMP_DIR", os.path.join(d, "no-toolkit"))
    tmp = os.path.join(d, "tmp")
    os.makedirs(tmp)
    monkeypatch.setattr(tempfile, "tempdir", tmp)
    assert toolkit.pin_has_enhance({"url": url, "tag": "", "commit": fork})
    assert not toolkit.pin_has_enhance({"url": url, "tag": "v0.14.0", "commit": upstream})
    with pytest.raises(CliError, match="tag v0.14.0 is .* not the pinned commit"):
        toolkit.pin_has_enhance({"url": url, "tag": "v0.14.0", "commit": fork})
    assert os.listdir(tmp) == []
    with pytest.raises(CliError, match="cannot read .* at v9"):
        toolkit.pin_has_enhance({"url": url, "tag": "v9", "commit": ""})
    assert os.listdir(tmp) == []


def test_pin_has_enhance_asks_once(wgame, d, monkeypatch, pin_stub):
    """The wizard and package ask the same question of the same pin: the
    toolkit is read once per pin (and toolkit dir) in a process."""
    monkeypatch.undo()
    pin_stub.undo()
    from xboxrecomp_cli import toolkit

    monkeypatch.setattr(toolkit, "_PIN_ENHANCE", {})
    monkeypatch.setenv("XBOXRECOMP_DIR", d)
    reads = []

    def pin_files(pin, rels):
        reads.append(pin["commit"])
        return {rel: pin["commit"] == "fork" for rel in rels}

    monkeypatch.setattr(toolkit, "pin_files", pin_files)
    fork = {"url": "u", "tag": "", "commit": "fork"}
    up = {"url": "u", "tag": "", "commit": "upstream"}
    assert toolkit.pin_has_enhance(fork) and toolkit.pin_has_enhance(dict(fork))
    assert not toolkit.pin_has_enhance(up) and not toolkit.pin_has_enhance(up)
    assert reads == ["fork", "upstream"]
    monkeypatch.setenv("XBOXRECOMP_DIR", os.path.join(d, "other"))
    assert toolkit.pin_has_enhance(fork) and reads == ["fork", "upstream", "fork"]


def test_config_reads_the_tag_not_the_tree(wgame):
    write(os.path.join(wgame.root, "packaging", "config.default.toml"), "[render]\nscale = 2\n")
    with pytest.raises(CliError, match="uncommitted changes"):
        wizard.resolve(wgame)
    cfg, _ = wizard.resolve(wgame, tag="v1.0.0")  # the tag's file, not the edit
    assert cfg["options"][1]["toml"]["1"] == "1"
    with pytest.raises(CliError, match=r"render.scale is 2"):
        wizard.resolve(wgame, dev=True)


def test_config_refusals(wgame):
    git(wgame.root, "commit", "-q", "--allow-empty", "-m", "after")
    with pytest.raises(CliError, match="HEAD has no tag"):
        wizard.resolve(wgame)
    with pytest.raises(CliError, match="no such tag"):
        wizard.resolve(wgame, tag="v9")
    with pytest.raises(CliError, match="not a plain tag"):
        wizard.resolve(wgame, tag="-x")
    git(wgame.root, "tag", "v1.0.1")
    git(wgame.root, "tag", "v1.0.2")
    with pytest.raises(CliError, match="several tags"):
        wizard.resolve(wgame)
    cfg, _ = wizard.resolve(wgame, dev=True)
    assert cfg["release"]["tag"] == "" and cfg["release"]["dev"] is True


def test_config_icon_optional_or_svg(wgame):
    # No icon: the config passes and the wizard's own icon applies.
    toml = os.path.join(wgame.root, "game.toml")
    with open(toml) as f:
        text = f.read()
    write(toml, text.replace('icon = "packaging/wizard-icon.png"\n', ""))
    m = manifest.parse_text(open(toml).read())
    assert m["wizard"]["icon"] == ""
    _, icon = wizard.resolve(wgame, dev=True)
    assert icon is None
    # An SVG (after a BOM and whitespace) passes as a PNG does.
    svg = b"\xef\xbb\xbf\n  <svg xmlns='http://www.w3.org/2000/svg'/>"
    write(os.path.join(wgame.root, "i.svg"), svg)
    write(toml, text.replace("packaging/wizard-icon.png", "i.svg"))
    _, icon = wizard.resolve(wgame, dev=True)
    assert icon == svg
    assert wizard.is_svg(b'<?xml version="1.0"?><svg/>') and not wizard.is_svg(PNG)


@pytest.mark.parametrize(
    "edit, error",
    [
        (("packaging/wizard-icon.png", b"\xff\xd8\xff\xe0JFIF"), "not a PNG or SVG"),
        (("packaging/config.default.toml", "[render]\nscale = 1\n"), "present.fullscreen is not"),
        (("packaging/config.default.toml", DEFAULTS.replace("= 1", '= "1"')), None),
        (("packaging/config.default.toml", DEFAULTS.replace("false", "1")), "not a boolean"),
    ],
)
def test_config_file_rules(wgame, edit, error):
    rel, data = edit
    write(os.path.join(wgame.root, *rel.split("/")), data)
    if error is None:  # a string key: the choice's literals are quoted
        cfg, _ = wizard.resolve(wgame, dev=True)
        assert cfg["options"][1]["toml"]["2"] == '"2"'
        return
    with pytest.raises(CliError, match=error):
        wizard.resolve(wgame, dev=True)


def test_config_flags(wgame):
    flags = wizard.package_flags()
    assert "--hud-pack" in flags and "--dump" not in flags  # --dump takes a value
    text = base_text() + WIZARD
    for flag, error in (("--no-such", "package takes no --no-such"), ("--plain", "wizard's own")):
        write(os.path.join(wgame.root, "game.toml"), text.replace("--hud-pack", flag))
        G = manifest.load(wgame.root)
        with pytest.raises(CliError, match=error):
            wizard.resolve(G, dev=True)


def test_config_without_table(d):
    root = os.path.join(d, "g")
    write(os.path.join(root, "game.toml"), base_text())
    with pytest.raises(CliError, match=r"no \[wizard\] table"):
        wizard.resolve(manifest.load(root), dev=True)


# ── [wizard] private (a game with no public repo) ───────────────────────

REPO_LINE = 'repo = "https://github.com/someone/blinx2-recomp.git"\n'


def test_manifest_wizard_private():
    """The four cases: public with a repo, public without one (an error,
    so a public game cannot lose its repo by accident), private without
    one, private with one (allowed; publishing is refused either way)."""
    text = base_text() + WIZARD
    w = manifest.parse_text(text)["wizard"]
    assert w["repo"].startswith("https://") and w["private"] is False
    with pytest.raises(manifest.ManifestError, match="wizard.repo: required"):
        manifest.parse_text(text.replace(REPO_LINE, ""))
    w = manifest.parse_text(text.replace(REPO_LINE, "private = true\n"))["wizard"]
    assert w["repo"] == "" and w["private"] is True
    w = manifest.parse_text(text.replace(REPO_LINE, REPO_LINE + "private = true\n"))["wizard"]
    assert w["repo"] and w["private"] is True
    # A repo that is set is still an https URL, private or not.
    bad = text.replace(REPO_LINE, 'repo = "git@github.com:a/b.git"\nprivate = true\n')
    with pytest.raises(manifest.ManifestError, match="wizard.repo: an https"):
        manifest.parse_text(bad)


def _private(wgame, keep_repo):
    toml = os.path.join(wgame.root, "game.toml")
    with open(toml) as f:
        text = f.read()
    new = (REPO_LINE if keep_repo else "") + "private = true\n"
    write(toml, text.replace(REPO_LINE, new))
    git(wgame.root, "commit", "-qam", "private")
    git(wgame.root, "tag", "-f", "v1.0.0")
    G = manifest.load(wgame.root)
    manifest.use(G)
    return G


@pytest.mark.parametrize("keep_repo", [False, True])
def test_private_config_and_publish(wgame, keep_repo, monkeypatch):
    G = _private(wgame, keep_repo)
    cfg, _ = wizard.resolve(G)
    # No repo and so no update check: the config names neither.
    assert cfg["private"] is True and "repo" not in cfg["release"]
    assert cfg["release"]["tag"] == "v1.0.0"
    assert "file:" not in wizard.config_json(cfg)
    # The release builder of wizard binaries refuses before any work.
    monkeypatch.setattr(wizard_build, "module_dir", lambda: pytest.fail("built a private wizard"))
    with pytest.raises(CliError, match="private = true"):
        wizard_build.build(G, out=os.path.join(G.root, "dist", "w"))


def test_public_config_keeps_the_repo(wgame):
    cfg, _ = wizard.resolve(wgame)
    assert "private" not in cfg and cfg["release"]["repo"].startswith("https://")
    wizard_build.refuse_private(wgame)  # no refusal


# ── wizard build: the pieces ─────────────────────────────────────────────


def test_parse_only():
    assert wizard_build.parse_only("") == ["macos", "windows", "linux"]
    assert wizard_build.parse_only("linux, macos,linux") == ["macos", "linux"]
    with pytest.raises(CliError, match="'steamos' is not one of"):
        wizard_build.parse_only("steamos")


def test_names_and_ids():
    assert wizard_build.asset_names("blinx2", "v0.4.0") == {
        "macos": "blinx2-wizard-v0.4.0-macos.zip",
        "windows": "blinx2-wizard-v0.4.0-windows-x64.zip",
        "linux": "blinx2-wizard-v0.4.0-linux-x64.tar.gz",
    }
    assert wizard_build.asset_names("g", "")["linux"] == "g-wizard-dev-linux-x64.tar.gz"
    assert wizard_build.app_id("https://github.com/Some_One/x.git", "g") == (
        "io.github.some-one.g-wizard"
    )
    assert wizard_build.app_id("https://example.invalid/x.git", "g") == "io.github.local.g-wizard"
    assert wizard_build.app_version("v0.4") == "0.4.0"
    assert wizard_build.app_version("blinx2-v1.2.3") == "1.2.3"
    assert wizard_build.app_version("") == "0.0.0"
    assert wizard_build.go_key("macos", "aarch64") == "darwin-arm64"
    assert wizard_build.go_key("linux", "x86_64") == "linux-amd64"


def test_go_command_lines():
    cmd = wizard_build.go_build_cmd("go", "out/w.exe", "v1", windows=True)
    assert cmd[:3] == ["go", "build", "-trimpath"]
    assert cmd[3] == "-ldflags=-s -w -buildid= -X main.version=v1 -H windowsgui"
    assert cmd[-3:] == ["-o", "out/w.exe", "./cmd/wizard"]
    assert "main.version=dev" in wizard_build.go_build_cmd("go", "w", "")[3]
    f = wizard_build.fyne_package_cmd("go", "fyne@v1", "N", "id", "w", "i.png", "v2.1")
    assert f[:4] == ["go", "run", "fyne@v1", "package"]
    assert f[f.index("-app-version") + 1] == "2.1.0" and f[-1] == "-release"


def test_pins_file_is_whole():
    p = wizard_build.load_pins(os.path.join(os.path.dirname(HERE), "wizard"))
    for k in ("go", "llvm_mingw", "fyne_tool", "builder", "uv", "mingit", "homebrew_install"):
        assert k in p, k
    for key in ("darwin-arm64", "darwin-amd64", "linux-amd64", "windows-amd64"):
        assert key in p["uv"]["assets"], key
    for pin in list(p["go"]["assets"].values()) + list(p["uv"]["assets"].values()):
        assert len(pin["sha256"]) == 64 and pin["url"].startswith("https://")
    assert p["builder"]["base"].count("@sha256:") == 1


def test_ensure_go_checks_the_version(d, monkeypatch):
    if os.name == "nt":
        pytest.skip("a shell-script go")
    root = os.path.join(d, "go")
    write(os.path.join(root, "bin", "go"), "#!/bin/sh\necho go1.0.0\n")
    os.chmod(os.path.join(root, "bin", "go"), 0o755)
    pins_ = {"go": {"version": "1.27.2", "assets": {}}}
    with pytest.raises(CliError, match="is not go1.27.2"):
        wizard_build.ensure_go(pins_, d, environ={"WIZARD_GO": root})
    write(os.path.join(root, "bin", "go"), "#!/bin/sh\necho go1.27.2\n")
    assert wizard_build.ensure_go(pins_, d, environ={"WIZARD_GO": root}) == root


def test_stage_module(d):
    mod = os.path.join(d, "mod")
    write(os.path.join(mod, "go.mod"), "module wizard\n")
    write(os.path.join(mod, "internal", "gamecfg", "config.json"), "{}\n")
    write(os.path.join(mod, ".tools", "go", "x"), "x")
    write(os.path.join(mod, "dist", "y"), "y")
    dest = os.path.join(d, "work", "src")
    wizard_build.stage_module(mod, dest, '{"schema": 1}\n')
    with open(os.path.join(dest, "internal", "gamecfg", "config.json")) as f:
        assert f.read() == '{"schema": 1}\n'
    assert not os.path.exists(os.path.join(dest, ".tools"))
    assert not os.path.exists(os.path.join(dest, "dist"))
    with open(os.path.join(mod, "internal", "gamecfg", "config.json")) as f:
        assert f.read() == "{}\n"  # the module itself is never written
    # The generic window icon stays unless the game has its own.
    write(os.path.join(mod, "internal", "gamecfg", "icon.png"), PNG)
    wizard_build.stage_module(mod, dest, "{}\n")
    with open(os.path.join(dest, "internal", "gamecfg", "icon.png"), "rb") as f:
        assert f.read() == PNG
    wizard_build.stage_module(mod, dest, "{}\n", PNG + b"game")
    with open(os.path.join(dest, "internal", "gamecfg", "icon.png"), "rb") as f:
        assert f.read() == PNG + b"game"
    with open(os.path.join(mod, "internal", "gamecfg", "icon.png"), "rb") as f:
        assert f.read() == PNG


def test_icon_commands(d):
    w = wizard_build.windres_cmd("/m", "wizard.rc", "/s/cmd/wizard/rsrc_windows_amd64.syso")
    assert w[0] == os.path.join("/m", "bin", "llvm-windres")
    assert "--target=pe-x86-64" in w and w[w.index("-O") + 1] == "coff"
    assert w[w.index("-i") + 1] == "wizard.rc" and w[-1].endswith("rsrc_windows_amd64.syso")
    assert "--no-preprocess" in w
    assert wizard_build.icongen_cmd("go", "i.svg", "/o")[1:] == [
        "run", "./cmd/icongen", "-in", "i.svg", "-out", "/o"
    ]  # fmt: skip
    assert wizard_build.desktop_entry("BLiNX 2", "blinx2-wizard") == (
        "[Desktop Entry]\nType=Application\nName=BLiNX 2 build wizard\nExec=blinx2-wizard\n"
        "Icon=blinx2-wizard\nCategories=Game;Utility;\n"
    )
    icons, src = os.path.join(d, "icons"), os.path.join(d, "src")
    write(os.path.join(icons, "icon-256.png"), PNG)
    wizard_build.stage_linux_icon(src, icons, "G", "g-wizard")
    assert sorted(os.listdir(os.path.join(src, "icon"))) == ["g-wizard.desktop", "g-wizard.png"]
    # The tar holds the binary, its icon and its menu entry.
    with open(os.path.join(wizard_build.module_dir(), "linux", "Dockerfile")) as f:
        df = f.read()
    assert "-cf - ${NAME} ${NAME}.png ${NAME}.desktop NOTICES.txt" in df
    assert "cp icon/${NAME}.png icon/${NAME}.desktop NOTICES.txt /out/" in df
    # The generic icon is the module's own SVG, and the license ships.
    assert os.path.isfile(os.path.join(wizard_build.module_dir(), wizard_build.GENERIC_ICON))
    root = os.path.dirname(wizard_build.module_dir())
    assert os.path.isfile(os.path.join(root, "LICENSES", "tabler-icons-MIT.txt"))


def _notice_tree(d):
    """A CLI root, a GOROOT and one module whose package builds in a C
    folder with its own licence, beside a sub-package and testdata."""
    cli, goroot, m = (os.path.join(d, n) for n in ("cli", "goroot", "mod"))
    write(os.path.join(cli, "LICENSE"), "MIT wizard\n")
    write(os.path.join(cli, "LICENSES", "tabler-icons-MIT.txt"), "MIT tabler\n")
    write(os.path.join(cli, "wizard", "notices", "mingw-w64-runtime.txt"), "mingw\n")
    write(os.path.join(goroot, "VERSION"), "go1.27.2\ntime x\n")
    write(os.path.join(goroot, "LICENSE"), "BSD go\r\n\r\n")
    write(os.path.join(goroot, "PATENTS"), "patents\n")
    write(os.path.join(m, "LICENSE"), "BSD mod\n")
    write(os.path.join(m, "README.md"), "no\n")
    write(os.path.join(m, "pkg", "a.go"), "package pkg\n")
    write(os.path.join(m, "pkg", "csrc", "LICENSE.md"), "zlib\n")
    write(os.path.join(m, "pkg", "csrc", "x.c"), "")
    write(os.path.join(m, "pkg", "sub", "b.go"), "package sub\n")
    write(os.path.join(m, "pkg", "sub", "LICENSE"), "not linked\n")
    write(os.path.join(m, "pkg", "testdata", "COPYING"), "not linked\n")
    # REUSE-style: one text per SPDX id, at the root and in a package.
    write(os.path.join(m, "LICENSES", "Apache-2.0.txt"), "apache\n")
    write(os.path.join(m, "pkg", "LICENSES", "CC0-1.0.txt"), "cc0\n")
    return cli, goroot, m


def test_notices(d):
    cli, goroot, m = _notice_tree(d)
    mods = {"example.org/m@v1.0.0": (m, {os.path.join(m, "pkg")})}
    secs = wizard_build.notice_sections(cli, goroot, mods)
    assert [t for t, _ in secs] == [
        "The wizard (xboxrecomp-cli)",
        "Go standard library (go1.27.2): LICENSE",
        "Go standard library (go1.27.2): PATENTS",
        "Tabler Icons (the glyphs and the generic window icon)",
        "MinGW-w64 runtime (in the Windows program)",
        "example.org/m@v1.0.0: LICENSE",
        "example.org/m@v1.0.0: LICENSES/Apache-2.0.txt",
        "example.org/m@v1.0.0: pkg/LICENSES/CC0-1.0.txt",
        "example.org/m@v1.0.0: pkg/csrc/LICENSE.md",
    ]
    text = wizard_build.notices_text(secs)
    assert text == wizard_build.notices_text(secs) and "\r" not in text
    assert (
        "=" * 72 + "\nexample.org/m@v1.0.0: pkg/csrc/LICENSE.md\n" + "=" * 72 + "\n\nzlib\n" in text
    )
    assert "not linked" not in text and d not in text
    # A linked module that ships no licence stops the build.
    bare = os.path.join(d, "bare")
    write(os.path.join(bare, "c.go"), "package c\n")
    with pytest.raises(CliError, match="no licence file"):
        wizard_build.notice_sections(cli, goroot, {"x.org/bare@v1": (bare, {bare})})


def test_mingw_notice(d):
    """The Windows build refuses an llvm-mingw whose runtime notices are
    not the copy NOTICES.txt carries; the copy in the tree is the pinned
    llvm-mingw's."""
    mod, mingw = os.path.join(d, "wizard"), os.path.join(d, "mingw")
    write(os.path.join(mod, wizard_build.MINGW_NOTICE), "notices v1\n")
    write(os.path.join(mingw, wizard_build.MINGW_RUNTIME), "notices v1\n")
    wizard_build.mingw_notice(mingw, mod)
    write(os.path.join(mingw, wizard_build.MINGW_RUNTIME), "notices v2\n")
    with pytest.raises(CliError, match="runtime notices changed"):
        wizard_build.mingw_notice(mingw, mod)
    with open(os.path.join(wizard_build.module_dir(), wizard_build.MINGW_NOTICE)) as f:
        assert f.read().startswith("MinGW-w64 runtime licensing")


def test_linked_modules(d, monkeypatch):
    seen = []

    def fake_run(cmd, env, cwd, capture_output):
        seen.append((env["GOOS"], env["GOARCH"], env["CGO_ENABLED"]))
        extra = "\nx.org/w@v1\t/m/w\t/m/w/win" if env["GOOS"] == "windows" else ""
        out = "a.org/b@v2\t/m/b\t/m/b/p" + extra
        return subprocess.CompletedProcess(cmd, 0, out.encode(), b"")

    monkeypatch.setattr(wizard_build.subprocess, "run", fake_run)
    mods = wizard_build.linked_modules("go", {}, d)
    assert mods == {"a.org/b@v2": ("/m/b", {"/m/b/p"}), "x.org/w@v1": ("/m/w", {"/m/w/win"})}
    assert {s[0] for s in seen} == {"darwin", "windows", "linux"} and {s[2] for s in seen} == {"1"}
    assert wizard_build.linked_cmd("go")[-1] == "./cmd/wizard"


def test_archives_carry_notices(d, monkeypatch):
    notices = os.path.join(d, "NOTICES.txt")
    write(notices, "licences\n")

    def fake_run(cmd, env=None, cwd=None, quiet=False):
        if "-o" in cmd:  # go build, windres
            write(cmd[cmd.index("-o") + 1], b"bin")
        elif cmd[0] == "lipo":
            write(cmd[cmd.index("-output") + 1], b"bin")
        elif "package" in cmd:  # fyne package writes <name>.app into cwd
            name = cmd[cmd.index("-name") + 1]
            write(os.path.join(cwd, name + ".app", "Contents", "MacOS", name), b"bin")

    monkeypatch.setattr(wizard_build, "_run", fake_run)
    src, work = os.path.join(d, "src"), os.path.join(d, "work")
    os.makedirs(os.path.join(src, "cmd", "wizard"))
    os.makedirs(work)
    win = os.path.join(d, "w.zip")
    wizard_build.build_windows("go", {}, src, work, "g-wizard", "v1", "/mingw", 1700000000,
                               win, work, notices)  # fmt: skip
    mac = os.path.join(d, "m.zip")
    wizard_build.build_macos("go", {}, src, work, "g-wizard", "v1", "i.png", "id", "fyne",
                             1700000000, mac, notices)  # fmt: skip
    with zipfile.ZipFile(win) as z:
        assert z.namelist() == ["g-wizard.exe", "NOTICES.txt"]
        assert z.read("NOTICES.txt") == b"licences\n"
    with zipfile.ZipFile(mac) as z:
        assert z.read("g-wizard.app/Contents/Resources/NOTICES.txt") == b"licences\n"
    # The Linux tar takes it from Docker's context (test_icon_commands).


def test_write_zip_is_reproducible(d):
    app = os.path.join(d, "G.app")
    write(os.path.join(app, "Contents", "MacOS", "g"), b"\x7fELF")
    os.chmod(os.path.join(app, "Contents", "MacOS", "g"), 0o755)
    write(os.path.join(app, "Contents", "Info.plist"), "<plist/>")
    zips = []
    for i, epoch in enumerate((1700000000, 1700000000, 1800000000)):
        if i:
            os.utime(os.path.join(app, "Contents", "Info.plist"), (5, 5))
        z = os.path.join(d, "a%d.zip" % i)
        wizard_build.write_zip(z, wizard_build._entries(app), epoch)
        with open(z, "rb") as f:
            zips.append(f.read())
    assert zips[0] == zips[1] and zips[0] != zips[2]
    with zipfile.ZipFile(os.path.join(d, "a0.zip")) as z:
        names = z.namelist()
        assert names == sorted(names) and names[0] == "G.app/Contents/"
        mode = {i.filename: i.external_attr >> 16 for i in z.infolist()}
    assert mode["G.app/Contents/MacOS/g"] == 0o100755
    assert mode["G.app/Contents/Info.plist"] == 0o100644
    assert mode["G.app/Contents/"] == 0o040755


def test_sums_text(d):
    write(os.path.join(d, "a.zip"), b"a")
    assert wizard_build.sums_text([os.path.join(d, "a.zip")]) == (
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb  a.zip\n"
    )


def test_docker_cmds(d):
    write(os.path.join(d, "ctx", "linux", "builder.Dockerfile"), "FROM x\n")
    p = {"builder": {"base": "golang@sha256:" + SHA, "snapshot": "20261010T000000Z",
                     "platform": "linux/arm64"}}  # fmt: skip
    ctx = os.path.join(d, "ctx")
    image, builder, asset = wizard_build.docker_cmds(p, ctx, "/w", "n", "v1", 42)
    assert image.startswith("xbr-wizard-builder:") and image in builder
    assert builder[:2] == ["docker", "build"] and "BASE=golang@sha256:" + SHA in builder
    assert "--output" in asset and "type=local,dest=/w" in asset and asset[-1] == ctx
    assert "SOURCE_DATE_EPOCH=42" in asset and "VERSION=v1" in asset
    # Never a bind mount: the build's only way out is --output.
    assert not any(a in ("-v", "--volume", "--mount") for a in builder + asset)
    _, _, dry = wizard_build.docker_cmds(p, ctx, "/w", "n", "", 42, dry=True)
    assert dry[dry.index("--target") + 1] == "dry" and "--output" not in dry
    # check's second build reuses no asset layer; the first keeps the cache.
    assert "--no-cache" not in asset and "FRESH=1" not in asset
    fresh = wizard_build.docker_cmds(p, ctx, "/w", "n", "v1", 42, no_cache=True)[2]
    assert "--no-cache" in fresh and "FRESH=1" in fresh and fresh[-1] == ctx
    p["builder"]["snapshot"] = "20261011T000000Z"
    assert wizard_build.docker_cmds(p, ctx, "/w", "n", "v1", 42)[0] != image


# ── pins refresh --wizard ────────────────────────────────────────────────


def _asset(name, size=10):
    return {
        "name": name,
        "size": size,
        "digest": "sha256:" + SHA,
        "browser_download_url": "https://dl.invalid/" + name,
    }


def test_wizard_pins_refresh(d, capsys):
    path = os.path.join(d, "pins.json")
    write(path, json.dumps({"go": {"version": "1"}, "uv": {}, "mingit": {}}))
    uv = {"tag_name": "0.14.0", "assets": [_asset(n) for n in pins.UV_ASSETS.values()]}
    uv["assets"].append(_asset("uv-other.tar.gz"))
    git_ = {
        "tag_name": "v2.57.0.windows.1",
        "assets": [_asset("MinGit-2.57.0-busybox-64-bit.zip"), _asset("MinGit-2.57.0-64-bit.zip")],
    }

    def get_json(url):
        return uv if "astral-sh" in url else git_

    pins.wizard_pins_refresh(path, get_json)
    with open(path) as f:
        out = json.load(f)
    assert out["go"] == {"version": "1"}  # moved by hand only
    assert out["uv"]["version"] == "0.14.0"
    assert set(out["uv"]["assets"]) == set(pins.UV_ASSETS)
    assert out["mingit"]["version"] == "2.57.0.windows.1"
    assert out["mingit"]["assets"]["windows-amd64"]["url"].endswith("/MinGit-2.57.0-64-bit.zip")
    assert out["uv"]["assets"]["linux-amd64"] == {
        "url": "https://dl.invalid/uv-x86_64-unknown-linux-gnu.tar.gz",
        "sha256": SHA,
        "size": 10,
    }
    full = uv["assets"]
    uv["assets"] = full[1:]
    with pytest.raises(CliError, match="release 0.14.0 has no uv-aarch64"):
        pins.wizard_pins_refresh(path, get_json)
    uv["assets"] = [dict(a, digest=None) for a in full]
    with pytest.raises(CliError, match="no sha256 digest"):
        pins.wizard_pins_refresh(path, get_json)


# ── doctor --json ────────────────────────────────────────────────────────


def test_doctor_json(wgame):
    lines, targets, blocked = doctor.doctor_report()
    out = json.loads(doctor.report_json(lines, targets, blocked))
    assert set(out) == {
        "host",
        "lines",
        "targets",
        "blocked",
        "blockers",
        "problems",
        "next",
        "toolkit_features",
        "resources",
    }
    assert set(out["host"]) == {"os", "arch", "python"}
    r = out["resources"]
    assert set(r) == {
        "cores",
        "mem_total",
        "mem_available",
        "disk_free",
        "disk_need",
        "jobs",
        "gen_jobs",
        "gen_job_bytes",
        "gen_job_source",
        "warnings",
    }
    assert r["jobs"] >= r["gen_jobs"] >= 1 and isinstance(r["warnings"], list)
    assert any(x.startswith("build jobs: ") for x in lines)
    assert out["lines"] == lines and out["targets"] == targets
    assert set(out["blockers"]) == {"windows", "steamos", "macos"}
    # Importing a dump is the wizard's own step: never a blocker.
    for b in out["blockers"].values():
        assert not any("default.xbe" in x for x in b)
    assert out["next"]


def test_doctor_json_exits_0(wgame, capsys):
    import argparse

    from xboxrecomp_cli import main as cli_main

    # The test game is not set up, so the text form fails; the JSON form
    # still prints the whole report and exits 0 (blockers carry the verdict).
    assert cli_main.cmd_doctor(argparse.Namespace(json=False)) != 0
    capsys.readouterr()
    assert cli_main.cmd_doctor(argparse.Namespace(json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lines"] and out["next"]


# ── package --seed-config and the installers ─────────────────────────────


def test_check_seed(wgame, d):
    seed = os.path.join(d, "seed.toml")
    write(seed, "[render]\r\nscale = 2\r\n")
    assert pkg.check_seed(seed) == "[render]\nscale = 2\n"
    write(seed, "[render]\nscale = 2\nwidth = 9\n[other]\nx = 1\n")
    with pytest.raises(CliError, match="render.width, other.x"):
        pkg.check_seed(seed)
    write(seed, "[render\n")
    with pytest.raises(CliError, match="not TOML"):
        pkg.check_seed(seed)
    with pytest.raises(CliError, match="--seed-config: .*nope"):
        pkg.check_seed(os.path.join(d, "nope"))


def test_nsis_copies_the_seed_once(d):
    from test_windows_templates import render_installer

    text = render_installer(os.path.join(d, "installer.nsi"))
    main = text[text.index('Section "BLiNX 2" SecMain') : text.index("SectionEnd")]
    uninst = text[text.index('Section "Uninstall"') :]
    # The in-place layout's seed goes; the new one is read from the
    # version being installed (versions\<v>.partial).
    drop = 'Delete "$INSTDIR\\config.seed.toml"'
    copy = 'CopyFiles /SILENT "${PART}\\config.seed.toml"'
    seeded = 'IfFileExists "${PART}\\config.seed.toml" 0 seed_done'
    guard = '\\config\\config.toml" seed_done 0'
    # An earlier version's enhance.toml is the player's file too: the game
    # renames it only while config.toml is missing.
    old = '\\config\\enhance.toml" 0 seed_copy'
    assert main.index(drop) < main.index("  File a\n") < main.index(seeded)
    assert (
        main.index(seeded)
        < main.index(guard)
        < main.index(old)
        < main.index("seed_copy:")
        < main.index(copy)
    )
    assert "Kept your existing settings" in main[main.index(old) : main.index("seed_copy:")]
    assert drop in uninst[: uninst.index("SectionEnd")]


# ── new writes the release workflow ──────────────────────────────────────


def test_workflow_template():
    text = scaffold.workflow_text({"SLUG": "my-game", "NAME": "My Game"})
    assert "@SLUG@" not in text and "./my-game wizard build" in text
    jobs = text[text.index("\njobs:\n") :]
    release = jobs[jobs.index("\n  release:\n") :]
    # Only the release job writes, and only on a tag push or when asked.
    assert text.count("contents: write") == 1 and "contents: write" in release
    assert "if: github.event_name == 'push' || inputs.release" in release
    assert "\n  verify:\n" in jobs and "contents: write" not in jobs[: jobs.index("\n  release:\n")]
    assert text.count("persist-credentials: false") == text.count("actions/checkout@")
    assert text.count("enable-cache: false") == text.count("astral-sh/setup-uv@")
    assert "assets/*.zip assets/*.tar.gz assets/SHA256SUMS" in release
    assert "assets/* " not in release and "--verify-tag" in release


def _install(il, bundle, root):
    import contextlib
    import io

    from test_steamos_install import found_umu

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = il.main(
            ["install", "--root", root, "--bundle", bundle],
            host_check=False,
            bench="/nonexistent-bench",
            umu=found_umu,
        )
    return rc, buf.getvalue()


def test_steamos_install_seeds_once(d, monkeypatch):
    import importlib.util

    from test_steamos_install import make_bundle, render

    # A scratch HOME, as test_steamos_install's tests have: no real ~/.local.
    os.makedirs(os.path.join(d, "home"))
    monkeypatch.setenv("HOME", os.path.join(d, "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = render("install_lib.py", os.path.join(d, "install_lib.py"))
    spec = importlib.util.spec_from_file_location("install_lib_seed", p)
    il = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(il)
    seed = {"config.seed.toml": b"[render]\nscale = 2\n"}
    root = os.path.join(d, "r")
    rc, out = _install(il, make_bundle(os.path.join(d, "b1"), "20261001.0000-a", extra=seed), root)
    assert rc == 0 and "config: your options" in out, out
    cfg = os.path.join(root, "config", "config.toml")
    with open(cfg, "rb") as f:
        assert f.read() == b"[render]\nscale = 2\n"
    # A later install, with other options, never replaces the player's file.
    write(cfg, "[render]\nscale = 3\n")
    seed = {"config.seed.toml": b"[render]\nscale = 1\n"}
    rc, out = _install(il, make_bundle(os.path.join(d, "b2"), "20261002.0000-a", extra=seed), root)
    assert rc == 0 and "config: your options" not in out, out
    with open(cfg) as f:
        assert f.read() == "[render]\nscale = 3\n"
    # A bundle without a seed writes no config.toml.
    root2 = os.path.join(d, "r2")
    rc, out = _install(il, make_bundle(os.path.join(d, "b3"), "20261001.0000-a"), root2)
    assert rc == 0 and not os.path.exists(os.path.join(root2, "config", "config.toml")), out
    # An earlier version's enhance.toml is kept: a seeded config.toml would
    # stop the game's one-time rename of it.
    root3 = os.path.join(d, "r3")
    write(os.path.join(root3, "config", "enhance.toml"), "[render]\nscale = 3\n")
    rc, out = _install(il, make_bundle(os.path.join(d, "b4"), "20261001.0000-a", extra=seed), root3)
    assert rc == 0 and "kept your existing settings" in out, out
    assert not os.path.exists(os.path.join(root3, "config", "config.toml"))


def test_check_second_build_is_fresh(d, monkeypatch):
    calls = []
    monkeypatch.setattr(wizard_build, "module_dir", lambda: d)
    monkeypatch.setattr(wizard_build, "load_pins", lambda mod: {})
    monkeypatch.setattr(wizard_build, "ensure_go", lambda pins, mod: d)
    monkeypatch.setattr(wizard_build, "_run", lambda *a, **k: None)

    def fake_build(G, tag, dev, only, out, keep_work=False, gocache="", private_ok=False):
        calls.append(gocache)
        assert private_ok  # the check publishes nothing
        return {"a.zip": "0" * 64}

    monkeypatch.setattr(wizard_build, "build", fake_build)
    assert wizard_build.check(None, "v1") == 0
    assert calls[0] == "" and calls[1] and os.path.basename(calls[1]) == "gocache"


def test_config_range(wgame):
    write(os.path.join(wgame.root, "game.toml"), base_text() + RANGE)
    cfg, _ = wizard.resolve(manifest.load(wgame.root), dev=True)
    scale = cfg["options"][1]
    assert scale["toml"] == {str(v): str(v) for v in range(1, 7)}
    assert scale["labels"] == {"1": "640x480", "6": "about 4K"} and scale["default"] == 1
    write(
        os.path.join(wgame.root, "game.toml"),
        base_text() + RANGE.replace('flag = "--hud-pack"', LINK, 1),
    )
    hud = wizard.resolve(manifest.load(wgame.root), dev=True)[0]["options"][0]
    assert hud["hint_link"] == {"text": "Get the font", "url": "https://fonts.example/f"}
    write(os.path.join(wgame.root, "game.toml"), base_text() + RANGE)
    for defaults, error in (
        (DEFAULTS.replace("scale = 1", "scale = 2"), r"render.scale is 2"),
        (DEFAULTS.replace("scale = 1", 'scale = "1"'), r"render.scale is not a whole number"),
        (DEFAULTS.replace("scale = 1", "scale = true"), r"render.scale is not a whole number"),
    ):
        write(os.path.join(wgame.root, "packaging", "config.default.toml"), defaults)
        with pytest.raises(CliError, match=error):
            wizard.resolve(manifest.load(wgame.root), dev=True)
