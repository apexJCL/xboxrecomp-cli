"""Known divergence (golden check --host-os): a frame marked for one host is
KNOWN there, never a pass, and a marked frame that matches again is named
so the marking is dropped. Synthetic images and logs in temp dirs.

  uv run pytest tests/test_golden_known.py
"""

import json
import os

import pytest
from test_golden import H, W, flat, make_golden, ref_png, with_rect
from test_golden_prune import ANCHOR, CLOSE, check, frame_def, scenario_run

from xboxrecomp_cli import golden as G

KNOWN = [{"host": "windows", "item": "TRACK-42", "why": "menu dump timing"}]
# A sixteenth of the frame off: outside the limits (bad 1%) but far under
# pace_fail_bad, the kind of failure a known divergence may cover.
OFF = with_rect(flat(100), [0, 0, 16, 16], 200)


def golden(tmp):
    return make_golden(tmp, frame_def(ref_png(tmp, "f", flat(100)), known_divergence=KNOWN), ANCHOR)


def test_known_on_its_host_is_never_a_pass(tmp_path):
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, OFF)
    rc, out = check(tmp, gj, d, "--host-os", "windows")
    assert rc == 0, out
    assert "\nKNOWN    s/f" in "\n" + out and "TRACK-42" in out, out
    assert "golden: PASS-with-known (1 KNOWN)" in out and "golden: pass" not in out, out
    assert "golden: KNOWN s/f: TRACK-42" in out, out


def test_other_hosts_ignore_the_marking(tmp_path):
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, OFF)
    for extra in ([], ["--host-os", "posix"]):
        rc, out = check(tmp, gj, d, *extra)
        assert rc == 1 and "FAIL" in out and "KNOWN" not in out, out
        assert "golden: REGRESSION (1 FAIL)" in out, out


def test_known_frame_that_matches_again_is_reported(tmp_path):
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, CLOSE)
    rc, out = check(tmp, gj, d, "--host-os", "windows")
    # Still not counted as a pass: the verdict changes only once the
    # marking is removed, but the stale marking is named.
    assert rc == 0, out
    assert "KNOWN-NOW-PASSES s/f" in out and "was CLOSE" in out, out
    assert "golden: KNOWN-NOW-PASSES s/f" in out and "drop its" in out, out
    assert "golden: PASS-with-known (1 KNOWN)" in out, out


def test_a_fail_elsewhere_still_wins(tmp_path):
    tmp = str(tmp_path)
    f = frame_def(ref_png(tmp, "f", flat(100)), known_divergence=KNOWN)
    g = dict(frame_def(ref_png(tmp, "g", flat(100))), name="g")
    gj = make_golden(tmp, f, ANCHOR, {"frames": [f, g]})
    d = scenario_run(tmp, OFF)
    rc, out = check(tmp, gj, d, "--host-os", "windows")
    assert rc == 1, out
    assert "golden: REGRESSION (1 FAIL, 1 KNOWN)" in out and "golden: KNOWN s/f" in out, out


def test_incomplete_marked_frame_stays_incomplete(tmp_path):
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, OFF)
    os.remove(os.path.join(d, "flip_00081.bmp"))
    rc, out = check(tmp, gj, d, "--host-os", "windows")
    assert rc == 2 and "marked as known on windows: TRACK-42" in out, out
    assert "golden: INCOMPLETE (1 INCOMPLETE)" in out, out


def test_gross_failures_stay_fail_on_the_marked_host(tmp_path):
    # A wrong screen (every pixel off) is a new regression, not the
    # divergence the item describes.
    tmp = str(tmp_path)
    gj = golden(tmp)
    rc, out = check(tmp, gj, scenario_run(tmp, flat(200)), "--host-os", "windows")
    assert rc == 1 and "not an ordinary outside-limits compare" in out, out
    assert "\nKNOWN " not in "\n" + out and "golden: REGRESSION (1 FAIL)" in out, out
    # So does a missing reference.
    os.remove(os.path.join(tmp, "frames", "f.png"))
    rc, out = check(tmp, gj, scenario_run(tmp, OFF), "--host-os", "windows")
    assert rc == 1 and "no reference PNG" in out and "golden: REGRESSION (1 FAIL)" in out, out
    # And a reference of another size.
    gj = golden(tmp)
    G.write_png(os.path.join(tmp, "frames", "f.png"), W // 2, H, flat(100)[: W // 2 * H * 3])
    rc, out = check(tmp, gj, scenario_run(tmp, OFF), "--host-os", "windows")
    assert rc == 1 and "differs from the reference" in out, out
    assert "\nKNOWN " not in "\n" + out and "golden: REGRESSION (1 FAIL)" in out, out


def test_a_missing_second_reference_is_not_known(tmp_path):
    # With every reference present the marked frame is KNOWN; with one
    # PNG gone the compare is not ordinary, even though the other
    # reference compared normally.
    tmp = str(tmp_path)
    refs = [
        {"label": "a", "sha256": ref_png(tmp, "f.a", flat(100))},
        {"label": "b", "sha256": ref_png(tmp, "f.b", flat(150))},
    ]
    gj = make_golden(
        tmp, frame_def(refs[0]["sha256"], references=refs, known_divergence=KNOWN), ANCHOR
    )
    rc, out = check(tmp, gj, scenario_run(tmp, OFF), "--host-os", "windows")
    assert rc == 0 and "\nKNOWN    s/f" in "\n" + out, out
    os.remove(os.path.join(tmp, "frames", "f.b.png"))
    rc, out = check(tmp, gj, scenario_run(tmp, OFF), "--host-os", "windows")
    assert rc == 1 and "no reference PNG" in out and "not an ordinary" in out, out
    assert "golden: REGRESSION (1 FAIL)" in out, out


def test_used_gives_a_known_scenario_its_own_verdict(tmp_path):
    # 3, not 0: no prune removes the frames the tracker item reads.
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, OFF)
    used = os.path.join(tmp, "used.tsv")
    rc, out = check(tmp, gj, d, "--host-os", "windows", "--used", used)
    assert rc == 0, out
    assert G.read_used(used)["s"][0] == 3
    rc, out = check(tmp, gj, d, "--used", used)
    assert rc == 1 and G.read_used(used)["s"][0] == 1, out


def test_known_header_replaces_the_verdict_line_not_a_warning():
    out = (
        "WARNING  s/f: no reference PNG at /r/b.png\n"
        "         hint\n"
        "FAIL     s/f (dump 1, present 61) vs a: mae 9\n"
        "         tile 1,2\n"
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = G.report_known("s/f", out, 1, KNOWN[0], "windows", ordinary=True)
    text = buf.getvalue()
    assert r == ("known", "s/f", "TRACK-42", False)
    assert text.startswith("KNOWN    s/f (dump 1, present 61) vs a: mae 9\n"), text
    assert "WARNING  s/f: no reference PNG" in text and "FAIL" not in text, text


def test_misspelt_hosts_are_refused(tmp_path):
    tmp = str(tmp_path)
    gj = golden(tmp)
    d = scenario_run(tmp, OFF)
    with pytest.raises(SystemExit):
        check(tmp, gj, d, "--host-os", "win")
    with open(gj) as f:
        g = json.load(f)
    g["scenarios"]["s"]["frames"][0]["known_divergence"][0]["host"] = "Windows"
    with open(gj, "w") as f:
        json.dump(g, f)
    # On every host, the Deck's too: the marking would silently never match.
    with pytest.raises(SystemExit):
        check(tmp, gj, d)
