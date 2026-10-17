"""Tests for the steamos templates (templates/steamos/): the installer
(install_lib.py) on fake bundles, rendered with the test game's values as
`package steamos` renders them; the umu-run search and the pinned umu-run
and Proton fetches with a fake download; and launch.sh with a fake umu-run,
zenity, flock and HOME (no game data, no Proton, no network).

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


def make_bundle(base, version, exe=b"MZ program", game=None, extra=None):
    """A payload like `package steamos` stages, with a real manifest;
    extra: more program files by relative path (enhance/hud.pack)."""
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
    files.update(extra or {})
    for n, data in files.items():
        os.makedirs(os.path.dirname(os.path.join(b, n)), exist_ok=True)
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
        ("config/config.toml", b"[render]\n"),
        ("config/launch.env", b"LOG_KEEP=3\n"),
        ("logs/game-1.log", b"log"),
    ):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


def found_umu(want_fetch, umu_dir):
    return "/opt/umu/umu-run"


def found_proton():
    return "/opt/compat/GE-Proton"


def run(il, bundle, root, *args, bench="/nonexistent-bench", umu=found_umu, proton=found_proton):
    """install_lib.main with the host check off and umu-run and Proton
    stubbed; returns (exit, output) and asserts the user data is untouched."""
    before = snapshot(il, root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = il.main(
            list(args) + ["--root", root, "--bundle", bundle],
            host_check=False,
            bench=bench,
            umu=umu,
            proton=proton,
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


def test_damaged_bundle_fetches_no_umu(il, d):
    """The bundle is verified before umu-run is looked for, so a damaged one
    never costs the umu-launcher download."""
    calls = []

    def umu(want_fetch, umu_dir):
        calls.append(want_fetch)
        return "/opt/umu/umu-run"

    b = make_bundle(os.path.join(d, "b"), "20261001.0000-ca-tb-g1")
    with open(os.path.join(b, EXE + ".exe"), "ab") as f:
        f.write(b"damage")
    rc, out = run(il, b, os.path.join(d, "r"), "install", "--fetch-umu", umu=umu)
    assert rc == 1 and "checksum mismatch" in out, out
    assert calls == []


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


# ── program subdirectories (enhance/hud.pack) ───────────────────────────

PACK = "enhance/hud.pack"
V1, V2, V3 = "20261001.0000-ca-tb-g1", "20261002.0000-ca-tb-g2", "20261003.0000-ca-tb-g3"


def read(path):
    with open(path, "rb") as f:
        return f.read()


def test_subdir_installed(il, d):
    """A --hud-pack bundle's enhance/hud.pack lands in versions/<v>/enhance/,
    where config.default.toml's relative key finds it; .DS_Store does not."""
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    with open(os.path.join(b, "enhance", ".DS_Store"), "wb") as f:
        f.write(b"finder, after unpacking")
    root = os.path.join(d, "r")
    rc, out = run(il, b, root, "install")
    assert rc == 0, out
    assert read(os.path.join(root, "current", PACK)) == b"pack 1"
    assert not os.path.exists(os.path.join(root, "current", "enhance", ".DS_Store"))
    assert not os.path.exists(os.path.join(root, "current", "game_files"))
    names = il.Bundle(b).program_files()
    assert PACK in names and not [n for n in names if n.startswith("game_files/")]


def test_subdir_tampered(il, d):
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    with open(os.path.join(b, PACK), "ab") as f:
        f.write(b"damage")
    root = os.path.join(d, "r")
    rc, out = run(il, b, root, "install")
    assert rc == 1 and "(checksum mismatch): %s" % PACK in out, out
    assert not os.path.exists(os.path.join(root, "versions", V1))


def test_subdir_copy_checked(il, d, monkeypatch):
    """The copy in versions/<v>.partial is checked file by file, the
    subdirectory's too."""
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    real = il.sha256_file

    def flaky(p, *a):
        return "0" * 64 if p.endswith(".partial/" + PACK) else real(p, *a)

    monkeypatch.setattr(il, "sha256_file", flaky)
    rc, out = run(il, b, os.path.join(d, "r"), "install")
    assert rc == 1 and "copy damaged (checksum mismatch): %s" % PACK in out, out


def test_subdir_unlisted_skipped(il, d):
    """Only what SHA256SUMS lists is taken from a subdirectory: a
    __pycache__/ or a stray file in the unpacked bundle stays there."""
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    for rel in ("enhance/extra.bin", "__pycache__/install_lib.cpython-314.pyc"):
        os.makedirs(os.path.dirname(os.path.join(b, rel)), exist_ok=True)
        with open(os.path.join(b, rel), "wb") as f:
            f.write(b"not in the sums")
    root = os.path.join(d, "r")
    rc, out = run(il, b, root, "install")
    assert rc == 0, out
    assert os.listdir(os.path.join(root, "current", "enhance")) == ["hud.pack"]
    assert not os.path.exists(os.path.join(root, "current", "__pycache__"))


def test_subdir_symlink_refused(il, d):
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    root = os.path.join(d, "r")
    # A listed file as a link (verify() follows it and passes).
    shutil.move(os.path.join(b, PACK), os.path.join(d, "elsewhere.pack"))
    os.symlink(os.path.join(d, "elsewhere.pack"), os.path.join(b, PACK))
    rc, out = run(il, b, root, "install")
    assert rc == 1 and "symlink in the bundle: %s" % PACK in out, out
    # The whole directory as a link: os.walk would not enter it.
    shutil.rmtree(os.path.join(b, "enhance"))
    os.makedirs(os.path.join(d, "enh"))
    shutil.move(os.path.join(d, "elsewhere.pack"), os.path.join(d, "enh", "hud.pack"))
    os.symlink(os.path.join(d, "enh"), os.path.join(b, "enhance"))
    rc, out = run(il, b, root, "install")
    assert rc == 1 and "symlink in the bundle: enhance" in out, out
    assert not os.path.exists(os.path.join(root, "versions", V1))


def test_subdir_upgrade_rollback_prune(il, d):
    """Each version keeps its own pack: an unchanged one is hard-linked, a
    new one copied, a version without one has no enhance/; rollback finds
    the old one in place, and prune takes it with its version."""
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), V1, extra={PACK: b"pack 1"})
    v2 = make_bundle(os.path.join(d, "2"), V2, exe=b"MZ v2", extra={PACK: b"pack 1"})
    v3 = make_bundle(os.path.join(d, "3"), V3, exe=b"MZ v3", extra={PACK: b"pack 3"})
    for b in (v1, v2):
        rc, out = run(il, b, root, "install")
        assert rc == 0, out
    vp = lambda v: os.path.join(root, "versions", v, PACK)  # noqa: E731
    assert os.stat(vp(V1)).st_ino == os.stat(vp(V2)).st_ino
    rc, out = run(il, v3, root, "install", "--keep", "1")
    assert rc == 0, out
    assert read(vp(V3)) == b"pack 3" and read(vp(V2)) == b"pack 1"
    assert not os.path.exists(os.path.join(root, "versions", V1)), "pruned with its pack"
    rc, out = run(il, v3, root, "rollback")
    assert rc == 0 and read(os.path.join(root, "current", PACK)) == b"pack 1", out
    v4 = make_bundle(os.path.join(d, "4"), "20261004.0000-ca-tb-g4", exe=b"MZ v4")
    rc, out = run(il, v4, root, "install")
    assert rc == 0, out
    assert not os.path.exists(os.path.join(root, "current", "enhance"))


def test_subdir_repairs_older_install(il, d):
    """A version dir with the same SHA256SUMS but a program file missing
    (or truncated) is not taken as installed: the install repairs it."""
    root = os.path.join(d, "r")
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    run(il, b, root, "install")
    shutil.rmtree(os.path.join(root, "versions", V1, "enhance"))
    rc, out = run(il, b, root, "install")
    assert rc == 0 and "installed versions/%s" % V1 in out, out
    assert read(os.path.join(root, "current", PACK)) == b"pack 1"
    rc, out = run(il, b, root, "install")
    assert rc == 0 and "already installed" in out, out


def test_subdir_repairs_truncated(il, d):
    root = os.path.join(d, "r")
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    run(il, b, root, "install")
    p = os.path.join(root, "versions", V1, PACK)
    os.remove(p)  # a fresh inode: the bundle's copy is not touched
    with open(p, "wb") as f:
        f.write(b"pack")
    rc, out = run(il, b, root, "install")
    assert rc == 0 and "installed versions/%s" % V1 in out, out
    assert read(os.path.join(root, "current", PACK)) == b"pack 1"


def test_reinstall_current_never_dangles(il, d, monkeypatch):
    """Reinstalling the current version renames the old dir aside before
    the new one goes in: current names a dir at every rename."""
    root = os.path.join(d, "r")
    b = make_bundle(d, V1, extra={PACK: b"pack 1"})
    run(il, b, root, "install")
    shutil.rmtree(os.path.join(root, "versions", V1, "enhance"))
    real_rename, real_rmtree = il.os.rename, il.shutil.rmtree
    seen = []

    def check(what):
        def f(a, *rest, **kw):
            if os.path.basename(str(a)) in (V1, V1 + ".old"):
                seen.append((what, os.path.isdir(os.path.join(root, "current"))))
            return (real_rename if what == "rename" else real_rmtree)(a, *rest, **kw)

        return f

    monkeypatch.setattr(il.os, "rename", check("rename"))
    monkeypatch.setattr(il.shutil, "rmtree", check("rmtree"))
    rc, out = run(il, b, root, "install")
    monkeypatch.undo()
    assert rc == 0, out
    assert ("rmtree", True) in seen and all(ok for what, ok in seen if what == "rmtree"), seen
    assert not os.path.exists(os.path.join(root, "versions", V1 + ".old"))
    assert read(os.path.join(root, "current", PACK)) == b"pack 1"


def test_sums_path_traversal_refused(il, d):
    """A SHA256SUMS line naming a path outside the bundle (../x, /x) is
    never installed: the install stops before versions/<v> exists."""
    root = os.path.join(d, "r")
    outside = os.path.join(d, "outside")
    with open(outside, "wb") as f:
        f.write(b"outside the bundle")
    digest = hashlib.sha256(b"outside the bundle").hexdigest()
    for name in ("../outside", outside):
        b = make_bundle(os.path.join(d, "b" + str(len(name))), V1, extra={PACK: b"pack 1"})
        # ../outside exists beside the bundle, with the digest it is listed with.
        shutil.copy2(outside, os.path.join(os.path.dirname(b), "outside"))
        with open(os.path.join(b, "SHA256SUMS"), "a") as f:
            f.write("%s  %s\n" % (digest, name))
        rc, out = run(il, b, root, "install")
        assert rc == 1 and "bundle file missing: %s" % name in out, out
        assert not os.path.exists(os.path.join(root, "versions", V1))


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


# ── Proton: the pinned fetch ─────────────────────────────────────────────


def proton_tar(path, top=None, extra=()):
    """A release-shaped tar.gz: <top>/proton, a file, a relative link and a
    hard link inside the tree, plus any extra (TarInfo, data) members."""
    top = top or VALUES["PROTON_DIR"]
    with tarfile.open(path, "w:gz") as t:

        def add(name, data=None, **kw):
            ti = tarfile.TarInfo(name)
            for k, v in kw.items():
                setattr(ti, k, v)
            if data is not None:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            else:
                t.addfile(ti)

        add(top, type=tarfile.DIRTYPE, mode=0o755)
        add(top + "/proton", b"#!/usr/bin/env python3\n", mode=0o755)
        add(top + "/files", type=tarfile.DIRTYPE, mode=0o755)
        add(top + "/files/lib.so", b"lib", mode=0o644)
        add(top + "/files/lib.so.1", type=tarfile.SYMTYPE, linkname="lib.so")
        add(top + "/files/lib.hard", type=tarfile.LNKTYPE, linkname=top + "/files/lib.so")
        for ti, data in extra:
            if data is None:
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def proton_release(il, d, monkeypatch):
    """make(extra=()) -> a fake download of a Proton tar.gz whose sha256 is
    the pin; fetch.calls lists the URLs fetched."""

    def make(extra=(), top=None):
        src = os.path.join(d, "proton.tar.gz")
        monkeypatch.setattr(il, "PROTON_SHA256", proton_tar(src, top, extra))
        calls = []

        def fetch(url, dst):
            calls.append(url)
            shutil.copyfile(src, dst)

        fetch.calls = calls
        return fetch

    return make


def compat_left(compat):
    return sorted(os.listdir(compat)) if os.path.isdir(compat) else []


def test_fetch_proton(il, d, home, proton_release):
    fetch = proton_release()
    compat = il.compat_dir()
    assert compat == os.path.join(home, ".local", "share", "Steam", "compatibilitytools.d")
    got = il.ensure_proton(fetch=fetch)
    tree = os.path.join(compat, VALUES["PROTON_DIR"])
    assert got == tree and fetch.calls == [VALUES["PROTON_URL"]]
    assert os.access(os.path.join(tree, "proton"), os.X_OK)
    assert os.readlink(os.path.join(tree, "files", "lib.so.1")) == "lib.so"
    with open(os.path.join(tree, "files", "lib.hard"), "rb") as f:
        assert f.read() == b"lib"
    # Only the tree: no tarball or partial tree left beside it.
    assert compat_left(compat) == [VALUES["PROTON_DIR"]]
    # There already (an earlier install, umu-run or ProtonUp-Qt): no download.
    assert il.ensure_proton(fetch=fetch) == tree and len(fetch.calls) == 1


def test_fetch_proton_replaces_dangling_link(il, d, home, proton_release):
    """A link left where the pinned tree goes (a removed ProtonUp-Qt
    folder, say) is replaced by the tree; its target is not touched."""
    compat = il.compat_dir()
    os.makedirs(compat)
    os.symlink(os.path.join(d, "gone"), os.path.join(compat, VALUES["PROTON_DIR"]))
    tree = il.ensure_proton(fetch=proton_release())
    assert not os.path.islink(tree) and os.path.isfile(os.path.join(tree, "proton"))


def test_fetch_proton_keeps_others(il, d, home, proton_release):
    """compatibilitytools.d is Steam's: other releases are never touched."""
    other = make_exe(os.path.join(il.compat_dir(), "GE-Proton10-1", "proton"))
    il.ensure_proton(fetch=proton_release())
    assert os.path.isfile(other)


def test_fetch_proton_hash_mismatch(il, d, home, proton_release, monkeypatch):
    fetch = proton_release()
    monkeypatch.setattr(il, "PROTON_SHA256", "0" * 64)
    with pytest.raises(il.InstallError, match="not the pinned"):
        il.ensure_proton(fetch=fetch)
    assert compat_left(il.compat_dir()) == []


def test_fetch_proton_unpinned_refused(il, d, home, monkeypatch):
    monkeypatch.setattr(il, "PROTON_SHA256", "@PROTON_SHA256@")
    with pytest.raises(il.InstallError, match="no Proton pin"):
        il.fetch_proton(il.compat_dir(), fetch=lambda u, p: pytest.fail("fetched"))


def test_fetch_proton_failed_download(il, d, home):
    def fetch(url, dst):
        open(dst, "w").close()
        raise OSError("network down")

    with pytest.raises(il.InstallError, match="download failed"):
        il.ensure_proton(fetch=fetch)
    assert compat_left(il.compat_dir()) == []


def _ti(name, **kw):
    ti = tarfile.TarInfo(name)
    for k, v in kw.items():
        setattr(ti, k, v)
    return ti


@pytest.mark.parametrize(
    "member",
    [
        (_ti("../evil"), b"x"),
        (_ti("/abs"), b"x"),
        (_ti("Other-Proton/x"), b"x"),
        (_ti("%s/../../x" % VALUES["PROTON_DIR"]), b"x"),
        (_ti("%s/out" % VALUES["PROTON_DIR"], type=tarfile.SYMTYPE, linkname="../../etc"), None),
        (_ti("%s/abs" % VALUES["PROTON_DIR"], type=tarfile.SYMTYPE, linkname="/etc/passwd"), None),
        (_ti("%s/hard" % VALUES["PROTON_DIR"], type=tarfile.LNKTYPE, linkname="etc/passwd"), None),
        (_ti("%s/dev" % VALUES["PROTON_DIR"], type=tarfile.CHRTYPE), None),
    ],
    ids=[
        "dotdot",
        "absolute",
        "other-top",
        "inner-dotdot",
        "link-out",
        "link-abs",
        "hard-out",
        "dev",
    ],
)
def test_fetch_proton_unsafe_members(il, d, home, proton_release, member):
    fetch = proton_release(extra=[member])
    with pytest.raises(il.InstallError):
        il.ensure_proton(fetch=fetch)
    assert compat_left(il.compat_dir()) == []
    assert not os.path.exists(os.path.join(d, "evil")) and not os.path.exists(os.path.join(d, "x"))


def test_fetch_proton_wrong_top(il, d, home, proton_release):
    """A pin whose tarball unpacks under another name is refused whole."""
    fetch = proton_release(top="GE-Proton9-1")
    with pytest.raises(il.InstallError):
        il.ensure_proton(fetch=fetch)
    assert compat_left(il.compat_dir()) == []


def test_install_proton_and_runtime_mark(il, d, home):
    """install ensures the pin (unless --no-proton) and marks the next
    launch for a runtime update; rollback marks it too; status names the
    pinned Proton."""
    root = os.path.join(d, "r")
    v1 = make_bundle(os.path.join(d, "1"), "20261001.0000-ca-tb-g1")
    v2 = make_bundle(os.path.join(d, "2"), "20261002.0000-ca-tb-g2", exe=b"MZ v2")
    calls = []
    rc, out = run(il, v1, root, "install", proton=lambda: calls.append(1))
    assert rc == 0 and calls == [1], out
    mark = os.path.join(root, "state", "runtime-update")
    assert os.path.isfile(mark)
    os.remove(mark)
    rc, out = run(il, v2, root, "install", "--no-proton", proton=lambda: calls.append(2))
    assert rc == 0 and calls == [1] and "--no-proton" in out, out
    assert os.path.isfile(mark)
    os.remove(mark)
    rc, out = run(il, v2, root, "rollback")
    assert rc == 0 and os.path.isfile(mark), out
    rc, out = run(il, v2, root, "status")
    assert "proton:   %s MISSING" % VALUES["PROTON_VERSION"] in out, out
    make_exe(os.path.join(pinned_proton(home), "proton"))
    rc, out = run(il, v2, root, "status")
    assert "proton:   %s (%s)" % (pinned_proton(home), VALUES["PROTON_VERSION"]) in out, out
    with pytest.raises(SystemExit):
        run(il, v2, root, "status", "--no-proton")


# ── launch.sh ────────────────────────────────────────────────────────────


def fake_install(d, home):
    """<root>/versions/v/ with the rendered launch.sh, and the game files."""
    root = os.path.join(d, "Games", APP)
    v = os.path.join(root, "versions", "v")
    os.makedirs(v)
    render("launch.sh", os.path.join(v, "launch.sh"), 0o755)
    with open(os.path.join(v, "launch.env.default"), "w") as f:
        f.write("RECOMP_KEYBOARD=1\n")
    with open(os.path.join(v, "config.default.toml"), "w") as f:
        f.write("[render]\n")
    os.makedirs(os.path.join(root, "game_files"))
    open(os.path.join(root, "game_files", "default.xbe"), "w").close()
    make_exe(os.path.join(pinned_proton(home), "proton"))
    return root, os.path.join(v, "launch.sh")


def pinned_proton(home):
    return os.path.join(
        home, ".local", "share", "Steam", "compatibilitytools.d", VALUES["PROTON_DIR"]
    )


# The fake umu-run: records its argv and the gamescope hint, then plays the
# game: after a moment it writes the game's log (RECOMP_STDIO_LOG, a Z:
# path) and exits.
FAKE_UMU = """echo "$0 $*" > "$HOME/umu.argv"
echo "${RECOMP_HOST_GAMESCOPE:-unset}" > "$HOME/umu.env"
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
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
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


def test_launch_gamescope_hint(d, home):
    """Under gamescope (Game Mode sets XDG_CURRENT_DESKTOP, a nested one
    GAMESCOPE_WAYLAND_DISPLAY) the launcher tells the game, so
    present.surface=auto hands gamescope the frame; a config/launch.env
    line wins."""
    root, launcher = fake_install(d, home)
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), FAKE_UMU)

    def hint(**env):
        r = launch(launcher, home, "/usr/bin:/bin", **env)
        assert r.returncode == 0, r.stdout
        with open(os.path.join(home, "umu.env")) as f:
            return f.read().strip()

    assert hint(XDG_CURRENT_DESKTOP="gamescope") == "1"
    assert hint(GAMESCOPE_WAYLAND_DISPLAY="gamescope-0") == "1"
    assert hint() == "unset"
    assert hint(XDG_CURRENT_DESKTOP="KDE") == "unset"
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("RECOMP_HOST_GAMESCOPE=0\n")
    assert hint(XDG_CURRENT_DESKTOP="gamescope") == "0"


def test_launch_config_layering(d, home):
    """The launcher sets RECOMP_CONFIG to the player's config.toml, removes
    an earlier version's config/enhance.toml.default, and leaves the rest
    of config/ to the game: an old enhance.toml is renamed by the game
    itself, and launch.env is never touched."""
    root, launcher = fake_install(d, home)
    umu = FAKE_UMU.replace(
        'echo "$0 $*"', 'echo "$RECOMP_CONFIG" > "$HOME/umu.config"; echo "$0 $*"'
    )
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), umu)
    cfg = os.path.join(root, "config")
    os.makedirs(cfg)
    old = {"enhance.toml": b"[render]\nscale = 2\n", "launch.env": b"LOG_KEEP=3\n"}
    for n, data in old.items():
        with open(os.path.join(cfg, n), "wb") as f:
            f.write(data)
    with open(os.path.join(cfg, "enhance.toml.default"), "w") as f:
        f.write("[render]\nscale = 1\n")
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 0, r.stdout
    with open(os.path.join(home, "umu.config")) as f:
        assert f.read().strip() == "Z:%s/config/config.toml" % root
    assert not os.path.exists(os.path.join(cfg, "enhance.toml.default"))
    assert not os.path.exists(os.path.join(cfg, "config.toml"))
    for n, data in old.items():
        with open(os.path.join(cfg, n), "rb") as f:
            assert f.read() == data, n
    with open(launcher) as f:
        text = f.read()
    assert "RECOMP_ENHANCE_CONFIG" not in text and 'cp "$HERE' not in text


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
    """Runtime, Proton and prefix there: no notice."""
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
        assert "Setting up Proton" in f.read()


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


# ── launch.sh: the pinned Proton, the runtime, one launch at a time ──────

# Records what umu-run would see: PROTONPATH, UMU_RUNTIME_UPDATE and
# whether the launcher's lock (fd 9) reached it.
FAKE_UMU_ENV = """echo "${PROTONPATH:-unset}" > "$HOME/umu.protonpath"
echo "${UMU_RUNTIME_UPDATE-unset}" > "$HOME/umu.runtime"
if [ -e /dev/fd/9 ]; then echo open; else echo closed; fi > "$HOME/umu.fd9"
echo x >> "$HOME/umu.count"
"""


def read_text(path):
    with open(path) as f:
        return f.read().strip()


def test_launch_pinned_proton(d, home):
    root, launcher = fake_install(d, home)
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), FAKE_UMU_ENV + FAKE_UMU)
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 0, r.stdout
    assert read_text(os.path.join(home, "umu.protonpath")) == pinned_proton(home)
    # Removed: a clear failure naming install.sh, and umu-run never starts.
    os.remove(os.path.join(home, "umu.protonpath"))
    shutil.rmtree(pinned_proton(home))
    r = launch(launcher, home, "/usr/bin:/bin")
    assert r.returncode == 1 and b"install.sh" in r.stdout, r.stdout
    assert VALUES["PROTON_VERSION"].encode() in r.stdout
    assert not os.path.exists(os.path.join(home, "umu.protonpath"))
    # PROTONPATH=GE-Proton in config/launch.env: the explicit opt-in to the
    # newest release, passed to umu-run as is.
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("PROTONPATH=GE-Proton\n")
    assert launch(launcher, home, "/usr/bin:/bin").returncode == 0
    assert read_text(os.path.join(home, "umu.protonpath")) == "GE-Proton"


def test_launch_default_protonpath_ignored(d, home):
    """A game shares launch.env.default with its Windows installer, whose
    PROTONPATH=GE-Proton line would undo the pin: the launcher drops it. The
    environment's PROTONPATH and config/launch.env's still win."""
    root, launcher = fake_install(d, home)
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), FAKE_UMU_ENV + FAKE_UMU)
    with open(os.path.join(root, "versions", "v", "launch.env.default"), "a") as f:
        f.write("PROTONPATH=GE-Proton\n")

    def protonpath(**env):
        r = launch(launcher, home, "/usr/bin:/bin", **env)
        assert r.returncode == 0, r.stdout
        return read_text(os.path.join(home, "umu.protonpath"))

    assert protonpath() == pinned_proton(home)
    assert protonpath(PROTONPATH="/opt/my-proton") == "/opt/my-proton"
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("PROTONPATH=GE-Proton\n")
    assert protonpath() == "GE-Proton"


def test_launch_runtime_update(d, home):
    """No runtime: umu-run downloads it (no UMU_RUNTIME_UPDATE). Installed:
    UMU_RUNTIME_UPDATE=0, except once after an install or rollback
    (state/runtime-update). The player's value wins."""
    root, launcher = fake_install(d, home)
    make_exe(os.path.join(home, ".local", "bin", "umu-run"), FAKE_UMU_ENV + FAKE_UMU)

    def runtime():
        r = launch(launcher, home, "/usr/bin:/bin")
        assert r.returncode == 0, r.stdout
        return read_text(os.path.join(home, "umu.runtime"))

    assert runtime() == "unset"
    # No runtime yet, the mark set (the first launch after an install): the
    # download is the newest runtime, so the mark goes, and the next launch
    # neither checks again nor shows the notice for it.
    mark = os.path.join(root, "state", "runtime-update")
    open(mark, "w").close()
    assert runtime() == "unset" and not os.path.exists(mark)
    os.makedirs(os.path.join(home, ".local", "share", "umu", "steamrt4"))
    assert runtime() == "0"
    open(mark, "w").close()
    assert runtime() == "unset" and not os.path.exists(mark)
    assert runtime() == "0"
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("UMU_RUNTIME_UPDATE=1\n")
    assert runtime() == "1"


def test_codename_notice(d, home):
    """PROTONPATH=GE-Proton: umu-run may download a newer release, whatever
    is installed, so the notice shows."""
    root, launcher = fake_install(d, home)
    os.makedirs(os.path.join(home, ".local", "share", "umu", "steamrt4"))
    os.makedirs(os.path.join(root, "prefix"))
    open(os.path.join(root, "prefix", "system.reg"), "w").close()
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    with open(os.path.join(root, "config", "launch.env"), "w") as f:
        f.write("PROTONPATH=GE-Proton\n")
    bindir = os.path.join(d, "bin")
    make_exe(os.path.join(bindir, "umu-run"), FAKE_UMU)
    make_exe(os.path.join(bindir, "zenity"), FAKE_ZENITY)
    r = launch(launcher, home, bindir + ":/usr/bin:/bin", DISPLAY=":0", FAKE_DELAY="2")
    assert r.returncode == 0, r.stdout
    assert "# Checking for the newest GE-Proton ..." in read_text(
        os.path.join(home, "zenity.lines")
    )


def test_runtime_check_notice(d, home):
    """The launch after an install shows the notice while umu-run checks
    the runtime for updates."""
    root, launcher = fake_install(d, home)
    os.makedirs(os.path.join(home, ".local", "share", "umu", "steamrt4"))
    os.makedirs(os.path.join(root, "prefix"))
    open(os.path.join(root, "prefix", "system.reg"), "w").close()
    os.makedirs(os.path.join(root, "state"))
    open(os.path.join(root, "state", "runtime-update"), "w").close()
    bindir = os.path.join(d, "bin")
    make_exe(os.path.join(bindir, "umu-run"), FAKE_UMU)
    make_exe(os.path.join(bindir, "zenity"), FAKE_ZENITY)
    r = launch(launcher, home, bindir + ":/usr/bin:/bin", DISPLAY=":0", FAKE_DELAY="2")
    assert r.returncode == 0, r.stdout
    assert "# Checking the Steam Linux Runtime for updates ..." in read_text(
        os.path.join(home, "zenity.lines")
    )


def fake_flock(bindir):
    """flock -n FD for a Mac: util-linux is not there. fcntl.flock on the
    inherited descriptor locks the same open file the launcher keeps."""
    import sys

    make_exe(
        os.path.join(bindir, "flock"),
        'exec "%s" -c "import fcntl, sys; fcntl.flock(int(sys.argv[1]), '
        'fcntl.LOCK_EX | fcntl.LOCK_NB)" "$2"\n' % sys.executable,
    )


def test_single_instance(d, home):
    root, launcher = fake_install(d, home)
    bindir = os.path.join(d, "bin")
    fake_flock(bindir)
    make_exe(os.path.join(bindir, "notify-send"), 'echo "$*" > "$HOME/notify.argv"\n')
    # The game leaves a child behind, as a wineserver does after the game.
    make_exe(
        os.path.join(bindir, "umu-run"),
        FAKE_UMU_ENV + "(sleep 5) &\n" + FAKE_UMU,
    )
    path = bindir + ":/usr/bin:/bin"
    e = {"HOME": home, "PATH": path, "LANG": "C", "FAKE_DELAY": "2", "DISPLAY": ":0"}
    first = subprocess.Popen(
        [BASH, launcher], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        for _ in range(100):
            if os.path.exists(os.path.join(home, "umu.count")):
                break
            time.sleep(0.05)
        logs_before = sorted(os.listdir(os.path.join(root, "logs")))
        r = launch(launcher, home, path, DISPLAY=":0")
        assert r.returncode == 0 and b"already starting or running" in r.stdout, r.stdout
        assert "already starting or running" in read_text(os.path.join(home, "notify.argv"))
        assert read_text(os.path.join(home, "umu.count")) == "x"
        assert sorted(os.listdir(os.path.join(root, "logs"))) == logs_before
    finally:
        assert first.wait(timeout=30) == 0
    assert read_text(os.path.join(home, "umu.fd9")) == "closed"
    # Quit and launch again at once: the left-behind child holds no lock.
    r = launch(launcher, home, path, DISPLAY=":0")
    assert r.returncode == 0 and b"already" not in r.stdout, r.stdout
    assert read_text(os.path.join(home, "umu.count")) == "x\nx"
