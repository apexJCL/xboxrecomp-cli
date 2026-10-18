"""Tests for the macOS patch (D9): the patch folder is made by the real
wrap step (package/macos.py patch_tree, which also builds xbr-swap) from
two fixture apps, and templates/macos/apply.command runs on a copy of the
base app in a temp folder. A stop at a named step is apply.command killing
itself (XBR_APPLY_STOP), as a kill or a power loss would.
"""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys

import pytest

from xboxrecomp_cli.package import macos
from xboxrecomp_cli.package import patch as patch_mod

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("clang") or not shutil.which("codesign"),
    reason="macOS with clang and codesign",
)

APP = "Fix"
V1 = "20261017.1000-c1111111-taaaaaaa-g2222222"
V2 = "20261017.1100-c3333333-taaaaaaa-g4444444"
V3 = "20261017.1200-c5555555-taaaaaaa-g6666666"

BASE_FILES = {
    "README.txt": b"readme 1",
    "game_files/default.xbe": b"XBEH game",
    "game_files/media/a.xpr": b"asset a",
    "game_files/media/b.xpr": b"asset b",
    "old.txt": b"only in the base",
}
NEW_FILES = {
    "README.txt": b"readme 2",
    "game_files/default.xbe": b"XBEH game",
    "game_files/media/a.xpr": b"asset a, changed",
    "game_files/media/b.xpr": b"asset b",
    "new.txt": b"only in the new version",
}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_app(root, version, files, says):
    """A signed <root>/Fix.app: an exe that prints says, the files in
    Resources/ with their manifest.json and SHA256SUMS."""
    app = os.path.join(root, APP + ".app")
    c = os.path.join(app, "Contents")
    os.makedirs(os.path.join(c, "MacOS"))
    res = os.path.join(c, "Resources")
    for path, data in files.items():
        os.makedirs(os.path.dirname(os.path.join(res, path)), exist_ok=True)
        with open(os.path.join(res, path), "wb") as f:
            f.write(data)
    man = {
        "schema": 2,
        "app": APP,
        "target": "macos",
        "version": version,
        "files": [{"path": p, "sha256": sha(d), "size": len(d)} for p, d in sorted(files.items())],
    }
    with open(os.path.join(res, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    with open(os.path.join(res, "SHA256SUMS"), "w") as f:
        f.writelines("%s  %s\n" % (sha(d), p) for p, d in sorted(files.items()))
    with open(os.path.join(c, "Info.plist"), "wb") as f:
        plistlib.dump(
            {
                "CFBundleExecutable": APP,
                "CFBundleIdentifier": "local.fix",
                "CFBundleName": APP,
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": version,
                "LSMinimumSystemVersion": "13.0",
            },
            f,
        )
    exe = os.path.join(c, "MacOS", APP)
    src = '#include <stdio.h>\nint main(void){puts("%s");return 0;}\n' % says
    subprocess.run(["clang", "-x", "c", "-", "-o", exe], input=src.encode(), check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", exe], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", app], check=True)
    return app


def base_of(app):
    p = os.path.join(app, "Contents", "Resources", "manifest.json")
    with open(p, "rb") as f:
        return patch_mod.Base(f.read(), app)


def make_patch(root, base_app, new_app, version):
    base = base_of(base_app)
    with open(os.path.join(new_app, "Contents", "Resources", "manifest.json")) as f:
        new = json.load(f)
    choice = patch_mod.Choice("patch", "", base, patch_mod.diff(base.manifest, new))
    return macos.patch_tree(os.path.join(root, "patch-dmg"), new_app, choice, version)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The base app, the new app and the patch folder of new against base."""
    mp = pytest.MonkeyPatch()
    mp.setattr(macos, "app", lambda: APP)
    d = str(tmp_path_factory.mktemp("macpatch"))
    base = make_app(os.path.join(d, "v1"), V1, BASE_FILES, "v1")
    new = make_app(os.path.join(d, "v2"), V2, NEW_FILES, "v2")
    p = make_patch(os.path.join(d, "out"), base, new, V2)
    mp.undo()
    return {"base": base, "new": new, "patch": p}


@pytest.fixture
def inst(built, tmp_path):
    """A copy of the base app in its own Applications folder, the patch
    folder (copied, so a test may delete it) and a temp HOME."""
    apps = tmp_path / "Applications"
    apps.mkdir()
    subprocess.run(["cp", "-R", built["base"], str(apps)], check=True)
    patch = tmp_path / "dmg"
    shutil.copytree(built["patch"], patch, symlinks=True)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "apps": str(apps),
        "app": str(apps / (APP + ".app")),
        "patch": str(patch),
        "home": str(home),
    }


def apply(inst, *args, stop=None, swap=None, script=None):
    env = dict(os.environ, HOME=inst["home"])
    env.pop("XBR_APPLY_STOP", None)
    env.pop("XBR_SWAP", None)
    if stop:
        env["XBR_APPLY_STOP"] = stop
    if swap:
        env["XBR_SWAP"] = swap
    cmd = [script or os.path.join(inst["patch"], "apply.command")]
    if "rollback" not in args or script is None:
        cmd += ["--app", inst["app"]]
    cmd += list(args)
    return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def res(app, path):
    with open(os.path.join(app, "Contents", "Resources", path), "rb") as f:
        return f.read()


def version_of(app):
    with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
        return plistlib.load(f)["CFBundleVersion"]


def says(app):
    r = subprocess.run(
        [os.path.join(app, "Contents", "MacOS", APP)], capture_output=True, text=True
    )
    return r.stdout.strip()


def tree(path):
    """{relative path: sha256 or link target} of a folder, for equality."""
    out = {}
    for dp, dns, fns in os.walk(path):
        for n in fns + [x for x in dns if os.path.islink(os.path.join(dp, x))]:
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, path)
            if os.path.islink(p):
                out[rel] = "-> " + os.readlink(p)
            else:
                with open(p, "rb") as f:
                    out[rel] = sha(f.read())
    return out


def codesign_ok(app):
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", app], capture_output=True)
    return r.returncode == 0


def assert_is_new(inst, built):
    app = inst["app"]
    assert version_of(app) == V2
    assert says(app) == "v2"
    assert tree(app) == tree(built["new"])
    assert codesign_ok(app)


def assert_untouched(inst, built):
    assert tree(inst["app"]) == tree(built["base"])
    names = set(os.listdir(inst["apps"]))
    assert names == {APP + ".app"}, names


def test_patch_folder(built):
    p = built["patch"]
    assert sorted(os.listdir(p)) == ["apply.command", "patch", "xbr-swap"]
    assert os.access(os.path.join(p, "apply.command"), os.X_OK)
    lipo = subprocess.run(
        ["lipo", "-archs", os.path.join(p, "xbr-swap")], capture_output=True, text=True
    )
    assert set(lipo.stdout.split()) == {"arm64", "x86_64"}
    res_dir = os.path.join(p, "patch", "Contents", "Resources")
    shipped = sorted(
        os.path.relpath(os.path.join(dp, n), res_dir)
        for dp, _, fns in os.walk(res_dir)
        for n in fns
    )
    # Only the changed and new files, with the full manifest and sums.
    assert shipped == sorted(
        ["README.txt", "game_files/media/a.xpr", "new.txt", "manifest.json", "SHA256SUMS"]
    )
    with open(os.path.join(p, "patch", "patch.json")) as f:
        doc = json.load(f)
    assert doc["base_version"] == V1 and doc["version"] == V2
    assert [e["path"] for e in doc["deleted"]] == ["old.txt"]
    assert doc["game_data"] is True
    kept = open(os.path.join(p, "patch", "kept.list")).read().splitlines()
    assert kept == [
        "%d %s %s" % (len(BASE_FILES[k]), sha(BASE_FILES[k]), k)
        for k in ("game_files/default.xbe", "game_files/media/b.xpr")
    ]
    for part in ("Info.plist", "MacOS", "_CodeSignature"):
        assert os.path.exists(os.path.join(p, "patch", "Contents", part))


def test_patch_folder_is_deterministic(built, tmp_path, monkeypatch):
    monkeypatch.setattr(macos, "app", lambda: APP)
    again = make_patch(str(tmp_path), built["base"], built["new"], V2)
    a, b = tree(built["patch"]), tree(again)
    # xbr-swap's ad hoc signature is the same for the same bytes; clang's
    # output is too, for the same source and flags.
    assert a == b


def test_apply(inst, built):
    r = apply(inst)
    assert r.returncode == 0, r.stderr
    assert_is_new(inst, built)
    prev = os.path.join(inst["apps"], ".%s.previous.app" % APP)
    tools = os.path.join(inst["apps"], ".%s.previous.tools" % APP)
    assert tree(prev) == tree(built["base"])
    assert sorted(os.listdir(tools)) == ["apply.command", "xbr-swap"]
    assert not os.path.exists(os.path.join(inst["apps"], ".%s.app.partial" % APP))
    assert not os.path.exists(os.path.join(inst["apps"], ".%s.apply.json" % APP))
    # The data folder (and anything else in HOME) is never written.
    assert os.listdir(inst["home"]) == []


def test_rerun_after_apply_is_a_no_op(inst, built):
    assert apply(inst).returncode == 0
    prev = os.path.join(inst["apps"], ".%s.previous.app" % APP)
    r = apply(inst)
    assert r.returncode == 0, r.stderr
    assert "is installed" in r.stdout
    assert_is_new(inst, built)
    assert tree(prev) == tree(built["base"])


@pytest.mark.parametrize("step", ["cloned", "overlaid", "verified"])
def test_kill_before_swap(inst, built, step):
    r = apply(inst, stop=step)
    assert r.returncode == -9
    # The app at the path is the base, whole and signed.
    assert tree(inst["app"]) == tree(built["base"])
    assert codesign_ok(inst["app"])
    r = apply(inst)
    assert r.returncode == 0, r.stderr
    assert_is_new(inst, built)
    assert sorted(os.listdir(inst["apps"])) == sorted(
        [APP + ".app", ".%s.previous.app" % APP, ".%s.previous.tools" % APP]
    )


def test_kill_after_swap(inst, built):
    r = apply(inst, stop="swapped")
    assert r.returncode == -9
    assert_is_new(inst, built)
    r = apply(inst)
    assert r.returncode == 0, r.stderr
    assert_is_new(inst, built)
    prev = os.path.join(inst["apps"], ".%s.previous.app" % APP)
    assert tree(prev) == tree(built["base"])
    assert not os.path.exists(os.path.join(inst["apps"], ".%s.apply.json" % APP))


def test_rollback_from_the_tools_copy(inst, built):
    assert apply(inst).returncode == 0
    shutil.rmtree(inst["patch"])
    tools = os.path.join(inst["apps"], ".%s.previous.tools" % APP)
    r = apply(inst, "rollback", script=os.path.join(tools, "apply.command"))
    assert r.returncode == 0, r.stderr
    assert "rolled back: %s -> %s" % (V2, V1) in r.stdout
    assert tree(inst["app"]) == tree(built["base"])
    # The changed game file is the base's again.
    assert res(inst["app"], "game_files/media/a.xpr") == BASE_FILES["game_files/media/a.xpr"]
    assert codesign_ok(inst["app"])
    assert says(inst["app"]) == "v1"
    assert os.listdir(inst["home"]) == []


def test_rollback_from_the_patch(inst, built):
    assert apply(inst).returncode == 0
    r = apply(inst, "rollback")
    assert r.returncode == 0, r.stderr
    assert tree(inst["app"]) == tree(built["base"])


def test_refuses_another_version(inst, built, tmp_path):
    other = make_app(str(tmp_path / "v3"), V3, BASE_FILES, "v3")
    shutil.rmtree(inst["app"])
    subprocess.run(["cp", "-R", other, inst["apps"]], check=True)
    before = tree(inst["app"])
    r = apply(inst)
    assert r.returncode != 0
    assert (
        "This update is for %s; this install is %s. Use the full installer for %s." % (V1, V3, V2)
        in r.stderr
    )
    assert tree(inst["app"]) == before
    assert sorted(os.listdir(inst["apps"])) == [APP + ".app"]


def test_refuses_a_wrong_manifest(inst, built):
    p = os.path.join(inst["app"], "Contents", "Resources", "manifest.json")
    with open(p, "a") as f:
        f.write(" ")
    r = apply(inst)
    assert r.returncode != 0
    assert "this install is %s with another manifest" % V1 in r.stderr
    assert sorted(os.listdir(inst["apps"])) == [APP + ".app"]


def test_refuses_with_no_app(inst, built):
    shutil.rmtree(inst["app"])
    r = apply(inst)
    assert r.returncode != 0
    assert "This update is for %s; this install is not installed" % V1 in r.stderr
    assert "Use the full installer for %s." % V2 in r.stderr
    assert os.listdir(inst["apps"]) == []


@pytest.mark.parametrize("same_size", [False, True])
def test_refuses_a_kept_file_mismatch(inst, built, same_size):
    p = os.path.join(inst["app"], "Contents", "Resources", "game_files", "media", "b.xpr")
    with open(p, "wb") as f:
        f.write(b"asset B" if same_size else b"asset b, damaged")
    r = apply(inst)
    assert r.returncode != 0
    assert "but game_files/media/b.xpr does not match it" in r.stderr
    assert sorted(os.listdir(inst["apps"])) == [APP + ".app"]


@pytest.mark.parametrize(
    "lst,line",
    [
        ("deleted.list", "../../../../victim"),
        ("deleted.list", "game_files/./a.xpr"),
        ("deleted.list", "/etc/hosts"),
        ("deleted.list", ""),
        ("whole.list", ".."),
        ("whole.list", "MacOS//x"),
        ("kept.list", "1 %s ../../x" % ("0" * 64)),
        ("kept.list", "no-sha"),
    ],
)
def test_refuses_unsafe_list_lines(inst, built, lst, line):
    """A list line that leaves the app (or is not a path) stops the run
    before the clone; nothing is written or removed."""
    victim = os.path.join(os.path.dirname(inst["apps"]), "victim")
    with open(victim, "w") as f:
        f.write("keep me")
    with open(os.path.join(inst["patch"], "patch", lst), "a") as f:
        f.write(line + "\n")
    before = tree(inst["app"])
    r = apply(inst)
    assert r.returncode != 0
    assert "damaged patch" in r.stderr and "nothing was changed" in r.stderr, r.stderr
    assert os.path.isfile(victim)
    assert sorted(os.listdir(inst["apps"])) == [APP + ".app"]
    assert tree(inst["app"]) == before


def test_eperm_at_the_swap(inst, built, tmp_path):
    real = os.path.join(inst["patch"], "xbr-swap")
    fake = tmp_path / "fake-swap"
    fake.write_text('#!/bin/sh\n[ "$1" = swap ] && exit 3\nexec "%s" "$@"\n' % real)
    fake.chmod(0o755)
    r = apply(inst, swap=str(fake))
    assert r.returncode == 1
    assert "App Management" in r.stderr
    assert "nothing was changed" in r.stderr
    assert '--into "%s"' % os.path.join(inst["home"], "Applications") in r.stderr
    assert_untouched(inst, built)


def test_eacces_at_the_clone(inst, built):
    os.chmod(inst["apps"], 0o555)
    try:
        r = apply(inst)
    finally:
        os.chmod(inst["apps"], 0o755)
    assert r.returncode == 1
    assert "is not writable" in r.stderr and "nothing was changed" in r.stderr
    assert '--into "%s"' % os.path.join(inst["home"], "Applications") in r.stderr
    assert_untouched(inst, built)


def test_into_another_folder(inst, built):
    into = os.path.join(inst["home"], "Applications")
    r = apply(inst, "--into", into)
    assert r.returncode == 0, r.stderr
    assert tree(os.path.join(into, APP + ".app")) == tree(built["new"])
    assert_untouched(inst, built)
