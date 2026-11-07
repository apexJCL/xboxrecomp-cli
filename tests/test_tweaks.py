"""The tweaks an update keeps from its base (package/tweaks.py): the HUD
flags and their guard (D3), a kept pack from the store, dist or a remake
(D4), strict --patch-from (D7), the plan's new fields (D2, D6) and the
config.toml override scan (D8). The fixture is test_incremental's: game2
in git, a fake compile, the real steamos tar.

  uv run pytest tests/test_tweaks.py
"""

import hashlib
import json
import os
import re
import shutil
import tarfile
import types

import pytest
import test_incremental
from test_incremental import (
    bulk,
    bundles,
    first_record,
    patches,
    plan_of,
    recompile,
    tar_manifest,
    tar_names,
)
from test_second_game import write

from xboxrecomp_cli import buildstate, host, main
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.enhance import hud as hud_mod
from xboxrecomp_cli.enhance import pack as pack_mod
from xboxrecomp_cli.enhance import png as png_mod
from xboxrecomp_cli.package import incr, tweaks

PACK = "enhance/hud.pack"
# test_incremental's fixture, under its own name for pytest.
game = test_incremental.game


def sha_of(data):
    return hashlib.sha256(data).hexdigest()


def make_pack(G, fill=b"\0\0\0\xff"):
    """dist/enhance/hud/hud.pack with a hud-pack.toml that names it, as
    enhance hud leaves them; returns the pack's bytes."""
    d = os.path.join(G.dist, "enhance", "hud")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "hud.pack")
    sha = pack_mod.write(path, [pack_mod.PackEntry(1, 1, 1, fill)], 2)
    write(
        os.path.join(d, "hud-pack.toml"),
        'tool = "xbr 0.1"\nscale = 2\nlanguages = ["en"]\nfont_sha256 = ""\n'
        'pack_sha256 = "%s"\nonly = []\nconfig = "c0"\n' % sha,
    )
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def packed(game, monkeypatch):
    """A first bundle with a HUD pack, and a runtime change after it."""
    monkeypatch.setattr(pkg, "reads_config", lambda: True)
    monkeypatch.setattr(pkg, "config_layer", lambda enhance, lib, target: (True, {}))
    G = game["G"]
    bulk(G)
    data = make_pack(G)
    game["run"](hud_pack="")
    base = first_record(game)
    recompile(game)
    game["base"], game["pack"] = base, data
    return game


def imported_pack(G):
    """What enhance hud --import leaves: no [enhance.hud] needed, none read."""
    folder = G.path("dist", "pngs")
    os.makedirs(folder)
    png_mod.write(os.path.join(folder, "00000000000000A1.png"), 2, 2, bytes((9, 8, 7, 255)) * 4)
    out = G.path("dist", "mine")
    a = types.SimpleNamespace(
        scale=None,
        out=out,
        font=None,
        jobs=1,
        only=[],
        list=False,
        language=[],
        import_dir=folder,
        from_dumps=[],
        misses=[],
        mips="auto",
        ignore_disk_space=True,
    )
    assert hud_mod.run(a) == 0
    return out


def test_an_imported_pack_goes_through_plan_and_package(game, monkeypatch):
    """--hud-pack DIR copies the pack: the plan runs no enhance hud and reads
    no [enhance.hud], and the real run then bundles exactly that file."""
    monkeypatch.setattr(pkg, "reads_config", lambda: True)
    monkeypatch.setattr(pkg, "config_layer", lambda enhance, lib, target: (True, {}))
    G = game["G"]
    bulk(G)
    with pytest.raises(host.CliError):
        hud_mod.hud_config()
    out = imported_pack(G)
    game["a"].hud_pack = out
    p = plan_of(game)
    game["a"].hud_pack = None
    row = next(r for r in p["stages"] if r["stage"] == "hud")
    assert row["action"] == "reuse" and row["why"] == ["from dist/mine"]
    assert "hud" not in p["unknown"] and p["output"]["kind"] != "none"
    game["run"](hud_pack=out)
    with open(os.path.join(out, "hud.pack"), "rb") as f:
        data = f.read()
    m = tar_manifest(G.path("dist", bundles(G)[-1]))
    assert m["enhance"]["hud_pack"]["sha256"] == sha_of(data)
    assert m["enhance"]["hud_pack"]["entries"] == 1


def test_the_plan_refuses_a_missing_pack_as_the_run_does(game, monkeypatch):
    monkeypatch.setattr(pkg, "reads_config", lambda: True)
    monkeypatch.setattr(pkg, "config_layer", lambda enhance, lib, target: (True, {}))
    gone = game["G"].path("dist", "none")
    game["a"].hud_pack = gone
    p = plan_of(game)
    game["a"].hud_pack = None
    row = next(r for r in p["stages"] if r["stage"] == "hud")
    with pytest.raises(host.CliError) as e:
        game["run"](hud_pack=gone)
    assert (row["action"], row["why"]) == ("refuse", [str(e.value)])
    assert (p["output"]["kind"], p["output"]["why"]) == ("none", str(e.value))
    assert p["output"]["why_codes"] == []


def patch_doc(G, name):
    with tarfile.open(G.path("dist", name)) as t:
        return json.load(t.extractfile(next(n for n in t.getnames() if n.endswith("/patch.json"))))


def store(game):
    return incr.packs_dir(buildstate.State(game["G"].root))


# ── D3: the flags and the guard ──────────────────────────────────────────


def test_hud_flags_are_exclusive(game):
    p = main.make_parser()[0]
    for pair in (
        ["--hud-pack", "--keep-hud-pack"],
        ["--keep-hud-pack", "--clear-hud-pack"],
        ["--hud-pack", "--clear-hud-pack"],
    ):
        with pytest.raises(SystemExit):
            p.parse_args(["package", "steamos"] + pair)
    a = p.parse_args(["package", "steamos", "--keep-hud-pack"])
    assert tweaks.hud_choice(a) == "keep"
    a = p.parse_args(["package", "steamos", "--hud-pack"])
    assert tweaks.hud_choice(a) == "make"


def test_keep_with_full_is_refused(game):
    a = main.make_parser()[0].parse_args(["package", "steamos", "--keep-hud-pack", "--full"])
    with pytest.raises(host.CliError, match="--full has no base"):
        main.cmd_package(a)


def test_the_guard_refuses_before_any_build(packed):
    G, calls = packed["G"], packed["calls"]
    before = dict(calls), bundles(G)
    with pytest.raises(host.CliError, match="carries a HUD pack: pass --keep-hud-pack"):
        packed["run"](patch_from=packed["base"]["sha256"])
    assert (dict(calls), bundles(G)) == before and patches(G) == []
    # The plan says the same without raising: the wizard still reads it.
    packed["a"].patch_from = packed["base"]["sha256"]
    try:
        out = plan_of(packed)["output"]
    finally:
        del packed["a"].patch_from
    assert out["kind"] == "none" and "carries a HUD pack" in out["why"], out


def test_full_needs_no_hud_flag(packed):
    out = packed["run"](full=True)
    assert "bundle: full (--full)" in out, out


# ── D4: a kept pack ──────────────────────────────────────────────────────


def kept(game, **kw):
    out = game["run"](patch_from=game["base"]["sha256"], keep_hud_pack=True, **kw)
    G = game["G"]
    (p,) = patches(G)
    return out, p


def test_keep_from_the_store(packed):
    G, base = packed["G"], packed["base"]
    shutil.rmtree(os.path.join(G.dist, "enhance"))
    out, p = kept(packed)
    assert "keep: the HUD pack of %s, from the store" % base["version"] in out, out
    doc = patch_doc(G, p)
    # 6.4: the same bytes and the same defaults: neither shipped nor
    # deleted, and the manifest repeats the base's record.
    touched = [e["path"] for k in ("changed", "new", "deleted") for e in doc[k]]
    for path in (PACK, "config.default.toml"):
        assert path not in touched and path not in tar_names(G.path("dist", p)), path
    m = tar_manifest(G.path("dist", p))
    (full,) = [n for n in bundles(G) if "-patch-from-" not in n]
    old = tar_manifest(G.path("dist", full))
    assert m["enhance"]["hud_pack"] == old["enhance"]["hud_pack"]
    assert m["enhance"]["hud_pack"]["sha256"] == sha_of(packed["pack"])
    # The base had no seed: the -o part is the base's.
    opt = re.compile(r"-o([0-9a-f]{6})")
    assert opt.search(m["version"]).group(1) == opt.search(old["version"]).group(1)


def test_keep_from_dist(packed):
    base = packed["base"]
    shutil.rmtree(store(packed))
    out, _p = kept(packed)
    assert "keep: the HUD pack of %s, from the dist" % base["version"] in out, out
    # Stored again for the next update.
    assert os.listdir(store(packed))


def test_keep_by_remake(packed, monkeypatch):
    G, data = packed["G"], packed["pack"]
    shutil.rmtree(store(packed))
    shutil.rmtree(os.path.join(G.dist, "enhance"))
    seen = []

    def fake_hud(ns):
        seen.append((ns.scale, ns.language, ns.no_record))
        with open(os.path.join(ns.out, "hud.pack"), "wb") as f:
            f.write(data)

    monkeypatch.setattr(tweaks, "remake_problem", lambda made: ("", None))
    monkeypatch.setattr(hud_mod, "run", fake_hud)
    out, _p = kept(packed)
    assert "making the HUD pack of %s again" % packed["base"]["version"] in out, out
    assert seen == [(2, ["en"], True)]
    assert not os.path.exists(os.path.join(buildstate.state_dir(G.root), tweaks.REMAKE_DIR))
    assert os.listdir(store(packed))


def test_keep_refuses_a_different_remake(packed, monkeypatch):
    G = packed["G"]
    shutil.rmtree(store(packed))
    shutil.rmtree(os.path.join(G.dist, "enhance"))

    def other_hud(ns):
        with open(os.path.join(ns.out, "hud.pack"), "wb") as f:
            f.write(b"other bytes")

    monkeypatch.setattr(tweaks, "remake_problem", lambda made: ("", None))
    monkeypatch.setattr(hud_mod, "run", other_hud)
    with pytest.raises(host.CliError, match="came out different"):
        kept(packed)
    assert patches(G) == []


def test_keep_refuses_changed_settings_before_any_build(packed, monkeypatch):
    G, calls = packed["G"], packed["calls"]
    shutil.rmtree(store(packed))
    shutil.rmtree(os.path.join(G.dist, "enhance"))
    monkeypatch.setattr(tweaks, "remake_problem", lambda made: ("the HUD settings changed", None))
    before = dict(calls)
    with pytest.raises(host.CliError, match=r"could not be made again the same \(the HUD"):
        kept(packed)
    assert dict(calls) == before
    packed["a"].patch_from, packed["a"].keep_hud_pack = packed["base"]["sha256"], True
    try:
        p = plan_of(packed)
    finally:
        del packed["a"].patch_from, packed["a"].keep_hud_pack
    assert p["output"]["kind"] == "none" and "HUD settings changed" in p["output"]["why"]
    assert {r["stage"]: r["action"] for r in p["stages"]}["hud"] == "refuse"


def test_keep_ignores_a_pack_of_other_bytes(packed):
    """dist holds a newer pack: not the base's, so the store's is taken."""
    G = packed["G"]
    make_pack(G, fill=b"\xff\0\0\xff")
    out, _p = kept(packed)
    assert "from the store" in out, out


def test_clear_lists_the_pack_deleted(packed):
    G = packed["G"]
    out = packed["run"](patch_from=packed["base"]["sha256"], clear_hud_pack=True)
    assert "hud pack: none (--clear-hud-pack)" in out, out
    (p,) = patches(G)
    assert PACK in [e["path"] for e in patch_doc(G, p)["deleted"]]
    assert not tar_manifest(G.path("dist", p)).get("enhance", {}).get("hud_pack")


def test_made_with_is_in_the_manifest(packed):
    m = tar_manifest(packed["G"].path("dist", bundles(packed["G"])[0]))
    made = m["enhance"]["hud_pack"]["made_with"]
    assert made == {
        "scale": 2,
        "languages": ["en"],
        "only": [],
        "font_sha256": "",
        "config": "c0",
        "cli": "0.1",
    }


def test_made_with_needs_the_packs_own_toml(d):
    path = os.path.join(d, "hud.pack")
    sha = pack_mod.write(path, [pack_mod.PackEntry(1, 1, 1, b"\0\0\0\xff")], 2)
    assert tweaks.made_with(path, sha) is None
    write(os.path.join(d, "hud-pack.toml"), 'pack_sha256 = "%s"\nconfig = "x"\n' % ("0" * 64))
    assert tweaks.made_with(path, sha) is None
    write(os.path.join(d, "hud-pack.toml"), 'pack_sha256 = "%s"\nconfig = "x"\n' % sha)
    assert tweaks.made_with(path, sha)["config"] == "x"


def test_pack_write_is_atomic_under_a_hard_link(d):
    """The store hard-links dist's pack: a new enhance hud must not change
    the stored bytes through the link."""
    path = os.path.join(d, "hud.pack")
    pack_mod.write(path, [pack_mod.PackEntry(1, 1, 1, b"\0\0\0\xff")], 2)
    linked = os.path.join(d, "stored.pack")
    os.link(path, linked)
    with open(linked, "rb") as f:
        old = f.read()
    pack_mod.write(path, [pack_mod.PackEntry(2, 1, 1, b"\xff\0\0\xff")], 2)
    with open(linked, "rb") as f:
        assert f.read() == old
    with open(path, "rb") as f:
        assert f.read() != old
    assert not os.path.exists(path + ".tmp")


def test_the_store_keeps_the_packs_of_kept_records(packed):
    st = buildstate.State(packed["G"].root)
    sha = sha_of(packed["pack"])
    assert incr.record_packs(st, incr.KEEP_PACKS) >= {sha}
    write(os.path.join(incr.packs_dir(st), "f" * 64 + ".pack"), "orphan")
    incr.prune_packs(st, None)
    assert sorted(os.listdir(incr.packs_dir(st))) == [sha + ".pack"]


def test_clean_lists_the_pack_store(packed):
    from xboxrecomp_cli import clean

    c = clean.collect(["state"])
    assert store(packed) in [p for _c, p, _n in c.items]


# ── D7: strict --patch-from ──────────────────────────────────────────────


def test_patch_from_over_half_is_refused(game):
    """No bulk: the program is most of the bundle, so a patch is over half
    of it, and the named base refuses rather than making the full bundle."""
    G = game["G"]
    game["run"]()
    base = first_record(game)
    recompile(game)
    before = bundles(G)
    with pytest.raises(host.CliError, match="make the full bundle with --full"):
        game["run"](patch_from=base["sha256"])
    # The plan cannot tell: its stand-in for the new manifest is the
    # newest record, the base itself. The run decides on the real one.
    assert bundles(G) == before and patches(G) == []


def test_patch_from_the_same_version_is_refused(game):
    G = game["G"]
    bulk(G)
    game["run"]()
    base = first_record(game)
    with pytest.raises(host.CliError, match="--full"):
        game["run"](patch_from=base["sha256"])
    assert patches(G) == []


# ── the plan's new fields ────────────────────────────────────────────────


def test_plan_bases_carry_id_and_tweaks(packed):
    p = plan_of(packed)
    (b,) = p["bases"]
    assert b["id"] == packed["base"]["sha256"][:8]
    assert b["tweaks"]["hud_pack"]["sha256"] == sha_of(packed["pack"])
    assert b["seed_config"] is False and "usable" in b and "why" in b


def test_plan_import_needed(game, d):
    G = game["G"]
    assert plan_of(game)["import"] == {"needed": False, "why": ""}
    os.remove(os.path.join(G.game_files, ".xbr-dump.json"))
    p = plan_of(game)
    assert p["import"]["needed"] and "no dump marker" in p["import"]["why"]
    assert {r["stage"]: r["action"] for r in p["stages"]}["import"] == "run"
    # Restored by hand: the fixture's teardown needs the real folder.
    real, G.game_files = G.game_files, os.path.join(d, "gone")
    try:
        assert "is missing" in plan_of(game)["import"]["why"]
    finally:
        G.game_files = real


def test_plan_keep_row(packed):
    packed["a"].patch_from, packed["a"].keep_hud_pack = packed["base"]["sha256"], True
    try:
        p = plan_of(packed)
    finally:
        del packed["a"].patch_from, packed["a"].keep_hud_pack
    rows = {r["stage"]: r for r in p["stages"]}
    assert rows["hud"]["action"] == "keep" and rows["hud"]["why"] == ["from the store"]
    assert p["output"]["kind"] == "patch", p["output"]


# ── D8: a player's config.toml that overrides the pack ───────────────────


def test_scan_pack_key():
    assert tweaks.scan_pack_key('[textures]\npack = "none"\n') == "none"
    assert tweaks.scan_pack_key("[textures]\npack = 'a b' # c\n") == "a b"
    assert tweaks.scan_pack_key("[ textures ] # t\npack=x # c\n") == "x"
    assert tweaks.scan_pack_key('[render]\npack = "x"\n[textures]\n') is None
    assert tweaks.scan_pack_key('[textures]\npacked = "x"\n') is None
    assert tweaks.scan_pack_key('[textures]\n[other]\npack = "x"\n') is None
    assert tweaks.scan_pack_key("") is None


def test_override_value():
    t = '[textures]\npack = "%s"\n'
    assert tweaks.override_value(t % "none", PACK) == "none"
    assert tweaks.override_value(t % PACK, PACK) is None
    assert tweaks.override_value(t % "none", None) is None
    assert tweaks.override_value(t % "old/hud.pack", None) == "old/hud.pack"
    assert tweaks.override_value("[render]\n", PACK) is None


def test_plan_config_overrides(game, d, monkeypatch):
    cfg = os.path.join(d, "config.toml")
    monkeypatch.setattr(tweaks, "host_config_toml", lambda target: cfg)
    assert plan_of(game)["config_overrides"] == []
    write(cfg, '[textures]\npack = "x.pack"\n')
    assert plan_of(game)["config_overrides"] == [{"key": "textures.pack", "value": "x.pack"}]


# ── The whole-game pack, by the same machinery ───────────────────────────


TEXTURE_PACK = "enhance/textures.pack"


def make_texture_pack(G, fill=b"\0\0\xff\xff"):
    """dist/enhance/textures/ as enhance textures build leaves it."""
    d = os.path.join(G.dist, "enhance", "textures")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "textures.pack")
    sha = pack_mod.write(path, [pack_mod.PackEntry(1, 1, 1, fill)], 2, version=3)
    write(
        os.path.join(d, "textures-pack.toml"),
        'tool = "xbr 0.1"\nscale = 2\npack_sha256 = "%s"\nowner_generated = true\n' % sha,
    )
    with open(path, "rb") as f:
        return f.read()


def test_texture_flags_are_exclusive_with_each_other_and_the_hud_pack(game):
    p = main.make_parser()[0]
    for pair in (
        ["--texture-pack", "--hud-pack"],
        ["--texture-preset", "all", "--texture-pack"],
        ["--keep-texture-pack", "--clear-texture-pack"],
        ["--keep-texture-pack", "--keep-hud-pack"],
    ):
        with pytest.raises(SystemExit):
            p.parse_args(["package", "steamos"] + pair)
    a = p.parse_args(["package", "steamos", "--texture-pack"])
    assert tweaks.hud_choice(a) == "make" and tweaks.flag_kind(a) is tweaks.TEXTURES
    a = p.parse_args(["package", "steamos", "--keep-texture-pack"])
    assert tweaks.hud_choice(a) == "keep" and tweaks.flag_kind(a) is tweaks.TEXTURES
    a = p.parse_args(["package", "steamos", "--keep-texture-pack", "--full"])
    with pytest.raises(host.CliError, match="--full has no base"):
        main.cmd_package(a)


def test_a_bogus_texture_preset_is_refused_before_the_plan_runs(game, monkeypatch):
    p = main.make_parser()[0]
    ran = []
    monkeypatch.setattr(pkg, "run_plan_prefix", lambda *a: ran.append(a))
    for extra in ([], ["--plan"]):
        a = p.parse_args(["package", "steamos", "--texture-preset", "bogus"] + extra)
        with pytest.raises(host.CliError, match="preset bogus"):
            main.cmd_package(a)
    assert not ran


@pytest.fixture
def textured(game, monkeypatch):
    monkeypatch.setattr(pkg, "reads_config", lambda: True)
    monkeypatch.setattr(pkg, "config_layer", lambda enhance, lib, target: (True, {}))
    G = game["G"]
    bulk(G)
    data = make_texture_pack(G)
    game["run"](texture_pack="")
    game["base"], game["pack"] = first_record(game), data
    recompile(game)
    return game


def test_a_texture_pack_goes_in_the_bundle_and_says_it_is_owner_made(textured):
    G = textured["G"]
    (full,) = [b for b in bundles(G) if "patch-from" not in b]
    full = G.path("dist", full)
    names = tar_names(full)
    assert TEXTURE_PACK in names and PACK not in names
    m = tar_manifest(full)
    rec = m["enhance"]["texture_pack"]
    assert rec["path"] == TEXTURE_PACK and rec["sha256"] == sha_of(textured["pack"])
    with tarfile.open(full) as t:
        n = next(x for x in t.getnames() if x.endswith("/config.default.toml"))
        text = t.extractfile(n).read().decode()
    assert "[textures]" in text and "enhance/textures.pack" in text


def test_a_patch_from_a_textured_base_needs_a_word_and_keep_keeps_the_bytes(textured):
    G = textured["G"]
    base = textured["base"]
    before = bundles(G)
    with pytest.raises(host.CliError, match="--keep-texture-pack"):
        textured["run"](patch_from=base["sha256"])
    assert bundles(G) == before and patches(G) == []
    out = textured["run"](patch_from=base["sha256"], keep_texture_pack=True)
    assert "keep: the texture pack of %s" % base["version"] in out, out
    (p,) = patches(G)
    # The same bytes: the patch leaves the file alone and its record stays.
    assert TEXTURE_PACK not in tar_names(G.path("dist", p))
    assert TEXTURE_PACK not in [e["path"] for e in patch_doc(G, p)["deleted"]]
    rec = tar_manifest(G.path("dist", p))["enhance"]["texture_pack"]
    assert rec["sha256"] == sha_of(textured["pack"])


def test_keep_refuses_a_whole_game_pack_that_is_gone_rather_than_remake_it(textured):
    G = textured["G"]
    shutil.rmtree(store(textured))
    os.unlink(os.path.join(G.dist, "enhance", "textures", "textures.pack"))
    with pytest.raises(host.CliError, match=r"texture pack"):
        textured["run"](patch_from=textured["base"]["sha256"], keep_texture_pack=True)


def test_clear_removes_the_texture_pack(textured):
    out = textured["run"](patch_from=textured["base"]["sha256"], clear_texture_pack=True)
    (p,) = patches(textured["G"])
    assert TEXTURE_PACK in [e["path"] for e in patch_doc(textured["G"], p)["deleted"]], out


def test_a_texture_pack_replaces_a_base_hud_pack_and_the_plan_says_so(packed):
    G = packed["G"]
    data = make_texture_pack(G)
    packed["a"].patch_from = packed["base"]["sha256"]
    packed["a"].texture_pack = ""
    try:
        row = next(s for s in plan_of(packed)["stages"] if s["stage"] == "textures")
    finally:
        del packed["a"].patch_from, packed["a"].texture_pack
    assert "it replaces the base's HUD pack" in row["why"], row
    packed["run"](patch_from=packed["base"]["sha256"], texture_pack="")
    (p,) = patches(G)
    assert PACK in [e["path"] for e in patch_doc(G, p)["deleted"]]
    enh = tar_manifest(G.path("dist", p))["enhance"]
    assert enh["texture_pack"]["sha256"] == sha_of(data) and not enh.get("hud_pack")
