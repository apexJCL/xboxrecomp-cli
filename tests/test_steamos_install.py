"""Tests for the steamos templates (templates/steamos/): the installer
(install_lib.py) on fake bundles, rendered with the test game's values as
`package steamos` renders them; the umu-run search and the pinned fetch with
a fake download; and launch.sh with a fake umu-run, zenity and HOME (no
game data, no Proton, no network).

Every installer command runs between two snapshots of hdd/, config/ and
logs/ (paths, sizes, mtimes, contents): the installer must never write the
user's data.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time

import pytest

from xboxrecomp_cli import pins
from xboxrecomp_cli.package import plib, steamos

pl = plib()
VALUES = steamos.steamos_values(pl)
APP = VALUES["APP"]
EXE = VALUES["EXE"]
BASH = shutil.which("bash")


def render(name, dst, mode=0o644):
    steamos.render_lf(steamos.template(name), VALUES, dst, mode)
    return dst


@pytest.fixture(scope="module")
def il(tmp_path_factory):
    """install_lib.py as the bundle carries it, imported."""
    p = render("install_lib.py", str(tmp_path_factory.mktemp("il") / "install_lib.py"))
    spec = importlib.util.spec_from_file_location("install_lib_rendered", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def home(d, monkeypatch):
    """A scratch HOME (and no XDG_DATA_HOME), so no test sees the real
    ~/.local; PATH without any umu-run."""
    h = os.path.join(d, "home")
    os.makedirs(h)
    monkeypatch.setenv("HOME", h)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return h


def make_exe(path, body="exit 0\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


def make_bundle(base, version, exe=b"MZ program", game=None):
    """A payload like `package steamos` stages, with a real manifest."""
    b = os.path.join(base, "%s-%s-steamos" % (APP, version))
    os.makedirs(os.path.join(b, "game_files", "media"))
    files = {
        EXE + ".exe": exe,
        "launch.sh": b"#!/bin/sh\nexit 0\n",
        "install.sh": b"#!/bin/sh\n",
        "install_lib.py": b"# lib\n",
        "launch.env.default": b"RECOMP_PB_BACKEND=d3d11\n",
        "README.txt": b"readme\n",
    }
    game = game or {"default.xbe": b"XBEH game", "media/a.xpr": b"asset a"}
    for n, data in files.items():
        with open(os.path.join(b, n), "wb") as f:
            f.write(data)
    for n, data in game.items():
        with open(os.path.join(b, "game_files", n), "wb") as f:
            f.write(data)
    st = {"commit": "a" * 40, "branch": "main", "dirty": False, "dirty_paths": 0, "_dirt": ""}
    m = pl.build_manifest(
        b,
        "steamos-x86_64-proton",
        version,
        st,
        st,
        "c" * 64,
        1,
        {"CMAKE_BUILD_TYPE": "Release"},
        "clang",
    )
    with open(os.path.join(b, "manifest.json"), "w") as f:
        json.dump(m, f)
    with open(os.path.join(b, "SHA256SUMS"), "w") as f:
        f.write(pl.sums_text(m))
    return b


def snapshot(il, root):
    out = {}
    for d in il.USER_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, d)):
            for n in dirnames + filenames:
                p = os.path.join(dirpath, n)
                st = os.lstat(p)
                data = b""
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        data = f.read()
                out[os.path.relpath(p, root)] = (
                    st.st_mode,
                    st.st_size,
                    st.st_mtime_ns,
                    hashlib.sha256(data).hexdigest(),
                )
    return out


def plant_user_data(root):
    for rel, data in (
        ("hdd/UDATA/4d530065/save.xsv", b"save"),
        ("config/enhance.toml", b"[render]\n"),
        ("config/launch.env", b"LOG_KEEP=3\n"),
        ("logs/game-1.log", b"log"),
    ):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


def found_umu(want_fetch, umu_dir):
    return "/opt/umu/umu-run"


def run(il, bundle, root, *args, bench="/nonexistent-bench", umu=found_umu):
    """install_lib.main with the host check off and umu-run stubbed;
    returns (exit, output) and asserts the user data is untouched."""
    before = snapshot(il, root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = il.main(
            list(args) + ["--root", root, "--bundle", bundle],
            host_check=False,
            bench=bench,
            umu=umu,
        )
    if "--import-saves" not in args:
        assert snapshot(il, root) == before, "user data changed by %s" % (args,)
    return rc, buf.getvalue()


# ── the templates ────────────────────────────────────────────────────────


def test_templates_game_agnostic():
    """The defaults name no game: every game value is a placeholder, and
    rendering fills them all."""
    for name in ("install.sh", "install_lib.py", "launch.sh", "README.part"):
        with open(os.path.join(steamos.DEFAULTS, name)) as f:
            text = f.read()
        for word in ("BLiNX", "blinx", "cat_recomp", "Burnout"):
            assert word not in text, (name, word)
    assert VALUES["UMU_SHA256"] == pins.UMU_LAUNCHER["sha256"]
    assert re.match(r"^[0-9a-f]{64}$", VALUES["UMU_SHA256"])
    assert VALUES["UMU_URL"].startswith("https://github.com/Open-Wine-Components/umu-launcher/")
    assert "/" + VALUES["UMU_VERSION"] + "/" in VALUES["UMU_URL"]
    assert VALUES["ROOT_ENV"] == "BLINX2_ROOT" and VALUES["EXE"] == "cat_recomp"


def test_templates_render_fully(d):
    for name, _mode in steamos.FILES + (("README.part", 0o644),):
        with open(render(name, os.path.join(d, name))) as f:
            text = f.read()
        left = re.findall(r"@[A-Z_]+@", text)
        if name == "README.part":
            left = [x for x in left if x != "@VERSION@"]  # readme() fills it
        assert not left, (name, left)
    r = subprocess.run([BASH, "-n", os.path.join(d, "launch.sh")])
    assert r.returncode == 0


def test_game_override_wins(d, monkeypatch):
    G = steamos.g()
    tdir = os.path.join(d, "tpl")
    make_exe(os.path.join(tdir, "steamos", "launch.sh"), "echo @NAME@\n")
    monkeypatch.setattr(G, "templates", tdir)
    assert steamos.template("launch.sh") == os.path.join(tdir, "steamos", "launch.sh")
    assert steamos.template("install.sh") == os.path.join(steamos.DEFAULTS, "install.sh")
    monkeypatch.setattr(G, "templates", "")
    assert steamos.template("launch.sh") == os.path.join(steamos.DEFAULTS, "launch.sh")


# ── install, update, rollback, uninstall ─────────────────────────────────


def test_first_install_layout(il, d):
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    root = os.path.join(d, "Games", APP)
    rc, out = run(il, b, root, "install")
    assert rc == 0, out
    assert os.readlink(os.path.join(root, "current")) == os.path.join(
        "versions", "20261001.0000-ca-tb-g1"
    )
    for rel in (
        "versions/20261001.0000-ca-tb-g1/%s.exe" % EXE,
        "game_files/default.xbe",
        "game_files/media/a.xpr",
        "state/history",
        "state/layout",
    ):
        assert os.path.isfile(os.path.join(root, rel)), rel
    launcher = os.path.join(root, APP)
    assert (
        os.path.isfile(launcher) and not os.path.islink(launcher) and os.access(launcher, os.X_OK)
    )
    assert os.access(os.path.join(root, "current", "launch.sh"), os.X_OK)
    assert not os.path.exists(os.path.join(root, "hdd")), "install must not create hdd/"
    # config/ is there from the first install, empty; state/umu-run names
    # the umu-run the install found.
    assert os.path.isdir(os.path.join(root, "config"))
    assert os.listdir(os.path.join(root, "config")) == []
    with open(os.path.join(root, "state", "umu-run")) as f:
        assert f.read() == "/opt/umu/umu-run\n"


def test_config_kept(il, d):
    """An existing config/ (and launch.env in it) is never touched; run()
    compares the snapshots."""
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    root = os.path.join(d, "r")
    os.makedirs(os.path.join(root, "config"))
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("PATH=/x\n")
    rc, out = run(il, b, root, "install")
    assert rc == 0 and "config: created" not in out, out
    with open(os.path.join(root, "config", "launch.env")) as f:
        assert f.read() == "PATH=/x\n"


def test_same_version_noop(il, d):
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    root = os.path.join(d, "r")
    run(il, b, root, "install")
    plant_user_data(root)
    hist = open(os.path.join(root, "state", "history")).read()
    rc, out = run(il, b, root, "install")
    assert rc == 0 and "already installed" in out and "unchanged" in out, out
    assert open(os.path.join(root, "state", "history")).read() == hist


def test_corrupt_bundle_keeps_current(il, d):
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), "20261001.0000-ca-tb-g1")
    run(il, v1, root, "install")
    v2 = make_bundle(os.path.join(d, "2"), "20261002.0000-ca-tb-g2", exe=b"MZ v2")
    with open(os.path.join(v2, EXE + ".exe"), "ab") as f:
        f.write(b"damage")
    rc, out = run(il, v2, root, "install")
    assert rc == 1 and "checksum mismatch" in out, out
    assert il.Root(root).current() == "20261001.0000-ca-tb-g1"
    assert not [n for n in os.listdir(os.path.join(root, "versions")) if n.endswith(".partial")]


def test_failed_copy_removes_partial(il, d, monkeypatch):
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), "20261001.0000-ca-tb-g1")
    run(il, v1, root, "install")
    v2 = make_bundle(os.path.join(d, "2"), "20261002.0000-ca-tb-g2", exe=b"MZ v2")
    real = il.sha256_file
    calls = {"n": 0}

    def flaky(p, *a):
        # Damage the check of the copy in versions/<v>.partial only.
        if ".partial" in p:
            calls["n"] += 1
            return "0" * 64
        return real(p, *a)

    monkeypatch.setattr(il, "sha256_file", flaky)
    rc, out = run(il, v2, root, "install")
    monkeypatch.undo()
    assert rc == 1 and calls["n"] and "copy damaged" in out, out
    assert il.Root(root).current() == "20261001.0000-ca-tb-g1"
    assert not os.path.exists(os.path.join(root, "versions", "20261002.0000-ca-tb-g2.partial"))


def test_switch_is_one_rename(il, d, monkeypatch):
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), "20261001.0000-ca-tb-g1")
    v2 = make_bundle(os.path.join(d, "2"), "20261002.0000-ca-tb-g2", exe=b"MZ v2")
    run(il, v1, root, "install")
    renames = []
    real = il.os.replace

    def spy(a, b):
        renames.append((os.path.basename(a), os.path.basename(b)))
        return real(a, b)

    monkeypatch.setattr(il.os, "replace", spy)
    run(il, v2, root, "install")
    monkeypatch.undo()
    assert [r for r in renames if r[1] == "current"] == [("current.new", "current")], renames
    # The unchanged files were hard-linked against v1.
    a = os.stat(os.path.join(root, "versions", "20261001.0000-ca-tb-g1", "launch.sh"))
    b = os.stat(os.path.join(root, "versions", "20261002.0000-ca-tb-g2", "launch.sh"))
    assert a.st_ino == b.st_ino


def test_prune_and_foreign(il, d):
    root = os.path.join(d, "r")
    vs = ["2026100%d.0000-ca-tb-g%d" % (i, i) for i in range(1, 6)]
    for i, v in enumerate(vs):
        b = make_bundle(os.path.join(d, str(i)), v, exe=("MZ %d" % i).encode())
        rc, out = run(il, b, root, "install", "--keep", "2")
        assert rc == 0, out
        if i == 0:  # a user's own dir beside the versions
            os.makedirs(os.path.join(root, "versions", "my-notes"))
    left = sorted(n for n in os.listdir(os.path.join(root, "versions")))
    # keep 2 newest (v4, v5) + current (v5) + previous (v4); v1-v3 removed.
    assert left == sorted(vs[3:] + ["my-notes"]), left
    assert "left alone: versions/my-notes" in out


def test_rollback(il, d):
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), "20261001.0000-ca-tb-g1")
    v2 = make_bundle(os.path.join(d, "2"), "20261002.0000-ca-tb-g2", exe=b"MZ v2")
    run(il, v1, root, "install")
    run(il, v2, root, "install")
    plant_user_data(root)
    rc, out = run(il, v2, root, "rollback")
    assert rc == 0 and il.Root(root).current() == "20261001.0000-ca-tb-g1", out
    rc, out = run(il, v2, root, "rollback", "20261002.0000-ca-tb-g2")
    assert rc == 0 and il.Root(root).current() == "20261002.0000-ca-tb-g2", out
    rc, out = run(il, v2, root, "rollback", "nope")
    assert rc == 1 and "not installed" in out
    rc, out = run(il, v2, root, "list")
    assert rc == 0 and "* 20261002" in out and "%s aaaaaaa" % VALUES["SOURCE_NAME"] in out, out


def test_dev_tree_refused(il, d):
    bench = os.path.join(d, "xbox-recomp")
    os.makedirs(os.path.join(bench, VALUES["SOURCE_NAME"]))
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    rc, out = run(il, b, os.path.join(bench, "Games"), "install", bench=bench)
    assert rc == 1 and "dev/bench tree" in out, out
    repo = os.path.join(d, "repo")
    os.makedirs(os.path.join(repo, ".git"))
    rc, out = run(il, b, os.path.join(repo, "inst"), "install")
    assert rc == 1 and "git checkout" in out, out


def test_desktop_entry_and_steam(il, d, monkeypatch):
    root = os.path.join(d, 'my "Games" $x', APP)
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    rc, out = run(il, b, root, "install")
    assert rc == 0, out
    with open(os.path.join(root, APP + ".desktop")) as f:
        entry = f.read().splitlines()
    assert entry[0] == "[Desktop Entry]"
    kv = dict(line.split("=", 1) for line in entry[1:])
    assert kv["Name"] == pl.PRODUCT_NAME and kv["Type"] == "Application"
    assert kv["Terminal"] == "false"
    assert kv["Icon"] == os.path.join(root, "current", "icon.png")
    # Exec: one quoted argument; ", ` and $ escaped, then backslashes doubled.
    want = '"%s"' % os.path.join(root, APP).replace('"', '\\"').replace("$", "\\$")
    assert kv["Exec"] == want.replace("\\", "\\\\"), kv["Exec"]
    # --steam hands the desktop entry (not the script) to the helper.
    bindir = os.path.join(d, "bin")
    argv_log = os.path.join(d, "helper.argv")
    make_exe(
        os.path.join(bindir, "steamos-add-to-steam"), 'printf "%%s\\n" "$@" > "%s"\n' % argv_log
    )
    make_exe(os.path.join(bindir, "pgrep"))
    monkeypatch.setenv("PATH", bindir + os.pathsep + "/usr/bin:/bin")
    monkeypatch.setenv("DISPLAY", ":0")
    rc, out = run(il, b, root, "install", "--steam")
    monkeypatch.undo()
    assert rc == 0, out
    with open(argv_log) as f:
        assert f.read().splitlines() == [os.path.join(root, APP + ".desktop")]
    rc, out = run(il, b, root, "uninstall")
    assert not os.path.exists(os.path.join(root, APP + ".desktop"))


def test_uninstall_keeps_user_data(il, d):
    root = os.path.join(d, "r")
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    run(il, b, root, "install")
    plant_user_data(root)
    os.makedirs(os.path.join(root, "prefix", "drive_c"))
    rc, out = run(il, b, root, "uninstall")
    assert rc == 0, out
    assert sorted(os.listdir(root)) == ["config", "hdd", "logs"], os.listdir(root)
    assert "Remove the %s shortcut" % pl.PRODUCT_NAME in out, out
    # Installing again into what uninstall left works; run() checks the
    # saves are untouched.
    rc, out = run(il, b, root, "install")
    assert rc == 0, out
    assert os.path.isfile(os.path.join(root, "game_files", "default.xbe"))


def test_foreign_root_refused(il, d):
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    root = os.path.join(d, "Games")
    os.makedirs(os.path.join(root, "game_files"))
    with open(os.path.join(root, "game_files", "mine.txt"), "w") as f:
        f.write("not yours")
    os.makedirs(os.path.join(root, "versions", "x"))
    for args in (("install",), ("uninstall",), ("rollback",)):
        rc, out = run(il, b, root, *args)
        assert rc == 1 and "not a %s install root" % APP in out, (args, out)
    assert os.path.isfile(os.path.join(root, "game_files", "mine.txt"))
    assert os.path.isdir(os.path.join(root, "versions", "x"))
    # An empty or absent root is fine for install.
    empty = os.path.join(d, "empty")
    os.makedirs(empty)
    assert run(il, b, empty, "install")[0] == 0


def test_import_saves(il, d):
    root = os.path.join(d, "r")
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    old = os.path.join(d, "old")
    os.makedirs(os.path.join(old, "UDATA", "4d530065"))
    with open(os.path.join(old, "UDATA", "4d530065", "s"), "w") as f:
        f.write("s")
    rc, out = run(il, b, root, "install", "--import-saves", old)
    assert rc == 0 and os.path.isfile(os.path.join(root, "hdd", "UDATA", "4d530065", "s")), out
    before = snapshot(il, root)
    rc, out = run(il, b, root, "install", "--import-saves", old)
    assert rc == 1 and "not empty" in out, out
    assert snapshot(il, root) == before


def test_tar_modes(il, d):
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    os.chmod(os.path.join(b, "launch.sh"), 0o644)  # as a Windows checkout would leave it
    out = os.path.join(d, "dist")
    os.makedirs(out)
    tar = steamos.wrap_steamos(b, "20261001.0000-ca-tb-g1", os.path.join(b, EXE + ".exe"), out)
    with tarfile.open(tar) as t:
        ms = {m.name: m for m in t.getmembers()}
        top = os.path.basename(b)
        assert all(n == top or n.startswith(top + "/") for n in ms)
        assert ms[top + "/launch.sh"].mode == 0o755 and ms[top + "/install.sh"].mode == 0o755
        assert ms[top + "/%s.exe" % EXE].mode == 0o644
        assert all(
            m.uid == 0 and m.gid == 0 and m.uname == "" and not m.issym() for m in ms.values()
        )
        assert len({m.mtime for m in ms.values()}) == 1
        t.extractall(os.path.join(d, "x"), filter="data")
    x = os.path.join(d, "x", top)
    assert os.access(os.path.join(x, "launch.sh"), os.X_OK)
    rc, o = run(il, x, os.path.join(d, "r"), "install")
    assert rc == 0, o


# ── umu-run: the search and the pinned fetch ─────────────────────────────


def umu_tar(path, body=b"#!/usr/bin/env python3\n# umu\n"):
    """A release-shaped tar: umu/umu-run and the link beside it."""
    with tarfile.open(path, "w") as t:
        ti = tarfile.TarInfo("umu")
        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
        t.addfile(ti)
        ti = tarfile.TarInfo("umu/umu-run")
        ti.size, ti.mode = len(body), 0o744
        t.addfile(ti, io.BytesIO(body))
        ti = tarfile.TarInfo("umu/umu_run.py")
        ti.type, ti.linkname = tarfile.SYMTYPE, "umu-run"
        t.addfile(ti)
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def fake_release(il, d, monkeypatch):
    """A fake download of a release tar whose sha256 is the pin; returns
    the list of URLs fetched."""
    src = os.path.join(d, "release.tar")
    monkeypatch.setattr(il, "UMU_SHA256", umu_tar(src))
    calls = []

    def fetch(url, dst):
        calls.append(url)
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())

    fetch.calls = calls
    return fetch


def test_find_umu_order(il, d, home, monkeypatch):
    assert il.find_umu() == (None, None)
    default = make_exe(os.path.join(home, ".local", "share", "xboxrecomp", "umu", "umu-run"))
    assert il.find_umu() == (default, "fetched copy")
    mine = make_exe(os.path.join(d, "mine", "umu-run"))
    assert il.find_umu(os.path.join(d, "mine")) == (mine, "fetched copy")
    local = make_exe(os.path.join(home, ".local", "bin", "umu-run"))
    assert il.find_umu(os.path.join(d, "mine")) == (local, "~/.local/bin")
    onpath = make_exe(os.path.join(d, "bin", "umu-run"))
    monkeypatch.setenv("PATH", os.path.join(d, "bin") + os.pathsep + "/usr/bin:/bin")
    assert il.find_umu() == (onpath, "PATH")
    # XDG_DATA_HOME moves the default copy.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    os.remove(local)
    os.remove(default)
    monkeypatch.setenv("XDG_DATA_HOME", os.path.join(d, "xdg"))
    xdg = make_exe(os.path.join(d, "xdg", "xboxrecomp", "umu", "umu-run"))
    assert il.find_umu() == (xdg, "fetched copy")


def test_fetch_umu(il, d, home, fake_release):
    umu_dir = os.path.join(d, "root", "umu-test")
    p = il.fetch_umu(umu_dir, fake_release)
    assert fake_release.calls == [il.UMU_URL]
    assert p == os.path.join(umu_dir, "umu-run")
    assert os.readlink(p) == os.path.join("umu-launcher-%s" % il.UMU_VERSION, "umu-run")
    assert os.access(p, os.X_OK)
    with open(p, "rb") as f:
        assert f.read() == b"#!/usr/bin/env python3\n# umu\n"
    # Only the program file: no link from the tar, no tar left behind.
    assert sorted(os.listdir(umu_dir)) == ["umu-launcher-%s" % il.UMU_VERSION, "umu-run"]
    assert os.listdir(os.path.join(umu_dir, "umu-launcher-%s" % il.UMU_VERSION)) == ["umu-run"]
    # A second fetch finds the copy and downloads nothing.
    assert il.fetch_umu(umu_dir, fake_release) == p and len(fake_release.calls) == 1
    assert il.find_umu(umu_dir) == (p, "fetched copy")


def test_fetch_umu_hash_mismatch(il, d, home, fake_release, monkeypatch):
    monkeypatch.setattr(il, "UMU_SHA256", "0" * 64)
    umu_dir = os.path.join(d, "u")
    with pytest.raises(il.InstallError, match="not the pinned"):
        il.fetch_umu(umu_dir, fake_release)
    assert os.listdir(umu_dir) == [], os.listdir(umu_dir)


def test_fetch_umu_unpinned_refused(il, d, monkeypatch):
    """A template that was never rendered fetches nothing."""
    monkeypatch.setattr(il, "UMU_URL", "@UMU_URL@")
    calls = []
    with pytest.raises(il.InstallError, match="no umu-launcher pin"):
        il.fetch_umu(os.path.join(d, "u"), lambda u, p: calls.append(u))
    assert calls == []


def test_fetch_umu_failed_download(il, d, home):
    def broken(url, dst):
        with open(dst, "wb") as f:
            f.write(b"half")
        raise OSError("network is unreachable")

    umu_dir = os.path.join(d, "u")
    with pytest.raises(il.InstallError, match="download failed"):
        il.fetch_umu(umu_dir, broken)
    assert os.listdir(umu_dir) == []


def test_ensure_umu(il, d, home, fake_release):
    umu_dir = os.path.join(d, "u")
    # None found, the user says no (or there is no terminal): the error
    # names --fetch-umu, and nothing is downloaded.
    with pytest.raises(il.InstallError, match="--fetch-umu"):
        il.ensure_umu(False, umu_dir, fake_release, ask=lambda q: False)
    assert fake_release.calls == []
    asked = []

    def yes(q):
        asked.append(q)
        return True

    p = il.ensure_umu(False, umu_dir, fake_release, ask=yes)
    assert p == os.path.join(umu_dir, "umu-run") and il.UMU_VERSION in asked[0]
    # Found now: no question, no download.
    assert il.ensure_umu(False, umu_dir, fake_release, ask=None) == p
    assert len(fake_release.calls) == 1
    # A ~/.local/bin one wins the search, but --fetch-umu still installs
    # (and uses) the pinned copy.
    make_exe(os.path.join(home, ".local", "bin", "umu-run"))
    other = os.path.join(d, "u2")
    assert il.ensure_umu(True, other, fake_release) == os.path.join(other, "umu-run")


def test_install_fetch_umu(il, d, home, fake_release, monkeypatch):
    """--fetch-umu through main, into a dir inside the test root."""
    monkeypatch.setattr(il, "download", fake_release)
    b = make_bundle(d, "20261001.0000-ca-tb-g1")
    root = os.path.join(d, "r")
    umu_dir = os.path.join(root, "umu-test")
    rc, out = run(il, b, root, "install", "--fetch-umu", "--umu-dir", umu_dir, umu=None)
    assert rc == 0, out
    with open(os.path.join(root, "state", "umu-run")) as f:
        assert f.read() == os.path.join(umu_dir, "umu-run") + "\n"
    # Not a tty: without --fetch-umu and no umu-run anywhere, install
    # refuses before it writes anything.
    root2 = os.path.join(d, "r2")
    monkeypatch.setattr(il.sys, "stdin", io.StringIO(""))
    rc, out = run(il, b, root2, "install", umu=None)
    assert rc == 1 and "--fetch-umu" in out and not os.path.exists(root2), out
    rc, out = run(il, b, root, "status", umu=None)
    assert rc == 0 and "umu-run:  %s/umu-run (recorded by install)" % umu_dir in out, out


# ── launch.sh ────────────────────────────────────────────────────────────


def fake_install(d, home):
    """<root>/versions/v/ with the rendered launch.sh, and the game files."""
    root = os.path.join(d, "Games", APP)
    v = os.path.join(root, "versions", "v")
    os.makedirs(v)
    render("launch.sh", os.path.join(v, "launch.sh"), 0o755)
    with open(os.path.join(v, "launch.env.default"), "w") as f:
        f.write("RECOMP_KEYBOARD=1\n")
    with open(os.path.join(v, "enhance.toml.default"), "w") as f:
        f.write("[render]\n")
    os.makedirs(os.path.join(root, "game_files"))
    open(os.path.join(root, "game_files", "default.xbe"), "w").close()
    return root, os.path.join(v, "launch.sh")


# The fake umu-run: records its argv and PATH, then plays the game: after
# a moment it writes the game's log (RECOMP_STDIO_LOG, a Z: path) and exits.
FAKE_UMU = """echo "$0 $*" > "$HOME/umu.argv"
sleep "${FAKE_DELAY:-0}"
echo started > "${RECOMP_STDIO_LOG#Z:}"
sleep "${FAKE_DELAY:-0}"
exit "${FAKE_RC:-0}"
"""


def launch(launcher, home, path, **env):
    e = {
        "HOME": home,
        "PATH": path,
        "LANG": "C",
    }
    e.update(env)
    return subprocess.run(
        [BASH, launcher], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60
    )


def test_launch_finds_umu_in_local_bin(d, home):
    """Game Mode's PATH has no ~/.local/bin: the launcher looks there."""
    root, launcher = fake_install(d, home)
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), FAKE_UMU)
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 0, r.stdout
    with open(os.path.join(home, "umu.argv")) as f:
        argv = f.read()
    assert argv.startswith(os.path.join(home, ".local", "bin", "umu-run") + " "), argv
    assert argv.rstrip().endswith("versions/v/%s.exe" % EXE), argv


def test_launch_umu_search(d, home):
    root, launcher = fake_install(d, home)
    # Nothing anywhere: a clear failure naming the fix.
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 1 and b"--fetch-umu" in r.stdout, r.stdout
    # The copy install.sh recorded (a --umu-dir).
    rec = make_exe(os.path.join(d, "elsewhere", "umu-run"), FAKE_UMU)
    os.makedirs(os.path.join(root, "state"))
    with open(os.path.join(root, "state", "umu-run"), "w") as f:
        f.write(rec + "\n")
    assert launch(launcher, home, "/usr/bin:/bin").returncode == 0
    with open(os.path.join(home, "umu.argv")) as f:
        assert f.read().startswith(rec + " ")
    # The per-user default copy, when nothing is recorded.
    os.remove(os.path.join(root, "state", "umu-run"))
    dflt = make_exe(os.path.join(home, ".local", "share", "xboxrecomp", "umu", "umu-run"), FAKE_UMU)
    assert launch(launcher, home, "/usr/bin:/bin").returncode == 0
    with open(os.path.join(home, "umu.argv")) as f:
        assert f.read().startswith(dflt + " ")
    # UMU_RUN in config/launch.env wins over all of them.
    mine = make_exe(os.path.join(d, "mine", "umu-run"), FAKE_UMU)
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("UMU_RUN=%s\n" % mine)
    assert launch(launcher, home, "/usr/bin:/bin").returncode == 0
    with open(os.path.join(home, "umu.argv")) as f:
        assert f.read().startswith(mine + " ")


# The fake zenity: records its arguments and every line it reads, and when
# its input ends (the launcher's notice is over).
FAKE_ZENITY = """echo "$*" > "$HOME/zenity.argv"
while IFS= read -r line; do echo "$line" >> "$HOME/zenity.lines"; done
echo closed > "$HOME/zenity.closed"
"""


def test_first_launch_notice(d, home):
    root, launcher = fake_install(d, home)
    bindir = os.path.join(d, "bin")
    make_exe(os.path.join(bindir, "umu-run"), FAKE_UMU)
    make_exe(os.path.join(bindir, "zenity"), FAKE_ZENITY)
    path = bindir + ":/usr/bin:/bin"
    t = time.monotonic()
    r = launch(launcher, home, path, DISPLAY=":0", FAKE_DELAY="2", FAKE_RC="3")
    assert r.returncode == 3, r.stdout  # the game's exit status
    assert time.monotonic() - t >= 4
    with open(os.path.join(home, "zenity.argv")) as f:
        argv = f.read()
    assert "--progress" in argv and "--auto-close" in argv and pl.PRODUCT_NAME in argv, argv
    with open(os.path.join(home, "zenity.lines")) as f:
        lines = f.read()
    assert "# Downloading the Steam Linux Runtime ..." in lines, lines
    # The notice closed once the game wrote its log, before the game ended.
    for _ in range(50):
        if os.path.exists(os.path.join(home, "zenity.closed")):
            break
        time.sleep(0.1)
    assert os.path.exists(os.path.join(home, "zenity.closed"))


def test_no_notice_once_set_up(d, home):
    """Runtime, Proton and prefix there: no notice, the launcher is umu-run
    (exec), as before."""
    root, launcher = fake_install(d, home)
    os.makedirs(os.path.join(home, ".local", "share", "umu", "steamrt3"))
    os.makedirs(
        os.path.join(home, ".local", "share", "Steam", "compatibilitytools.d", "GE-Proton10-1")
    )
    os.makedirs(os.path.join(root, "prefix"))
    open(os.path.join(root, "prefix", "system.reg"), "w").close()
    bindir = os.path.join(d, "bin")
    make_exe(os.path.join(bindir, "umu-run"), FAKE_UMU)
    make_exe(os.path.join(bindir, "zenity"), FAKE_ZENITY)
    r = launch(launcher, home, bindir + ":/usr/bin:/bin", DISPLAY=":0", FAKE_RC="5")
    assert r.returncode == 5, r.stdout
    assert not os.path.exists(os.path.join(home, "zenity.argv"))
    # Not set up, but LAUNCH_NOTICE=0, or no display (ssh): no notice either.
    os.remove(os.path.join(root, "prefix", "system.reg"))
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("LAUNCH_NOTICE=0\n")
    assert launch(launcher, home, bindir + ":/usr/bin:/bin", DISPLAY=":0").returncode == 0
    os.remove(os.path.join(root, "config", "launch.env"))
    assert launch(launcher, home, bindir + ":/usr/bin:/bin").returncode == 0
    assert not os.path.exists(os.path.join(home, "zenity.argv"))


def test_notice_notification_without_window_tools(d, home):
    """No zenity or kdialog: a notification instead."""
    root, launcher = fake_install(d, home)
    bindir = os.path.join(d, "bin")
    make_exe(os.path.join(bindir, "umu-run"), FAKE_UMU)
    make_exe(os.path.join(bindir, "notify-send"), 'echo "$*" > "$HOME/notify.argv"\n')
    # Only what the launcher needs from /usr/bin and /bin, without the
    # host's own zenity or kdialog.
    tools = os.path.join(d, "tools")
    os.makedirs(tools)
    for t in ("date", "ls", "tail", "rm", "mkdir", "cp", "cmp", "sleep", "dirname", "cat"):
        for base in ("/usr/bin", "/bin"):
            if os.path.exists(os.path.join(base, t)):
                os.symlink(os.path.join(base, t), os.path.join(tools, t))
                break
    r = launch(launcher, home, bindir + ":" + tools, WAYLAND_DISPLAY="wayland-0")
    assert r.returncode == 0, r.stdout
    with open(os.path.join(home, "notify.argv")) as f:
        assert "First launch" in f.read()


def test_launch_caps_old_logs(d, home):
    """A log an earlier launch left over LOG_MAX_MB is cut to its first and
    last halves; smaller ones, and LOG_MAX_MB=0, leave them whole. With
    PROTON_LOG set, Proton's log goes to logs/, not the home folder."""
    root, launcher = fake_install(d, home)
    logs = os.path.join(root, "logs")
    os.makedirs(logs)
    mib = 1 << 20
    big = b"".join(b"%07d\n" % i for i in range(3 * mib // 8))  # 3 MiB
    for name, body in (("steam-default.log", big), ("umu.log", b"small\n")):
        with open(os.path.join(logs, name), "wb") as f:
            f.write(body)
    os.makedirs(os.path.join(root, "config"))
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("LOG_MAX_MB=2\nPROTON_LOG=1\n")
    env_dump = 'echo "$PROTON_LOG_DIR" > "$HOME/proton_log_dir"\n'
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), env_dump + FAKE_UMU)
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 0, r.stdout
    with open(os.path.join(logs, "steam-default.log"), "rb") as f:
        got = f.read()
    cut = b"\n[launcher: %d bytes cut here (LOG_MAX_MB)]\n" % (len(big) - 2 * mib)
    assert got == big[:mib] + cut + big[-mib:], len(got)
    assert not os.path.exists(os.path.join(logs, "steam-default.log.cap"))
    with open(os.path.join(home, "proton_log_dir")) as f:
        assert f.read().strip() == logs
    # LOG_MAX_MB=0: nothing cut.
    with open(os.path.join(logs, "steam-default.log"), "wb") as f:
        f.write(big)
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("LOG_MAX_MB=0\n")
    assert launch(launcher, home, "/usr/bin:/bin").returncode == 0
    with open(os.path.join(logs, "steam-default.log"), "rb") as f:
        assert f.read() == big
