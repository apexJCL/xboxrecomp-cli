"""hud-map: a game's HUD table for the toolkit's HUD layer (design A15).

  capture  SCENARIO     a run at 4:3 that records the HUD's sprites
  propose  CAPTURE...   a table proposal from captures (kinds, groups)
  review   PROPOSAL     a contact sheet: boxes by kind, a wide preview
  emit     [PROPOSAL]   the proposal into the game's table, checked
  check    [SCENARIO]   mod on and off at each ratio; a sheet per scenario

Frames only: captures, sheets and frame dumps stay on this machine (under
dist/ by default); the table they lead to holds addresses, names and kinds,
never pixels. The table's schema is the layer parser's (hud_anchor.c);
emit --check reports what it would refuse.
"""

import os
import sys
import tomllib

from ..host import CliError

VERBS = ("capture", "propose", "review", "emit", "check")


def _run_opts(p):
    p.add_argument("--exe", default="", help="the game's executable (default the macOS build)")
    p.add_argument(
        "--secs", type=int, default=0, help="stop the run after SECS (default 2x the scenario's)"
    )
    p.add_argument(
        "--lock-wait", type=int, default=3600, help="seconds to wait for the Mac run lock"
    )


def configure(p):
    sub = p.add_subparsers(dest="action", metavar="action")
    sub.required = True
    c = sub.add_parser("capture", description="a run at 4:3 that records the HUD's sprites")
    c.add_argument("scenario", help="a golden.json scenario, or @preset (an input preset)")
    c.add_argument(
        "--level",
        type=int,
        choices=(0, 1),
        default=1,
        help="1: through the game's HUD mod ([hud] mod); 0: the table's [capture] draws, mod off",
    )
    c.add_argument("--frames", default="600:120", help="FROM:COUNT guest frames (default 600:120)")
    c.add_argument("--out", default="", help="the capture dir (default dist/hud-map/SCENARIO)")
    _run_opts(c)
    pr = sub.add_parser("propose", description="a table proposal from captures")
    pr.add_argument("captures", nargs="+", help="capture dirs or capture.jsonl files")
    pr.add_argument("--out", default="", help="the proposal (default CAPTURE/proposal.toml)")
    rv = sub.add_parser("review", description="a contact sheet for a proposal")
    rv.add_argument("proposal")
    rv.add_argument("--ratio", default="16:9", help="the wide preview's ratio (default 16:9)")
    rv.add_argument(
        "--capture",
        action="append",
        default=[],
        help="a capture to draw (repeatable; default the proposal's Captures line)",
    )
    rv.add_argument("--out", default="", help="the sheet (default CAPTURE/review-WxH.html)")
    e = sub.add_parser("emit", description="a proposal into the game's table, checked")
    e.add_argument("proposal", nargs="?", default="", help="the proposal (none: check the table)")
    e.add_argument("--check", action="store_true", help="check only, write nothing")
    e.add_argument(
        "--accept-proposed",
        action="store_true",
        help="take rows still marked proposed = true",
    )
    e.add_argument(
        "--no-analysis",
        action="store_true",
        help="write without disasm/ (the row test skipped)",
    )
    k = sub.add_parser("check", description="mod on and off at each ratio, with a sheet")
    k.add_argument("scenario", nargs="*", help="scenarios (default [hud] scenarios)")
    k.add_argument("--ratios", default="16:9,16:10,21:9", help="ratios (default 16:9,16:10,21:9)")
    k.add_argument("--at", default="", help="host flips to dump, as fb_dump_at takes them")
    k.add_argument("--out", default="", help="the check dir (default dist/hud-map/check/STAMP)")
    _run_opts(k)


def _table_doc(G):
    from .runs import table_path

    path = table_path(G)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CliError("%s: %s" % (G.rel(path), e)) from e


def _analysis(G):
    from .analysis import Analysis

    try:
        return Analysis.of(G)
    except CliError as e:
        print("hud-map: %s; without the analysis" % e, file=sys.stderr)
        return None


def _propose(G, a):
    from . import capture as C
    from . import propose as P

    caps = C.load(a.captures)
    doc, notes = P.propose(caps, _table_doc(G), _analysis(G))
    out = a.out or P.default_out(a.captures[0])
    P.write(doc, notes, out, [os.path.abspath(c) for c in a.captures])
    print(
        "hud-map propose: %s (%d sites, %d groups)"
        % (out, len(doc.get("site", [])), len(doc.get("group", [])))
    )
    for n in notes:
        print("  " + n)
    return 0


def _review(G, a):
    from . import review as R

    out, n = R.review(a.proposal, R.ratio(a.ratio), a.capture or None, a.out or None)
    print("hud-map review: %s (%d frames)" % (out, n))
    return 0


def _emit(G, a):
    from . import emit as E
    from .runs import table_path

    if a.accept_proposed and not a.proposal:
        raise CliError("--accept-proposed goes with a PROPOSAL")
    path = table_path(G)
    an = _analysis(G)
    rc, msgs, report = E.emit(
        path,
        a.proposal or None,
        E.Hooked.of(G),
        an,
        accept=a.accept_proposed,
        dry=a.check,
        no_analysis=a.no_analysis,
    )
    for kind, rid, what in report:
        print("  %s %s %s" % (kind, "0x%08X" % rid if isinstance(rid, int) else rid, what))
    for m in msgs:
        print("%s: %s" % (G.rel(path), m), file=sys.stderr)
    if an is None and rc == 0:
        print("hud-map emit: no analysis: the row test was skipped (run analyze)", file=sys.stderr)
    if rc == 0:
        verb = "checked" if a.check or not a.proposal else "written" if report else "unchanged"
        print("hud-map emit: %s %s" % (G.rel(path), verb))
    return rc


def run(a):
    from .. import host
    from . import runs

    G = host.g()
    if a.action == "capture":
        return runs.do_capture(G, a)
    if a.action == "propose":
        return _propose(G, a)
    if a.action == "review":
        return _review(G, a)
    if a.action == "emit":
        return _emit(G, a)
    return runs.do_check(G, a)
