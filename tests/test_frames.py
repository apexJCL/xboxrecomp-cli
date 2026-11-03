"""Tests for frames.py: the PNG encoder's filters against plain per-byte
ones, the round trip (24 and 32 bpp, top-down and bottom-up, alpha), the
conversion's checks (a mismatch or a conflict keeps the BMP), compact's
dry run, pins and links, the after-run hook's opt-out, and every frame
reader's PNG-else-BMP fallback with the golden compare unchanged.
"""

import contextlib
import io
import os
import random
import struct
import zlib

import pytest
from test_golden import (
    H,
    W,
    flat,
    make_golden,
    metal_log,
    ref_png,
    run_check,
    setup_run,
    with_rect,
    write_bmp,
)

from xboxrecomp_cli import frames as F
from xboxrecomp_cli import golden as G
from xboxrecomp_cli import main as M
from xboxrecomp_cli.hudmap import capture, sheet


def bmp_bytes(w, h, px, bpp=3, top_down=True, alpha=None):
    """A BMP of w x h RGB pixels; bpp 4 writes alpha (bytes, or 0)."""
    src = 3
    stride = (w * bpp + 3) & ~3
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            i = y * w + x
            r, g, b = px[src * i : src * i + 3]
            row += bytes((b, g, r))
            if bpp == 4:
                row.append(alpha[i] if alpha else 0)
        rows.append(bytes(row) + b"\0" * (stride - w * bpp))
    body = b"".join(rows if top_down else rows[::-1])
    hdr = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack(
        "<IiiHHIIiiII", 40, w, -h if top_down else h, 1, 8 * bpp, 0, len(body), 0, 0, 0, 0
    )
    return hdr + info + body


def noisy(w, h, seed=1):
    """A picture with flat parts, gradients and noise, so every filter wins
    some row."""
    rnd = random.Random(seed)
    out = bytearray()
    for y in range(h):
        for x in range(w):
            if y < h // 4:
                out += bytes((40, 40, 40))
            elif y < h // 2:
                out += bytes(((x * 4) & 255, (y * 3) & 255, (x + y) & 255))
            else:
                out += bytes(rnd.randrange(256) for _ in range(3))
    return bytes(out)


def unfilter_ref(data):
    """A plain reference PNG decoder (no SWAR): (w, h, bpp, pixels, filters)."""
    pos, idat = 8, b""
    while pos < len(data):
        n, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8 : pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, _, ct, _, _, _ = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
    bpp = 3 if ct == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev, out, used = [0] * stride, [], set()
    for y in range(h):
        ft = raw[y * (stride + 1)]
        used.add(ft)
        cur = list(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pr = [0, a, b, (a + b) // 2, G._paeth(a, b, c)][ft]
            cur[i] = (cur[i] + pr) & 255
        out.append(bytes(cur))
        prev = cur
    return w, h, bpp, b"".join(out), used


# ---- the filters --------------------------------------------------------


def test_swar_filters_match_per_byte():
    rnd = random.Random(7)
    n = 300
    a = bytes(rnd.randrange(256) for _ in range(n))
    b = bytes(rnd.randrange(256) for _ in range(n))
    c = bytes(rnd.randrange(256) for _ in range(n))
    # The corners: every pair of extremes.
    ext = [0, 1, 127, 128, 254, 255]
    a += bytes(x for x in ext for _ in ext for _ in ext)
    b += bytes(y for _ in ext for y in ext for _ in ext)
    c += bytes(z for _ in ext for _ in ext for z in ext)
    n = len(a)
    ia, ib = int.from_bytes(a, "little"), int.from_bytes(b, "little")
    sub = F._sub_bytes(ia, ib, n).to_bytes(n, "little")
    assert sub == bytes((x - y) & 255 for x, y in zip(a, b))
    avg = F._avg_bytes(ia, ib, n).to_bytes(n, "little")
    assert avg == bytes((x + y) >> 1 for x, y in zip(a, b))
    assert F._paeth_pred(a, b, c, n) == bytes(G._paeth(x, y, z) for x, y, z in zip(a, b, c))


@pytest.mark.parametrize("bpp", [3, 4])
def test_png_round_trip_uses_filters_and_matches_reference(bpp):
    w, h = 37, 29
    rgb = noisy(w, h)
    if bpp == 4:
        px = bytearray(w * h * 4)
        px[0::4], px[1::4], px[2::4] = rgb[0::3], rgb[1::3], rgb[2::3]
        px[3::4] = bytes(i & 255 for i in range(w * h))
        px = bytes(px)
    else:
        px = rgb
    data = F.encode_png(w, h, bpp, px)
    assert F.decode_png(data) == (w, h, bpp, px)
    rw, rh, rbpp, rpx, used = unfilter_ref(data)
    assert (rw, rh, rbpp, rpx) == (w, h, bpp, px)
    assert len(used) >= 2, used  # chosen per row, not one filter


def test_filtered_png_smaller_than_unfiltered():
    w, h = 64, 64
    rgb = bytes(((x * 4) & 255) for y in range(h) for x in range(w) for _ in range(3))
    plain = zlib.compress(b"".join(b"\0" + rgb[y * w * 3 : (y + 1) * w * 3] for y in range(h)), 9)
    assert len(F.encode_png(w, h, 3, rgb)) < len(plain)


# ---- BMP in -------------------------------------------------------------


@pytest.mark.parametrize("bpp,top", [(3, True), (3, False), (4, True), (4, False)])
def test_bmp_reader_orders_and_alpha(tmp_path, bpp, top):
    w, h = 5, 3  # odd width: row padding
    rgb = noisy(w, h, seed=3)
    p = tmp_path / "f.bmp"
    p.write_bytes(bmp_bytes(w, h, rgb, bpp=bpp, top_down=top))
    assert F.read_bmp_px(str(p)) == (w, h, 3, rgb)  # uniform alpha: RGB
    assert G.read_bmp(str(p)) == (w, h, rgb)


def test_bmp_varying_alpha_kept_as_rgba(tmp_path):
    w, h = 4, 2
    rgb = noisy(w, h)
    alpha = bytes(range(8))
    p = tmp_path / "f.bmp"
    p.write_bytes(bmp_bytes(w, h, rgb, bpp=4, alpha=alpha))
    w2, h2, bpp, px = F.read_bmp_px(str(p))
    assert bpp == 4 and px[3::4] == alpha and px[0::4] == rgb[0::3]
    assert F.convert(str(p))[0] == "done"
    png = tmp_path / "f.png"
    assert F.decode_png(png.read_bytes())[3] == px
    assert G.read_png(str(png)) == (w, h, rgb)


# ---- one file -----------------------------------------------------------


def test_convert_keeps_name_pixels_and_mtime(tmp_path):
    p = tmp_path / "flip_01139.bmp"
    write_bmp(str(p), with_rect(flat(30), [4, 4, 9, 9], 220))
    os.utime(p, (1_700_000_000, 1_700_000_000))
    before = G.read_image(str(p))
    st, bsize, psize, note = F.convert(str(p))
    assert st == "done" and note == "" and psize < bsize
    png = tmp_path / "flip_01139.png"
    assert not p.exists() and png.exists()
    assert G.read_image(str(png)) == before
    assert int(os.stat(png).st_mtime) == 1_700_000_000
    assert [x.name for x in tmp_path.iterdir()] == ["flip_01139.png"]  # no tmp left


def test_convert_mismatch_keeps_bmp(tmp_path):
    p = tmp_path / "f.bmp"
    write_bmp(str(p), flat(9))

    def bad(w, h, bpp, px):
        return F.encode_png(w, h, bpp, bytes([px[0] ^ 1]) + px[1:])

    st, _, _, note = F.convert(str(p), encode=bad)
    assert st == "mismatch" and "differ" in note
    assert p.exists() and not (tmp_path / "f.png").exists()


def test_convert_conflict_and_same(tmp_path):
    p = tmp_path / "f.bmp"
    write_bmp(str(p), flat(9))
    G.write_png(str(tmp_path / "f.png"), W, H, flat(10))
    assert F.convert(str(p))[0] == "conflict" and p.exists()
    G.write_png(str(tmp_path / "f.png"), W, H, flat(9))
    assert F.convert(str(p))[0] == "same" and not p.exists()


def test_convert_unsupported_kept(tmp_path):
    p = tmp_path / "f.bmp"
    data = bytearray(bmp_bytes(2, 2, bytes(12)))
    data[28:30] = struct.pack("<H", 16)
    p.write_bytes(bytes(data))
    assert F.convert(str(p))[0] == "unsupported" and p.exists()


# ---- compact ------------------------------------------------------------


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = M.main(argv)
    return rc, out.getvalue(), err.getvalue()


def tree(tmp_path):
    run = tmp_path / "runs" / "20261003-005602" / "frames"
    other = tmp_path / "runs" / "20261101-072943" / "frames"
    for d in (run, other):
        d.mkdir(parents=True)
        for n in (61, 62):
            write_bmp(str(d / ("flip_%05d.bmp" % n)), with_rect(flat(n), [2, 2, 5, 5], 200))
    outside = tmp_path / "outside.bmp"
    write_bmp(str(outside), flat(1))
    os.symlink(outside, other / "link.bmp")
    return run, other, outside


def test_compact_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run, other, outside = tree(tmp_path)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    rc, out, err = run_main(["frames", "compact", "--dry-run", "--jobs", "1", "runs"])
    assert rc == 0, err
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    assert "would convert 4 BMPs" in out and "would save" in out, out


def test_compact_pins_links_and_outside(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run, other, outside = tree(tmp_path)
    pins = tmp_path / "golden.json"
    pins.write_text('{"why": "reference run 20261003-005602; also flip_00062.bmp"}')
    rc, out, err = run_main(["frames", "compact", "--jobs", "2", "--pins", str(pins), "runs"])
    assert rc == 0, err
    # The cited run stays BMP; in the other, the named flip stays too.
    assert sorted(p.name for p in run.iterdir()) == ["flip_00061.bmp", "flip_00062.bmp"]
    assert sorted(p.name for p in other.iterdir()) == [
        "flip_00061.png",
        "flip_00062.bmp",
        "link.bmp",
    ]
    assert os.path.islink(other / "link.bmp") and outside.exists()
    assert "converted 1 BMPs" in out and "3 pinned" in out, out


def test_compact_mismatch_exit_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "d"
    d.mkdir()
    write_bmp(str(d / "a.bmp"), flat(5))
    real = F.encode_png
    monkeypatch.setattr(F, "encode_png", lambda w, h, b, px: real(w, h, b, bytes(len(px))))
    rc, out, err = run_main(["frames", "compact", "--jobs", "1", "d"])
    assert rc == 1 and "kept: mismatch" in err, (out, err)
    assert (d / "a.bmp").exists() and not (d / "a.png").exists()


def test_compact_usage_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_main(["frames", "compact"])[0] == 2
    assert run_main(["frames", "compact", "nope"])[0] == 2
    assert run_main(["frames", "nope"])[0] == 2


def test_after_run_opt_out(tmp_path, monkeypatch):
    write_bmp(str(tmp_path / "flip_00061.bmp"), flat(5))
    said = []
    assert F.after_run([str(tmp_path)], say=said.append, env={"XBR_FRAMES_BMP": "1"}) == 0
    assert (tmp_path / "flip_00061.bmp").exists() and "kept as BMP" in said[0]
    assert F.after_run([str(tmp_path)], say=said.append, bmp=True, env={}) == 0
    assert (tmp_path / "flip_00061.bmp").exists()
    assert F.after_run([str(tmp_path)], say=said.append, env={}, jobs=1) == 0
    assert (tmp_path / "flip_00061.png").exists()


# ---- readers: PNG, else BMP ---------------------------------------------


def test_golden_paths_prefer_png(tmp_path):
    d = str(tmp_path)
    assert G.dump_image_path(d, 1).endswith("frame_0001.bmp")  # neither: the dumper's name
    write_bmp(os.path.join(d, "flip_00061.bmp"), flat(3))
    assert G.dump_image_path(d, 1).endswith("flip_00061.bmp")
    F.convert(os.path.join(d, "flip_00061.bmp"))
    assert G.dump_image_path(d, 1).endswith("flip_00061.png")
    assert G.flip_path(d, 61).endswith(".png")
    assert G.FLIP_NAME_RE.match("flip_00061.png")


def test_golden_check_same_metrics_after_compact(tmp_path):
    rect = [8, 8, 16, 16]
    ref = with_rect(flat(100), rect, 200)
    sha = ref_png(str(tmp_path), "f", ref)
    frame = {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]}
    gj = make_golden(str(tmp_path), frame)
    run = with_rect(flat(100), rect, 197)  # CLOSE, not EXACT: real metrics
    d = setup_run(str(tmp_path), metal_log({60: 1, 61: 1, 62: 1}), {60: run, 61: run, 62: ref})
    rc1, out1 = run_check(str(tmp_path), gj, d)
    assert rc1 == 0 and "CLOSE" in out1, out1
    assert F.after_run([d], say=lambda m: None, env={}, jobs=1) == 0
    assert not [n for n in os.listdir(d) if n.endswith(".bmp")]
    rc2, out2 = run_check(str(tmp_path), gj, d)
    assert rc2 == rc1
    assert out2.replace(".png", ".bmp") == out1


def test_hudmap_readers_take_png(tmp_path):
    fd = tmp_path / "frames"
    fd.mkdir()
    write_bmp(str(fd / "flip_00300.bmp"), flat(1))
    write_bmp(str(fd / "flip_00420.bmp"), flat(2))
    F.convert(str(fd / "flip_00420.bmp"))
    got = sheet.frames_of(str(tmp_path))
    assert got[300].endswith(".bmp") and got[420].endswith(".png")
    write_bmp(str(tmp_path / "frame_00010.bmp"), flat(1))
    jl = str(tmp_path / "capture.jsonl")
    fr = capture.Frame({"frame": 10, "bmp": "frame_00010.bmp"}, jl)
    assert fr.bmp.endswith("frame_00010.bmp")
    F.convert(str(tmp_path / "frame_00010.bmp"))
    fr = capture.Frame({"frame": 10, "bmp": "frame_00010.bmp"}, jl)
    assert fr.bmp.endswith("frame_00010.png")


def test_compact_overlapping_dirs_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    write_bmp(str(d / "f.bmp"), flat(5))
    rc, out, err = run_main(["frames", "compact", "--jobs", "2", "a", "a/b"])
    assert rc == 0, err
    assert "total: converted 1 BMPs" in out, out
    assert [p.name for p in d.iterdir()] == ["f.png"]


# ---- review fixes: alpha, header, pins from the reference text ----------


def test_bmp_constant_ff_alpha_kept_as_rgba(tmp_path):
    # Only an alpha of 0 everywhere goes; a constant 0xFF is data too.
    w, h = 3, 2
    rgb = noisy(w, h)
    p = tmp_path / "f.bmp"
    p.write_bytes(bmp_bytes(w, h, rgb, bpp=4, alpha=b"\xff" * (w * h)))
    _, _, bpp, px = F.read_bmp_px(str(p))
    assert bpp == 4 and px[3::4] == b"\xff" * (w * h)
    assert F.convert(str(p))[0] == "done"
    assert F.decode_png((tmp_path / "f.png").read_bytes())[2:] == (4, px)
    assert G.read_png(str(tmp_path / "f.png")) == (w, h, rgb)


def test_bmp_v5_header_left_as_bmp(tmp_path):
    # A V5 header's colour space and profile would not survive the PNG.
    w, h = 2, 2
    d = bmp_bytes(w, h, noisy(w, h))
    body = d[54:]
    v5 = struct.pack("<IiiHHIIiiII", 124, w, -h, 1, 24, 0, len(body), 0, 0, 0, 0)
    v5 += b"\0" * (124 - 40)
    hdr = struct.pack("<2sIHHI", b"BM", 14 + 124 + len(body), 0, 0, 14 + 124)
    p = tmp_path / "v5.bmp"
    p.write_bytes(hdr + v5 + body)
    status = F.convert(str(p))[0]
    assert status == "unsupported" and p.exists() and not (tmp_path / "v5.png").exists()


def test_pins_reference_text_paths_folders_stamps(tmp_path):
    ws = tmp_path / "ws"
    refs = (
        "The reference frames are `runs/topic/b3/host-after/host_07000.bmp` and\n"
        "`host-before/host_05000.bmp`. The A/B dumps are in runs/ab-topic/.\n"
        "Copy runs to runs/ with cp -c; see bench-logs/20261003-005602.\n"
        "The workspace is %s.\n" % (ws / "game")
    )
    pins = F.Pins(refs=refs, keep=F.ancestors(str(ws / "game")))
    top = str(ws / "runs")

    def why(rel):
        return pins.why(str(ws / "runs" / rel), top)

    assert why("topic/b3/host-after/host_07000.bmp").startswith("named")
    assert why("topic/b3/host-before/host_05000.bmp").startswith("named")
    assert why("topic/b3/host-after/host_01000.bmp") is None
    assert why("ab-topic/x/f.bmp").startswith("folder cited (runs/ab-topic)")
    # Naming the store, or a folder above the game, cites nothing in it.
    assert why("other/f.bmp") is None
    assert why("20261003-005602/frames/f.bmp") == "run 20261003-005602 cited"
    # A bare name in prose pins nothing; in golden.json it does.
    assert F.Pins(refs="see host_01000.bmp").why(str(ws / "a" / "host_01000.bmp"), top) is None
    assert F.Pins(names="host_01000.bmp").why(str(ws / "a" / "host_01000.bmp"), top)


def git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def test_compact_reads_the_games_reference_text(tmp_path, monkeypatch):
    here = os.path.dirname(os.path.abspath(__file__))
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.toml").write_text(
        open(os.path.join(here, "testdata", "game", "game.toml"), encoding="utf-8").read()
    )
    (game / "TASKS.md").write_text("- done: frames in runs/cited-topic/shot.bmp\n")
    (game / "openspec" / "changes" / "x").mkdir(parents=True)
    (game / "openspec" / "changes" / "x" / "tasks.md").write_text("A/B in `runs/ab/`\n")
    git(game, "init", "-q", "-b", "main")
    git(game, "add", "-A")
    git(game, "commit", "-q", "-m", "x")
    runs = tmp_path / "runs"
    for rel in ("cited-topic/shot.bmp", "cited-topic/other.bmp", "ab/a/f.bmp", "free/f.bmp"):
        (runs / rel).parent.mkdir(parents=True, exist_ok=True)
        write_bmp(str(runs / rel), flat(3))
    monkeypatch.chdir(game)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = F.main(["compact", "--dry-run", "--jobs", "1", str(runs)], root=str(game))
    out = buf.getvalue()
    assert rc == 0, out
    assert "would convert 2 BMPs" in out and "2 pinned" in out, out
    assert (
        "left as BMP: %s (named (runs/cited-topic/shot.bmp))" % (runs / "cited-topic/shot.bmp")
        in out
    )
    assert "left as BMP: %s (folder cited (runs/ab))" % (runs / "ab/a/f.bmp") in out
    assert "reference text" in out
    # The real run converts only what nothing cites.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert F.main(["compact", "--jobs", "1", str(runs)], root=str(game)) == 0
    assert (runs / "cited-topic/shot.bmp").exists() and (runs / "ab/a/f.bmp").exists()
    assert (runs / "free/f.png").exists() and (runs / "cited-topic/other.png").exists()


def test_logs_warning_only_for_a_worktree_folder(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    (main / "a").write_text("a\n")
    git(main, "init", "-q", "-b", "main")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "x")
    (main / "bench-logs").mkdir()
    assert F.logs_warning(str(main)) is None  # the main checkout's own store
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "b", str(wt))
    assert F.logs_warning(str(wt)) is None  # no bench-logs yet
    os.symlink(main / "bench-logs", wt / "bench-logs")
    assert F.logs_warning(str(wt)) is None  # the link to the store
    os.remove(wt / "bench-logs")
    (wt / "bench-logs").mkdir()
    w = F.logs_warning(str(wt))
    assert w and "not a link to the shared store" in w and str(main / "bench-logs") in w, w


def test_link_never_overwrites_a_png_made_meanwhile(tmp_path, monkeypatch):
    p = tmp_path / "f.bmp"
    write_bmp(str(p), flat(9))
    real = os.link

    def racing(src, dst, **kw):
        # Another writer puts a PNG there between the check and the link.
        open(dst, "wb").write(b"theirs")
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "link", racing)
    status, *_ = F.convert(str(p))
    assert status == "conflict" and p.exists()
    assert (tmp_path / "f.png").read_bytes() == b"theirs"
    assert not [n for n in os.listdir(tmp_path) if ".tmp-" in n]
