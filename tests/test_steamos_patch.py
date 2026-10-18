"""Tests for `install.sh patch` (install_lib.cmd_patch, D6 and D7) on fake
bundles: the patch tar is made by the real wrap (package/steamos.py) from
two staged payloads, unpacked, and applied to a temp root that a full
install of the base made. A stop at each named step is a forked child that
exits there, as a kill or a power loss would.
"""

import json
import os
import shutil
import tarfile

import pytest
from test_steamos_install import APP, EXE, PACK, V1, V2, V3, load_il, make_bundle, read, run

from xboxrecomp_cli.package import patch as patch_mod
from xboxrecomp_cli.package import steamos


@pytest.fixture(scope="module")
def il(tmp_path_factory):
    return load_il(tmp_path_factory)


GAME = {"default.xbe": b"XBEH game", "media/a.xpr": b"asset a", "media/b.xpr": b"asset b"}


def make_patch(d, base_bundle, new_bundle, name="p"):
    """The steamos patch tar of new_bundle against base_bundle, unpacked
    into d/<name>/; returns the patch folder."""
    with open(os.path.join(base_bundle, "manifest.json"), "rb") as f:
        base = patch_mod.Base(f.read(), base_bundle)
    with open(os.path.join(new_bundle, "manifest.json")) as f:
        new = json.load(f)
    choice = patch_mod.Choice("patch", "", base, patch_mod.diff(base.manifest, new))
    out = os.path.join(d, name + "-out")
    os.makedirs(out, exist_ok=True)
    tar = steamos.wrap_steamos(
        new_bundle, new["version"], os.path.join(new_bundle, EXE + ".exe"), out, choice=choice
    )
    dst = os.path.join(d, name)
    with tarfile.open(tar) as t:
        t.extractall(dst, filter="data")
    (top,) = os.listdir(dst)
    return os.path.join(dst, top)


def tree(root):
    """Every path under root but the run's own records: contents, modes of
    executables and link targets, with root's own path taken out (the
    launcher and the desktop entry name it)."""
    skip = {"state/history", "state/runtime-update", "state/launch.lock"}
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in sorted(dirnames + filenames):
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            if rel in skip:
                continue
            if os.path.islink(p):
                out[rel] = ("link", os.readlink(p))
            elif os.path.isfile(p):
                out[rel] = (read(p).replace(root.encode(), b"<root>"), os.stat(p).st_mode & 0o111)
            else:
                out[rel] = ("dir",)
    return out


def bundles(d, v2_game=None, v2_extra=None):
    v1 = make_bundle(os.path.join(d, "1"), V1, game=dict(GAME), extra={PACK: b"pack 1"})
    v2 = make_bundle(
        os.path.join(d, "2"),
        V2,
        exe=b"MZ v2",
        game=v2_game or dict(GAME),
        extra=v2_extra if v2_extra is not None else {PACK: b"pack 1"},
    )
    return v1, v2


def installed(il, d, bundle, name="r"):
    root = os.path.join(d, name)
    rc, out = run(il, bundle, root, "install")
    assert rc == 0, out
    return root


def current(root):
    return os.path.basename(os.readlink(os.path.join(root, "current")))


def launchable(root):
    """current names a version holding its whole program, by its sums."""
    v = os.path.join(root, "current")
    assert os.path.isfile(os.path.join(v, EXE + ".exe"))
    with open(os.path.join(v, "SHA256SUMS")) as f:
        for line in f:
            sha, rel = line.rstrip("\n").split("  ", 1)
            if not rel.startswith("game_files/"):
                assert patch_mod.hashlib.sha256(read(os.path.join(v, rel))).hexdigest() == sha
    return True


# ── the patch itself ─────────────────────────────────────────────────────


def test_patch_tar_holds_only_what_changed(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    assert os.path.basename(p) == "%s-%s-steamos-patch-from-%s" % (APP, V2, patch_mod.short(V1))
    have = set()
    for dirpath, _dirs, files in os.walk(p):
        for f in files:
            have.add(os.path.relpath(os.path.join(dirpath, f), p))
    assert EXE + ".exe" in have and "patch.json" in have and "manifest.json" in have
    assert not any(h.startswith("game_files") for h in have), have
    assert PACK not in have and "README.txt" not in have
    doc = json.loads(read(os.path.join(p, "patch.json")))
    assert doc["base_version"] == V1 and doc["version"] == V2 and not doc["game_data"]
    assert [e["path"] for e in doc["changed"]] == [EXE + ".exe"]


def test_patch_tar_deterministic(il, d):
    v1, v2 = bundles(d)
    with open(os.path.join(v1, "manifest.json"), "rb") as f:
        base = patch_mod.Base(f.read(), v1)
    with open(os.path.join(v2, "manifest.json")) as f:
        new = json.load(f)
    choice = patch_mod.Choice("patch", "", base, patch_mod.diff(base.manifest, new))
    shas = []
    for n in ("a", "b"):
        out = os.path.join(d, n)
        os.makedirs(out)
        t = steamos.wrap_steamos(v2, V2, os.path.join(v2, EXE + ".exe"), out, choice=choice)
        shas.append(patch_mod.hashlib.sha256(read(t)).hexdigest())
    assert shas[0] == shas[1]


# ── apply ────────────────────────────────────────────────────────────────


def test_patch_equals_full_install(il, d):
    """A patched root and a fully installed one are the same, byte for
    byte: versions, game files, launcher, desktop entry, runtime marks."""
    v1, v2 = bundles(d, v2_game=dict(GAME, **{"media/b.xpr": b"asset b2", "media/c.xpr": b"c"}))
    p = make_patch(d, v1, v2)
    a = installed(il, d, v1, "a")
    b = installed(il, d, v1, "b")
    rc, out = run(il, p, a, "install", "--yes")
    assert rc == 0, out
    assert "WARNING: " + il.GAME_DATA_WARNING in out
    rc, out2 = run(il, v2, b, "install")
    assert rc == 0, out2
    assert tree(a) == tree(b)
    assert os.path.isfile(os.path.join(a, "state", "runtime-update"))
    # Unchanged program files are the base's own inodes.
    ino = lambda r, v: os.stat(os.path.join(r, "versions", v, PACK)).st_ino  # noqa: E731
    assert ino(a, V1) == ino(a, V2)
    assert "patch.json" not in os.listdir(os.path.join(a, "versions", V2))


def test_code_only_patch_keeps_715_game_files(il, d, monkeypatch):
    """A B3-shaped dump: a code-only patch writes no game file and leaves
    all 715 in place, untouched."""
    game = {"media/f%03d.bin" % i: b"g%d" % i for i in range(714)}
    game["default.xbe"] = b"XBEH"
    v1, v2 = bundles(d, v2_game=dict(game))
    v1 = make_bundle(os.path.join(d, "1b"), V1, game=dict(game), extra={PACK: b"pack 1"})
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    gdir = os.path.join(root, "game_files")
    before = {}
    for dirpath, _dirs, files in os.walk(gdir):
        for f in files:
            st = os.stat(os.path.join(dirpath, f))
            before[os.path.join(dirpath, f)] = (st.st_ino, st.st_mtime_ns)
    assert len(before) == 715
    writes = []
    real = il.shutil.copy2
    monkeypatch.setattr(
        il.shutil, "copy2", lambda s, t, **k: (writes.append(t), real(s, t, **k))[1]
    )
    rc, out = run(il, p, root, "patch")
    assert rc == 0, out
    assert not [w for w in writes if os.sep + "game_files" + os.sep in w], writes
    after = {}
    for dirpath, _dirs, files in os.walk(gdir):
        for f in files:
            st = os.stat(os.path.join(dirpath, f))
            after[os.path.join(dirpath, f)] = (st.st_ino, st.st_mtime_ns)
    assert after == before
    assert "game files: 0 written, 0 removed, 715 kept" in out


def test_kept_game_file_mismatch_refuses_before_any_write(il, d):
    v1, v2 = bundles(d, v2_game=dict(GAME, **{"media/c.xpr": b"c"}))
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    with open(os.path.join(root, "game_files", "media", "a.xpr"), "wb") as f:
        f.write(b"asset A")  # same size, another sha256
    snap = tree(root)
    rc, out = run(il, p, root, "patch", "--yes")
    assert rc == 1 and "game_files/media/a.xpr does not match" in out, out
    assert tree(root) == snap


def test_deleted_and_stray_game_files(il, d):
    """Only the paths the patch deletes are removed; a file the base never
    had is reported and left."""
    g2 = dict(GAME)
    del g2["media/b.xpr"]
    v1, v2 = bundles(d, v2_game=g2)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    with open(os.path.join(root, "game_files", "mine.txt"), "w") as f:
        f.write("x")
    rc, out = run(il, p, root, "patch", "--yes")
    assert rc == 0, out
    assert not os.path.exists(os.path.join(root, "game_files", "media", "b.xpr"))
    assert os.path.isfile(os.path.join(root, "game_files", "mine.txt"))
    assert "left alone: game_files/mine.txt" in out


def test_game_data_patch_needs_yes_off_a_tty(il, d):
    v1, v2 = bundles(d, v2_game=dict(GAME, **{"media/a.xpr": b"asset A2"}))
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "--yes" in out and current(root) == V1, out
    assert read(os.path.join(root, "game_files", "media", "a.xpr")) == b"asset a"


def test_base_not_current_refuses(il, d):
    """A base that is installed but not current is no base."""
    v1, v2 = bundles(d)
    v3 = make_bundle(os.path.join(d, "3"), V3, exe=b"MZ v3", game=dict(GAME))
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    rc, out = run(il, v3, root, "install")
    assert rc == 0, out
    snap = tree(root)
    rc, out = run(il, p, root, "patch")
    assert rc == 1, out
    assert "This update is for %s; this install is %s." % (V1, V3) in out
    assert "Use the full installer for %s." % V2 in out
    assert tree(root) == snap


def test_no_install_refuses(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = os.path.join(d, "empty")
    rc, out = run(il, p, root, "patch")
    assert rc == 1, out
    assert "This update is for %s; this install is not installed." % V1 in out, out
    assert not os.path.exists(root) or os.listdir(root) == []


def test_wrong_base_manifest_refuses(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    doc = json.loads(read(os.path.join(p, "patch.json")))
    doc["base_manifest_sha256"] = "0" * 64
    with open(os.path.join(p, "patch.json"), "w") as f:
        json.dump(doc, f)
    snap = tree(root)
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "another manifest" in out and "full installer" in out, out
    assert tree(root) == snap


def test_kept_program_file_mismatch_refuses(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    with open(os.path.join(root, "versions", V1, PACK), "wb") as f:
        f.write(b"pack X")
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "versions/%s/%s does not match" % (V1, PACK) in out, out
    assert current(root) == V1 and not os.path.exists(os.path.join(root, "versions", V2))


def test_damaged_patch_refuses(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    with open(os.path.join(p, EXE + ".exe"), "ab") as f:
        f.write(b"!")
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "damaged" in out and current(root) == V1, out


def edit_patch_json(p, change):
    path = os.path.join(p, "patch.json")
    with open(path) as f:
        doc = json.load(f)
    change(doc)
    with open(path, "w") as f:
        json.dump(doc, f)


def test_patch_deleting_outside_the_install_refused(il, d):
    """A deleted "game_files/../../x" is refused before any write; the file
    it names stays."""
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    victim = os.path.join(d, "victim")
    with open(victim, "w") as f:
        f.write("keep me")
    rel = "game_files/" + os.path.relpath(victim, os.path.join(root, "game_files"))
    assert ".." in rel.split("/")
    edit_patch_json(
        p, lambda doc: doc["deleted"].append({"path": rel, "sha256": "0" * 64, "size": 7})
    )
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "unsafe path" in out, out
    assert os.path.isfile(victim) and current(root) == V1
    assert not os.path.exists(os.path.join(root, "versions", V2))


@pytest.mark.parametrize("key", ["base_version", "version"])
def test_patch_with_a_bad_version_refused(il, d, key):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    if key == "version":
        mpath = os.path.join(p, "manifest.json")
        with open(mpath) as f:
            m = json.load(f)
        m["version"] = "../evil"
        with open(mpath, "w") as f:
            json.dump(m, f)
    edit_patch_json(p, lambda doc: doc.__setitem__(key, "../evil"))
    rc, out = run(il, p, root, "patch")
    assert rc == 1 and "bad version" in out, out
    assert current(root) == V1


def test_patch_names(il):
    for rel in ["a", "game_files/a b/c.xbe", "x.y"]:
        assert il.patch_path_ok(rel), rel
    for rel in ["", "/a", "a\\b", "../a", "a/..", "a//b", "./a", "a/./b", "a/", "a\tb", None]:
        assert not il.patch_path_ok(rel), rel
    assert il.patch_version_ok("20261017.1200-c1")
    for v in ["", "../v", "a/b", "a:b", ".v", "v.partial", "v.old", None]:
        assert not il.patch_version_ok(v), v


def test_patch_command_on_full_bundle_refused(il, d):
    v1, _v2 = bundles(d)
    root = installed(il, d, v1)
    rc, out = run(il, v1, root, "patch")
    assert rc == 1 and "not a patch" in out, out


@pytest.mark.parametrize("command", ["install", "patch"])
def test_refuses_while_game_runs(il, d, command):
    """install and patch refuse while the launcher holds launch.lock."""
    if il.fcntl is None:
        pytest.skip("no flock here")
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    with open(os.path.join(root, "state", "launch.lock"), "a") as f:
        il.fcntl.flock(f.fileno(), il.fcntl.LOCK_EX)
        rc, out = run(il, p if command == "patch" else v2, root, command)
    assert rc == 1 and "the game is running" in out, out
    assert current(root) == V1


def test_rollback_and_prune_after_patches(il, d):
    v1, v2 = bundles(d)
    v3 = make_bundle(os.path.join(d, "3"), V3, exe=b"MZ v3", game=dict(GAME), extra={PACK: b"p3"})
    p2 = make_patch(d, v1, v2, "p2")
    p3 = make_patch(d, v2, v3, "p3")
    root = installed(il, d, v1)
    for p in (p2, p3):
        rc, out = run(il, p, root, "patch")
        assert rc == 0, out
    rc, out = run(il, p3, root, "rollback")
    assert rc == 0 and current(root) == V2, out
    assert read(os.path.join(root, "current", EXE + ".exe")) == b"MZ v2"
    rc, out = run(il, v3, root, "install", "--keep", "1")
    assert rc == 0, out
    assert sorted(os.listdir(os.path.join(root, "versions"))) == sorted([V2, V3])


def test_patch_runs_the_proton_step(il, d):
    """A base installed before the Proton pin still gets it from a patch."""
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = os.path.join(d, "r")
    rc, out = run(il, v1, root, "install", "--no-proton")
    assert rc == 0, out
    calls = []
    rc, out = run(il, p, root, "patch", proton=lambda: calls.append(1))
    assert rc == 0 and calls == [1], out


def test_rerun_after_success_is_a_noop(il, d):
    v1, v2 = bundles(d)
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    assert run(il, p, root, "patch")[0] == 0
    rc, out = run(il, p, root, "patch")
    assert rc == 0 and "already installed" in out and "(unchanged)" in out, out


# ── a stop at each step ──────────────────────────────────────────────────


def stopped_at(il, name, fn):
    """Runs fn in a forked child that exits at step `name`; returns the
    child's exit status (9 when it stopped there)."""
    pid = os.fork()
    if pid == 0:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        try:
            il.STEP_HOOK = lambda n: os._exit(9) if n == name else None
            fn()
        finally:
            os._exit(0)
    _pid, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


@pytest.mark.parametrize(
    "name", ["patch-before-rename", "program", "game-file", "game-files", "switched"]
)
def test_patch_stopped_at_each_step(il, d, name):
    """A stop before and after the rename, during and after the game-file
    step, and after the switch leaves a launchable version, and a rerun
    ends where a whole run does."""
    v1, v2 = bundles(d, v2_game=dict(GAME, **{"media/b.xpr": b"asset b2"}))
    p = make_patch(d, v1, v2)
    root = installed(il, d, v1)
    ref = installed(il, d, v1, "ref")
    assert run(il, p, ref, "patch", "--yes")[0] == 0
    code = stopped_at(il, name, lambda: run(il, p, root, "patch", "--yes"))
    assert code == 9, name
    assert launchable(root)
    assert current(root) == (V2 if name == "switched" else V1)
    rc, out = run(il, p, root, "patch", "--yes")
    assert rc == 0, out
    assert tree(root) == tree(ref)


def test_reinstall_stopped_between_renames_is_repaired(il, d, monkeypatch):
    """Without RENAME_EXCHANGE, a reinstall of current stopped between its
    two renames leaves versions/<cur>.old; the next run puts it back first."""
    v1, _v2 = bundles(d)
    root = installed(il, d, v1)
    shutil.rmtree(os.path.join(root, "versions", V1, "enhance"))
    monkeypatch.setattr(il, "exchange", lambda a, b: False)
    code = stopped_at(il, "reinstall-between", lambda: run(il, v1, root, "install"))
    assert code == 9
    assert not os.path.exists(os.path.join(root, "versions", V1))
    assert os.path.isdir(os.path.join(root, "versions", V1 + ".old"))
    rc, out = run(il, v1, root, "install")
    assert rc == 0 and "repaired" in out, out
    assert launchable(root) and read(os.path.join(root, "current", PACK)) == b"pack 1"
    assert not os.path.exists(os.path.join(root, "versions", V1 + ".old"))


# ── the HUD tweaks across a chain (wizard-paths 6.3) ─────────────────────

DEFAULTS = "config.default.toml"
STOCK = b"[render]\nscale = 1\n"
WITH_PACK = STOCK + b'\n[textures]\npack = "enhance/hud.pack"\n'


def test_clear_then_new_pack_chain_equals_full_installs(il, d):
    """Full with pack A; then --clear-hud-pack (the patch deletes the pack
    and changes the defaults); then --hud-pack B (both back, B's bytes).
    After each patch the root is the one full installs make."""
    v1 = make_bundle(
        os.path.join(d, "1"), V1, game=dict(GAME), extra={PACK: b"pack A", DEFAULTS: WITH_PACK}
    )
    v2 = make_bundle(
        os.path.join(d, "2"), V2, exe=b"MZ v2", game=dict(GAME), extra={DEFAULTS: STOCK}
    )
    v3 = make_bundle(
        os.path.join(d, "3"),
        V3,
        exe=b"MZ v2",
        game=dict(GAME),
        extra={PACK: b"pack B", DEFAULTS: WITH_PACK},
    )
    p12 = make_patch(d, v1, v2, "p12")
    doc = json.loads(read(os.path.join(p12, "patch.json")))
    assert [e["path"] for e in doc["deleted"]] == [PACK]
    assert DEFAULTS in [e["path"] for e in doc["changed"]]
    p23 = make_patch(d, v2, v3, "p23")
    doc = json.loads(read(os.path.join(p23, "patch.json")))
    assert [e["path"] for e in doc["new"]] == [PACK]
    assert [e["path"] for e in doc["changed"]] == [DEFAULTS]
    assert read(os.path.join(p23, PACK)) == b"pack B"
    a = installed(il, d, v1, "a")
    b = installed(il, d, v1, "b")
    for patch_dir, full in ((p12, v2), (p23, v3)):
        rc, out = run(il, patch_dir, a, "patch", "--yes")
        assert rc == 0, out
        rc, out = run(il, full, b, "install")
        assert rc == 0, out
        assert tree(a) == tree(b)
    assert read(os.path.join(a, "current", PACK)) == b"pack B"
    assert read(os.path.join(a, "current", DEFAULTS)) == WITH_PACK
    assert not os.path.exists(os.path.join(a, "versions", V2, PACK))


def test_override_note_after_install_and_patch(il, d):
    """D8: a player's textures.pack that hides the build's pack, or names
    one the build no longer ships, gets one line; the file is untouched."""
    v1 = make_bundle(
        os.path.join(d, "1"), V1, game=dict(GAME), extra={PACK: b"pack A", DEFAULTS: WITH_PACK}
    )
    v2 = make_bundle(
        os.path.join(d, "2"), V2, exe=b"MZ v2", game=dict(GAME), extra={DEFAULTS: STOCK}
    )
    root = installed(il, d, v1)
    assert "note: " not in run(il, v1, root, "install")[1]
    cfg = os.path.join(root, "config", "config.toml")
    with open(cfg, "w") as f:
        f.write('[render]\nscale = 2\n[textures]\npack = "none"  # mine\n')
    rc, out = run(il, v1, root, "install")
    assert rc == 0 and 'textures.pack = "none", which overrides this build\'s HUD pack' in out, out
    # The clear: "none" then hides nothing.
    rc, out = run(il, make_patch(d, v1, v2), root, "patch", "--yes")
    assert rc == 0 and "note: " not in out, out
    with open(cfg, "w") as f:
        f.write('[textures]\npack = "enhance/hud.pack"\n')
    rc, out = run(il, v2, root, "install")
    assert rc == 0 and "this build ships no HUD pack" in out, out
    assert read(cfg) == b'[textures]\npack = "enhance/hud.pack"\n'


def test_scan_pack_key(il, d):
    p = os.path.join(d, "c.toml")
    for text, want in (
        ('[textures]\npack = "none"\n', "none"),
        ("[ textures ] # t\npack='a b' # c\n", "a b"),
        ("[textures]\npack = x # c\n", "x"),
        ('[render]\npack = "x"\n[textures]\n', None),
        ('[textures]\npacked = "x"\n', None),
        ('[textures]\n[other]\npack = "x"\n', None),
    ):
        with open(p, "w") as f:
            f.write(text)
        assert il.scan_pack_key(p) == want, text
    assert il.scan_pack_key(os.path.join(d, "missing.toml")) is None


def test_status_names_the_update_id_and_tweaks(il, d):
    v1 = make_bundle(os.path.join(d, "1"), V1, game=dict(GAME))
    root = installed(il, d, v1)
    rc, out = run(il, v1, root, "status")
    want = patch_mod.hashlib.sha256(read(os.path.join(v1, "manifest.json"))).hexdigest()[:8]
    assert rc == 0 and "update id: %s\n" % want in out, out
    assert "tweaks:   none\n" in out, out
    hp = {"sha256": "ab" * 32, "scale": 4, "entries": 12}
    assert il.tweaks_line({"enhance": {"hud_pack": hp}}) == "HUD pack 4x, 12 entries (abababab)"
