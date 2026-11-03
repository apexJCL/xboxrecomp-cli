"""`saves list` and `saves import` (saves/) on synthesized saves: no game
data, every save here is made up (a SaveMeta.xbx and random bytes signed
under the zero key where a recipe says so).

  uv run pytest tests/test_saves.py
"""

import datetime
import hashlib
import hmac
import json
import os
import random
import socket
import tomllib
import zipfile

import pytest
from conftest import GAME

from xboxrecomp_cli import main as cli_main
from xboxrecomp_cli import manifest
from xboxrecomp_cli.saves import importer, procs, sources
from xboxrecomp_cli.saves.importer import Config, SaveError
from xboxrecomp_cli.saves.sources import SourceError

TID = 0x4D530065
T = "4d530065"
OTHER = "4541005b"
SIZE = 4096
RECIPE = {"glob": "*/game.bin", "size": SIZE, "data": [0, 1000], "digest": 1000, "key": "roamable"}
NOW = datetime.datetime(2026, 11, 3, 12, 0, 0, tzinfo=datetime.UTC)


def cfg(**kw):
    base = dict(
        name="Test Game",
        slug="testgame",
        exe="test_recomp",
        title_ids=[TID],
        signing="resign",
        tdata="optional",
        signed=[RECIPE],
        data={},
        game_files="",
        version="0.0.0",
    )
    base.update(kw)
    return Config(**base)


def meta(name, extra=""):
    return b"\xff\xfe" + ("Name=%s\r\n%s" % (name, extra)).encode("utf-16-le")


def blob(seed, signed=True, key=bytes(16)):
    b = bytearray(random.Random(seed).randbytes(SIZE))
    if signed:
        b[1000:1020] = hmac.new(key, bytes(b[0:1000]), hashlib.sha1).digest()
    return bytes(b)


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def save(root, sid, name="Slot", seed=1, tid=T, kind="UDATA", **kw):
    d = os.path.join(root, kind, tid, sid)
    put(os.path.join(d, "SaveMeta.xbx"), meta(name))
    put(os.path.join(d, "game.bin"), blob(seed, **kw))
    return d


def hdd(d, name="hdd"):
    return os.path.join(d, name)


def plan_for(src, target, c=None, **kw):
    c = c or cfg()
    tree = sources.open_source(src)
    contents = importer.classify(tree, c)
    p = importer.make_plan(src, target, contents, c, **kw)
    p.backup_dir = importer.backup_dir(target)
    return tree, p


def do_import(src, target, c=None, **kw):
    c = c or cfg()
    tree, p = plan_for(src, target, c, **kw)
    with tree:
        os.makedirs(target, exist_ok=True)
        r = importer.execute(p, c, now=NOW)
    return p, r


def read(path):
    with open(path, "rb") as f:
        return f.read()


# ---- sources: the save root ----


@pytest.mark.parametrize(
    "layout",
    [
        "hdd",  # an install's hdd/ (holds UDATA)
        "E",  # a console's E: copy
        "wrap/E",  # one folder above
        "UDATA",  # the UDATA folder itself
        "title",  # the title folder itself
    ],
)
def test_root_by_layout(d, layout):
    top = os.path.join(d, "src")
    if layout == "UDATA":
        save(top, "AAAA", tid=T)
        src = os.path.join(top, "UDATA")
    elif layout == "title":
        save(top, "AAAA")
        src = os.path.join(top, "UDATA", T)
    else:
        save(os.path.join(top, layout), "AAAA")
        src = top if layout != "hdd" else os.path.join(top, "hdd")
    with sources.open_source(src) as tree:
        c = importer.classify(tree, cfg())
    assert [s.sid for s in c.saves] == ["AAAA"]
    assert c.saves[0].name == "Slot"


def test_zip_root_and_deflate(d):
    z = os.path.join(d, "s.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("backup/E/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("Z"))
        f.writestr("backup/E/UDATA/%s/AAAA/game.bin" % T, blob(1))
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg())
        assert tree.where == "backup/E"
        assert [s.name for s in c.saves] == ["Z"]


def test_two_roots_refused(d):
    save(os.path.join(d, "a", "E"), "AAAA")
    save(os.path.join(d, "b", "E"), "BBBB")
    with pytest.raises(SourceError, match="several save roots"):
        sources.open_source(d)


def test_no_saves(d):
    put(os.path.join(d, "x", "readme.txt"), b"hi")
    with pytest.raises(SourceError, match="no saves"):
        sources.open_source(os.path.join(d, "x"))


def test_loose_saves_are_no_title(d):
    top = os.path.join(d, "loose")
    put(os.path.join(top, "AAAA", "SaveMeta.xbx"), meta("x"))
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    assert c.saves == [] and c.loose == ["loose/AAAA"]


def test_not_a_zip(d):
    put(os.path.join(d, "img.qcow2"), b"QFI\xfb" + bytes(100))
    with pytest.raises(SourceError, match="not a folder or a zip"):
        sources.open_source(os.path.join(d, "img.qcow2"))


# ---- sources: names, links, caps ----


@pytest.mark.parametrize(
    "name,why",
    [
        ("", "empty"),
        ("..", "'.'"),
        ("a\x01b", "control"),
        ("a\\b", "backslash"),
        ("c:x", "':'"),
        ("a*b", "Windows refuses"),
        ("a?", "Windows refuses"),
        ('a"', "Windows refuses"),
        ("a<", "Windows refuses"),
        ("a|", "Windows refuses"),
        ("x" * 43, "42 bytes"),
        ("é" * 22, "42 bytes"),
        ("name.", "dot or space"),
        ("name ", "dot or space"),
        ("CON", "device"),
        ("nul.txt", "device"),
        ("com1", "device"),
        ("LPT9.bin", "device"),
    ],
)
def test_name_rules(name, why):
    assert why in (sources.name_problem(name) or "")


@pytest.mark.parametrize("name", ["SaveMeta.xbx", "x" * 42, "COM0", "com10", "console", "a.b.c"])
def test_names_allowed(name):
    assert sources.name_problem(name) is None


def test_bad_name_refuses_only_its_save(d):
    top = hdd(d)
    save(top, "GOOD")
    bad = save(top, "BADX")
    put(os.path.join(bad, "x" * 43), b"1")
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    by = {s.sid: s for s in c.saves}
    assert not by["GOOD"].why
    assert "42 bytes" in by["BADX"].why


def test_zip_traversal_and_absolute(d):
    z = os.path.join(d, "evil.zip")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
        f.writestr("hdd/UDATA/%s/AAAA/../../../../escape" % T, b"x")
        f.writestr("/abs/UDATA/%s/BBBB/SaveMeta.xbx" % T, meta("abs"))
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg())
    s = c.saves[0]
    assert s.sid == "AAAA" and "'..'" in s.why
    assert any("absolute" in w for _, w in c.notes)


def test_zip_symlink_entry(d):
    z = os.path.join(d, "link.zip")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
        info = zipfile.ZipInfo("hdd/UDATA/%s/AAAA/game.bin" % T)
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        f.writestr(info, "/etc/passwd")
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg())
    assert "link" in c.saves[0].why


def test_zip_size_cap(d, monkeypatch):
    monkeypatch.setattr(sources, "MAX_FILE", 100)
    z = os.path.join(d, "big.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
        f.writestr("hdd/UDATA/%s/AAAA/game.bin" % T, bytes(10000))
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg())
    assert "16 MiB" in c.saves[0].why


def test_zip_lying_size_caught(d, monkeypatch):
    # A zip whose index understates a file: the read is capped and checked.
    z = os.path.join(d, "lie.zip")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
    with sources.open_source(z) as tree:
        e = tree.files[0]
        e.size = 3
        with pytest.raises(SourceError, match="changed"):
            e.read()


def test_too_many_entries(d, monkeypatch):
    monkeypatch.setattr(sources, "MAX_ENTRIES", 5)
    top = hdd(d)
    for i in range(4):
        save(top, "S%03d" % i)
    with pytest.raises(SourceError, match="too many"):
        sources.open_source(top)


def test_total_cap(d, monkeypatch):
    monkeypatch.setattr(sources, "MAX_TOTAL", 5000)
    top = hdd(d)
    save(top, "AAAA")
    save(top, "BBBB")
    with pytest.raises(SourceError, match="64 MiB"):
        sources.open_source(top)


def test_depth_cap(d):
    top = hdd(d)
    s = save(top, "AAAA")
    put(os.path.join(s, "a", "b", "c", "d", "e"), b"x")
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    assert "too deep" in c.saves[0].why


def test_folder_symlinks_never_followed(d):
    top = hdd(d)
    s = save(top, "AAAA")
    outside = os.path.join(d, "secret")
    put(outside, b"secret")
    os.symlink(outside, os.path.join(s, "link.bin"))
    linked = os.path.join(d, "elsewhere")
    os.makedirs(linked)
    os.symlink(linked, os.path.join(top, "UDATA", T, "LINKDIR"))
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    by = {s.sid: s for s in c.saves}
    assert "link" in by["AAAA"].why
    assert "LINKDIR" not in by or by["LINKDIR"].why


def test_source_itself_a_link(d):
    top = hdd(d)
    save(top, "AAAA")
    os.symlink(top, os.path.join(d, "ln"))
    with pytest.raises(SourceError, match="is a link"):
        sources.open_source(os.path.join(d, "ln"))


def test_case_twins_refused(d):
    z = os.path.join(d, "twins.zip")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
        f.writestr("hdd/UDATA/%s/AAAA/game.bin" % T, blob(1))
        f.writestr("hdd/UDATA/%s/AAAA/GAME.BIN" % T, blob(2))
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg())
    assert "differ only by case" in c.saves[0].why


# ---- classification ----


def test_foreign_no_title_mixed_case(d):
    top = hdd(d)
    save(top, "AAAA", tid=T.upper())
    save(top, "BBBB", tid=OTHER)
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    assert [s.sid for s in c.saves] == ["AAAA"]
    assert c.foreign == {OTHER: 1}


def test_missing_savemeta(d):
    top = hdd(d)
    put(os.path.join(top, "UDATA", T, "NOMETA", "game.bin"), blob(1))
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    assert "no SaveMeta" in c.saves[0].why


def test_recipe_size_mismatch(d):
    top = hdd(d)
    s = save(top, "AAAA")
    put(os.path.join(s, "game.bin"), bytes(10))
    with sources.open_source(top) as tree:
        c = importer.classify(tree, cfg())
    assert "10 bytes, not the 4096" in c.saves[0].why


def test_no_saves_table_warns(d):
    top = hdd(d)
    save(top, "AAAA")
    _, p = plan_for(top, hdd(d, "t"), cfg(signing="", signed=[]))
    assert p.saves[0].signing == "unknown"
    assert any("no [saves] signing" in w for w in p.warnings)


def test_savemeta_name_and_nocopy():
    assert importer.meta_value(meta("Hello", "NoCopy=1\r\n"), "NoCopy") == "1"
    assert importer.meta_value(meta("Hello")[2:], "name") == "Hello"


# ---- signing ----


def test_zero_key_pinned():
    # The kernel's XboxSignatureKey (toolkit kernel_bridge.c): if it ever
    # changes, every save moved between builds needs this re-sign.
    assert importer.ZERO_KEY == b"\x00" * 16


def test_hmac_vector():
    # HMAC-SHA1 under 16 zero bytes over "abc", pinned.
    got = importer.resign(b"abc" + bytes(20), [{"data": [0, 3], "digest": 3}])
    assert got[3:].hex() == "9b4a918f398d74d3e367970aba3cbe54e4d2b5d9"


def test_blinx2_recipe():
    # BLiNX 2's blinx2data.bin (openspec save-import task 0.1): 81920
    # bytes, one HMAC-SHA1 over [0, 33972) with the digest at 33972. The
    # file here is random bytes of that size, signed by hand.
    r = {"glob": "*/blinx2data.bin", "size": 81920, "data": [0, 33972], "digest": 33972}
    b = bytearray(random.Random(81920).randbytes(81920))
    b[33972:33992] = hmac.new(bytes(16), bytes(b[0:33972]), hashlib.sha1).digest()
    assert importer.signed_ok(bytes(b), [r])
    stale = bytes(b[:33972]) + bytes(20) + bytes(b[33992:])
    assert not importer.signed_ok(stale, [r])
    assert importer.resign(stale, [r]) == bytes(b)
    doc = _doc()
    doc["saves"] = {"signing": "resign", "tdata": "ignore", "signed": [dict(r, key="roamable")]}
    assert manifest.validate(doc)["saves"]["signed"][0]["digest"] == 33972


def test_two_recipes_one_glob():
    r1 = {"data": [0, 100], "digest": 100}
    r2 = {"data": [200, 300], "digest": 300}
    b = random.Random(5).randbytes(400)
    out = importer.resign(b, [r1, r2])
    assert out[100:120] == hmac.new(bytes(16), out[0:100], hashlib.sha1).digest()
    assert out[300:320] == hmac.new(bytes(16), out[200:300], hashlib.sha1).digest()
    assert out[:100] == b[:100] and out[200:300] == b[200:300]
    assert importer.signed_ok(out, [r1, r2])
    assert importer.resign(out, [r1, r2]) == out


def test_import_resigns_foreign_keys(d):
    top = hdd(d)
    save(top, "AAAA", seed=3, key=b"\x11" * 16)
    target = hdd(d, "t")
    do_import(top, target)
    got = read(os.path.join(target, "UDATA", T, "AAAA", "game.bin"))
    assert got == blob(3)  # the same data, signed under the zero key
    assert got != blob(3, key=b"\x11" * 16)


def test_zero_key_save_unchanged(d):
    top = hdd(d)
    save(top, "AAAA", seed=4)
    target = hdd(d, "t")
    do_import(top, target)
    assert read(os.path.join(target, "UDATA", T, "AAAA", "game.bin")) == blob(4)


def test_signing_none_untouched(d):
    top = hdd(d)
    save(top, "AAAA", seed=4, signed=False)
    target = hdd(d, "t")
    do_import(top, target, cfg(signing="none", signed=[]))
    assert read(os.path.join(target, "UDATA", T, "AAAA", "game.bin")) == blob(4, signed=False)


# ---- backup ----


def _zip_names(path):
    with zipfile.ZipFile(path) as z:
        return z.namelist(), z.read(sources.MANIFEST).decode()


def test_backup_is_a_menu_saves_zip(d):
    src = hdd(d, "src")
    save(src, "NEW1", seed=9)
    target = hdd(d)
    save(target, "OLD1", seed=8)
    put(os.path.join(target, "TDATA", T, "cfg.dat"), b"cfg")
    p, r = do_import(src, target)
    [b] = r["backup"]
    assert b == os.path.join(
        d, "config", "exports", "save-backups", "testgame-saves-backup-20261103-120000.zip"
    )
    names, text = _zip_names(b)
    assert names[0] == sources.MANIFEST
    m = tomllib.loads(text)
    assert m["kind"] == "saves" and m["format"] == 1 and m["title_id"] == T
    assert m["keys"] == "zero" and m["files"] == 3
    assert m["bytes"] == len(meta("Slot")) + SIZE + 3
    assert "hdd/UDATA/%s/OLD1/game.bin" % T in names
    assert "hdd/TDATA/%s/cfg.dat" % T in names
    with zipfile.ZipFile(b) as z:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        for i in z.infolist():
            assert sources.name_problem(i.filename.rstrip("/").split("/")[-1]) is None
    # The CLI reads its own backup back as a source, so it restores too.
    with sources.open_source(b) as tree:
        c = importer.classify(tree, cfg())
    assert [s.sid for s in c.saves] == ["OLD1"]


def test_backup_stamp_collision(d):
    target = hdd(d)
    save(target, "OLD1")
    bdir = importer.backup_dir(target)
    put(os.path.join(bdir, "testgame-saves-backup-20261103-120000.zip"), b"other")
    c = cfg()
    b = importer.write_backup(c, target, TID, bdir, NOW)
    assert b.endswith("-20261103-120000-2.zip")
    assert read(os.path.join(bdir, "testgame-saves-backup-20261103-120000.zip")) == b"other"


def test_backup_with_to(d):
    target = os.path.join(d, "elsewhere", "hdd")
    save(target, "OLD1")
    src = hdd(d, "src")
    save(src, "NEW1")
    _, r = do_import(src, target)
    assert r["backup"][0].startswith(os.path.join(d, "elsewhere", "config", "exports"))


def test_nothing_to_back_up(d):
    src = hdd(d, "src")
    save(src, "NEW1")
    _, r = do_import(src, hdd(d, "fresh"))
    assert r["backup"] == []


def test_backup_failure_stops_import(d, monkeypatch):
    src = hdd(d, "src")
    save(src, "NEW1")
    target = hdd(d)
    save(target, "OLD1")

    def boom(path, files):
        raise OSError("disk full")

    monkeypatch.setattr(importer, "_verify_backup", boom)
    with pytest.raises(SaveError, match="backup"):
        do_import(src, target)
    assert not os.path.exists(os.path.join(target, "UDATA", T, "NEW1"))


# ---- conflicts ----


def test_skip_then_replace(d):
    src = hdd(d, "src")
    save(src, "AAAA", name="new", seed=2)
    target = hdd(d)
    save(target, "aaaa", name="old", seed=1)
    p, _ = do_import(src, target)
    assert p.saves[0].action == "skip"
    old = os.path.join(target, "UDATA", T, "aaaa")
    assert read(os.path.join(old, "game.bin")) == blob(1)
    p, r = do_import(src, target, replace=True)
    assert p.saves[0].action == "replace"
    # replaced in place under the name already there (no case twin)
    assert os.listdir(os.path.join(target, "UDATA", T)) == ["aaaa"]
    assert read(os.path.join(old, "game.bin")) == blob(2)
    assert r["backup"]
    assert not os.path.exists(os.path.join(target, importer.STAGING))


def test_only_some_saves(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    save(src, "BBBB")
    target = hdd(d, "t")
    do_import(src, target, only=["bbbb"])
    assert os.listdir(os.path.join(target, "UDATA", T)) == ["BBBB"]
    with pytest.raises(SaveError, match="no save CCCC".lower()):
        plan_for(src, target, only=["CCCC"])


def test_tdata_modes(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    put(os.path.join(src, "TDATA", T, "sub", "a.cfg"), b"new")
    target = hdd(d, "t")
    _, p = plan_for(src, target, cfg(tdata="ignore"), with_tdata=True)
    assert p.tdata == []
    _, p = plan_for(src, target, cfg(tdata="optional"))
    assert p.tdata == [] and any("--with-tdata" in w for w in p.warnings)
    _, p = plan_for(src, target, cfg(tdata="optional"), with_tdata=True)
    assert [a for _, _, a, _ in p.tdata] == ["import"]
    _, p = plan_for(src, target, cfg(tdata="required"))
    assert [a for _, _, a, _ in p.tdata] == ["import"]


def test_tdata_per_file_skip_replace(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    put(os.path.join(src, "TDATA", T, "a.cfg"), b"new-a")
    put(os.path.join(src, "TDATA", T, "b.cfg"), b"new-b")
    target = hdd(d)
    put(os.path.join(target, "TDATA", T, "A.CFG"), b"old-a")
    c = cfg(tdata="required")
    do_import(src, target, c)
    td = os.path.join(target, "TDATA", T)
    assert read(os.path.join(td, "A.CFG")) == b"old-a"
    assert read(os.path.join(td, "b.cfg")) == b"new-b"
    do_import(src, target, c, replace=True)
    assert read(os.path.join(td, "A.CFG")) == b"new-a"
    assert sorted(os.listdir(td)) == ["A.CFG", "b.cfg"]


def test_required_tdata_missing_warns(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    _, p = plan_for(src, hdd(d, "t"), cfg(tdata="required"))
    assert any("no TDATA" in w for w in p.warnings)


def test_title_files_only_when_absent(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    put(os.path.join(src, "UDATA", T, "TitleMeta.xbx"), b"new")
    target = hdd(d)
    do_import(src, target)
    assert read(os.path.join(target, "UDATA", T, "TitleMeta.xbx")) == b"new"
    put(os.path.join(target, "UDATA", T, "TitleMeta.xbx"), b"mine")
    save(src, "BBBB")
    do_import(src, target, replace=True)
    assert read(os.path.join(target, "UDATA", T, "TitleMeta.xbx")) == b"mine"


# ---- staging and recovery ----


def test_interrupted_replace_keeps_old_save(d, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA", seed=2)
    target = hdd(d)
    save(target, "AAAA", seed=1)
    real = os.rename
    calls = []

    def flaky(a, b):
        calls.append((a, b))
        # the rename of the new save into place fails after the old one
        # went aside
        if os.path.basename(a).startswith("new-") and b.endswith("AAAA"):
            raise OSError("in use")
        return real(a, b)

    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(SaveError, match="as it was"):
        do_import(src, target, replace=True)
    monkeypatch.setattr(os, "rename", real)
    assert read(os.path.join(target, "UDATA", T, "AAAA", "game.bin")) == blob(1)
    assert not os.path.exists(os.path.join(target, importer.STAGING))


def test_stale_staging_recovered(d):
    target = hdd(d)
    save(target, "KEEP")
    stage = os.path.join(target, importer.STAGING)
    # A run stopped after renaming AAAA aside: its old copy and its record.
    old = os.path.join(stage, "old-1")
    put(os.path.join(old, "SaveMeta.xbx"), meta("old"))
    with open(old + ".json", "w", encoding="utf-8") as f:
        json.dump(["UDATA", T, "AAAA"], f)
    put(os.path.join(stage, "new-2", "junk"), b"x")
    assert importer.stale_staging(target)
    back = importer.recover(target)
    assert back == [os.path.join(target, "UDATA", T, "AAAA")]
    assert os.path.isfile(os.path.join(target, "UDATA", T, "AAAA", "SaveMeta.xbx"))
    assert not os.path.exists(stage)


def test_stale_staging_old_with_record_and_dest_taken_refuses(d):
    # A record still there means the run never finished: this may be a
    # failed undo whose marker could not be written, so nothing is deleted.
    target = hdd(d)
    save(target, "AAAA", name="new")
    stage = os.path.join(target, importer.STAGING)
    old = os.path.join(stage, "old-1")
    put(os.path.join(old, "SaveMeta.xbx"), meta("old"))
    with open(old + ".json", "w", encoding="utf-8") as f:
        json.dump(["UDATA", T, "AAAA"], f)
    with pytest.raises(SaveError, match="keep whichever") as e:
        importer.recover(target)
    assert e.value.code == "stage"
    assert os.path.isfile(os.path.join(old, "SaveMeta.xbx"))
    m = read(os.path.join(target, "UDATA", T, "AAAA", "SaveMeta.xbx"))
    assert importer.meta_value(m, "Name") == "new"


def test_stale_staging_old_without_record_dropped(d):
    # A finished run drops the records before removing staging; a stop in
    # between leaves aside copies that are safe to delete.
    target = hdd(d)
    save(target, "AAAA", name="new")
    stage = os.path.join(target, importer.STAGING)
    put(os.path.join(stage, "old-1", "SaveMeta.xbx"), meta("old"))
    assert importer.recover(target) == []
    assert not os.path.exists(stage)
    m = read(os.path.join(target, "UDATA", T, "AAAA", "SaveMeta.xbx"))
    assert importer.meta_value(m, "Name") == "new"


def test_success_drops_records_before_staging(d, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA", name="new")
    target = hdd(d)
    save(target, "AAAA", name="old")
    seen = []
    real = importer.shutil.rmtree

    def spy(path, *a, **k):
        if path.endswith(importer.STAGING):
            seen.extend(n for n in os.listdir(path) if n.endswith(".json"))
        return real(path, *a, **k)

    monkeypatch.setattr(importer.shutil, "rmtree", spy)
    do_import(src, target, replace=True)
    assert seen == []


# ---- safety ----


def test_refuse_while_game_runs(d):
    c = cfg()
    ps = [(4242, 1, ["/Applications/T.app/Contents/MacOS/test_recomp"])]
    with pytest.raises(SaveError, match="is running") as e:
        importer.safety(c, hdd(d), hdd(d, "src"), procs_list=ps)
    assert e.value.code == "running"


def test_no_process_list_warns(d, monkeypatch):
    monkeypatch.setattr(procs, "list_processes", lambda exe: None)
    w = importer.safety(cfg(), hdd(d), hdd(d, "src"))
    assert any("could not check" in x for x in w)


@pytest.mark.parametrize(
    "srcdir,argv",
    [
        ("Cxbx-Reloaded/EmuDisk/Partition1", ["C:\\Cxbx\\cxbxr-ldr.exe", "/load", "x.xbe"]),
        ("Cxbx-Reloaded/EmuDisk/Partition1", ["cxbx.exe"]),
        ("xemu/hdd", ["/usr/bin/xemu"]),
        ("xemu/hdd", ["/usr/bin/flatpak", "run", "app.xemu.xemu"]),
    ],
)
def test_refuse_while_emulator_runs(d, srcdir, argv):
    src = os.path.join(d, srcdir)
    os.makedirs(src)
    with pytest.raises(SaveError) as e:
        importer.safety(cfg(), hdd(d), src, procs_list=[(77, 1, argv)])
    assert e.value.code == "emulator"
    # the same emulator running is fine for a source not on its disk
    importer.safety(cfg(), hdd(d), hdd(d, "plain"), procs_list=[(77, 1, argv)])


def test_parse_tasklist():
    text = '"System Idle Process","0","Services","0","8 K"\r\n"cxbxr-ldr.exe","912","Console","1","40,000 K"\r\n'
    ps = procs.parse_tasklist(text)
    assert ps[1] == (912, 0, ["cxbxr-ldr.exe"])
    assert procs.emulator_running(ps, procs.CXBXR, me=1) == [(912, "cxbxr-ldr.exe")]


def test_refuse_target_in_bundle(d):
    t = os.path.join(d, "Game.app", "Contents", "hdd")
    with pytest.raises(SaveError, match="app bundle"):
        importer.safety(cfg(), t, None, procs_list=[])


def test_refuse_target_in_game_files(d):
    gf = os.path.join(d, "game_files")
    os.makedirs(gf)
    with pytest.raises(SaveError, match="game files"):
        importer.safety(cfg(game_files=gf), os.path.join(gf, "hdd"), None, procs_list=[])


def test_refuse_link_in_target(d):
    target = hdd(d)
    real = os.path.join(d, "real")
    os.makedirs(os.path.join(real))
    os.makedirs(os.path.join(target, "UDATA"))
    os.symlink(real, os.path.join(target, "UDATA", T))
    with pytest.raises(SaveError, match="is a link") as e:
        importer.safety(cfg(), target, None, procs_list=[])
    assert e.value.code == "link"


def test_refuse_link_inside_target_tree(d):
    src = hdd(d, "src")
    save(src, "NEW1")
    target = hdd(d)
    s = save(target, "OLD1")
    os.symlink(os.path.join(d, "x"), os.path.join(s, "ln"))
    with pytest.raises(SaveError, match="is a link"):
        do_import(src, target)


def test_legacy_warning(d):
    gf = os.path.join(d, "game_files")
    save(gf, "OLD1")
    w = importer.safety(cfg(game_files=gf), hdd(d), None, procs_list=[])
    assert any("older builds kept saves" in x for x in w)
    save(hdd(d), "NEW1")
    assert importer.safety(cfg(game_files=gf), hdd(d), None, procs_list=[]) == []


# ---- the target ----


def test_default_target(d):
    c = cfg(
        data={
            "dir_env": "TG_DATA_DIR",
            "windows": "%LOCALAPPDATA%\\TG",
            "macos": "~/Library/Application Support/TG",
            "steamos": os.path.join(d, "Games", "TG"),
        }
    )
    t, _ = importer.default_target(c, "macos", env={"TG_DATA_DIR": d})
    assert t == os.path.join(d, "hdd")
    t, _ = importer.default_target(c, "macos", env={})
    assert t.endswith(os.path.join("Library", "Application Support", "TG", "hdd"))
    with pytest.raises(SaveError, match="--to"):
        importer.default_target(c, "linux", env={})
    os.makedirs(os.path.join(d, "Games", "TG"))
    t, _ = importer.default_target(c, "linux", env={})
    assert t == os.path.join(d, "Games", "TG", "hdd")


# ---- the menu's saves zip ----


def menu_zip(path, tid=T, files=None, keys="zero", count=None):
    files = files or {
        "hdd/UDATA/%s/AAAA/SaveMeta.xbx" % tid: meta("Menu"),
        "hdd/UDATA/%s/AAAA/game.bin" % tid: blob(6),
    }
    n = len(files) if count is None else count
    text = (
        '# Saves exported by the in-game menu (xboxrecomp)\nformat = 1\nkind = "saves"\n'
        'title_id = "%s"\ntitle = "Test"\ncreated = "2026-11-03T00:00:00Z"\nkeys = "%s"\n'
        'files = %d\nbytes = %d\ntoolkit = "x"\n'
        % (tid, keys, n, sum(len(v) for v in files.values()))
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(sources.MANIFEST, text)
        z.writestr("hdd/", b"")
        z.writestr("hdd/UDATA/", b"")
        for k, v in files.items():
            z.writestr(k, v)


def test_menu_zip_imports(d):
    z = os.path.join(d, "menu.zip")
    menu_zip(z)
    target = hdd(d, "t")
    p, _ = do_import(z, target)
    assert p.contents.manifest["kind"] == "saves"
    assert read(os.path.join(target, "UDATA", T, "AAAA", "game.bin")) == blob(6)


def test_menu_zip_incomplete(d):
    z = os.path.join(d, "menu.zip")
    menu_zip(z, count=5)
    with pytest.raises(SourceError, match="incomplete"):
        with sources.open_source(z) as tree:
            importer.classify(tree, cfg())


def test_menu_zip_other_keys_warns(d):
    z = os.path.join(d, "menu.zip")
    menu_zip(z, keys="0123456789abcdef")
    _, p = plan_for(z, hdd(d, "t"))
    assert any("other console keys" in w for w in p.warnings)


def test_menu_zip_title_mismatch(d):
    z = os.path.join(d, "menu.zip")
    menu_zip(z, files={"hdd/UDATA/%s/AAAA/SaveMeta.xbx" % OTHER: meta("x")}, tid=T)
    # the manifest's title_id must match the folders inside
    with zipfile.ZipFile(z) as f:
        assert sources.MANIFEST in f.namelist()
    with pytest.raises(SourceError, match="says title"):
        with sources.open_source(z) as tree:
            importer.classify(tree, cfg())


# ---- game.toml [saves] ----


def _doc():
    with open(os.path.join(GAME, "game.toml"), "rb") as f:
        return tomllib.load(f)


def test_manifest_saves_defaults():
    doc = _doc()
    doc["saves"] = {"signing": "none"}
    m = manifest.validate(doc)
    assert m["saves"]["title_ids"] == [TID] and m["saves"]["tdata"] == "optional"
    assert "saves" not in manifest.validate(_doc())


@pytest.mark.parametrize(
    "saves,msg",
    [
        ({"signing": "maybe"}, "saves.signing"),
        ({"tdata": "always"}, "saves.tdata"),
        ({"signing": "resign"}, "needs at least one"),
        ({"signing": "none", "signed": [RECIPE]}, 'only with signing = "resign"'),
        ({"signing": "resign", "signed": [dict(RECIPE, glob="../x")]}, ".glob"),
        ({"signing": "resign", "signed": [dict(RECIPE, data=[10, 5])]}, ".data"),
        ({"signing": "resign", "signed": [dict(RECIPE, digest=SIZE - 10)]}, ".digest"),
        ({"signing": "resign", "signed": [dict(RECIPE, digest=500)]}, "inside"),
        ({"signing": "resign", "signed": [dict(RECIPE, key="console")]}, ".key"),
        ({"signing": "resign", "signed": [dict(RECIPE, extra=1)]}, "unknown key"),
        (
            {"signing": "resign", "signed": [RECIPE, dict(RECIPE, size=SIZE * 2)]},
            "share size",
        ),
        (
            {
                "signing": "resign",
                "signed": [RECIPE, dict(RECIPE, data=[2000, 3000], digest=1010)],
            },
            "overlaps",
        ),
        ({"title_ids": []}, "at least one title"),
    ],
)
def test_manifest_saves_errors(saves, msg):
    doc = _doc()
    doc["saves"] = saves
    with pytest.raises(manifest.ManifestError, match=msg.replace("[", r"\[").replace("(", r"\(")):
        manifest.validate(doc)


def test_manifest_two_recipes_one_glob_ok():
    doc = _doc()
    doc["saves"] = {
        "signing": "resign",
        "signed": [RECIPE, dict(RECIPE, data=[2000, 3000], digest=3000)],
    }
    assert len(manifest.validate(doc)["saves"]["signed"]) == 2


# ---- the command ----


def run_cli(args, capsys, monkeypatch):
    monkeypatch.setattr(procs, "list_processes", lambda exe: [])
    rc = cli_main.main(["--game", GAME] + args)
    return rc, capsys.readouterr()


def test_cli_dry_run_writes_nothing(d, capsys, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA")
    target = hdd(d, "t")
    rc, out = run_cli(
        ["saves", "import", "--from", src, "--to", target, "--dry-run", "--json"],
        capsys,
        monkeypatch,
    )
    assert rc == 0, out.err
    plan = json.loads(out.out)
    assert plan["dry_run"] and plan["saves"][0]["action"] == "import"
    assert not os.path.exists(target)


def test_cli_import_and_list(d, capsys, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA", name="Mine")
    target = hdd(d, "t")
    rc, out = run_cli(["saves", "import", "--from", src, "--to", target], capsys, monkeypatch)
    assert rc == 0, out.err
    assert "imported 1 save" in out.out
    rc, out = run_cli(["saves", "list", "--to", target, "--json"], capsys, monkeypatch)
    assert rc == 0
    assert [s["name"] for s in json.loads(out.out)["saves"]] == ["Mine"]
    rc, out = run_cli(["saves", "list", "--from", src], capsys, monkeypatch)
    assert "Mine" in out.out


def test_cli_refusal_code(d, capsys, monkeypatch):
    rc, out = run_cli(
        ["saves", "import", "--from", os.path.join(d, "nope"), "--to", hdd(d)],
        capsys,
        monkeypatch,
    )
    assert rc == 1 and "refused: source" in out.err


def test_cli_refuses_running_game(d, capsys, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA")
    monkeypatch.setattr(
        procs, "list_processes", lambda exe: [(99999, 1, ["/x/cat_recomp"])], raising=True
    )
    rc = cli_main.main(["--game", GAME, "saves", "import", "--from", src, "--to", hdd(d, "t")])
    err = capsys.readouterr().err
    assert rc == 1 and "refused: running" in err
    assert not os.path.exists(hdd(d, "t"))


# ---- review fixes: undo, case clashes, errors, the lock ----


def _two_replaces(d):
    src = hdd(d, "src")
    target = hdd(d)
    for sid, seed in (("S1", 1), ("S2", 2)):
        save(src, sid, seed=seed + 10)
        save(target, sid, seed=seed)
    return src, target


def test_second_replace_fails_first_rolled_back(d, monkeypatch):
    src, target = _two_replaces(d)
    real = os.rename

    def flaky(a, b):
        if os.path.basename(a).startswith("new-") and os.path.basename(b) == "S2":
            raise OSError("in use")
        return real(a, b)

    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(SaveError, match="as it was") as e:
        do_import(src, target, replace=True)
    assert e.value.code == "write"
    monkeypatch.setattr(os, "rename", real)
    for sid, seed in (("S1", 1), ("S2", 2)):
        assert read(os.path.join(target, "UDATA", T, sid, "game.bin")) == blob(seed)
    assert sorted(os.listdir(os.path.join(target, "UDATA", T))) == ["S1", "S2"]
    assert not os.path.exists(os.path.join(target, importer.STAGING))
    assert not os.path.exists(os.path.join(target, importer.LOCK))


def test_failed_undo_keeps_staging_and_blocks_next_run(d, monkeypatch):
    src, target = _two_replaces(d)
    real = os.rename

    def flaky(a, b):
        if os.path.basename(a).startswith("new-") and os.path.basename(b) == "S2":
            raise OSError("in use")
        if os.path.basename(b).startswith("undo-"):
            raise OSError("still in use")
        return real(a, b)

    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(SaveError, match="undoing the swap failed"):
        do_import(src, target, replace=True)
    monkeypatch.setattr(os, "rename", real)
    stage = os.path.join(target, importer.STAGING)
    assert os.path.isfile(os.path.join(stage, importer.UNDO_FAILED))
    # The old S1 is still in staging, never deleted.
    olds = [n for n in os.listdir(stage) if n.startswith("old-") and not n.endswith(".json")]
    assert any(
        read(os.path.join(stage, n, "game.bin")) == blob(1)
        for n in olds
        if os.path.isfile(os.path.join(stage, n, "game.bin"))
    )
    with pytest.raises(SaveError) as e:
        do_import(src, target, replace=True)
    assert e.value.code == "stage"
    assert os.path.isdir(stage)


@pytest.mark.parametrize(
    "names",
    [
        ("UDATA/%s/ABC/SaveMeta.xbx" % T, "UDATA/%s/abc/SaveMeta.xbx" % T),
        ("UDATA/%s/ABC/a.bin" % T, "UDATA/%s/abc/b.bin" % T),
        ("UDATA/%s/ZZ/SaveMeta.xbx" % T.upper(), "UDATA/%s/ZZ/SaveMeta.xbx" % T),
    ],
)
def test_case_clash_at_any_level(d, names):
    z = os.path.join(d, "c.zip")
    with zipfile.ZipFile(z, "w") as f:
        for n in names:
            f.writestr("hdd/" + n, meta("x"))
        f.writestr("hdd/UDATA/%s/OK/SaveMeta.xbx" % T, meta("ok"))
    with sources.open_source(z) as tree:
        c = importer.classify(tree, cfg(signing="none", signed=[]))
    good = [s for s in c.saves if not s.why]
    if names[0].split("/")[1] != names[1].split("/")[1]:
        assert good == []  # the title folders clash: nothing under them
    else:
        assert [s.sid for s in good] == ["OK"]
    assert all(
        "differ only by case" in (s.why or "") for s in c.saves if s.sid.lower() in ("abc", "zz")
    )
    assert any("differ only by case" in w for _, w in c.notes) or any(
        "differ only by case" in (s.why or "") for s in c.saves
    )
    target = hdd(d, "t")
    if good:
        do_import(z, target, cfg(signing="none", signed=[]))
        assert os.listdir(os.path.join(target, "UDATA", T)) == ["OK"]
        assert not os.path.exists(os.path.join(target, importer.STAGING))


def test_staging_error_is_a_refusal(d, monkeypatch):
    src = hdd(d, "src")
    save(src, "AAAA")
    target = hdd(d)
    save(target, "AAAA", seed=7)

    def full(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(importer, "_stage_write", full)
    with pytest.raises(SaveError, match="could not be staged") as e:
        do_import(src, target, replace=True)
    assert e.value.code == "write"
    assert not os.path.exists(os.path.join(target, importer.STAGING))
    assert read(os.path.join(target, "UDATA", T, "AAAA", "game.bin")) == blob(7)


def test_staging_crc_error_is_a_refusal(d):
    z = os.path.join(d, "crc.zip")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hdd/UDATA/%s/AAAA/SaveMeta.xbx" % T, meta("ok"))
        f.writestr("hdd/UDATA/%s/AAAA/game.bin" % T, blob(1))
    raw = bytearray(read(z))
    i = raw.index(blob(1)[:16])
    raw[i] ^= 0xFF
    put(z, bytes(raw))
    target = hdd(d, "t")
    # A recipe reads the file while classifying: the save is refused there.
    _, p = plan_for(z, target)
    assert "CRC" in p.saves[0].why
    # With none the damage shows first while staging.
    with pytest.raises(SaveError) as e:
        do_import(z, target, cfg(signing="none", signed=[]))
    assert e.value.code == "write"
    assert not os.path.exists(os.path.join(target, importer.STAGING))


def test_backup_read_error_and_half_zip_removed(d, monkeypatch):
    src = hdd(d, "src")
    save(src, "NEW1")
    target = hdd(d)
    save(target, "OLD1")
    real_open = open

    def bad_open(path, *a, **kw):
        if str(path).endswith("game.bin") and "OLD1" in str(path):
            raise PermissionError(13, "denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", bad_open)
    with pytest.raises(SaveError) as e:
        do_import(src, target)
    monkeypatch.setattr("builtins.open", real_open)
    assert e.value.code == "backup"

    def boom(z, name, data, when):
        raise OSError("disk full")

    monkeypatch.setattr(importer, "_zput", boom)
    with pytest.raises(SaveError):
        do_import(src, target)
    bdir = importer.backup_dir(target)
    assert not os.path.isdir(bdir) or os.listdir(bdir) == []
    assert not os.path.exists(os.path.join(target, "UDATA", T, "NEW1"))


def test_second_import_waits_for_the_lock(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    target = hdd(d)
    os.makedirs(os.path.join(target, importer.STAGING, "new-1"))
    put(
        os.path.join(target, importer.LOCK),
        ("%d %s\n" % (os.getpid(), socket.gethostname())).encode(),
    )
    with pytest.raises(SaveError, match="another import") as e:
        do_import(src, target)
    assert e.value.code == "busy"
    # The running import's staging is left alone.
    assert os.path.isdir(os.path.join(target, importer.STAGING, "new-1"))


def test_stale_lock_taken_over(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    target = hdd(d)
    # A pid that cannot be running: above any real pid limit.
    put(os.path.join(target, importer.LOCK), ("99999999 %s\n" % socket.gethostname()).encode())
    do_import(src, target)
    assert os.path.isdir(os.path.join(target, "UDATA", T, "AAAA"))
    assert not os.path.exists(os.path.join(target, importer.LOCK))


@pytest.mark.parametrize("listing", [[], None])
def test_windows_unknown_process_list_is_not_stale(d, monkeypatch, listing):
    # A failed or empty tasklist must not make a live lock look dead.
    target = hdd(d)
    path = os.path.join(target, importer.LOCK)
    put(path, ("99999999 %s\n" % socket.gethostname()).encode())
    monkeypatch.setattr(importer.os, "name", "nt")
    monkeypatch.setattr(procs, "list_processes", lambda exe: listing)
    assert importer._stale_lock(path) is None


def test_windows_dead_pid_is_stale(d, monkeypatch):
    target = hdd(d)
    path = os.path.join(target, importer.LOCK)
    text = "99999999 %s\n" % socket.gethostname()
    put(path, text.encode())
    monkeypatch.setattr(importer.os, "name", "nt")
    monkeypatch.setattr(procs, "list_processes", lambda exe: [(os.getpid(), 1, ["python"])])
    assert importer._stale_lock(path) == text


def test_stale_takeover_keeps_a_lock_that_changed(d):
    # Another run took the lock between our check and our move: it goes back.
    target = hdd(d)
    path = os.path.join(target, importer.LOCK)
    put(path, b"1 live-run\n")
    assert not importer._take_over(path, "99999999 dead-run\n")
    assert read(path) == b"1 live-run\n"
    assert [n for n in os.listdir(target) if ".stale-" in n] == []


def test_stale_takeover_moves_the_dead_lock(d):
    target = hdd(d)
    path = os.path.join(target, importer.LOCK)
    put(path, b"99999999 dead-run\n")
    assert importer._take_over(path, "99999999 dead-run\n")
    assert not os.path.exists(path)
    assert [n for n in os.listdir(target) if ".stale-" in n] == []


def test_lock_from_another_host_is_kept(d):
    src = hdd(d, "src")
    save(src, "AAAA")
    target = hdd(d)
    put(os.path.join(target, importer.LOCK), b"1 some-other-host\n")
    with pytest.raises(SaveError, match="another import"):
        do_import(src, target)
