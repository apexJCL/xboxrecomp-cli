#!/usr/bin/env python3
"""Tests for golden.py: anchor transitions, INCOMPLETE on a missing anchored
flip, masks and their presence check, CPU skip. Synthetic images and logs in
a temp dir; plain asserts. Runs alone or under pytest.

  uv run python tests/test_golden.py
  uv run pytest tests/test_golden.py

Exit 0 when every test passes. Also covers the pace check (CLOSE in limits,
INCOMPLETE outside, FAIL on a wrong screen) and FAIL-over-INCOMPLETE.
"""

import contextlib
import io
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
from xboxrecomp_cli import golden as G  # noqa: E402

W, H = 64, 64


def write_bmp(path, rgb):
    """24-bit top-down BMP of a W x H image (rgb bytes, row-major)."""
    stride = (W * 3 + 3) & ~3
    body = bytearray()
    for y in range(H):
        row = bytearray()
        for x in range(W):
            r, g, b = rgb[3 * (y * W + x) : 3 * (y * W + x) + 3]
            row += bytes((b, g, r))
        body += row + b"\0" * (stride - W * 3)
    hdr = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, W, -H, 1, 24, 0, len(body), 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(hdr + info + body)


def flat(v):
    return bytes([v]) * (W * H * 3)


def with_rect(base, rect, v):
    p = bytearray(base)
    x0, y0, w, h = rect
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            p[3 * (y * W + x) : 3 * (y * W + x) + 3] = bytes((v, v, v))
    return bytes(p)


# ---- anchors ------------------------------------------------------------------


def test_anchor_needs_a_transition_and_two_hits():
    ev = {"after_flip": 10, "max_batches": 100}
    b = {f: 500 for f in range(1, 60)}
    b[20] = 0  # one stray low flip (missing backend line, black frame)
    for f in range(40, 60):
        b[f] = 80
    assert G.find_anchor(b, ev) == 40


def test_anchor_not_at_after_flip_without_a_crossing():
    ev = {"after_flip": 10, "max_batches": 100}
    b = {f: 50 for f in range(1, 60)}  # already low before after_flip
    assert G.find_anchor(b, ev) is None


def test_anchor_min_batches():
    ev = {"after_flip": 5, "min_batches": 5}
    b = {f: 3 for f in range(1, 40)}
    b[12] = 6  # single spike
    for f in range(30, 40):
        b[f] = 5
    assert G.find_anchor(b, ev) == 30


def test_anchor_gap_breaks_a_run():
    ev = {"after_flip": 1, "min_batches": 5}
    b = {f: 3 for f in range(1, 40)}
    b[20] = 5
    del b[21]  # the next flip is missing from the log
    for f in range(30, 40):
        b[f] = 5
    assert G.find_anchor(b, ev) == 30


def menu_timeline(menu):
    """A story-like flip log: an opening movie (600 batches) from flip 50, a
    locker room (90) from 160, the slot list (40) at `menu`, its full list
    (140) 10 flips later, then a 1-batch saving screen 30 flips later."""
    b = {f: 5 for f in range(1, 400)}
    for f in range(50, 150):
        b[f] = 600
    for f in range(150, menu):
        b[f] = 90 if f >= 160 else 600
    for f in range(menu, menu + 30):
        b[f] = 40 if f < menu + 10 else 140
    for f in range(menu + 30, 400):
        b[f] = 1
    return b


CHAIN = {
    "opening": {"after_flip": 20, "min_batches": 500},
    "locker": {"after": "opening", "max_batches": 100},
    "menu": {"after": "locker", "max_batches": 60},
}


def test_anchor_after_finds_its_screen_at_any_pace():
    # A fixed after_flip set from a normal run (menu at 300) skips the menu of
    # a slow run (at 250) and anchors on the next low screen; chained to the
    # locker room, it finds the menu at both.
    fixed = {"after_flip": 280, "max_batches": 60}
    assert G.find_anchor(menu_timeline(300), fixed) == 300
    assert G.find_anchor(menu_timeline(250), fixed) == 280  # the saving screen
    for menu in (250, 300):
        assert G.find_anchor(menu_timeline(menu), CHAIN["menu"], CHAIN) == menu


def test_anchor_after_needs_the_prior_event():
    b = {f: 90 for f in range(1, 400)}  # no opening movie: no locker, no menu
    b[300] = b[301] = 40
    assert G.find_anchor(b, CHAIN["menu"], CHAIN) is None


def test_anchor_after_keeps_after_flip_as_a_floor():
    ev = {"after": "locker", "after_flip": 350, "max_batches": 60}
    assert G.find_anchor(menu_timeline(250), ev, CHAIN) is None


def exits(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


def test_anchor_after_unknown_or_cyclic_exits():
    b = menu_timeline(250)
    assert exits(lambda: G.find_anchor(b, {"after": "nope", "max_batches": 60}, CHAIN))
    loop = {"a": {"after": "b", "max_batches": 60}, "b": {"after": "a", "max_batches": 60}}
    assert exits(lambda: G.find_anchor(b, loop["a"], loop))


# ---- check: INCOMPLETE, masks, skip -----------------------------------------------


def make_golden(tmp, frame, anchors=None, extra=None):
    g = {
        "compare": {
            "pixel_tol": 8,
            "max_channel_mae": 1.0,
            "max_bad_fraction": 0.01,
            "max_tile_bad_fraction": 0.5,
        },
        "scenarios": {
            "s": dict({"seconds": 1, "frames": [frame], "anchors": anchors or {}}, **(extra or {}))
        },
    }
    path = os.path.join(tmp, "golden.json")
    with open(path, "w") as f:
        json.dump(g, f)
    return path


def run_check(tmp, golden_json, run_dir):
    G.GOLDEN_JSON = golden_json
    G.FRAMES_DIR = os.path.join(tmp, "frames")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = G.cmd_check(["s=" + run_dir])
    return rc, out.getvalue()


def setup_run(tmp, log_lines, flips):
    run = os.path.join(tmp, "run")
    os.makedirs(os.path.join(run, "frames"), exist_ok=True)
    with open(os.path.join(run, "game-stdio.log"), "w") as f:
        f.write("\n".join(log_lines) + "\n")
    for n, img in flips.items():
        write_bmp(os.path.join(run, "frames", "flip_%05d.bmp" % n), img)
    return os.path.join(run, "frames")


def ref_png(tmp, name, img):
    os.makedirs(os.path.join(tmp, "frames"), exist_ok=True)
    G.write_png(os.path.join(tmp, "frames", name + ".png"), W, H, img)
    return G.pixel_sha((W, H, img))


def metal_log(batches):
    return ["[METAL] flip %d batches %d" % (f, b) for f, b in sorted(batches.items())]


def test_incomplete_when_anchor_missing_but_flips_dumped():
    with tempfile.TemporaryDirectory() as tmp:
        img = flat(100)
        sha = ref_png(tmp, "f", img)
        frame = {
            "name": "f",
            "dump": 1,
            "sha256": sha,
            "size": [W, H],
            "anchor": {"event": "e", "ref_flip": 10},
        }
        gj = make_golden(tmp, frame, {"e": {"after_flip": 1, "min_batches": 5}})
        d = setup_run(tmp, metal_log({f: 1 for f in range(1, 80)}), {61: img})
        rc, out = run_check(tmp, gj, d)
        assert rc == 2 and "INCOMPLETE" in out, out


def test_incomplete_when_target_flip_not_dumped():
    with tempfile.TemporaryDirectory() as tmp:
        img = flat(100)
        sha = ref_png(tmp, "f", img)
        frame = {
            "name": "f",
            "dump": 1,
            "sha256": sha,
            "size": [W, H],
            "anchor": {"event": "e", "ref_flip": 10},
        }
        gj = make_golden(tmp, frame, {"e": {"after_flip": 1, "min_batches": 5}})
        b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
        d = setup_run(tmp, metal_log(b), {61: img})  # target is 30+61-10 = 81
        rc, out = run_check(tmp, gj, d)
        assert rc == 2 and "INCOMPLETE" in out, out


def test_incomplete_when_no_flip_dumps_pulled():
    with tempfile.TemporaryDirectory() as tmp:
        img = flat(100)
        sha = ref_png(tmp, "f", img)
        frame = {
            "name": "f",
            "dump": 1,
            "sha256": sha,
            "size": [W, H],
            "anchor": {"event": "e", "ref_flip": 10},
        }
        gj = make_golden(tmp, frame, {"e": {"after_flip": 1, "min_batches": 5}})
        b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
        d = setup_run(tmp, metal_log(b), {})
        write_bmp(os.path.join(d, "frame_0001.bmp"), img)  # plain dump only
        rc, out = run_check(tmp, gj, d)
        assert rc == 2 and "INCOMPLETE" in out, out


def test_anchored_target_is_used():
    with tempfile.TemporaryDirectory() as tmp:
        img = flat(100)
        sha = ref_png(tmp, "f", img)
        frame = {
            "name": "f",
            "dump": 1,
            "sha256": sha,
            "size": [W, H],
            "anchor": {"event": "e", "ref_flip": 10},
        }
        gj = make_golden(tmp, frame, {"e": {"after_flip": 1, "min_batches": 5}})
        b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
        d = setup_run(tmp, metal_log(b), {61: flat(0), 81: img})
        rc, out = run_check(tmp, gj, d)
        assert rc == 0 and "EXACT" in out, out


def test_mask_and_presence():
    with tempfile.TemporaryDirectory() as tmp:
        rect = [8, 8, 16, 16]
        ref = with_rect(flat(100), rect, 200)
        sha = ref_png(tmp, "f", ref)
        frame = {
            "name": "f",
            "dump": 1,
            "sha256": sha,
            "size": [W, H],
            "masks": [
                {
                    "rect": rect,
                    "presence": {"pixel_tol": 96, "max_bad_fraction": 0.02, "max_channel_mae": 60},
                }
            ],
        }
        gj = make_golden(tmp, frame)
        # Pulsing inside the mask: passes.
        d = setup_run(tmp, metal_log({61: 1}), {61: with_rect(flat(100), rect, 160)})
        rc, out = run_check(tmp, gj, d)
        assert rc == 0 and "CLOSE" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        sha = ref_png(tmp, "f", ref)
        gj = make_golden(tmp, frame)
        # The masked thing is gone (background there): presence fails.
        d = setup_run(tmp, metal_log({61: 1}), {61: flat(100)})
        rc, out = run_check(tmp, gj, d)
        assert rc == 1 and "presence" in out, out


def test_cpu_skip():
    with tempfile.TemporaryDirectory() as tmp:
        sha = ref_png(tmp, "f", flat(100))
        frame = {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]}
        gj = make_golden(tmp, frame, extra={"skip_backends": ["cpu"], "skip_why": "slow"})
        d = setup_run(tmp, ["[GPU] flip 61 0 ms fence 0x0 batches 3"], {61: flat(0)})
        rc, out = run_check(tmp, gj, d)
        assert rc == 0 and "SKIP" in out, out


# ---- stock guarantee: a run with enhancements on is never a golden ---------------

STOCK = "[ENHANCE] render.scale=1 present.filter=nearest present.fullscreen=0 (display.aspect=4:3)"


def enhance_case(tmp, enhance_lines):
    sha = ref_png(tmp, "f", flat(100))
    frame = {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]}
    gj = make_golden(tmp, frame)
    d = setup_run(tmp, enhance_lines + metal_log({61: 3}), {61: flat(100)})
    return run_check(tmp, gj, d)


def test_enhance_scale_2_fails():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK.replace("render.scale=1", "render.scale=2")])
        assert rc == 1 and "not a stock run" in out and "render.scale=2" in out, out


def test_enhance_aniso_fails_and_stock_passes():
    aniso = STOCK.replace("render.scale=1", "render.scale=1 render.aniso=%d")
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [aniso % 16])
        assert rc == 1 and "not a stock run" in out and "render.aniso=16" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(
            tmp, ["[ENHANCE] render.aniso=99 out of range 0..16; using 16", aniso % 16]
        )
        assert rc == 1 and "render.aniso=16" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [aniso % 0])
        assert rc == 0 and "EXACT" in out, out


def test_enhance_aspect_fails():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(
            tmp,
            [
                "[ENHANCE] display.aspect=16:9 not implemented yet (hor+ is a later slice); using 4:3",
                STOCK,
            ],
        )
        assert rc == 1 and "display.aspect=16:9" in out, out


OVERSCAN = STOCK.replace("(display.aspect=4:3)", "(display.aspect=4:3 display.overscan={})")


def test_enhance_overscan_fails():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [OVERSCAN.format(9)])
        assert rc == 1 and "not a stock run" in out and "display.overscan=9" in out, out


def test_enhance_overscan_refused_or_ignored_passes():
    """The clamp warning, the hor+ refusal and the CPU rasteriser's line
    carry the raw value; only the summary line's value counts, and a summary
    without the key (0, or an older toolkit) is stock."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(
            tmp,
            [
                "[ENHANCE] display.overscan=-1 out of range 0..64; using 0",
                "[ENHANCE] display.overscan=9 ignored by the CPU rasteriser:"
                " it presents the whole frame",
                STOCK,
            ],
        )
        assert rc == 0 and "EXACT" in out, out
    rx = G.ENHANCE_OVERSCAN_RE
    assert rx.search(OVERSCAN.format(12)).group(1) == "12"
    assert rx.search(STOCK) is None
    assert (
        rx.search(
            "[ENHANCE] display.overscan=9 ignored with display.aspect=16:9: the 4:3"
            " edges are inside the padded frame; using 0"
        )
        is None
    )


def test_enhance_stock_passes():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK])
        assert rc == 0 and "EXACT" in out, out


def test_enhance_pack_fails():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(
            tmp, [STOCK, "[ENHANCE] textures.pack=enhance/hud.pack (268 entries, scale 4)"]
        )
        assert rc == 1 and "textures.pack=enhance/hud.pack" in out, out


def test_enhance_pack_none_or_refused_passes():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK, "[ENHANCE] textures.pack=none"])
        assert rc == 0 and "EXACT" in out, out
        rc, out = enhance_case(
            tmp,
            [
                STOCK,
                "[ENHANCE] textures.pack=enhance/hud.pack: not a texture pack "
                "(/opt/game/enhance/hud.pack); no pack",
            ],
        )
        assert rc == 0 and "EXACT" in out, out


def test_enhance_no_line_passes():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [])
        assert rc == 0 and "EXACT" in out, out


PACED = (
    "[ENHANCE] render.scale=1 present.filter=nearest present.fullscreen=0"
    " present.pacing={} (display.aspect=4:3)"
)


def test_enhance_pacing_spin_and_lock30_pass():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [PACED.format("spin"), "[ENHANCE] fps.mode=lock30"])
        assert rc == 0 and "EXACT" in out, out


def test_enhance_pacing_sleep_fails():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [PACED.format("sleep")])
        assert rc == 1 and "present.pacing=sleep" in out, out


def test_enhance_fps_mode_fails_even_when_unavailable():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(
            tmp,
            [
                PACED.format("spin"),
                "[ENHANCE] fps.mode=lock60 not available for this title (stage logic"
                " advances a fixed 1/30 s per frame; see docs/env.md); using lock30",
            ],
        )
        assert rc == 1 and "fps.mode=lock60" in out, out


GLOW = "[ENHANCE] fx.glow={} fx.glow_intensity={}"


def test_enhance_game_keys_from_game_toml():
    """The game's keys come from its enhance_stock (testdata: BLiNX 2's):
    fx.glow=off is non-stock, an intensity of 1.0 is stock 1, and fx.glow=
    never reads fx.glow_intensity='s value."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK, GLOW.format("on", "1.0")])
        assert rc == 0 and "EXACT" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK, GLOW.format("off", "1")])
        assert rc == 1 and "fx.glow=off" in out and "fx.glow_intensity" not in out, out
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = enhance_case(tmp, [STOCK, GLOW.format("on", "0.5")])
        assert rc == 1 and "fx.glow_intensity=0.5" in out and "fx.glow=on" not in out, out
    rx = G.game_key_re("fx.glow")
    assert rx.search("[ENHANCE] fx.glow_intensity=1") is None
    assert rx.search("[ENHANCE] xfx.glow=off") is None
    assert rx.search("[ENHANCE] fx.glow_intensity=1 fx.glow=off").group(1) == "off"


def test_enhance_without_game_table():
    """A game without enhance_stock checks the toolkit's keys only: fps.mode
    is BLiNX 2's, not the CLI's."""
    saved = dict(G.GAME_ENHANCE)
    G.GAME_ENHANCE = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = enhance_case(tmp, [PACED.format("spin"), "[ENHANCE] fps.mode=lock60"])
            assert rc == 0 and "EXACT" in out, out
    finally:
        G.GAME_ENHANCE = saved


def test_enhance_stock_flags_configure():
    saved = (G.GOLDEN_JSON, G.FRAMES_DIR, G.REPO, G.GOLDEN_DIR, dict(G.GAME_ENHANCE))
    try:
        rest = G.take_config(
            ["--golden-json", "/x/g.json", "--enhance-stock", "fx.glow=on", "check", "a=b"]
        )
        assert rest == ["check", "a=b"] and G.GAME_ENHANCE == {"fx.glow": "on"}
        assert G.enhance_args({"b": "2", "a": "1"}) == [
            "--enhance-stock",
            "a=1",
            "--enhance-stock",
            "b=2",
        ]
    finally:
        G.GOLDEN_JSON, G.FRAMES_DIR, G.REPO, G.GOLDEN_DIR, G.GAME_ENHANCE = saved


def check_allowing(tmp, lines, *allow):
    sha = ref_png(tmp, "f", flat(100))
    frame = {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]}
    gj = make_golden(tmp, frame)
    d = setup_run(tmp, lines + metal_log({61: 3}), {61: flat(100)})
    G.GOLDEN_JSON = gj
    G.FRAMES_DIR = os.path.join(tmp, "frames")
    args = []
    for a in allow:
        args += ["--allow-enhance", a]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = G.cmd_check(args + ["s=" + d])
    return rc, out.getvalue()


def test_allow_enhance_game_key():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = check_allowing(tmp, [STOCK, GLOW.format("off", "1")], "fx.glow=off")
        assert rc == 0 and "NOTE" in out and "fx.glow=off" in out, out


def test_allow_enhance_pack():
    with tempfile.TemporaryDirectory() as tmp:
        pack = "[ENHANCE] textures.pack=enhance/hud.pack (268 entries, scale 4)"
        rc, out = check_allowing(tmp, [STOCK, pack], "textures.pack=enhance/hud.pack")
        assert rc == 0 and "NOTE" in out and "textures.pack=enhance/hud.pack" in out, out


def test_allow_enhance_overscan():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = check_allowing(tmp, [OVERSCAN.format(9)], "display.overscan=9")
        assert rc == 0 and "NOTE" in out and "display.overscan=9" in out, out


def test_allow_enhance_evaluates_with_a_note():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = check_allowing(tmp, [PACED.format("sleep")], "present.pacing=sleep")
        assert rc == 0 and "EXACT" in out and "NOTE" in out and "present.pacing=sleep" in out, out


def test_allow_enhance_is_only_that_value():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = check_allowing(
            tmp,
            [PACED.format("sleep").replace("render.scale=1", "render.scale=2")],
            "present.pacing=sleep",
        )
        assert (
            rc == 1
            and "render.scale=2" in out
            and "present.pacing=sleep" not in out.split("not a stock run")[1].split(")")[0]
        ), out


def test_allow_enhance_bad_key_and_record_refuse():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            check_allowing(tmp, [], "present.vsync=1")
            raise AssertionError("bad key accepted")
        except SystemExit as e:
            assert "KEY=VALUE" in str(e)
        try:
            G.cmd_record(["--allow-enhance", "present.pacing=sleep", "s=" + tmp])
            raise AssertionError("record accepted --allow-enhance")
        except SystemExit as e:
            assert "stock runs only" in str(e)


# ---- pace: flips per wall second from the anchor to the frame ------------------


def paced_run(tmp, fps, timed=True, img=None, flip=81):
    """Anchor e at flip 30 (ref_flip 10, dump 1: target 81); flips at fps.
    The default image is close to the reference, not equal (no EXACT)."""
    b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
    lines = metal_log(b)
    if timed:
        lines += [
            "[GPU] flip %d %d ms fence 0x0 batches 0" % (f, round(f * 1000 / fps))
            for f in sorted(b)
        ]
    return setup_run(tmp, lines, {flip: flat(101) if img is None else img})


ANCHORS = {"e": {"after_flip": 1, "min_batches": 5}}


def paced_frame(tmp, ref_pace=None, name="f", ref_flip=10, **extra):
    sha = ref_png(tmp, name, flat(100))
    anchor = {"event": "e", "ref_flip": ref_flip}
    if ref_pace is not None:
        anchor["ref_pace"] = ref_pace
    return dict({"name": name, "dump": 1, "sha256": sha, "size": [W, H], "anchor": anchor}, **extra)


def paced_golden(tmp, ref_pace=None, scen_extra=None, **frame_extra):
    return make_golden(tmp, paced_frame(tmp, ref_pace, **frame_extra), ANCHORS, scen_extra)


OFF = with_rect(flat(100), [0, 0, 16, 16], 200)  # 6.25% bad: outside limits, not gross


def test_pace_ok():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=57.0)  # run 60: 5% off
        rc, out = run_check(tmp, gj, paced_run(tmp, 60))
        assert rc == 0 and "CLOSE" in out and "run 60.0 fps vs ref 57.0 fps" in out, out
        assert "pace mismatch" not in out, out


def test_pace_mismatch_within_limits_is_close():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41))
        assert rc == 0 and out.startswith("CLOSE"), out
        assert "pace mismatch (run 41.0 fps vs ref 60.0 fps), but within limits" in out, out


def test_pace_mismatch_outside_limits_is_incomplete():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert (
            rc == 2
            and out.startswith("INCOMPLETE s/f")
            and "pace mismatch (run 41.0 fps vs ref 60.0 fps); outside limits" in out
        ), out
        assert "golden: INCOMPLETE (1 INCOMPLETE)" in out, out


def test_pace_mismatch_wrong_screen_fails():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=flat(200)))
        assert rc == 1 and "wrong screen at any pace" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0, pace_fail_bad=0.05)  # per-frame floor
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 1 and "wrong screen at any pace" in out, out


def test_pace_mismatch_exact_stays_exact():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=flat(100)))
        assert rc == 0 and out.startswith("EXACT"), out


def test_pace_threshold_from_golden_json():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0, max_pace_diff=0.5)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 1 and out.startswith("FAIL") and "pace mismatch" not in out, out
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0, max_pace_diff=None)  # opted out
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 1 and out.startswith("FAIL") and "pace" not in out, out


def test_pace_threshold_scenario_level():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0, scen_extra={"max_pace_diff": 0.5})
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 1 and "limit +-50%" in out and "pace mismatch" not in out, out
    with tempfile.TemporaryDirectory() as tmp:  # the frame's own limit wins
        gj = paced_golden(tmp, ref_pace=60.0, scen_extra={"max_pace_diff": 0.5}, max_pace_diff=0.1)
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 2 and "pace mismatch" in out, out


def test_pace_unknown_is_not_checked():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp)  # no ref_pace recorded
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=OFF))
        assert rc == 1 and "reference unknown" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0)  # run log has no flip times
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, timed=False, img=OFF))
        assert rc == 1 and "not timed" in out, out


def test_pace_target_not_after_anchor():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, ref_pace=60.0, ref_flip=70)  # target 30+61-70 = 21
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, flip=21))
        assert rc == 0 and "target flip 21 not after anchor 30" in out, out


def test_fail_dominates_incomplete():
    with tempfile.TemporaryDirectory() as tmp:
        sha = ref_png(tmp, "g", flat(100))
        plain = {"name": "g", "dump": 1, "sha256": sha, "size": [W, H]}  # plain dump 61
        paced = paced_frame(tmp, 60.0)
        gj = make_golden(tmp, paced, ANCHORS, {"frames": [paced, plain]})
        d = paced_run(tmp, 41, img=OFF)
        write_bmp(os.path.join(d, "flip_00061.bmp"), flat(200))
        rc, out = run_check(tmp, gj, d)
        assert rc == 1 and "golden: REGRESSION (1 FAIL, 1 INCOMPLETE)" in out, out


def split_golden(tmp, ref_pace):
    ref = with_rect(flat(50), [0, 0, 32, 16], 255)  # white panel text
    os.makedirs(os.path.join(tmp, "frames"), exist_ok=True)
    G.write_png(os.path.join(tmp, "frames", "f.a.png"), W, H, ref)
    frame = {
        "name": "f",
        "dump": 1,
        "anchor": {"event": "e", "ref_flip": 10},
        "references": [
            {
                "label": "a",
                "sha256": G.pixel_sha((W, H, ref)),
                "size": [W, H],
                "anchor_flip": 10,
                "pace": ref_pace,
            }
        ],
        "split": {
            "panel": {
                "rects": [[0, 0, 32, 16]],
                "max_channel_mae": 1.5,
                "max_bad_fraction": 0.03,
                "max_tile_bad_fraction": 0.25,
            },
            "overlay": {
                "max_spread": 8,
                "min_luma": 200,
                "min_pixels": 100,
                "max_bad_fraction": 0.005,
            },
            "background": {
                "max_channel_mae": 3.0,
                "max_bad_fraction": 0.1,
                "max_tile_bad_fraction": 0.9,
                "min_luma_std": 10,
                "new_view_min_mae": 16,
            },
        },
    }
    return make_golden(tmp, frame, ANCHORS), ref


def test_split_frame_pace_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        gj, ref = split_golden(tmp, 60.0)
        # Background off (panel fine): the split check FAILs; at a mismatched
        # pace that is INCOMPLETE, with the split and whole-frame compares shown.
        rc, out = run_check(tmp, gj, paced_run(tmp, 41, img=with_rect(ref, [16, 32, 16, 16], 150)))
        assert rc == 2 and out.startswith("INCOMPLETE") and "pace mismatch" in out, out
        assert "for information: FAIL" in out and "whole frame vs a" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        gj, ref = split_golden(tmp, 60.0)
        # Another screen behind the same panel: wrong at any pace.
        rc, out = run_check(
            tmp, gj, paced_run(tmp, 41, img=with_rect(flat(200), [0, 0, 32, 16], 255))
        )
        assert rc == 1 and "wrong screen at any pace" in out, out
    with tempfile.TemporaryDirectory() as tmp:
        gj, ref = split_golden(tmp, 60.0)
        rc, out = run_check(tmp, gj, paced_run(tmp, 60, img=with_rect(ref, [16, 32, 16, 16], 150)))
        assert rc == 1 and out.startswith("FAIL"), out  # pace matches: a plain FAIL


def test_record_stores_pace():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp)
        d = paced_run(tmp, 50)
        write_bmp(os.path.join(d, "flip_00061.bmp"), flat(100))  # dump 1
        G.GOLDEN_JSON, G.FRAMES_DIR = gj, os.path.join(tmp, "frames")
        with contextlib.redirect_stdout(io.StringIO()):
            assert G.cmd_record(["s=" + d]) == 0
        with open(gj) as f:
            a = json.load(f)["scenarios"]["s"]["frames"][0]["anchor"]
        assert a["ref_flip"] == 30 and a["ref_pace"] == 50.0, a


def test_reference_stores_pace():
    with tempfile.TemporaryDirectory() as tmp:
        frame = {"name": "f", "dump": 1, "anchor": {"event": "e", "ref_flip": 10}}
        gj = make_golden(tmp, frame, ANCHORS)
        d = paced_run(tmp, 50)
        write_bmp(os.path.join(d, "flip_00061.bmp"), flat(100))
        G.GOLDEN_JSON, G.FRAMES_DIR = gj, os.path.join(tmp, "frames")
        with contextlib.redirect_stdout(io.StringIO()):
            assert G.cmd_reference(["s", "f", "a", d]) == 0
        with open(gj) as f:
            r = json.load(f)["scenarios"]["s"]["frames"][0]["references"][0]
        assert r["anchor_flip"] == 30 and r["pace"] == 50.0, r


def test_pace_of_a_log():
    t = {10: 1000, 70: 2000}
    assert G.pace(t, 10, 70) == 60.0
    assert G.pace(t, 10, 71) is None and G.pace(None, 10, 70) is None


# ---- verdict_window: content on its own clock near the anchored target ---------


def phase_run(tmp, near):
    """Anchor e at flip 30 (target 81, outside limits there); `near` maps
    other flips to images."""
    b = {f: (5 if f >= 30 else 1) for f in range(1, 120)}
    return setup_run(tmp, metal_log(b), {81: OFF, **near})


def test_verdict_window_passes_on_a_near_flip():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, verdict_window={"flips": 8, "why": "art clock"})
        rc, out = run_check(tmp, gj, phase_run(tmp, {87: flat(101)}))
        assert rc == 0 and out.startswith("CLOSE"), out
        assert "verdict_window +-8: flip offset +6 passes (flip_00087.bmp)" in out, out


def test_verdict_window_off_or_too_narrow_fails():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp)
        rc, out = run_check(tmp, gj, phase_run(tmp, {87: flat(101)}))
        assert rc == 1 and out.startswith("FAIL"), out
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, verdict_window=4)
        rc, out = run_check(tmp, gj, phase_run(tmp, {87: flat(101)}))
        assert rc == 1 and out.startswith("FAIL") and "verdict_window" not in out, out


def test_verdict_window_wrong_screen_everywhere_fails():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, verdict_window=8)
        rc, out = run_check(tmp, gj, phase_run(tmp, {f: flat(200) for f in range(73, 90)}))
        assert rc == 1 and out.startswith("FAIL"), out


def test_verdict_window_never_rescues_a_garbage_target():
    with tempfile.TemporaryDirectory() as tmp:
        gj = paced_golden(tmp, verdict_window={"flips": 8, "why": "art clock"})
        d = phase_run(tmp, {87: flat(101)})
        write_bmp(os.path.join(d, "flip_00081.bmp"), flat(0))  # a black present
        rc, out = run_check(tmp, gj, d)
        assert rc == 1 and out.startswith("FAIL") and "verdict_window" not in out, out


def test_anchor_chains_checked_at_load():
    with tempfile.TemporaryDirectory() as tmp:
        frame = paced_frame(tmp)
        for anchors in (
            {"e": {"after": "nope", "min_batches": 5}},
            {"e": {"after": "f", "min_batches": 5}, "f": {"after": "e", "max_batches": 9}},
        ):
            G.GOLDEN_JSON = make_golden(tmp, frame, anchors)
            assert exits(G.load_golden)
        G.GOLDEN_JSON = make_golden(tmp, frame, {"e": {"min_batches": 5}, "f": {"after": "e"}})
        assert G.load_golden()["scenarios"]["s"]["anchors"]["f"]["after"] == "e"


def test_verdict_window_widens_dumps_and_needs():
    from xboxrecomp_cli import golden_run as GR

    fr = {"name": "f", "dump": 1, "anchor": {"event": "e", "ref_flip": 10}}
    sc = {"frames": [fr], "anchors": ANCHORS, "dump_slack": 5}
    assert G.dumpat_ranges(sc) == "54-68"
    fr["verdict_window"] = 9
    assert G.dumpat_ranges(sc) == "47-75"
    b = {f: (5 if f >= 30 else 1) for f in range(1, 60)}
    assert GR.frame_needs(sc, b) == [("f", 75, True)]  # target 81 + 9, capped at the dumps
    b = {f: (5 if f >= 10 else 1) for f in range(1, 60)}
    assert GR.frame_needs(sc, b) == [("f", 70, True)]  # target 61 + 9


def test_pace_judged_against_own_backend():
    # An idle scene that paces 60 fps on D3D11 and 50 on Metal: a run is off
    # only when it is off from its own backend's reference, or, with none of
    # those, from every reference; never for missing the other backend's.
    fr = {
        "name": "f",
        "dump": 1,
        "anchor": {"event": "e", "ref_flip": 10},
        "references": [
            {"label": "metal", "anchor_flip": 10, "pace": 50.0, "sha256": "0", "size": [1, 1]},
            {"label": "d3d11-b", "anchor_flip": 10, "pace": 60.0, "sha256": "0", "size": [1, 1]},
        ],
    }
    sc = {"anchors": ANCHORS}
    batches = {f: (5 if f >= 10 else 1) for f in range(1, 80)}

    def at(fps):
        return {f: f * 1000.0 / fps for f in range(1, 80)}

    def off(fps, backend):
        return G.pace_report(fr, sc, {}, batches, at(fps), backend)[0]

    assert off(50, "metal") is None and off(60, "d3d11") is None
    assert "run 60.0 fps vs ref 50.0" in off(60, "metal")
    assert "run 50.0 fps vs ref 60.0" in off(50, "d3d11")
    # No reference of its own (cpu): off only when off from both.
    assert off(50, "cpu") is None and off(60, "cpu") is None and off(56, None) is None
    assert off(70, "cpu") and off(40, None)
    # A backend's label is the backend or backend-<variant>, not a prefix.
    fr["references"][1]["label"] = "d3d11b"
    assert off(60, "d3d11") is None and off(70, "d3d11")
    fr["references"][1]["label"] = "d3d11-b"
    # An own reference without a pace leaves the others to judge.
    del fr["references"][0]["pace"]
    assert off(60, "metal") is None and off(70, "metal")


def test_dump_slack_before_after():
    # dump_slack [before, after]: a host that only runs early of the
    # reference (a slower host's) dumps that far before, not as far after.
    fr = {"name": "f", "dump": 1, "anchor": {"event": "e", "ref_flip": 10}}
    plain = {"name": "p", "dump": 2}
    sc = {"frames": [fr], "anchors": ANCHORS, "dump_slack": [20, 5]}
    assert G.dump_slack(sc) == (20, 5) and G.dump_slack({}) == (12, 12)
    assert G.dump_slack({"dump_slack": 7}) == (7, 7) and G.dump_slack(sc, 3) == (3, 3)
    assert G.dumpat_ranges(sc) == "39-68"
    sc["frames"].append(plain)
    assert G.dumpat_ranges(sc) == "39-68,119-123"  # an unanchored frame takes none
    from xboxrecomp_cli import golden_run as GR

    b = {f: (5 if f >= 10 else 1) for f in range(1, 60)}
    assert GR.frame_needs({"frames": [fr], "anchors": ANCHORS, "dump_slack": [20, 5]}, b) == [
        ("f", 63, True)
    ]
    for bad in ([1], [1, 2, 3], [-1, 2], ["1", 2]):
        assert exits(lambda bad=bad: G.dump_slack({"dump_slack": bad}))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   " + name)
            except AssertionError as e:
                fails += 1
                print("FAIL " + name + ": " + str(e)[:400])
    sys.exit(1 if fails else 0)


def test_frame_without_reference_is_noref():
    # A frame just added to golden.json, before golden reference records it:
    # check says NOREF (INCOMPLETE, never a pass), refs does not warn, and
    # the run still dumps it so the reference can be recorded.
    with tempfile.TemporaryDirectory() as tmp:
        frame = {"name": "new", "dump": 1}
        gj = make_golden(tmp, frame)
        d = setup_run(tmp, metal_log({f: 1 for f in range(1, 80)}), {61: flat(100)})
        rc, out = run_check(tmp, gj, d)
        assert rc == 2 and "NOREF    s/new" in out and "golden: INCOMPLETE" in out, out
        assert G.frame_refs(frame)[0][1] is None
        assert "no reference yet" in G.missing_ref_hint(frame)
        G.GOLDEN_JSON, repo, G.REPO = gj, G.REPO, tmp
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                G.cmd_refs([])
        finally:
            G.REPO = repo
        assert "WARNING" not in buf.getvalue(), buf.getvalue()
        assert G.dumpat_ranges(G.load_golden()["scenarios"]["s"]) == "59-63"


def test_skipped_frames_are_not_planned_on_their_backend():
    # A frame skipped on d3d11 is neither dumped nor pulled nor listed for
    # a d3d11 run, and still is for every other backend.
    sc = {
        "frames": [
            {"name": "a", "dump": 1},
            {"name": "b", "dump": 2, "skip_backends": ["d3d11"]},
        ]
    }
    assert G.dumpat_ranges(sc) == "59-63,119-123"
    assert G.dumpat_ranges(sc, backend="metal") == "59-63,119-123"
    assert G.dumpat_ranges(sc, backend="d3d11") == "59-63"
    assert [fr["name"] for fr in G.backend_frames(sc, "d3d11")] == ["a"]
    # The scenario's list applies to frames without their own.
    sc2 = {"skip_backends": ["cpu"], "frames": [{"name": "a", "dump": 1}]}
    assert G.dumpat_ranges(sc2, backend="cpu") == ""
    with tempfile.TemporaryDirectory() as tmp:
        G.GOLDEN_JSON = os.path.join(tmp, "golden.json")
        with open(G.GOLDEN_JSON, "w") as f:
            json.dump({"compare": {}, "scenarios": {"s": dict(sc, seconds=1)}}, f)
        log = os.path.join(tmp, "log")
        with open(log, "w") as f:
            f.write("\n".join(metal_log({n: 1 for n in range(1, 130)})) + "\n")

        def out(fn, args):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fn(args)
            return buf.getvalue().split()

        assert out(G.cmd_dumpat, ["s", "--backend", "d3d11"]) == ["59-63"]
        assert out(G.cmd_frames, ["s"]) == ["0001", "0002"]
        assert out(G.cmd_frames, ["s", "--backend", "d3d11"]) == ["0001"]
        pulls = out(G.cmd_pulls, ["s", log, "--backend", "d3d11"])
        assert pulls == ["flip_%05d.bmp" % n for n in range(59, 64)]
        assert len(out(G.cmd_pulls, ["s", log])) == 10


def test_pad_step_count_cross_checked():
    # The runtime's "PAD: script of N step(s)" must equal the CLI's parse of
    # the scenario's script: an older toolkit that stops at the first f-step
    # (0 steps) would idle through the route and never match.
    with tempfile.TemporaryDirectory() as tmp:
        img = flat(100)
        sha = ref_png(tmp, "f", img)
        frame = {"name": "f", "dump": 1, "sha256": sha, "size": [W, H]}
        env = {"env": {"RECOMP_DEBUG": "pad_script=f1:a,f5:b:3"}}
        gj = make_golden(tmp, frame, extra=env)
        flips = metal_log({f: 1 for f in range(1, 80)})
        for line, rc, word in (
            ("  PAD: script of 2 step(s)", 0, "golden: pass"),
            ("  PAD: script of 0 step(s)", 1, "the runtime loaded 0 pad script step(s)"),
            (None, 2, "no 'PAD: script of N step(s)'"),
        ):
            d = setup_run(tmp, ([line] if line else []) + flips, {61: img})
            got, out = run_check(tmp, gj, d)
            assert got == rc and word in out, out
        # An @file script is counted from the file under the game root.
        os.makedirs(os.path.join(tmp, "fx"))
        with open(os.path.join(tmp, "fx", "r.pad"), "w") as f:
            f.write("# rec\nf1:a\nf2:b\nf3:lx=10:4\n")
        assert G.pad_steps_expected({"RECOMP_DEBUG": "pad_script=@fx/r.pad"}, tmp) == 3
        assert G.pad_steps_expected({"RECOMP_PAD_SCRIPT": "@fx/r.pad"}, tmp) == 3
        assert G.pad_steps_expected({"RECOMP_PAD_SCRIPT": "@fx/gone.pad"}, tmp) is None
        assert G.pad_steps_expected({"RECOMP_DEBUG": "pad_script=@seed:x/r.pad"}, tmp) is None
        assert G.pad_steps_expected({}, tmp) is None
