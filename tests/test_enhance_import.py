"""enhance textures export, enhance hud --import and --from-dumps, on synthetic
dumps: the walk an owner takes, and what each refusal says.

  uv run pytest tests/test_enhance_import.py
"""

import hashlib
import json
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import A, fake_tree, write  # noqa: E402
from test_enhance_png import chunk, file_of  # noqa: E402
from test_enhance_textures import dump_texture  # noqa: E402

from xboxrecomp_cli import host, main  # noqa: E402
from xboxrecomp_cli.enhance import hud, imported, mips, pack, png, textures  # noqa: E402
from xboxrecomp_cli.host import CliError  # noqa: E402

ONE = "00000000000000A1"  # one level
MIP = "00000000000000B2"  # three levels
TALL = "00000000000000C3"  # 4x8, one level


@pytest.fixture
def d(tmp_path):
    return str(tmp_path)


def solid(w, h, c=(10, 20, 30, 255)):
    return bytes(c) * (w * h)


def bgra(w, h, c=(10, 20, 30, 255)):
    return bytes((c[2], c[1], c[0], c[3])) * (w * h)


def chain(w, h, levels, c=(10, 20, 30, 255)):
    out = b""
    for _ in range(levels):
        out += bgra(w, h, c)
        w, h = max(1, w // 2), max(1, h // 2)
    return out


def a_dump(d):
    """A run's dump: one-level, mipped and tall textures, and what stays out."""
    p = os.path.join(d, "scene")
    dump_texture(p, ONE, 16, 16, texels=bgra(16, 16))
    dump_texture(p, MIP, 16, 16, levels=3, texels=chain(16, 16, 3))
    dump_texture(p, TALL, 16, 32, texels=bgra(16, 32))
    dump_texture(p, "00000000000000D4", 16, 16, cube=True, texels=bgra(16, 16))
    dump_texture(p, "00000000000000E5", 16, 16, fmt="LIN_A8R8G8B8")
    return p


def export_args(dump, **kw):
    a = A(verb="export", dump=[dump], out=None, only=[], misses=[])
    a.__dict__.update(kw)
    return a


def hud_args(**kw):
    a = A(
        scale=None,
        out=None,
        font=None,
        jobs=1,
        only=[],
        list=False,
        language=[],
        import_dir=None,
        from_dumps=[],
        misses=[],
        mips="auto",
        ignore_disk_space=True,
    )
    a.__dict__.update(kw)
    return a


def pack_bytes(w, h, levels):
    n = 0
    for _ in range(levels):
        n += w * h * 4
        w, h = max(1, w // 2), max(1, h // 2)
    return n


def pack_rows(data, index_off, count):
    return [pack.ENTRY.unpack_from(data, index_off + pack.ENTRY.size * i) for i in range(count)]


def read_pack(path):
    """{hash: (w, h, levels, kind, payload)}, the file's own index, its header
    and each entry's extent checked against the format."""
    with open(path, "rb") as f:
        data = f.read()
    magic, version, count, scale, index_off, text_scale = pack.HEADER.unpack_from(data)
    # The last field's high half is v3's bitmap of the kinds.
    assert (magic, index_off, text_scale & 0xFFFFFFFF) == (pack.MAGIC, pack.HEADER.size, 0)
    kinds = [r[4] & 3 for r in pack_rows(data, index_off, count)]
    assert version == (3 if pack.KIND_WORLD in kinds else 2)
    assert (count, scale) == pack.read_header(path) and scale >= 1
    rows = pack_rows(data, index_off, count)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    ends = [r[5] for r in rows[1:]] + [len(data)]
    for r, end in zip(rows, ends):
        assert end - r[5] == pack_bytes(r[1], r[2], r[3]), r
    return {
        "%016X" % r[0]: (r[1], r[2], r[3], r[4] & 3, data[r[5] : end]) for r, end in zip(rows, ends)
    }


def edit(path, c=(200, 0, 0, 255), size=None):
    w, h, _ = png.read(path)
    if size:
        w, h = size
    png.write(path, w, h, solid(w, h, c))


# ---- the walk ----------------------------------------------------------------


def test_export_writes_level_0_and_the_index(d, capsys):
    with fake_tree(d):
        assert textures.run(export_args(a_dump(d))) == 0
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        names = sorted(os.listdir(out_dir))
        assert names == sorted(h + ".png" for h in (ONE, MIP, TALL)) + ["index.json"]
        assert png.read(os.path.join(out_dir, MIP + ".png")) == (16, 16, solid(16, 16))
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
            rows = json.load(f)["textures"]
        assert rows[MIP]["levels"] == 3 and rows[TALL]["height"] == 32
        out = capsys.readouterr().out
        assert "wrote 3 PNG" in out and "left out" in out and "keep them private" in out


def test_export_never_overwrites_an_edit(d, capsys):
    with fake_tree(d):
        dump = a_dump(d)
        textures.run(export_args(dump))
        p = os.path.join(host.g().dist, "enhance", "export", ONE + ".png")
        edit(p)
        textures.run(export_args(dump))
        assert png.read(p)[2] == solid(16, 16, (200, 0, 0, 255))
        assert "3 were there already" in capsys.readouterr().out


def test_export_says_when_an_alpha_hides_the_image(d, capsys):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, texels=bgra(16, 16, (9, 9, 9, 0)))
        textures.run(export_args(p))
        assert "alpha 0 everywhere" in capsys.readouterr().out


def test_import_packs_only_the_edited_at_their_own_sizes(d, capsys):
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        G = host.g()
        out_dir = os.path.join(G.dist, "enhance", "export")
        edit(os.path.join(out_dir, ONE + ".png"), size=(64, 64))
        edit(os.path.join(out_dir, MIP + ".png"), size=(32, 32))
        assert hud.run(hud_args(import_dir=out_dir)) == 0
        got = read_pack(os.path.join(G.dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [ONE, MIP]
        assert got[ONE][:4] == (64, 64, 7, pack.KIND_WORLD)
        assert got[MIP][:4] == (32, 32, 6, pack.KIND_WORLD)
        out = capsys.readouterr().out
        assert "1 PNG files are as they were exported" in out
        assert "textures.pack" in out


def test_a_mipped_guest_shows_no_old_colour_at_any_level(d):
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        G = host.g()
        out_dir = os.path.join(G.dist, "enhance", "export")
        edit(os.path.join(out_dir, MIP + ".png"))
        hud.run(hud_args(import_dir=out_dir))
        w, h, levels, _, payload = read_pack(os.path.join(G.dist, "enhance", "import", "hud.pack"))[
            MIP
        ]
        assert (w, h, levels) == (16, 16, 5)
        # Payload is BGRA; the dump's own colour must be nowhere in it.
        assert len(payload) == 4 * (256 + 64 + 16 + 4 + 1)
        assert payload == bytes((0, 0, 200, 255)) * (256 + 64 + 16 + 4 + 1)


@pytest.mark.parametrize(
    "mips,levels_one,levels_mip", [("auto", 1, 5), ("full", 5, 5), ("none", 1, 1)]
)
def test_mips_option(d, mips, levels_one, levels_mip):
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        G = host.g()
        out_dir = os.path.join(G.dist, "enhance", "export")
        edit(os.path.join(out_dir, ONE + ".png"))
        edit(os.path.join(out_dir, MIP + ".png"))
        hud.run(hud_args(import_dir=out_dir, mips=mips))
        got = read_pack(os.path.join(G.dist, "enhance", "import", "hud.pack"))
        assert got[ONE][2] == levels_one and got[MIP][2] == levels_mip


# ---- import's refusals --------------------------------------------------------


def test_a_wrong_aspect_stops_the_build_and_says_what_would_do(d):
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        G = host.g()
        out_dir = os.path.join(G.dist, "enhance", "export")
        edit(os.path.join(out_dir, TALL + ".png"), size=(32, 32))
        with pytest.raises(CliError) as e:
            hud.run(hud_args(import_dir=out_dir))
        msg = str(e.value)
        assert "32x32, its texture 16x32" in msg and "32x64" in msg and "aspect" in msg
        assert not os.path.exists(os.path.join(G.dist, "enhance", "import", "hud.pack"))


def test_the_dump_checks_a_folder_with_no_index(d):
    with fake_tree(d):
        dump = a_dump(d)
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, TALL + ".png"), 8, 8, solid(8, 8))
        with pytest.raises(CliError, match="aspect"):
            hud.run(hud_args(import_dir=folder, from_dumps=[dump]))


def test_a_hash_nothing_knows_is_said_and_gets_a_chain(d, capsys):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE.lower() + ".PNG"), 8, 4, solid(8, 4))
        hud.run(hud_args(import_dir=folder))
        out = capsys.readouterr().out
        assert "no size to check" in out and ONE in out
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert got[ONE][:3] == (8, 4, 4)


def test_a_cube_map_is_refused(d):
    with fake_tree(d):
        dump = a_dump(d)
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, "00000000000000D4.png"), 8, 8, solid(8, 8))
        with pytest.raises(CliError, match="never uses an entry"):
            hud.run(hud_args(import_dir=folder, from_dumps=[dump]))


def test_a_volatile_texture_is_said_not_refused(d, capsys):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, texels=bgra(16, 16))
        write(os.path.join(p, "volatile.json"), json.dumps([{"hashes": [ONE]}]))
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 8, 8, solid(8, 8))
        hud.run(hud_args(import_dir=folder, from_dumps=[p]))
        assert "rarely applies" in capsys.readouterr().out


def test_a_render_target_upload_is_said_not_refused(d, capsys):
    # It was hashed and uploaded, so the runtime can use an entry for it, and
    # the fact is one sighting: an owner who means it gets it.
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, texels=bgra(16, 16), rt=True)
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 8, 8, solid(8, 8))
        hud.run(hud_args(import_dir=folder, from_dumps=[p]))
        assert "render target" in capsys.readouterr().out
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert ONE in got


def test_export_leaves_out_a_render_target_upload(d, capsys):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, texels=bgra(16, 16))
        dump_texture(p, TALL, 16, 32, texels=bgra(16, 32), rt=True)
        out = os.path.join(d, "out")
        assert textures.run(export_args(p, out=out)) == 0
        assert os.listdir(out).count(ONE + ".png") == 1
        assert not os.path.exists(os.path.join(out, TALL + ".png"))
        assert "1 left out" in capsys.readouterr().out


def test_other_png_names_and_a_bad_file(d, capsys):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, "notes.png"), 1, 1, solid(1, 1))
        with pytest.raises(CliError, match="no <hash>.png"):
            hud.run(hud_args(import_dir=folder))
        assert "notes.png" in capsys.readouterr().out
        write(os.path.join(folder, ONE + ".png"), "not a png")
        with pytest.raises(CliError, match="not a PNG"):
            hud.run(hud_args(import_dir=folder))


def test_two_files_of_one_hash(d):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, "00000000000000aa.png"), 1, 1, solid(1, 1))
        png.write(os.path.join(folder, "00000000000000AA.PNG"), 1, 1, solid(1, 1))
        if len(os.listdir(folder)) < 2:
            pytest.skip("a case-insensitive folder holds one of them")
        with pytest.raises(CliError, match="same hash"):
            hud.run(hud_args(import_dir=folder))


def test_every_png_unedited(d):
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        with pytest.raises(CliError, match="as it was exported"):
            hud.run(hud_args(import_dir=out_dir))


def test_a_dropped_alpha_is_said(d, capsys):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, fmt="DXT5", texels=bgra(16, 16, (9, 9, 9, 128)))
        textures.run(export_args(p))
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        edit(os.path.join(out_dir, ONE + ".png"))
        hud.run(hud_args(import_dir=out_dir))
        assert "draws solid" in capsys.readouterr().out


@pytest.mark.parametrize(
    "kw,why",
    [
        (dict(font="x"), "--font"),
        (dict(language=["fr"]), "--font"),
        (dict(scale=2), "--scale"),
        (dict(misses=["x"]), "--misses"),
    ],
)
def test_options_that_would_change_nothing(d, kw, why):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        with pytest.raises(CliError, match=why):
            hud.run(hud_args(import_dir=folder, **kw))


@pytest.mark.parametrize("kw", [dict(misses=["x"]), dict(mips="none")])
def test_their_options_alone_are_refused(d, kw):
    with fake_tree(d):
        with pytest.raises(CliError, match="go with --import"):
            hud.run(hud_args(**kw))


def test_import_needs_a_folder(d):
    with fake_tree(d):
        with pytest.raises(CliError, match="no such folder"):
            hud.run(hud_args(import_dir=os.path.join(d, "none")))


def test_a_linear_texture_is_refused_by_name(d):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, fmt="LIN_A8R8G8B8", texels=bgra(16, 16))
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 16, 16, solid(16, 16, (200, 0, 0, 255)))
        with pytest.raises(CliError, match=ONE + r".*\(linear\), so a pack should not hold it"):
            hud.run(hud_args(import_dir=folder, from_dumps=[p]))


def test_a_linear_row_of_the_index_is_refused(d):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 16, 16, solid(16, 16))
        row = {"width": 16, "height": 16, "format": "LIN_A8R8G8B8"}
        write(os.path.join(folder, "index.json"), json.dumps({"textures": {ONE: row}}))
        with pytest.raises(CliError, match="linear"):
            hud.run(hud_args(import_dir=folder))


def test_export_leaves_out_a_linear_texture(d, capsys):
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 16, 16, texels=bgra(16, 16))
        dump_texture(p, MIP, 16, 16, fmt="LIN_A8R8G8B8", texels=bgra(16, 16))
        textures.run(export_args(p))
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        assert sorted(os.listdir(out_dir)) == [ONE + ".png", "index.json"]
        assert "1 left out" in capsys.readouterr().out


def test_a_long_thin_png_is_refused_before_it_is_decoded(d, monkeypatch):
    with fake_tree(d):
        p = os.path.join(d, ONE + ".png")
        head = struct.pack(">IIBBBBB", 1 << 28, 1, 8, png.RGBA, 0, 0, 0)
        with open(p, "wb") as f:
            f.write(png.SIGNATURE + chunk(b"IHDR", head) + chunk(b"IEND", b""))

        def no_decoding(_):
            raise AssertionError("decoded")

        monkeypatch.setattr(png, "read", no_decoding)
        problem = imported._import_one((ONE + ".png", p, None, "auto"))[5]
        assert "over the runtime's 16384" in problem


@pytest.mark.parametrize(
    "row",
    [
        {"width": "16", "height": 16},
        {"width": 16, "height": [16]},
        {"width": True, "height": True},
        {"width": -16, "height": 16},
        "not a row",
    ],
)
def test_a_bad_row_of_the_index_has_no_size_to_check(d, capsys, row):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 8, 4, solid(8, 4))
        write(os.path.join(folder, "index.json"), json.dumps({"textures": {ONE: row}}))
        assert hud.run(hud_args(import_dir=folder)) == 0
        out = capsys.readouterr().out
        assert "bad field" in out and "no size to check" in out


@pytest.mark.parametrize(
    "extra", [{"levels": "4"}, {"sha256": 7}, {"sha256": "beef"}, {"class": 1}, {"alpha": "yes"}]
)
def test_a_bad_field_is_ignored_and_the_size_kept(d, capsys, extra):
    with fake_tree(d):
        folder = os.path.join(d, "mine")
        os.makedirs(folder)
        png.write(os.path.join(folder, ONE + ".png"), 8, 8, solid(8, 8))
        row = dict({"width": 16, "height": 8}, **extra)
        write(os.path.join(folder, "index.json"), json.dumps({"textures": {ONE: row}}))
        with pytest.raises(CliError, match="aspect"):
            hud.run(hud_args(import_dir=folder))
        assert "bad field" in capsys.readouterr().out


# ---- what counts as an edit ---------------------------------------------------


def exported(d):
    """The folder of a whole export, and the dump it came from."""
    dump = a_dump(d)
    textures.run(export_args(dump))
    return os.path.join(host.g().dist, "enhance", "export"), dump


def test_an_alpha_only_edit_is_packed(d):
    with fake_tree(d):
        out_dir, _ = exported(d)
        edit(os.path.join(out_dir, ONE + ".png"), (10, 20, 30, 128))
        hud.run(hud_args(import_dir=out_dir))
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [ONE]


def resaved_rgb(w, h):
    return png.encode(w, h, solid(w, h), alpha=False)


def resaved_16_bit(w, h):
    px = bytes(v for c in (10, 20, 30, 255) for v in (c, c))
    return file_of(w, h, 16, png.RGBA, [b"\0" + px * w] * h)


def resaved_palette(w, h):
    return file_of(
        w, h, 8, png.PALETTE, [b"\0" + bytes(w)] * h, chunk(b"PLTE", bytes((10, 20, 30)))
    )


@pytest.mark.parametrize("resave", [resaved_rgb, resaved_16_bit, resaved_palette])
def test_a_resave_in_another_form_is_not_an_edit(d, resave):
    with fake_tree(d):
        out_dir, _ = exported(d)
        with open(os.path.join(out_dir, ONE + ".png"), "wb") as f:
            f.write(resave(16, 16))
        with pytest.raises(CliError, match="as it was exported"):
            hud.run(hud_args(import_dir=out_dir))


def test_a_killed_export_loses_no_png_and_the_next_one_finishes(d, monkeypatch):
    with fake_tree(d):
        dump = a_dump(d)
        real = png.write
        calls = []

        def killed(path, *rest):
            if calls:
                raise KeyboardInterrupt
            calls.append(path)
            real(path, *rest)

        monkeypatch.setattr(png, "write", killed)
        with pytest.raises(KeyboardInterrupt):
            textures.run(export_args(dump))
        monkeypatch.setattr(png, "write", real)
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
            rows = json.load(f)["textures"]
        assert len(rows) == 1 and os.path.basename(calls[0])[:-4] in rows
        assert not [f for f in os.listdir(out_dir) if f.endswith(".tmp")]
        textures.run(export_args(dump))
        with pytest.raises(CliError, match="as it was exported"):
            hud.run(hud_args(import_dir=out_dir))


def test_export_saves_its_index_as_it_goes(d, monkeypatch):
    with fake_tree(d):
        dump = a_dump(d)
        saved = []
        real = imported._save_index
        monkeypatch.setattr(imported, "INDEX_EVERY", 1)
        monkeypatch.setattr(imported, "_save_index", lambda *a: (saved.append(1), real(*a)))
        textures.run(export_args(dump))
        assert len(saved) == 4  # one a PNG, and the last


def test_a_full_disk_during_export_is_a_clean_error(d, monkeypatch):
    with fake_tree(d):
        dump = a_dump(d)

        def full(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(png, "write", full)
        with pytest.raises(CliError, match="No space left"):
            textures.run(export_args(dump))
        monkeypatch.setattr(imported, "_save_index", full)
        with pytest.raises(CliError, match="No space left"):
            textures.run(export_args(dump))


def test_a_lost_index_is_said_and_the_dump_or_another_export_finds_them(d, capsys):
    with fake_tree(d):
        out_dir, dump = exported(d)
        index = os.path.join(out_dir, "index.json")
        os.remove(index)
        capsys.readouterr()
        hud.run(hud_args(import_dir=out_dir))
        out = capsys.readouterr().out
        assert "3 have no record of what export wrote" in out and ONE in out
        assert "3 of the entries had nothing to compare" in out
        # The dump is a baseline of its own: nothing to say, nothing to pack.
        with pytest.raises(CliError, match="as it was exported"):
            hud.run(hud_args(import_dir=out_dir, from_dumps=[dump]))
        assert "no record" not in capsys.readouterr().out
        # Export again: the unchanged get their real baseline, an edit none.
        edit(os.path.join(out_dir, ONE + ".png"))
        textures.run(export_args(dump))
        with open(index, encoding="utf-8") as f:
            rows = json.load(f)["textures"]
        assert "sha256" not in rows[ONE] and rows[ONE]["width"] == 16
        assert len(rows[MIP]["sha256"]) == 64
        capsys.readouterr()
        hud.run(hud_args(import_dir=out_dir))
        out = capsys.readouterr().out
        assert "1 have no record" in out and "2 PNG files are as they were exported" in out
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [ONE]


def test_a_second_export_keeps_the_first_baseline_under_an_edit(d):
    with fake_tree(d):
        out_dir, dump = exported(d)
        edit(os.path.join(out_dir, ONE + ".png"))
        textures.run(export_args(dump))
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as f:
            sha = json.load(f)["textures"][ONE]["sha256"]
        assert sha == hashlib.sha256(solid(16, 16)).hexdigest()


def test_the_real_command_lines(d, capsys):
    """Through argparse, so a dest name the code reads must be one it sets."""
    with fake_tree(d):
        dump = a_dump(d)
        p = main.make_parser()[0]
        out_dir = os.path.join(d, "folder")
        a = p.parse_args(["enhance", "textures", "export", "--dump", dump, "--out", out_dir])
        assert textures.run(a) == 0
        edit(os.path.join(out_dir, ONE + ".png"))
        pack_dir = os.path.join(d, "made")
        args = ["--import", out_dir, "--out", pack_dir, "--mips", "none", "--ignore-disk-space"]
        a = p.parse_args(["enhance", "hud"] + args)
        assert imported.wanted(a)
        assert hud.run(a) == 0
        assert read_pack(os.path.join(pack_dir, "hud.pack"))[ONE][2] == 1
        a = p.parse_args(["enhance", "hud", "--from-dumps", dump, "--out", pack_dir, "--only", ONE])
        a.ignore_disk_space = True
        assert hud.run(a) == 0
        assert sorted(read_pack(os.path.join(pack_dir, "hud.pack"))) == [ONE]


# ---- --from-dumps and --misses -----------------------------------------------


def test_from_dumps_upscales_what_can_be_packed(d, capsys):
    with fake_tree(d):
        hud.run(hud_args(from_dumps=[a_dump(d)]))
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [ONE, MIP, TALL]
        assert got[ONE][:4] == (32, 32, 6, pack.KIND_WORLD)
        assert got[MIP][:3] == (32, 32, 6)
        assert got[TALL][:2] == (32, 64)
        assert "left out" in capsys.readouterr().out


def miss_log(d, *lines):
    p = os.path.join(d, "run.log")
    write(p, "\n".join(("noise line",) + lines) + "\n")
    return p


def test_misses_narrow_the_set_and_say_what_is_not_dumped(d, capsys):
    with fake_tree(d):
        log = miss_log(
            d,
            "[TEXPACK] miss %s 16x16 DXT1 x1 candidate" % ONE,
            "[TEXPACK] miss %s 16x16 DXT1 x3 linear/mip (art off)" % MIP,
            "[TEXPACK] miss 00000000000000F6 16x16 fmt24 x1 linear",
        )
        hud.run(hud_args(from_dumps=[a_dump(d)], misses=[log]))
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [ONE]
        out = capsys.readouterr().out
        assert "1 with no dumped texels" in out and "00000000000000F6" in out
        assert "1 the pack holds" in out


def test_a_log_with_no_misses_says_how_to_get_them(d):
    with fake_tree(d):
        with pytest.raises(CliError, match="RECOMP_TRACE=tex_pack"):
            hud.run(hud_args(from_dumps=[a_dump(d)], misses=[miss_log(d)]))


def test_list_writes_nothing(d, capsys):
    with fake_tree(d):
        assert hud.run(hud_args(from_dumps=[a_dump(d)], list=True)) == 0
        assert not os.path.exists(os.path.join(host.g().dist, "enhance", "import"))
        out = capsys.readouterr().out
        assert "guest 16x16 x3" in out and "3 textures" in out


def test_only_picks_by_hash_with_wildcards(d):
    with fake_tree(d):
        hud.run(hud_args(from_dumps=[a_dump(d)], only=["*b2"]))
        got = read_pack(os.path.join(host.g().dist, "enhance", "import", "hud.pack"))
        assert sorted(got) == [MIP]


def test_jobs_give_the_same_pack(d):
    with fake_tree(d):
        G = host.g()
        dump = a_dump(d)
        path = os.path.join(G.dist, "enhance", "import", "hud.pack")
        hud.run(hud_args(from_dumps=[dump], jobs=1))
        with open(path, "rb") as f:
            one = f.read()
        hud.run(hud_args(from_dumps=[dump], jobs=2))
        with open(path, "rb") as f:
            assert f.read() == one


def test_the_game_needs_no_hud_section(d):
    with fake_tree(d):
        with pytest.raises(CliError):
            hud.hud_config()
        hud.run(hud_args(from_dumps=[a_dump(d)]))


def test_the_imported_pack_is_not_remade_by_package(d):
    from xboxrecomp_cli.package import tweaks

    with fake_tree(d):
        hud.run(hud_args(from_dumps=[a_dump(d)]))
        out_dir = os.path.join(host.g().dist, "enhance", "import")
        sha = host.sha256_path(os.path.join(out_dir, "hud.pack"))
        assert tweaks.made_with(os.path.join(out_dir, "hud.pack"), sha) is None


# ---- the levels ----------------------------------------------------------------


def test_a_chain_on_a_non_square_texture_has_each_level_in_its_place(d):
    """Red and green differ across both axes and at every level, and each 2x2
    has an exact mean, so a wrong offset, a swapped axis or a level left out shows."""

    def rgba_of(rows):
        return b"".join(bytes((v, 255 - v, 7, 255)) for row in rows for v in row)

    def blown_up(rows):
        """The level above: each texel's four sum to four times its value."""
        out = []
        for row in rows:
            out.append([v + dv for v in row for dv in (-4, -2)])
            out.append([v + dv for v in row for dv in (2, 4)])
        return out

    l3 = [[100]]
    l2 = [[90, 110]]
    l1 = [[70, 90, 90, 110], [90, 110, 110, 130]]
    l0 = blown_up(l1)
    with fake_tree(d):
        p = os.path.join(d, "scene")
        dump_texture(p, ONE, 8, 4, levels=3, texels=bgra(8, 4) + bgra(4, 2) + bgra(2, 1))
        textures.run(export_args(p))
        G = host.g()
        out_dir = os.path.join(G.dist, "enhance", "export")
        png.write(os.path.join(out_dir, ONE + ".png"), 8, 4, rgba_of(l0))
        hud.run(hud_args(import_dir=out_dir))
        w, h, levels, _, got = read_pack(os.path.join(G.dist, "enhance", "import", "hud.pack"))[ONE]
        assert (w, h, levels) == (8, 4, 4)
        want = b"".join(rgba_of(rows) for rows in (l0, l1, l2, l3))
        # The payload is BGRA.
        assert bytes(got[2::4]) == bytes(want[0::4]) and bytes(got[1::4]) == bytes(want[1::4])
        assert bytes(got[0::4]) == bytes(want[2::4]) and bytes(got[3::4]) == bytes(want[3::4])


def test_half_weighs_colour_by_alpha():
    # One opaque red, three clear greens: the mean must stay red, no green fringe.
    rgba = bytes((255, 0, 0, 255)) + bytes((0, 255, 0, 0)) * 3
    assert mips.down2(2, 2, rgba) == (1, 1, bytes((255, 0, 0, 64)))


def test_half_keeps_the_colour_under_no_alpha():
    rgba = bytes((100, 50, 25, 0)) * 4
    assert mips.down2(2, 2, rgba)[2] == bytes((100, 50, 25, 0))


def test_half_on_a_line_and_an_odd_side():
    rgba = bytes((10, 0, 0, 255)) + bytes((30, 0, 0, 255))
    assert mips.down2(2, 1, rgba) == (1, 1, bytes((20, 0, 0, 255)))
    assert mips.down2(1, 2, rgba) == (1, 1, bytes((20, 0, 0, 255)))
    w, h, _ = mips.down2(3, 5, solid(3, 5))
    assert (w, h) == (1, 2)


def test_only_a_world_entry_takes_version_3(d):
    # A HUD piece is art and its pack stays one an older toolkit loads; a world
    # texture needs the kind that only v3 has, for textures.world to govern it.
    with fake_tree(d):
        textures.run(export_args(a_dump(d)))
        out_dir = os.path.join(host.g().dist, "enhance", "export")
        path = os.path.join(out_dir, "index.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        rows = doc["textures"] if "textures" in doc else doc
        for row in rows.values():
            row["class"] = "ui"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        edit(os.path.join(out_dir, ONE + ".png"), size=(32, 32))
        assert hud.run(hud_args(import_dir=out_dir)) == 0
        path = os.path.join(host.g().dist, "enhance", "import", "hud.pack")
        assert read_pack(path)[ONE][3] == pack.KIND_ART
        with open(path, "rb") as f:
            assert pack.HEADER.unpack_from(f.read(pack.HEADER.size))[1] == 2


def test_payload_sizes_follow_the_floored_layout():
    assert imported.chain_length(16, 16) == 5
    assert imported.chain_length(1, 1) == 1
    assert imported.chain_length(3, 5) == 3
    assert len(imported.payload(3, 5, solid(3, 5), 3)) == 4 * (15 + 2 + 1)


def test_levels_for():
    one, mip = {"levels": 1, "width": 16, "class": "world"}, {"levels": 4, "width": 16}
    hud_piece = dict(one, **{"class": "ui"})
    assert imported.levels_for(one, 16, 16, "auto") == 1
    assert imported.levels_for(one, 32, 32, "auto") == 6
    assert imported.levels_for(hud_piece, 32, 32, "auto") == 1
    assert imported.levels_for(mip, 16, 16, "auto") == 5
    assert imported.levels_for(None, 16, 16, "auto") == 5
    assert imported.levels_for(one, 16, 16, "full") == 5
    assert imported.levels_for(mip, 16, 16, "none") == 1
