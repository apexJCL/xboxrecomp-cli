"""doctor's SDL3 line on macOS: the installed version against game.toml's
[toolchain] sdl3, a warning when its major.minor is older, never a blocker."""

import json
import os
import stat

import pytest

from xboxrecomp_cli import doctor, fetch, host, manifest, pins
from xboxrecomp_cli.host import CliError


def fake_tool(d, name, out, rc=0):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\nprintf '%%s\\n' '%s'\nexit %d\n" % (out, rc))
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


@pytest.fixture
def bin_dir(d, monkeypatch):
    b = os.path.join(d, "bin")
    os.makedirs(b)
    # Only the fakes: no real pkg-config or brew answers for this host.
    monkeypatch.setenv("PATH", b)
    return b


def test_installed_from_pkg_config(bin_dir):
    fake_tool(bin_dir, "pkg-config", "3.4.16")
    assert doctor.sdl3_installed() == "3.4.16"


def test_installed_from_the_homebrew_keg(bin_dir, d):
    fake_tool(bin_dir, "pkg-config", "", rc=1)  # no sdl3.pc on its path
    keg = os.path.join(d, "Cellar", "sdl3", "3.4.16_1")
    os.makedirs(os.path.join(keg, "lib"))
    open(os.path.join(keg, "lib", "libSDL3.0.dylib"), "w").close()
    opt = os.path.join(d, "opt-sdl3")
    os.symlink(keg, opt)
    fake_tool(bin_dir, "brew", opt)
    assert doctor.sdl3_installed() == "3.4.16"


def test_not_installed(bin_dir):
    fake_tool(bin_dir, "pkg-config", "", rc=1)
    fake_tool(bin_dir, "brew", "", rc=1)
    assert doctor.sdl3_installed() == ""


@pytest.mark.parametrize(
    "installed, pin, line, warned",
    [
        ("3.2.10", "3.4.18", "sdl3:       3.2.10 (pin 3.4.18)", True),  # older
        ("3.4.18", "3.4.18", "sdl3:       3.4.18 (pin 3.4.18)", False),  # equal
        ("3.4.16", "3.4.18", "sdl3:       3.4.16 (pin 3.4.18)", False),  # patch behind
        ("3.6.0", "3.4.18", "sdl3:       3.6.0 (pin 3.4.18)", False),  # newer
        ("3.4.16", "", "sdl3:       3.4.16", False),  # no pin
        ("", "3.4.18", "sdl3:       not found (pin 3.4.18)", False),
        ("3.2.10", "release-3.4.18", "sdl3:       3.2.10 (pin release-3.4.18)", False),  # bad pin
        ("3.2.10", "3", "sdl3:       3.2.10 (pin 3)", False),  # bad pin
    ],
)
def test_report(installed, pin, line, warned):
    got, w = doctor.sdl3_report(installed, pin)
    assert got == line
    assert (
        (w == "SDL3 3.2.10 is older than the pinned 3.4.18: brew upgrade sdl3")
        if warned
        else w is None
    )


def test_doctor_report_warns_and_keeps_macos(d, monkeypatch):
    monkeypatch.setattr(host, "host_os", lambda: "macos")
    monkeypatch.setattr(host, "CLT", d)
    monkeypatch.setattr(doctor, "brew_missing", lambda pkgs=None: [])
    monkeypatch.setattr(doctor, "sdl3_installed", lambda: "3.2.10")
    lines, _, blocked = doctor.doctor_report()
    assert "sdl3:       3.2.10 (pin 3.4.18)" in lines
    assert "warning:    SDL3 3.2.10 is older than the pinned 3.4.18: brew upgrade sdl3" in lines
    assert "SDL3" not in blocked.get("macos", "")


def _release(version, digest=True):
    def asset(name):
        return {
            "name": name,
            "size": 15651004,
            "digest": "sha256:" + "ab" * 32 if digest else None,
            "browser_download_url": "https://github.invalid/%s" % name,
        }

    return {
        "tag_name": "release-" + version,
        "assets": [asset("SDL3-%s.zip" % version), asset("SDL3-%s.tar.gz" % version)],
    }


def test_sdl3_pin():
    urls = []

    def get_json(url):
        urls.append(url)
        return _release("3.4.18")

    pin = pins.sdl3_pin("3.4.18", get_json)
    assert urls == ["https://api.github.com/repos/libsdl-org/SDL/releases/tags/release-3.4.18"]
    assert pin == {
        "version": "3.4.18",
        "url": "https://github.invalid/SDL3-3.4.18.tar.gz",
        "sha256": "ab" * 32,
        "size": 15651004,
    }
    with pytest.raises(CliError, match="no sha256 digest"):
        pins.sdl3_pin("3.4.18", lambda url: _release("3.4.18", digest=False))
    with pytest.raises(CliError, match="has no SDL3-3.4.19.tar.gz"):
        pins.sdl3_pin("3.4.19", lambda url: _release("3.4.18"))
    json.dumps(pin)  # it goes into setup-pins.json as is


class _Download:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        out, self.data = self.data, b""
        return out


@pytest.mark.parametrize("sdl3", ["3.4.18", ""])
def test_pins_refresh(d, monkeypatch, sdl3):
    """With [toolchain] sdl3 set the tarball joins the two entries every
    game has; without it the file holds those two only, as before."""
    g = manifest.current()
    monkeypatch.setitem(g.m["toolchain"], "sdl3", sdl3)
    monkeypatch.setattr(g, "pins", os.path.join(d, "setup-pins.json"))
    tag = g.m["toolchain"]["llvm_mingw"]
    mingw = {
        "tag_name": tag,
        "assets": [
            {
                "name": n.format(tag=tag),
                "size": 1,
                "digest": "sha256:" + "cd" * 32,
                "browser_download_url": "u",
            }
            for n in fetch.MINGW_ASSETS.values()
        ],
    }
    monkeypatch.setattr(
        pins, "_get_json", lambda url: _release(sdl3) if "libsdl-org" in url else mingw
    )
    monkeypatch.setattr(pins.urllib.request, "urlopen", lambda req: _Download(b"nsis"))
    monkeypatch.setattr(pins.env, "find_uv", lambda: ("uv", None))
    monkeypatch.setattr(pins.host, "run", lambda *a, **k: None)
    monkeypatch.setattr(pins, "pin_report", lambda *a: "")
    pins.pins_refresh()
    with open(g.pins) as f:
        out = json.load(f)
    want = {"_comment", "llvm_mingw", "nsis"} | ({"sdl3"} if sdl3 else set())
    assert set(out) == want
    if sdl3:
        assert out["sdl3"]["version"] == "3.4.18"
        assert out["sdl3"]["url"].endswith("/SDL3-3.4.18.tar.gz")
