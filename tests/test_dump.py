"""import: the vendored XDVDFS reader on synthetic images, and every rule
in dump.py, against a scratch copy of the test game. The images are
built here (the layout upstream's test_xdvdfs.py uses), so no disc is
needed."""

import json
import os
import shutil
import struct
import time

import pytest
from conftest import GAME
from test_cli import raises
from test_xbe import header

from xboxrecomp_cli import dump, host, manifest
from xboxrecomp_cli.package import lib as pkg_lib
from xboxrecomp_cli.xdvdfs import MAGIC, SECTOR, Xiso, XisoError

ATTR_DIR = 0x10
TITLE_ID = 0x4D530065  # the test game's [xbe] title_id
XBE = header("BLiNX 2", TITLE_ID)


def _entry(name, sector, size, attr=0):
    nb = name.encode("ascii")
    e = struct.pack("<HHIIBB", 0, 0, sector, size, attr, len(nb)) + nb
    return e + b"\x00" * ((-len(e)) % 4)


def build_image(path, tree, base=0):
    """An XDVDFS image at path: descriptor at sector 32, the root directory
    at 33. tree is [(name, bytes | tree | int | None)]: an int is a
    directory entry pointing at that sector (a loop when it is an
    ancestor's), None an empty directory (size 0, sector 0). The
    file is written sparsely, so a large base costs nothing."""
    blobs = {}
    nxt = [33]

    def alloc(n):
        s = nxt[0]
        nxt[0] += n
        return s

    def lay(entries, sector):
        body = b""
        for name, v in entries:
            if v is None:
                body += _entry(name, 0, 0, ATTR_DIR)
            elif isinstance(v, list):
                sub = alloc(1)
                lay(v, sub)
                body += _entry(name, sub, SECTOR, ATTR_DIR)
            elif isinstance(v, int):
                body += _entry(name, v, SECTOR, ATTR_DIR)
            else:
                s = alloc(max(1, (len(v) + SECTOR - 1) // SECTOR))
                blobs[s] = v
                body += _entry(name, s, len(v))
        assert len(body) <= SECTOR
        blobs[sector] = body

    lay(tree, alloc(1))
    desc = bytearray(SECTOR)
    desc[: len(MAGIC)] = MAGIC
    struct.pack_into("<II", desc, 0x14, 33, SECTOR)
    blobs[32] = bytes(desc)
    with open(path, "wb") as f:
        for s, payload in blobs.items():
            f.seek(base + s * SECTOR)
            f.write(payload)
        f.truncate(base + (nxt[0] + 2) * SECTOR)
    return path


TREE = [
    ("default.xbe", XBE),
    ("media", [("intro.bik", b"BIK1" * 700), ("logo.bik", b"BIK2")]),
    ("readme.txt", b"hello"),
]


def write_folder(root, tree):
    os.makedirs(root, exist_ok=True)
    for name, v in tree:
        p = os.path.join(root, name)
        if isinstance(v, list):
            write_folder(p, v)
        else:
            with open(p, "wb") as f:
                f.write(v)
    return root


@pytest.fixture
def game(d):
    """The test game copied to d/game, current while the test runs."""
    root = os.path.join(d, "game")
    os.makedirs(root)
    shutil.copy(os.path.join(GAME, "game.toml"), root)
    shutil.copytree(os.path.join(GAME, "config"), os.path.join(root, "config"))
    saved = manifest.current()
    G = manifest.load(root)
    manifest.use(G)
    try:
        yield G
    finally:
        manifest.use(saved)


def files_of(root):
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


# ── the reader ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("base", [0, 0x18300000])
def test_reader_finds_the_partition(d, base):
    p = build_image(os.path.join(d, "t.iso"), TREE, base)
    with Xiso(p) as iso:
        assert iso.base == base
        assert iso.read(iso.find("DEFAULT.XBE")) == XBE
        names = sorted((p + "/" + e.name).lstrip("/") for p, e in iso.walk())
    assert names == ["default.xbe", "media/intro.bik", "media/logo.bik", "readme.txt"]


def test_reader_refuses_a_file_that_is_not_an_image(d):
    p = os.path.join(d, "x.zip")
    with open(p, "wb") as f:
        f.write(b"PK\3\4" + b"\0" * 100000)
    with pytest.raises(XisoError):
        Xiso(p)


# ── classifying and verifying the source ─────────────────────────────────


def test_image_import_into_an_empty_root(game, d):
    iso = build_image(os.path.join(d, "BLiNX 2.iso"), TREE, 0x18300000)
    assert dump.run_import(iso) == 0
    got = files_of(game.game_files)
    assert got.pop(dump.MARKER)
    assert got == {
        "default.xbe": XBE,
        "media/intro.bik": b"BIK1" * 700,
        "media/logo.bik": b"BIK2",
        "readme.txt": b"hello",
    }
    m = dump.read_marker()
    st = os.stat(iso)
    assert m["schema"] == 1 and m["format"] == "image" and m["base"] == 0x18300000
    assert (m["source"], m["source_size"], m["source_mtime"]) == (
        os.path.realpath(iso),
        st.st_size,
        int(st.st_mtime),
    )
    assert (m["files"], m["bytes"]) == (4, len(XBE) + 2800 + 4 + 5)
    assert m["xbe_sha256"] == host.sha256_path(game.xbe)
    assert m["when"].endswith("Z") and m["cli"]
    assert not os.path.exists(dump.partial_dir()) and not os.path.exists(dump.old_dir())
    # The marker is the CLI's, never the bundle's.
    assert pkg_lib.game_file_excluded(dump.MARKER)


def test_a_wrong_title_is_refused_before_any_write(game, d):
    tree = [("default.xbe", header("Burnout 3", 0x4541000A))] + TREE[1:]
    iso = build_image(os.path.join(d, "b3.iso"), tree)
    e = raises(dump.run_import, iso, match='this is "Burnout 3" (0x4541000A), not')
    assert "(0x4D530065)" in e
    assert not os.path.lexists(dump.partial_dir())
    assert os.listdir(game.game_files) == [] if os.path.isdir(game.game_files) else True
    nameless = build_image(os.path.join(d, "n.iso"), [("default.xbe", header("", 1))])
    raises(dump.run_import, nameless, match="an XBE with an empty title (0x00000001)")


def test_what_is_not_a_source(game, d):
    raises(dump.run_import, os.path.join(d, "nope"), match="no such file or folder")
    os.makedirs(os.path.join(d, "empty"))
    raises(
        dump.run_import,
        os.path.join(d, "empty"),
        match="no default.xbe in %s: the folder must be the disc's root" % os.path.join(d, "empty"),
    )
    p = os.path.join(d, "game.7z")
    with open(p, "wb") as f:
        f.write(b"7z" + b"\0" * 70000)
    raises(dump.run_import, p, match="must be extracted first, and a .cue/.bin set needs its .bin")


# ── reruns ───────────────────────────────────────────────────────────────


def test_rerun_is_a_no_op(game, d, capsys):
    iso = build_image(os.path.join(d, "g.iso"), TREE)
    dump.run_import(iso)
    before = os.stat(os.path.join(game.game_files, "media", "intro.bik")).st_mtime_ns
    capsys.readouterr()
    imp = dump.prepare(iso)
    assert imp.verdict == "same" and not imp.writes
    imp.run()
    assert "already imported from g.iso on " in capsys.readouterr().out
    assert os.stat(os.path.join(game.game_files, "media", "intro.bik")).st_mtime_ns == before


def test_files_put_there_by_hand_are_adopted(game, d):
    src = write_folder(os.path.join(d, "disc"), TREE)
    write_folder(game.game_files, TREE)
    imp = dump.prepare(src)
    assert imp.verdict == "adopt" and imp.plan_why() == "disc: files in place, marker only"
    imp.run()
    m = dump.read_marker()
    assert m["format"] == "folder" and m["files"] == 4
    assert dump.prepare(src).verdict == "same"


def test_a_different_dump_by_hand_is_refused(game, d):
    src = write_folder(os.path.join(d, "disc"), TREE)
    write_folder(game.game_files, TREE[:1] + [("other.bin", b"x")])
    raises(
        dump.prepare,
        src,
        match="already holds a dump that was put there by hand; rerun with --replace",
    )
    assert sorted(os.listdir(game.game_files)) == ["default.xbe", "other.bin"]


def test_another_source_is_refused(game, d):
    dump.run_import(build_image(os.path.join(d, "a.iso"), TREE))
    other = build_image(os.path.join(d, "b.iso"), TREE + [("x.bin", b"x")])
    e = raises(dump.prepare, other, match="holds the dump imported from a.iso on ")
    assert "(this source's path and size differ)" in e


def test_a_changed_tree_under_a_marker_is_refused(game, d):
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    dump.run_import(iso)
    with open(os.path.join(game.game_files, "readme.txt"), "ab") as f:
        f.write(b"!")
    raises(dump.prepare, iso, match="differs from a.iso, which it was imported from (size differs")


def test_replace_swaps_the_tree(game, d):
    write_folder(game.game_files, TREE[:1] + [("other.bin", b"x")])
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    dump.run_import(iso, replace=True)
    assert "other.bin" not in os.listdir(game.game_files)
    assert dump.read_marker()["source"] == os.path.realpath(iso)
    assert not os.path.lexists(dump.old_dir())


# ── interruptions ────────────────────────────────────────────────────────


def test_a_failed_swap_puts_the_old_tree_back(game, d, monkeypatch):
    write_folder(game.game_files, [("default.xbe", XBE), ("mine.bin", b"m")])
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    real = dump.rename

    def second_fails(a, b, tries=5):
        if a == dump.partial_dir():
            raise OSError(28, "No space left on device")
        real(a, b, tries)

    monkeypatch.setattr(dump, "rename", second_fails)
    raises(dump.run_import, iso, True, match="import stopped: ")
    assert sorted(os.listdir(game.game_files)) == ["default.xbe", "mine.bin"]
    assert not os.path.lexists(dump.partial_dir()) and not os.path.lexists(dump.old_dir())


def test_a_crash_after_the_first_rename_is_recovered(game, d, capsys):
    write_folder(dump.old_dir(), [("default.xbe", XBE), ("mine.bin", b"m")])
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    # A killed process: game_files/ is gone, game_files.old/ holds it.
    raises(dump.prepare, iso, match="by hand")
    assert "put back from game_files.old/" in capsys.readouterr().out
    assert sorted(os.listdir(game.game_files)) == ["default.xbe", "mine.bin"]
    assert not os.path.lexists(dump.old_dir())


def test_an_old_tree_beside_a_finished_import_is_removed(game, d, capsys):
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    dump.run_import(iso)
    write_folder(dump.old_dir(), [("mine.bin", b"m")])
    assert "game_files.old" in dump.leftover_note()
    assert dump.prepare(iso).verdict == "same"
    assert "removing game_files.old/" in capsys.readouterr().out
    assert not os.path.lexists(dump.old_dir())
    assert dump.leftover_note() is None


def test_an_old_tree_beside_an_unmarked_one_is_kept(game, d, capsys):
    write_folder(game.game_files, [("default.xbe", XBE)])
    write_folder(dump.old_dir(), [("mine.bin", b"m")])
    dump.recover()
    assert "keeping both" in capsys.readouterr().out
    assert os.path.isdir(dump.old_dir())


def test_a_stale_partial_is_removed_after_the_title_check(game, d):
    write_folder(dump.partial_dir(), [("half.bin", b"h")])
    bad = build_image(os.path.join(d, "b3.iso"), [("default.xbe", header("Burnout 3", 7))])
    raises(dump.prepare, bad, match="not ")
    assert os.path.isdir(dump.partial_dir())  # a wrong disc writes nothing
    dump.run_import(build_image(os.path.join(d, "a.iso"), TREE))
    assert not os.path.lexists(dump.partial_dir())


# ── what an image may hold ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, rule",
    [
        ("..", "an empty, '.' or '..' name"),
        ("a:b", "a character Windows refuses"),
        ("CON.txt", "a name Windows reserves"),
        ("x.", "a name ending in a dot or a space"),
        ("a\\b", "a path separator or NUL"),
    ],
)
def test_names_an_image_may_not_hold(game, d, name, rule):
    iso = build_image(os.path.join(d, "a.iso"), TREE + [("media2", [(name, b"x")])])
    raises(dump.prepare, iso, match="the image holds %r in /media2: %s" % (name, rule))
    assert not os.path.lexists(game.game_files) or not os.listdir(game.game_files)


def test_names_that_differ_by_case_are_refused(game, d):
    iso = build_image(os.path.join(d, "a.iso"), TREE + [("README.TXT", b"x")])
    raises(dump.prepare, iso, match="names that differ only by case")


def test_a_directory_loop_is_refused(game, d):
    iso = build_image(os.path.join(d, "a.iso"), TREE + [("again", 33)])
    raises(dump.prepare, iso, match="the image's directory /again loops (sector 33 seen twice)")


def test_a_deep_image_is_refused(game, d):
    tree = [("f.bin", b"x")]
    for i in range(dump.MAX_DEPTH + 1):
        tree = [("d%d" % i, tree)]
    iso = build_image(os.path.join(d, "a.iso"), TREE + tree)
    raises(dump.prepare, iso, match="nests directories deeper than 32")


def test_a_path_that_escapes_the_root_is_refused(d):
    root = os.path.join(d, "root")
    os.makedirs(root)
    os.symlink(d, os.path.join(root, "out"))
    raises(dump.safe_dest, root, "out/x.bin", match="would be written outside")
    assert dump.safe_dest(root, "media/x.bin") == os.path.join(root, "media", "x.bin")


def test_a_source_in_or_around_game_files_is_refused(game, d):
    write_folder(os.path.join(game.game_files, "disc"), TREE)
    raises(dump.prepare, os.path.join(game.game_files, "disc"), match="the source is inside")
    shutil.rmtree(game.game_files)
    write_folder(game.root, TREE)
    raises(dump.prepare, game.root, match="the source contains this game's game_files/")


# ── folders and links ────────────────────────────────────────────────────


def test_a_folder_is_copied_whole(game, d):
    src = write_folder(os.path.join(d, "disc"), TREE + [("UDATA", [("save.bin", b"s")])])
    dump.run_import(src)
    got = files_of(game.game_files)
    assert got["UDATA/save.bin"] == b"s" and got["media/logo.bik"] == b"BIK2"
    m = dump.read_marker()
    # UDATA is the pipeline's, not the dump's: copied, not counted.
    assert m["files"] == 4 and m["xbe_sha256"] == host.sha256_path(os.path.join(src, "default.xbe"))
    assert dump.prepare(src).verdict == "same"


def test_a_folder_holding_a_linked_folder_is_refused(game, d):
    src = write_folder(os.path.join(d, "disc"), TREE)
    os.symlink(os.path.join(src, "media"), os.path.join(src, "media2"))
    raises(dump.prepare, src, match="is a link to a folder")


def test_link(game, d, monkeypatch, capsys):
    src = write_folder(os.path.join(d, "disc"), TREE)
    dump.run_import(src, link=True)
    assert os.readlink(game.game_files) == os.path.realpath(src)
    out = capsys.readouterr().out
    assert "--link: the pipeline writes default_analysis.json into " in out
    assert dump.origin_note() == " (linked to %s)" % os.path.realpath(src)
    assert dump.prepare(src, link=True).verdict == "same"
    assert "already linked" in capsys.readouterr().out
    raises(dump.prepare, src, match="is a link to")  # a copy over a link needs --replace
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    raises(dump.prepare, iso, False, True, match="--link takes a folder")
    monkeypatch.setattr(host, "host_os", lambda s=None: "windows")
    raises(dump.prepare, src, False, True, match="--link: use a copy on Windows")


# ── --check ──────────────────────────────────────────────────────────────


def test_check(game, d, capsys):
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    dump.run_import(iso)
    assert dump.run_import(iso, check_only=True) == 0
    assert "every file matches (name, size, sha256)" in capsys.readouterr().out
    # Extras the dump never holds do not count.
    write_folder(os.path.join(game.game_files, "UDATA"), [("save.bin", b"s")])
    write_folder(game.game_files, [(".DS_Store", b"x")])
    assert dump.run_import(iso, check_only=True) == 0
    p = os.path.join(game.game_files, "readme.txt")
    with open(p, "wb") as f:
        f.write(b"jello")
    write_folder(game.game_files, [("extra.bin", b"e")])
    capsys.readouterr()
    assert dump.run_import(iso, check_only=True) == 1
    out = capsys.readouterr().out
    assert "  content differs: readme.txt" in out and "  extra: extra.bin" in out
    assert "check: 2 differences" in out


# ── free space ───────────────────────────────────────────────────────────


def test_free_space(game, d, monkeypatch):
    class Usage:
        free = 1000

    iso = build_image(os.path.join(d, "a.iso"), TREE)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage)
    raises(dump.prepare, iso, match="needs 268.4 MB free on ")
    write_folder(game.game_files, [("mine.bin", b"m")])
    raises(dump.prepare, iso, True, match="(the old game_files/ is removed after the swap)")
    # A folder on the game's own APFS volume is cloned: only the margin.
    Usage.free = dump.MARGIN
    src = write_folder(os.path.join(d, "disc"), TREE + [("big.bin", b"b" * 4096)])
    monkeypatch.setattr(host, "host_os", lambda s=None: "macos")
    monkeypatch.setattr(dump, "same_volume", lambda a, b: True)
    assert dump.prepare(src, True).verdict == "fresh"
    monkeypatch.setattr(dump, "same_volume", lambda a, b: False)
    raises(dump.prepare, src, True, match="needs ")


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no time.tzset on Windows")
def test_marker_date_is_local(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert dump.marker_date({"when": "2026-10-12T01:28:44Z"}) == "2026-10-11"
        assert dump.marker_date({"when": "garbage-here"}) == "garbage-he"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_progress_is_in_one_unit():
    assert dump.progress_text(291_300_000, 3_300_000_000) == "0.3 of 3.3 GB"
    assert dump.progress_text(1_500_000, 4_000_000) == "1.5 of 4.0 MB"


def test_marker_is_json_with_a_newline(game, d):
    dump.run_import(build_image(os.path.join(d, "a.iso"), TREE))
    with open(os.path.join(game.game_files, dump.MARKER), "rb") as f:
        raw = f.read()
    assert raw.endswith(b"}\n") and json.loads(raw)["schema"] == dump.MARKER_SCHEMA


# ── the words around it: doctor, setup, the pipeline ─────────────────────


def test_doctor_and_the_hints(game, d):
    from xboxrecomp_cli import doctor, pipeline, setup

    fix = "run '%s import PATH' with your disc image or folder, or put the files in game_files/"
    assert doctor.dump_fix() == fix % host.cli_name()
    assert "no game_files/default.xbe: " + fix % host.cli_name() in setup.host_blockers("windows")
    assert not any("default.xbe" in b for b in setup.host_blockers("windows", importing=True))
    assert pipeline.dump_hint() == "(%s)" % (fix % host.cli_name())
    lines = doctor.doctor_report()[0]
    assert "game files: MISSING: " + fix % host.cli_name() in lines
    dump.run_import(build_image(os.path.join(d, "a.iso"), TREE))
    write_folder(dump.old_dir(), [("x", b"x")])
    lines = doctor.doctor_report()[0]
    gf = [x for x in lines if x.startswith("game files:")][0]
    assert gf.endswith(" (imported from a.iso, %s)" % dump.marker_date(dump.read_marker()))
    left = lines[lines.index(gf) + 1]
    assert (
        left == "            game_files.old/ left by an interrupted import: run '%s import "
        "PATH' again, or remove it" % host.cli_name()
    )


# ── the review's cases ───────────────────────────────────────────────────


def test_a_crash_right_after_the_swap_leaves_a_marked_tree(game, d, monkeypatch, capsys):
    write_folder(game.game_files, [("default.xbe", XBE), ("mine.bin", b"m")])
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    real = dump.rename

    def killed_after_the_swap(a, b, tries=5):
        real(a, b, tries)
        if a == dump.partial_dir():
            raise KeyboardInterrupt

    monkeypatch.setattr(dump, "rename", killed_after_the_swap)
    with pytest.raises(KeyboardInterrupt):
        dump.run_import(iso, replace=True)
    monkeypatch.setattr(dump, "rename", real)
    # The marker went in with the tree, so the swap alone finished it.
    assert dump.read_marker()["source"] == os.path.realpath(iso)
    assert os.path.isdir(dump.old_dir())
    assert dump.prepare(iso).verdict == "same"
    assert "removing game_files.old/" in capsys.readouterr().out
    assert not os.path.lexists(dump.old_dir())


def test_a_writing_import_beside_an_unmarked_old_tree_is_refused_first(game, d, capsys):
    # What an older CLI left when it died between the swap and the marker.
    write_folder(game.game_files, TREE)
    write_folder(dump.old_dir(), [("mine.bin", b"m")])
    other = write_folder(os.path.join(d, "other"), [("default.xbe", XBE), ("b.bin", b"bb")])
    e = raises(dump.prepare, other, True, match="game_files.old/ is left from an import that")
    assert "remove game_files.old/ to keep game_files/" in e
    assert not os.path.lexists(dump.partial_dir())
    raises(dump.prepare, other, True, True, match="game_files.old/ is left")
    assert os.listdir(dump.old_dir()) == ["mine.bin"]
    # The same source without --replace adopts, which marks the tree, and
    # the old one goes as after any finished import.
    disc = write_folder(os.path.join(d, "disc"), TREE)
    dump.run_import(disc)
    assert dump.read_marker() and not os.path.lexists(dump.old_dir())


def test_a_link_counts_as_a_finished_swap(game, d):
    src = write_folder(os.path.join(d, "disc"), TREE)
    os.symlink(os.path.realpath(src), game.game_files)
    write_folder(dump.old_dir(), [("mine.bin", b"m")])
    dump.recover()
    assert not os.path.lexists(dump.old_dir())


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX modes that bind this user"
)
def test_an_unreadable_folder_stops_the_import(game, d):
    src = write_folder(os.path.join(d, "src"), TREE)
    media = os.path.join(src, "media")
    os.chmod(media, 0)
    try:
        raises(dump.prepare, src, match="cannot read %s" % media)
        assert not os.path.lexists(dump.partial_dir())
        # The same in game_files/, which the size pass and --check walk.
        os.chmod(media, 0o755)
        dump.run_import(src)
        os.chmod(os.path.join(game.game_files, "media"), 0)
        raises(dump.prepare, src, match="cannot read ")
        raises(dump.check, src, match="cannot read ")
    finally:
        os.chmod(media, 0o755)
        if os.path.isdir(game.game_files):
            os.chmod(os.path.join(game.game_files, "media"), 0o755)


def test_a_dangling_link_in_the_folder_is_named(game, d):
    src = write_folder(os.path.join(d, "src"), TREE)
    os.symlink(os.path.join(d, "gone.bin"), os.path.join(src, "gone.bin"))
    raises(dump.prepare, src, match="gone.bin is a link to %s" % os.path.join(d, "gone.bin"))


def test_saves_are_carried_over_an_empty_looking_tree(game, d, capsys):
    write_folder(
        game.game_files,
        [("UDATA", [("save.bin", b"s")]), ("default_analysis.json", b"{}")],
    )
    src = write_folder(os.path.join(d, "src"), TREE)
    dump.run_import(src)
    got = files_of(game.game_files)
    assert got["UDATA/save.bin"] == b"s" and got["default_analysis.json"] == b"{}"
    assert "kept from the old game_files/: UDATA, default_analysis.json" in capsys.readouterr().out
    assert not os.path.lexists(dump.old_dir())
    assert dump.prepare(src).verdict == "same"


def test_replace_carries_saves_and_the_source_wins_a_shared_name(game, d, capsys):
    write_folder(
        game.game_files,
        TREE[:1]
        + [("other.bin", b"x"), ("UDATA", [("save.bin", b"s")]), ("TDATA", [("t.bin", b"old")])],
    )
    src = write_folder(os.path.join(d, "src"), TREE + [("TDATA", [("t.bin", b"new")])])
    dump.run_import(src, replace=True)
    got = files_of(game.game_files)
    assert "other.bin" not in got
    assert got["UDATA/save.bin"] == b"s" and got["TDATA/t.bin"] == b"new"
    out = capsys.readouterr().out
    assert "kept from the old game_files/: UDATA" in out
    assert "replaced by the source's own: TDATA (the old game_files/ held them too)" in out


def test_a_link_over_saves_is_refused(game, d):
    write_folder(game.game_files, [("UDATA", [("save.bin", b"s")])])
    src = write_folder(os.path.join(d, "disc"), TREE)
    raises(dump.prepare, src, False, True, match="game_files/ holds UDATA, which --link would")
    assert os.path.isfile(os.path.join(game.game_files, "UDATA", "save.bin"))


def test_empty_directories_sharing_a_sector_are_not_a_loop(game, d):
    iso = build_image(os.path.join(d, "a.iso"), TREE + [("e1", None), ("e2", None)])
    dump.run_import(iso)
    for name in ("e1", "e2"):
        assert os.path.isdir(os.path.join(game.game_files, name))
    src = write_folder(os.path.join(d, "disc"), TREE)
    os.makedirs(os.path.join(src, "empty", "deeper"))
    dump.run_import(src, replace=True)
    assert os.path.isdir(os.path.join(game.game_files, "empty", "deeper"))


def test_the_title_check_reads_only_the_header(game, d, monkeypatch):
    iso = build_image(os.path.join(d, "a.iso"), TREE)

    def whole(*a):
        raise AssertionError("read the whole XBE")

    monkeypatch.setattr(Xiso, "read", whole)
    with dump.Source(iso) as src:
        assert src.xbe_header() == XBE[:0x10000]


def test_a_failed_removal_of_the_old_tree_is_said(game, d, monkeypatch, capsys):
    write_folder(game.game_files, [("default.xbe", XBE), ("mine.bin", b"m")])
    iso = build_image(os.path.join(d, "a.iso"), TREE)
    real = dump.rmtree

    def busy(path):
        if path == dump.old_dir():
            raise PermissionError(13, "Permission denied")
        real(path)

    monkeypatch.setattr(dump, "rmtree", busy)
    assert dump.run_import(iso, replace=True) == 0
    out = capsys.readouterr().out
    assert "could not remove game_files.old/ (Permission denied): remove it later" in out
    monkeypatch.setattr(dump, "rmtree", real)
    assert dump.prepare(iso).verdict == "same"
    assert not os.path.lexists(dump.old_dir())


def test_the_import_and_check_count_the_same_files(game, d, capsys):
    src = write_folder(os.path.join(d, "src"), TREE + [("default_analysis.json", b"{}")])
    imp = dump.prepare(src)
    assert imp.plan_why().endswith(", 4 files")
    imp.run()
    out = capsys.readouterr().out
    assert "(folder, 0.0 MB, 4 files) into" in out and "(4 files, " in out
    dump.check(src)
    assert ", 4 files" in capsys.readouterr().out
