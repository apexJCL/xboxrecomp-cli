"""The bench's flip count: run_game.sh's flip_count() on game logs, and
checks.report_flips / check_run_end on the run-info line it writes. The
log lines are real ones from Deck runs under Proton: a BLiNX 2 golden run
with the flip trace (D3D11) and plain runs without it, where the count
used to read 0 although the game presented thousands of frames.

  uv run pytest tests/test_bench_flips.py
"""

import os
import re
import shutil
import subprocess
import sys

import pytest

from xboxrecomp_cli.bench import checks
from xboxrecomp_cli.bench.remote import host_text

# A golden run (RECOMP_TRACE=flip): the backend's line per present, the
# walker's, and the every-300 summary.
TRACED = (
    "[D3D11] flip 1 batches 0 prog 0 tex 0 rtt 0 (self 0) skipped 0 culled 0 clears 1 uploads 0"
    " (mip 0) changed 0 rehash 0 vb 0/0 bytes occ 1 (visible 0, pending 0, late 0, superseded 0,"
    " settled 0 (stale 0, first 0), sync 0 us) present 0x00104000 walker 0x00104000\n"
    "[GPU] flip 1 0 ms fence 0x0000000D batches 0 tex 0 presents 0x00104000 640x480 raster 0.000"
    " ms (xf 0.000 setup 0.000 texdec 0.000 px 0.000) yuv 0 wr 0 zrej 0 tris 0 drop 0 clip 0\n"
    "[d3d11] present #1: surface 0x00104000 (walker said 0x00104000), 1 clears, 0 draws\n"
    "[D3D11] flip 2 batches 2 prog 2 tex 2 rtt 0 (self 0) skipped 0 culled 0 clears 1 uploads 1"
    " (mip 1) changed 0 rehash 0 vb 216/128 bytes occ 0 (visible 0, pending 0, late 0,"
    " superseded 0, settled 0 (stale 0, first 0), sync 0 us) present 0x00104000 walker 0x00104000\n"
    "[PB-PERF] flip 2 wall 33.50 ms walk 8.04 ms (draw 3.62, decode 3.85) methods 72052\n"
)

# A plain bench run (no flip trace; SSX 3 on the Deck, 90 s): only the
# walker's first-flips detail and the every-300 summary, the last at #4800.
PLAIN = (
    "  [GPU] flip 1: read=0 write=0 presents 0x00204000 (-> 0x80204000)\n"
    "  [GPU] flip 2: read=1 write=1 presents 0x00204000 (-> 0x80204000)\n"
    "[d3d11] present #1: surface 0x00204000 (walker said 0x00204000), 1 clears, 0 draws,"
    " 0 skipped, 0 culled, 0 texture uploads, 0 rehashes, 0 combiner shaders, 2 zclears\n"
    "[d3d11] present #300: surface 0x00330000 (walker said 0x00330000), 300 clears, 327 draws\n"
    "  [NV2A] vblank 4800: last 600 in 10001 ms, gap 14.5-18.8 ms, ack give-ups 0, late acks 0\n"
    "[d3d11] present #4500: surface 0x00330000 (walker said 0x00330000), 4500 clears\n"
    "[d3d11] present #4800: surface 0x00330000 (walker said 0x00330000), 4800 clears,"
    " 4826 draws\n"
    "[d3d11] blend: 0 draws with a constant factor, 2 SET_BLEND_COLOR writes (last 0x00000000)\n"
)

# No D3D11 present line: Metal's "[metal] present #N" must not count, since
# bench runs are D3D11 under Proton.
NONE = "[BOOT] xbe loaded\n[metal] present #1: surface 0x00104000 (drawn here), 1 clears\n"

FLOOR = "4800 (at least: no flip trace, the last [d3d11] present #N line; logged every 300)"


def flip_count_func():
    m = re.search(r"^flip_count\(\) \{\n.*?\n\}\n", host_text("run_game.sh"), re.S | re.M)
    assert m, "run_game.sh has no flip_count()"
    return m.group(0)


needs_bash = pytest.mark.skipif(
    not shutil.which("bash") or sys.platform == "win32", reason="run_game.sh is bash"
)


@needs_bash
@pytest.mark.parametrize(
    "text, want",
    [(TRACED, "2"), (PLAIN, FLOOR), (NONE, "0"), ("", "0"), (PLAIN + TRACED, "2")],
    ids=["traced", "plain", "no-d3d11", "empty", "traced-wins"],
)
def test_flip_count(tmp_path, text, want):
    log = tmp_path / "game-stdio.log"
    log.write_text(text)
    script = "set -euo pipefail\n" + flip_count_func() + 'flip_count "$1"\n'
    r = subprocess.run(["bash", "-c", script, "_", str(log)], capture_output=True, text=True)
    assert (r.returncode, r.stdout) == (0, want + "\n"), r.stderr


def run_dir(tmp_path, flips, limit="90"):
    d = tmp_path / "run"
    d.mkdir()
    (d / "exit-code").write_text("124\n")
    (d / "game-stdio.log").write_text(PLAIN)
    (d / "run-info.txt").write_text(
        "env:    RECOMP_PB_BACKEND=d3d11\nargs:   (none)\nlimit:  %s s (SIGINT)\n"
        "flips:  %s\nload:   1.0 1.0 1.0 at start, 2.0 2.0 2.0 at end (8 cpus)\nbusy:   no\n"
        % (limit, flips)
    )
    return str(d)


def said(fn, *args):
    got = []
    rc = fn(*args, out=got.append)
    return got, rc


def test_report_flips(tmp_path):
    log = run_dir(tmp_path, "2700")
    assert said(checks.report_flips, log)[0] == ["end: 2700 flips (30.0/s)"]


def test_report_flips_floor(tmp_path):
    log = run_dir(tmp_path, FLOOR)
    assert said(checks.report_flips, log)[0] == [
        "end: at least 4800 flips (53.3/s; no flip trace, set RECOMP_TRACE=flip for the exact count)"
    ]


@pytest.mark.parametrize("flips", ["0", "? (no game-stdio.log)"])
def test_report_flips_silent(tmp_path, flips):
    assert said(checks.report_flips, run_dir(tmp_path, flips))[0] == []


def test_report_flips_no_limit(tmp_path):
    log = run_dir(tmp_path, "2700")
    info = os.path.join(log, "run-info.txt")
    with open(info) as f:
        text = f.read().replace("limit:  90 s (SIGINT)", "limit:  none")
    with open(info, "w") as f:
        f.write(text)
    assert said(checks.report_flips, log)[0] == []


def test_floor_under_min_flips_is_inconclusive(tmp_path):
    # A floor up to 299 short cannot fail a run: say so, never "perf regression".
    log = run_dir(tmp_path, FLOOR)
    got, rc = said(checks.check_run_end, log, "4900")
    assert rc == 3
    assert got == [
        "end: ran to the 90 s limit",
        "end: INCONCLUSIVE at least 4800 flips, under the floor 4900 (53.3/s): no flip trace, "
        "so the count is a floor; set RECOMP_TRACE=flip",
    ]
    got, rc = said(checks.check_run_end, log, "5099")
    assert rc == 3, got
    got, rc = said(checks.check_run_end, log, "4700")
    assert rc == 0 and got[-1] == "end: at least 4800 flips (53.3/s; floor 4700)"


def test_floor_far_under_min_flips_fails(tmp_path):
    # Even N+299 is under the minimum: a sure miss, judged like an exact count.
    log = run_dir(tmp_path, FLOOR)
    got, rc = said(checks.check_run_end, log, "5100")
    assert rc == 1
    assert got[-1] == (
        "end: FAIL at least 4800 flips under the floor 5100 (53.3/s) on an idle host: "
        "perf regression? load 1.0 1.0 1.0 at start, 2.0 2.0 2.0 at end (8 cpus)"
    )
