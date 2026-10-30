"""The macOS app's bundled Homebrew dylibs carry their licences
(package/macos.py stage_dylib_licences), found from each dylib's Cellar
keg; a dylib with none stops the build. Fake kegs in a temp dir."""

import os

import pytest

from xboxrecomp_cli.host import CliError
from xboxrecomp_cli.package import macos


def keg(root, formula, version, files):
    k = os.path.join(root, "Cellar", formula, version)
    os.makedirs(os.path.join(k, "lib"))
    for n, text in files.items():
        with open(os.path.join(k, n), "wb") as f:
            f.write(text)
    return k


def stage_toolkit(res, *names):
    os.makedirs(os.path.join(res, "LICENSES"), exist_ok=True)
    for n in names:
        with open(os.path.join(res, "LICENSES", n), "w") as f:
            f.write("toolkit")


def lib(k, name):
    return {"name": name, "src": os.path.join(k, "lib", name)}


def test_brew_keg():
    assert macos.brew_keg("/opt/homebrew/Cellar/sdl3/3.4.18/lib/libSDL3.0.dylib") == (
        "sdl3",
        "3.4.18",
        "/opt/homebrew/Cellar/sdl3/3.4.18",
    )
    # Intel Homebrew.
    assert macos.brew_keg("/usr/local/Cellar/libepoxy/1.5.10_1/lib/libepoxy.0.dylib") == (
        "libepoxy",
        "1.5.10_1",
        "/usr/local/Cellar/libepoxy/1.5.10_1",
    )
    assert macos.brew_keg("/usr/local/lib/libfoo.dylib") is None
    assert macos.brew_keg("/opt/homebrew/Cellar/sdl3") is None


def test_licences_staged(tmp_path):
    root = str(tmp_path)
    sdl = keg(root, "sdl3", "3.4.18", {"LICENSE.txt": b"zlib\r\n", "README.md": b"x"})
    epoxy = keg(root, "libepoxy", "1.5.10", {"COPYING": b"mit"})
    ssl = keg(root, "openssl@4", "4.0.3", {"LICENSE.txt": b"apache"})
    res = os.path.join(root, "Resources")
    stage_toolkit(res, "MPL-2.0-librashader.txt")
    libs = [
        lib(sdl, "libSDL3.0.dylib"),
        lib(sdl, "libSDL3.dylib"),
        lib(epoxy, "libepoxy.0.dylib"),
        lib(ssl, "libcrypto.4.dylib"),
        {"name": "librashader.dylib", "src": "/build/librashader.dylib"},
    ]
    macos.stage_dylib_licences(res, libs)
    d = os.path.join(res, "LICENSES")
    assert sorted(os.listdir(d)) == [
        "BUNDLED-LIBRARIES.md",
        "MPL-2.0-librashader.txt",
        "libepoxy-COPYING",
        "openssl@4-LICENSE.txt",
        "sdl3-LICENSE.txt",
        "sdl3-yuv2rgb-BSD-3-Clause.txt",
    ]
    with open(os.path.join(d, "sdl3-yuv2rgb-BSD-3-Clause.txt")) as f:
        assert f.read().startswith("Copyright (c) 2016, Adrien Descamps\n")
    with open(os.path.join(d, "sdl3-LICENSE.txt"), "rb") as f:
        assert f.read() == b"zlib\n"
    with open(os.path.join(d, "BUNDLED-LIBRARIES.md")) as f:
        text = f.read()
    # One entry per keg, with the version from its Cellar path and the
    # licence the wizard's credits give.
    assert "- SDL 3.4.18 (Zlib, https://github.com/libsdl-org/SDL): " in text
    assert "`libSDL3.0.dylib`, `libSDL3.dylib`" in text
    assert "- libepoxy 1.5.10 (MIT, " in text
    assert "- OpenSSL 4.0.3 (Apache-2.0, " in text
    assert "  - `openssl@4-LICENSE.txt`" in text
    assert "  - `sdl3-LICENSE.txt`\n  - `sdl3-yuv2rgb-BSD-3-Clause.txt`\n" in text
    assert "librashader" not in text


def test_unknown_formula_named_plainly(tmp_path):
    root = str(tmp_path)
    k = keg(root, "zstd", "1.5.7", {"LICENSE": b"bsd"})
    macos.stage_dylib_licences(os.path.join(root, "R"), [lib(k, "libzstd.1.dylib")])
    with open(os.path.join(root, "R", "LICENSES", "BUNDLED-LIBRARIES.md")) as f:
        assert "- zstd 1.5.7: `libzstd.1.dylib`" in f.read()


def test_missing_licence_fails(tmp_path):
    root = str(tmp_path)
    ok = keg(root, "sdl3", "3.4.18", {"LICENSE.txt": b"zlib"})
    bare = keg(root, "libfoo", "1.0", {"README.md": b"no licence"})
    res = os.path.join(root, "Resources")
    libs = [
        lib(ok, "libSDL3.0.dylib"),
        lib(bare, "libfoo.1.dylib"),
        {"name": "libbar.dylib", "src": "/usr/local/lib/libbar.dylib"},
    ]
    with pytest.raises(CliError) as e:
        macos.stage_dylib_licences(res, libs)
    assert "libfoo.1.dylib" in str(e.value) and "libbar.dylib" in str(e.value)
    assert "libSDL3" not in str(e.value)
    # The remedy names where a non-Homebrew library's licence goes.
    assert "package.macos.COVERED" in str(e.value)
    # Nothing staged on failure.
    assert not os.path.exists(os.path.join(res, "LICENSES"))


def test_covered_needs_its_file(tmp_path):
    """A library the toolkit's LICENSES/ covers stops the build when that
    file is not staged (an older toolkit, or a renamed file)."""
    res = os.path.join(str(tmp_path), "Resources")
    shader = {"name": "librashader.dylib", "src": "/build/librashader.dylib"}
    with pytest.raises(CliError) as e:
        macos.stage_dylib_licences(res, [shader])
    assert "MPL-2.0-librashader.txt" in str(e.value)
    stage_toolkit(res, "MPL-2.0-librashader.txt")
    macos.stage_dylib_licences(res, [shader])
    # Nothing of ours to list.
    assert sorted(os.listdir(os.path.join(res, "LICENSES"))) == ["MPL-2.0-librashader.txt"]


def test_covered_tracks_the_shader_lib():
    from xboxrecomp_cli.package import incr

    assert incr.SHADER_LIBS["macos"] in macos.COVERED


def test_notice_staged(tmp_path):
    """Apache-2.0 4(d): a keg's NOTICE ships too."""
    root = str(tmp_path)
    k = keg(root, "openssl@4", "4.0.3", {"LICENSE.txt": b"apache", "NOTICE": b"n"})
    res = os.path.join(root, "R")
    macos.stage_dylib_licences(res, [lib(k, "libcrypto.4.dylib")])
    assert os.path.isfile(os.path.join(res, "LICENSES", "openssl@4-NOTICE"))


def test_two_kegs_of_one_formula_fail(tmp_path):
    root = str(tmp_path)
    a = keg(root, "openssl@4", "4.0.3", {"LICENSE.txt": b"a"})
    b = keg(root, "openssl@4", "4.0.4", {"LICENSE.txt": b"b"})
    res = os.path.join(root, "R")
    with pytest.raises(CliError) as e:
        macos.stage_dylib_licences(res, [lib(a, "libcrypto.4.dylib"), lib(b, "libssl.4.dylib")])
    assert "two openssl@4 kegs" in str(e.value) and "4.0.3, 4.0.4" in str(e.value)
    assert not os.path.exists(os.path.join(res, "LICENSES"))


def test_toolkit_clash_fails(tmp_path):
    """A staged licence name the toolkit already used is never overwritten."""
    root = str(tmp_path)
    k = keg(root, "sdl3", "3.4.18", {"LICENSE.txt": b"zlib"})
    res = os.path.join(root, "R")
    stage_toolkit(res, "sdl3-LICENSE.txt")
    with pytest.raises(CliError) as e:
        macos.stage_dylib_licences(res, [lib(k, "libSDL3.0.dylib")])
    assert "LICENSES/sdl3-LICENSE.txt is already staged" in str(e.value)
    with open(os.path.join(res, "LICENSES", "sdl3-LICENSE.txt")) as f:
        assert f.read() == "toolkit"


def test_no_libs_writes_nothing(tmp_path):
    res = os.path.join(str(tmp_path), "Resources")
    macos.stage_dylib_licences(res, [])
    assert not os.path.exists(res)
