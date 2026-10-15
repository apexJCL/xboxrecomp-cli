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
import zipfile

import pytest

from xboxrecomp_cli import doctor, manifest, pins, scaffold, wizard, wizard_build
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
def wgame(d):
    """The test game with a [wizard] table, its icon and defaults file,
    committed and tagged v1.0.0; current while the test runs."""
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


@pytest.mark.parametrize(
    "edit, error",
    [
        (("packaging/wizard-icon.png", b"GIF89a"), "is not a PNG"),
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
    assert set(out) == {"host", "lines", "targets", "blocked", "blockers", "problems", "next"}
    assert set(out["host"]) == {"os", "arch", "python"}
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
    drop = 'Delete "$INSTDIR\\config.seed.toml"'
    copy = 'CopyFiles /SILENT "$INSTDIR\\config.seed.toml"'
    seeded = 'IfFileExists "$INSTDIR\\config.seed.toml" 0 seed_done'
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

    def fake_build(G, tag, dev, only, out, keep_work=False, gocache=""):
        calls.append(gocache)
        return {"a.zip": "0" * 64}

    monkeypatch.setattr(wizard_build, "build", fake_build)
    assert wizard_build.check(None, "v1") == 0
    assert calls[0] == "" and calls[1] and os.path.basename(calls[1]) == "gocache"
