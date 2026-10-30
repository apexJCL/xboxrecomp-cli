"""hud-map: the table's checks (the layer parser's), the merge, the propose
rules, emit's row test, review, the runs (mocked) and check's sheet. Every
capture here is synthetic; no game runs."""

import json
import os
import tomllib

import pytest

from xboxrecomp_cli import main, manifest
from xboxrecomp_cli.host import CliError
from xboxrecomp_cli.hudmap import analysis as A
from xboxrecomp_cli.hudmap import capture as C
from xboxrecomp_cli.hudmap import emit as E
from xboxrecomp_cli.hudmap import propose as P
from xboxrecomp_cli.hudmap import review as R
from xboxrecomp_cli.hudmap import runs as RUN
from xboxrecomp_cli.hudmap import sheet as S
from xboxrecomp_cli.hudmap import table as T

CANVAS = {"w": 640, "h": 480, "origin": "centre", "y": "down", "target": [640, 480]}
LAYOUT = {"stride": 24, "x_off": 0, "prim": 4}


def table(**over):
    doc = {
        "version": 1,
        "canvas": dict(CANVAS),
        "layout": dict(LAYOUT),
        "group": [{"name": "life", "kind": "left"}],
        "site": [
            {"ret_va": 0x12005, "element": "heart", "group": "life"},
            {"ret_va": 0x12105, "element": "clock", "kind": "right"},
        ],
    }
    doc.update(over)
    return doc


# ── table.validate: one case per parser rule ─────────────────────────────


def test_valid_table():
    assert T.validate(table()) == []


@pytest.mark.parametrize(
    "change, err",
    [
        (lambda d: d.update(version=2), "version must be 1"),
        (lambda d: d.update(version="1"), "version must be 1"),
        (lambda d: d["canvas"].update(bogus=1), "unknown key canvas.bogus"),
        (lambda d: d["site"][0].update(colour=1), "unknown key site.0.colour"),
        (lambda d: d["canvas"].update(source="x"), "canvas.source=x"),
        (lambda d: d["canvas"].update(w=0), "w and h must be numbers"),
        (lambda d: d["canvas"].update(w=5000), "w and h must be numbers"),
        (lambda d: d["canvas"].update(origin="middle"), "canvas.origin"),
        (lambda d: d["canvas"].update(y="left"), "canvas.y"),
        (lambda d: d["canvas"].update(view=[0, 0, 640]), "canvas.view is not"),
        (lambda d: d["canvas"].update(view=[-1, 0, 640, 480]), "canvas.view needs"),
        (lambda d: d["canvas"].update(target="screen"), "canvas.target is not"),
        (lambda d: d["canvas"].update(target=[0, 480]), "canvas.target is not [w, h]"),
        (lambda d: d["canvas"].update(source="memory"), "source memory needs w_va"),
        (lambda d: d["canvas"].update(w_va=0x1000), "need source"),
        (lambda d: d["layout"].update(stride=2), "layout.stride must be 4..256"),
        (lambda d: d["layout"].update(stride=300), "layout.stride must be 4..256"),
        (lambda d: d["layout"].update(x_off=22), "layout.x_off must lie"),
        (lambda d: d["layout"].update(u_off=21), "layout.u_off must lie"),
        (lambda d: d["layout"].update(prim=0), "layout.prim must be 1..64"),
        (lambda d: d["group"][0].update(name=""), "group 0: a name"),
        (lambda d: d["group"][0].update(name="x" * 48), "group 0: a name"),
        (lambda d: d["group"].append({"name": "life", "kind": "left"}), "listed twice"),
        (lambda d: d["group"][0].update(kind="up"), "unknown kind up"),
        (lambda d: d["group"][0].update(park=10), "park is for world groups"),
        (lambda d: d["group"][0].update(kind="world", park=-1), "park is not a positive"),
        (lambda d: d["group"][0].update(ref_x="a"), "ref_x is not a number"),
        (lambda d: d["site"][0].update(ret_va=0), "site 0: ret_va is not"),
        (lambda d: d["site"][0].update(element=""), "an element of"),
        (lambda d: d["site"][0].update(kind="left"), "kind belongs to group life"),
        (lambda d: d["site"][0].update(group="nope"), "unknown group nope"),
        (lambda d: d["site"][1].pop("kind"), "no group, and no kind"),
        (lambda d: d["site"][1].update(kind="up"), "unknown kind up"),
        (lambda d: d["site"].append(dict(d["site"][0])), "listed twice"),
        (lambda d: d.update(wrapper=[{"va": "x"}]), "wrapper 0: va is not"),
    ],
)
def test_parser_rules(change, err):
    doc = table()
    change(doc)
    errs = T.validate(doc)
    assert any(err in e for e in errs), errs


def test_center_spelling_and_target_canvas():
    doc = table(canvas={"source": "target", "origin": "center"})
    doc["site"][1]["kind"] = "center"
    assert T.validate(doc) == []


def test_proposal_keys_only_in_proposals():
    doc = table()
    doc["site"][0].update(proposed=True, evidence={"rule": "x"})
    assert T.validate(doc, proposal=True) == []
    assert any("site.0.proposed" in e for e in T.validate(doc))


def test_draws_spellings():
    doc = {"capture": {"draws": ["sig:Draw", 0x11000, {"fn": "0x11100", "vertex": "esp+4"}, []]}}
    got = [e for e, _ in T.draws(doc)]
    assert got == ["sig:Draw", "0x00011000", "0x00011100", None]


def test_dumps_round_trips():
    doc = table(wrapper=[{"va": 0x13000, "name": "wrap"}])
    back = tomllib.loads(T.dumps(doc, head=["a head"]))
    assert back == doc
    assert "ret_va = 0x00012005" in T.dumps(doc)


# ── merge ────────────────────────────────────────────────────────────────

EXISTING = """\
# BLiNX's HUD table.
version = 1

[canvas]
w = 640
h = 480

[layout]
stride = 24
x_off = 0

# The hearts.
[[site]]
ret_va = 0x00012005
element = "heart"
kind = "left"  # by eye
"""


def test_merge_keeps_comments_and_unchanged_rows():
    text, report = T.merge(
        EXISTING,
        [("site", {"ret_va": 0x12005, "element": "heart", "kind": "left", "proposed": False})],
    )
    assert report == [] and text == EXISTING


def test_merge_changes_and_adds():
    text, report = T.merge(
        EXISTING,
        [
            ("site", {"ret_va": 0x12005, "element": "heart", "kind": "right"}),
            ("group", {"name": "g", "kind": "centre"}),
            ("site", {"ret_va": 0x12105, "element": "clock", "group": "g", "evidence": {}}),
        ],
    )
    assert report == [
        ("site", 0x12005, "changed"),
        ("group", "g", "added"),
        ("site", 0x12105, "added"),
    ]
    assert "# BLiNX's HUD table." in text and "# The hearts." in text
    doc = tomllib.loads(text)
    assert [s["ret_va"] for s in doc["site"]] == [0x12005, 0x12105]
    assert doc["site"][0]["kind"] == "right"
    assert "evidence" not in doc["site"][1]
    # The group lands before the sites, as the order of kinds has it.
    assert text.index("[[group]]") < text.index("# The hearts.")


def test_merge_into_nothing():
    text, _ = T.merge("", [("canvas", CANVAS), ("layout", LAYOUT)])
    assert tomllib.loads(text) == {"version": 1, "canvas": CANVAS, "layout": LAYOUT}


# ── capture ──────────────────────────────────────────────────────────────


def write_capture(d, sprites, frames=(0, 1, 2), canvas=CANVAS):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "capture.jsonl"), "w") as f:
        # As the toolkit writes them: a frame's sprites, then its frame line.
        for fr in frames:
            for s in sprites:
                if s["frame"] == fr:
                    f.write(json.dumps(s) + "\n")
            line = {"type": "frame", "frame": fr, "flip": fr * 2, "canvas": canvas}
            line["bmp"] = "frame_%05d.bmp" % fr
            f.write(json.dumps(line) + "\n")
    return d


def sprite(frame, ret, box, chain=(), **kw):
    d = {
        "type": "sprite",
        "level": 1,
        "frame": frame,
        "queue": 0,
        "chain": ["0x%08X" % c for c in chain],
        "ret": "0x%08X" % ret,
        "site": None,
        "element": None,
        "group": None,
        "n": 4,
        "bbox": list(box),
        "prims": 1,
        "tex": None,
    }
    d.update(kw)
    return d


TOOLKIT_LINES = """\
{"type":"sprite","level":1,"frame":2,"queue":7,"chain":["0x00012340"],"ret":"0x00045678","site":"0x00012340","element":"score","group":"score","n":4,"bbox":[300,10,340,30],"prims":1,"tex":"0x00000000DEADBEEF"}
{"type":"sprite","level":0,"frame":2,"queue":null,"draw":"0x0004A3C0","name":"draw_quad","chain":["0x00012900","0x00012010"],"ret":"0x00012F00","site":null,"element":null,"group":null,"n":4,"bbox":null,"prims":1,"tex":null}
{"type":"sprite","level":1,"frame":2,"queue":7,"chain":["0x00012340"],"ret":"0x00045678","site":null,"element":null,"group":null,"n":4,"bbox":[1,2,3,4],"prims":1,"tex":null}
{"type":"frame","frame":2,"flip":31,"canvas":{"w":640,"h":480,"origin":"top-left","y":"down","view":[0,0,640,480],"target":[640,480],"pad":0},"bmp":"frame_00002.bmp"}
{"type":"frame","frame":3,"flip":null,"canvas":{"w":640,"h":480,"origin":"top-left","y":"down","view":[0,0,640,480],"target":[640,480],"pad":0},"bmp":null}
"""


def test_capture_reads_the_toolkit_format(d):
    open(os.path.join(d, "capture.jsonl"), "w").write(TOOLKIT_LINES)
    cap = C.Capture(d)
    one, zero, unlisted = cap.sprites
    assert (one.level, one.queue, one.element, one.tex) == (1, 7, "score", "0x00000000DEADBEEF")
    assert (zero.draw, zero.name, zero.bbox, zero.queue) == (0x4A3C0, "draw_quad", None, None)
    # Named by the site, else the outermost wrapper (level 1), else the
    # draw's own return (a level-0 chain is a stack scan).
    assert [s.name_ret() for s in cap.sprites] == [0x12340, 0x12F00, 0x12340]
    assert cap.frames[2].bmp == os.path.join(d, "frame_00002.bmp")
    assert cap.frames[3].bmp is None and cap.frames[3].flip is None
    assert cap.canvas(2).tw == 640 and not cap.canvas(2).centre


def test_review_draws_a_frame_without_picture(d):
    open(os.path.join(d, "capture.jsonl"), "w").write(TOOLKIT_LINES)
    prop = os.path.join(d, "proposal.toml")
    P.write(table(), [], prop, [d])
    out, n = R.review(prop, (16, 9))
    text = open(out).read()
    assert n == 2 and text.count('href="frame_00002.bmp"') == 2 and 'fill="#222"' in text


def test_canvas_maps_centre_origin_and_view():
    c = C.Canvas({"w": 640, "h": 480, "origin": "centre", "view": [80, 0, 480, 480]})
    assert c.tw == 560 and c.to_px([-320, -240, 320, 240]) == [80, 0, 560, 480]
    assert c.to_units(80) == -320
    up = C.Canvas({"w": 640, "h": 480, "origin": "top-left", "y": "up"})
    assert up.to_px([0, 10, 10, 20]) == [0, -20, 10, -10]


def test_capture_errors(d):
    with pytest.raises(CliError, match="no capture"):
        C.Capture(os.path.join(d, "none"))
    with open(os.path.join(d, "capture.jsonl"), "w") as f:
        f.write("{not json\n" + json.dumps({"type": "frame", "frame": 0}) + "\n")
    with pytest.raises(CliError, match="not JSON"):
        C.Capture(d)


def test_capture_skips_a_partial_last_line(d, capsys):
    path = write_capture(d, [sprite(0, 0x12005, [0, 0, 10, 10])])
    with open(os.path.join(path, "capture.jsonl"), "a") as f:
        f.write('{"type": "sprite", "fra')
    cap = C.Capture(path)
    assert len(cap.sprites) == 1
    assert "partial last line skipped" in capsys.readouterr().err


# ── propose rules ────────────────────────────────────────────────────────


def test_rule_zero_area():
    assert P.zero_area([0, 5, 10, 5]) and P.zero_area(None) and not P.zero_area([0, 0, 1, 1])


def test_rule_spans_is_stretch():
    assert P.kind_rule([(0, [0, 0, 640, 20])], [[0, 0, 640, 20]], 640)[0] == "stretch"


def test_rule_thirds():
    for box, kind in (
        ([10, 0, 50, 9], "left"),
        ([300, 0, 340, 9], "centre"),
        ([600, 0, 630, 9], "right"),
    ):
        assert P.kind_rule([(0, box)], [box], 640)[:1] == (kind,)


def test_rule_slides_in_keeps_on_canvas_third():
    frames = [(0, [-200, 0, -100, 9]), (1, [-50, 0, 50, 9]), (2, [10, 0, 110, 9])]
    kind, rule, _ = P.kind_rule(frames, [b for _, b in frames], 640)
    assert kind == "left" and "slides in" in rule


def test_rule_world_moves_freely():
    frames = [(0, [-40, 10, -20, 30]), (1, [200, 100, 220, 120]), (2, [100, 50, 120, 70])]
    assert P.moves_freely(frames)
    assert P.kind_rule(frames, [b for _, b in frames], 640)[0] == "world"
    # A slide on one axis is not a world marker.
    assert not P.moves_freely([(0, [0, 0, 1, 1]), (1, [10, 0, 11, 1]), (2, [20, 0, 21, 1])])


def test_rule_park_bound():
    boxes = [[300, 0, 340, 9], [-700, 0, -680, 9], [-650, 0, -640, 9]]
    park = P.park_bound(boxes, 640)
    assert 320 < park < 1020
    assert P.park_bound([[300, 0, 340, 9]], 640) is None


def test_rule_vote_ties_go_first():
    assert P.vote(["left", "right"]) == "left" and P.vote(["r", "l", "l"]) == "l"


def test_rule_group_candidates():
    els = {1: [0, 0, 10, 10], 2: [11, 0, 20, 10], 3: [100, 0, 110, 10], 4: [21, 0, 30, 10]}
    funcs = {1: 0x100, 2: 0x100, 3: 0x100, 4: 0x200}
    assert P.group_candidates(els, funcs.get) == [[1, 2]]


class FakeAnalysis:
    def __init__(self, funcs):
        self.funcs = funcs

    def function(self, va):
        for s, e, n in self.funcs:
            if s <= va < e:
                return s, e, n
        return None


def test_propose_builds_rows_groups_and_keeps_listed(d):
    sprites = []
    for f in (0, 1, 2):
        sprites += [
            sprite(f, 0x12005, [-310, -230, -300, -220]),
            sprite(f, 0x12015, [-299, -230, -290, -220]),
            sprite(f, 0x12105, [290, -230, 300, -220]),
            sprite(f, 0x12205, [-10, 0, 10, 0]),  # zero-area
        ]
    cap = C.Capture(write_capture(d, sprites))
    known = table(site=[{"ret_va": 0x12105, "element": "clock", "kind": "centre"}], group=[])
    an = FakeAnalysis([(0x12000, 0x12100, "hud_hearts"), (0x12100, 0x12200, "")])
    doc, notes = P.propose([cap], known, an)
    sites = {s["ret_va"]: s for s in doc["site"]}
    assert set(sites) == {0x12005, 0x12015, 0x12105}
    assert sites[0x12105]["proposed"] is False and sites[0x12105]["kind"] == "centre"
    assert sites[0x12105]["evidence"]["rule_kind"] == "right"
    assert sites[0x12005]["group"] == "hud_hearts" and sites[0x12005]["proposed"]
    assert doc["group"] == [
        {
            "name": "hud_hearts",
            "kind": "left",
            "proposed": True,
            "evidence": {
                "rule": "drawn by one function, boxes touching; its elements' kind",
                "elements": ["e_00012005", "e_00012015"],
            },
        }
    ]
    assert any("0x00012205" in n and "zero-area" in n for n in notes)
    assert T.validate(doc, proposal=True) == []
    out = P.write(doc, notes, os.path.join(d, "proposal.toml"), [d])
    assert tomllib.load(open(out, "rb"))["site"][0]["ret_va"] == 0x12005


def test_propose_parked_world_gets_a_group(d):
    sprites = []
    for f, (x, y) in enumerate([(-340, -100), (0, 50), (-200, 0), (-1000, 10)]):
        sprites.append(sprite(f, 0x15005, [x, y, x + 10, y + 10]))
    sprites.append(sprite(0, 0x15005, [-1100, 0, -1090, 10]))
    cap = C.Capture(write_capture(d, sprites, frames=(0, 1, 2, 3)))
    doc, _ = P.propose([cap])
    (site,) = doc["site"]
    assert "kind" not in site and site["group"] == "e_00015005"
    (g,) = doc["group"]
    assert g["kind"] == "world" and g["park"] > 0
    # No [layout] without the game's table: propose notes it for emit.
    assert T.check_rows(doc) == []


# ── analysis and emit ────────────────────────────────────────────────────


def write_analysis(d):
    dd = os.path.join(d, "disasm")
    os.makedirs(os.path.join(dd, "asm"))
    funcs = [
        {"start": "0x11000", "end": "0x11100", "name": "Draw", "section": ".text"},
        {"start": "0x12000", "end": "0x12100", "name": "hud", "section": ".text"},
        {"start": "0x13000", "end": "0x13100", "name": "wrap", "section": ".text"},
    ]
    xrefs = [
        {"from": "0x12000", "to": "0x11000", "type": "call"},
        {"from": "0x13010", "to": "0x11000", "type": "call"},
        {"from": "0x12100", "to": "0x13000", "type": "call"},
        {"from": "0x12200", "to": "0x11000", "type": "jump"},
    ]
    json.dump(funcs, open(os.path.join(dd, "functions.json"), "w"))
    json.dump(xrefs, open(os.path.join(dd, "xrefs.json"), "w"))
    with open(os.path.join(dd, "asm", "text.asm"), "w") as f:
        f.write("  0x00012000  e8fbefffff   call 0x11000\n")
        f.write("  0x00013010  ff15aabbccdd   call [0xddccbbaa]\n")
        f.write("  0x00012100  e8fb0e0000   call 0x13000\n")
        f.write("  0x00012200  e9fbefffff   jmp 0x11000\n")
    return A.Analysis(dd)


def test_analysis_returns(d):
    an = write_analysis(d)
    assert an.returns_to({0x11000}) == {0x12005: 0x11000, 0x13016: 0x11000}
    assert an.function(0x12005)[2] == "hud" and an.body(0x13000) == (0x13000, 0x13100)
    with pytest.raises(CliError, match="run analyze"):
        A.Analysis(os.path.join(d, "none"))


def emit_doc(**over):
    return table(
        wrapper=[{"va": 0x13000}],
        site=[
            {"ret_va": 0x12005, "element": "heart", "group": "life"},
            {"ret_va": 0x12105, "element": "clock", "kind": "right"},
        ],
        **over,
    )


def test_emit_row_test_passes(d):
    an = write_analysis(d)
    assert E.check(emit_doc(), E.Hooked(["0x11000", "0x13000"]), an) == []


def test_emit_site_must_follow_a_hooked_call(d):
    an = write_analysis(d)
    doc = emit_doc()
    doc["site"][1]["ret_va"] = 0x12107
    errs = E.check(doc, E.Hooked(["0x11000", "0x13000"]), an)
    assert errs == ["site 0x00012107: does not follow a call to a hooked function or a wrapper"]


def test_emit_site_inside_a_wrapper(d):
    an = write_analysis(d)
    doc = emit_doc()
    doc["site"][0]["ret_va"] = 0x13016
    errs = E.check(doc, E.Hooked(["0x11000", "0x13000"]), an)
    assert errs == ["site 0x00013016: inside wrapper 0x00013000's body (name its callers instead)"]


def test_emit_wrapper_and_draws_must_be_hooked(d):
    an = write_analysis(d)
    doc = emit_doc(capture={"draws": ["sig:Other", "0x11000"]})
    errs = E.check(doc, E.Hooked(["0x11000"]), an)
    assert "wrapper 0x00013000: not in [mods] hook_functions (no hook slot)" in errs
    assert "capture.draws sig:Other: not in [mods] hook_functions" in errs
    # A sig: entry resolved by analyze counts by its VA too.
    hooked = E.Hooked(["sig:Draw"], [{"entry": "sig:Draw", "va": "0x00011000"}])
    assert hooked.has("0x11000") and hooked.has("sig:Draw")


def test_emit_writes_refuses_and_checks(d):
    an = write_analysis(d)
    hooked = E.Hooked(["0x11000", "0x13000"])
    tp = os.path.join(d, "config", "hud.toml")
    prop = os.path.join(d, "proposal.toml")
    doc = emit_doc()
    doc["site"][1].update(proposed=True, evidence={"rule": "x"})
    open(prop, "w").write(T.dumps(doc))
    rc, msgs, _ = E.emit(tp, prop, hooked, an)
    assert rc == 1 and "still proposed" in msgs[0] and msgs[1] == "  site 0x00012105"
    assert not os.path.exists(tp)
    rc, msgs, report = E.emit(tp, prop, hooked, an, dry=True, accept=True)
    assert (rc, msgs) == (0, []) and len(report) == 6 and not os.path.exists(tp)
    rc, _, _ = E.emit(tp, prop, hooked, an, accept=True)
    written = tomllib.load(open(tp, "rb"))
    assert rc == 0 and "evidence" not in written["site"][1]
    # Once in the table, a proposed row that changes nothing passes.
    assert E.emit(tp, prop, hooked, an)[0] == 0
    assert E.emit(tp, None, hooked, an) == (0, [], [])
    # A table the parser refuses fails the check.
    open(tp, "a").write('\n[[site]]\nret_va = 0x00012005\nelement = "again"\nkind = "left"\n')
    rc, msgs, _ = E.emit(tp, None, hooked, an)
    assert rc == 1 and "site 0x00012005: listed twice" in msgs


def test_emit_refuses_unknown_proposal_keys(d):
    prop = os.path.join(d, "p.toml")
    open(prop, "w").write("version = 1\n[[site]]\nret_va = 1\ncolour = 2\n")
    rc, msgs, _ = E.emit(os.path.join(d, "t.toml"), prop, E.Hooked([]), None)
    assert rc == 1 and msgs[0].endswith("unknown key site.0.colour")


# ── review ───────────────────────────────────────────────────────────────


def test_review_rules():
    assert R.ratio("16:9") == (16, 9) and R.pad_of(640, 480, (16, 9)) == 107
    assert R.pad_of(640, 480, (4, 3)) == 0
    assert R.dx_of("left", [0, 0, 1, 1], 640, 100) == -100
    assert R.dx_of("right", [0, 0, 1, 1], 640, 100) == 100
    assert R.dx_of("proportional", [470, 0, 490, 1], 640, 100) == 50
    assert R.dx_of("centre", [0, 0, 1, 1], 640, 100) == 0
    assert R.stretch_x(0, 640, 100) == -100 and R.stretch_x(640, 640, 100) == 740
    with pytest.raises(CliError):
        R.ratio("wide")


def test_review_sheet_links_frames(d):
    cap_dir = write_capture(
        os.path.join(d, "cap"),
        [sprite(0, 0x12005, [-310, -230, -300, -220]), sprite(1, 0x19005, [0, 0, 5, 5])],
    )
    prop = os.path.join(cap_dir, "proposal.toml")
    P.write(table(), [], prop, [cap_dir])
    assert R.sources(prop) == [cap_dir]
    out, n = R.review(prop, (16, 9))
    assert n == 3 and out == os.path.join(cap_dir, "review-16x9.html")
    text = open(out).read()
    assert 'href="frame_00000.bmp"' in text and "heart life 0x00012005" in text
    assert "unlisted - 0x00019005" in text and "data:" not in text


# ── runs (mocked) ────────────────────────────────────────────────────────


class FakeProc:
    def __init__(self, on_start, rc=0, polls=None):
        on_start()
        self.returncode = None
        self.rc = rc
        self.polls = polls
        self.signals = []

    def poll(self):
        if self.polls is not None:
            self.polls -= 1
            if self.polls < 0:
                self.returncode = self.rc
        return self.returncode

    def send_signal(self, s):
        self.signals.append(s)
        self.returncode = -s

    def wait(self, t=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_parse_frames():
    assert RUN.parse_frames("600:120") == (600, 120)
    for bad in ("600", "1:0", "a:b", "0:5", "1:10001"):
        with pytest.raises(CliError):
            RUN.parse_frames(bad)


def test_envs_strip_recomp_and_add_keys(d):
    env = RUN.base_env(
        {"RECOMP_TRACE": "x", "HOME": "/h"}, {"RECOMP_INPUT_SCRIPT": "@p"}, d, "/c.toml"
    )
    assert "RECOMP_TRACE" not in env and env["HOME"] == "/h"
    assert env["RECOMP_INPUT_SCRIPT"] == "@p" and env["RECOMP_CONFIG"] == "/c.toml"
    assert env["SDL_AUDIODRIVER"] == "dummy" and env["RECOMP_HEADLESS"] == "1"
    RUN.capture_env(env, "/o", (600, 120), "/g/config/hud.toml")
    assert env["RECOMP_DEBUG"] == (
        "hud_capture=/o,hud_capture_at=600:120,hud_capture_table=/g/config/hud.toml"
    )
    with pytest.raises(CliError, match="comma"):
        RUN.capture_env({}, "/a,b", (1, 1), None)
    env = RUN.check_env({}, "/f", "300,420")
    assert env["RECOMP_TRACE"] == "hud,aspect"
    assert env["RECOMP_DEBUG"] == "fb_dump=/f/,fb_dump_at=300,420"


def test_run_config(d):
    p = RUN.run_config(os.path.join(d, "c.toml"), "16:9", "hud-anchors", False)
    assert tomllib.load(open(p, "rb")) == {
        "display": {"aspect": "16:9"},
        "mods": {"hud-anchors": False},
    }


def test_play_stops_when_done(d):
    clock = iter(range(100))
    calls = []
    rc, why = RUN.play(
        "/x",
        {},
        d,
        os.path.join(d, "log"),
        50,
        lambda: len(calls) > 2,
        popen=lambda *a, **k: FakeProc(lambda: None),
        clock=lambda: next(clock),
        sleep=calls.append,
    )
    assert why == "done" and rc == -2


def test_play_sees_exit_and_limit(d):
    clock = iter(range(100))
    rc, why = RUN.play(
        "/x",
        {},
        d,
        os.path.join(d, "log"),
        50,
        lambda: False,
        popen=lambda *a, **k: FakeProc(lambda: None, rc=3, polls=1),
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )
    assert (rc, why) == (3, "exited")
    clock = iter(range(100))
    rc, why = RUN.play(
        "/x",
        {},
        d,
        os.path.join(d, "log"),
        5,
        lambda: False,
        popen=lambda *a, **k: FakeProc(lambda: None),
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )
    assert why == "limit"


def test_captured_up_to(d):
    p = os.path.join(d, "c.jsonl")
    assert RUN.captured_up_to(p) == 0
    open(p, "w").write(
        '{"type":"frame","frame":600,"bmp":null}\n'
        '{"type":"sprite","level":1,"frame":601,"bbox":null}\n'
        '{"type":"frame","frame":601,"canvas":null,"bmp":null}\n'
        '{"type":"sprite","level":1,"frame":719}\n'
        '{"type":"frame","frame":719,"can'
    )
    # A frame counts once its whole frame line is there.
    assert RUN.captured_up_to(p) == 601
    log = os.path.join(d, "game-stdio.log")
    assert not RUN.capture_done(log)
    open(log, "w").write("x\n[hud.capture] done: frames 600..719, 3 lines, in d\n")
    assert RUN.capture_done(log)


def test_dumpat():
    assert RUN.dumpat_for(20) == "300,420,540,660,780,900,1020"
    assert RUN.last_flip("300,420") == 420


class FakeGame:
    def __init__(self, d, hud=None, scenarios=None):
        self.root = d
        self.slug = "mygame"
        self.dist = os.path.join(d, "dist")
        self.game_files = "game_files"
        self.m = {
            "hud": hud or {},
            "input": {"script_env": "RECOMP_INPUT_SCRIPT", "presets": ["@menu"]},
            "golden": {"json": "golden.json"},
        }
        json.dump({"scenarios": scenarios or {}}, open(os.path.join(d, "golden.json"), "w"))

    def path(self, p):
        return os.path.join(self.root, p)

    def rel(self, p):
        return os.path.relpath(p, self.root)


class Args:
    def __init__(self, **kw):
        self.__dict__.update(
            dict(frames="10:3", secs=0, out="", level=1, exe="", lock_wait=1, at="")
        )
        self.__dict__.update(kw)


@pytest.fixture
def no_lock(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def fake(cmd, wait, say):
        yield 0

    monkeypatch.setattr(RUN, "locked", fake)


def test_capture_run(d, no_lock):
    G = FakeGame(d, hud={"mod": "hud-anchors"})
    exe = os.path.join(d, "game")
    open(exe, "w").close()
    out = os.path.join(d, "cap")
    seen = {}

    def popen(argv, cwd, env, **k):
        seen.update(env=env, cwd=cwd)

        def start():
            with open(os.path.join(out, "capture.jsonl"), "w") as f:
                for fr in (10, 11, 12):
                    f.write(json.dumps({"type": "frame", "frame": fr}) + "\n")

        return FakeProc(start)

    rc = RUN.do_capture(
        G, Args(scenario="@menu", exe=exe, out=out), say=lambda s: None, popen=popen
    )
    assert rc == 0 and seen["cwd"] == d
    env = seen["env"]
    assert env["RECOMP_INPUT_SCRIPT"] == "@menu"
    assert "hud_capture=%s,hud_capture_at=10:3" % out in env["RECOMP_DEBUG"]
    cfg = tomllib.load(open(env["RECOMP_CONFIG"], "rb"))
    assert cfg == {"display": {"aspect": "4:3"}, "mods": {"hud-anchors": True}}
    # The run's own hdd, gone after the run.
    assert env["RECOMP_HDD_DIR"] == os.path.join(out, "hdd") and not os.path.exists(
        env["RECOMP_HDD_DIR"]
    )


def test_capture_needs_the_mod_or_the_table(d, no_lock):
    G = FakeGame(d)
    with pytest.raises(CliError, match=r"\[hud\] mod"):
        RUN.do_capture(G, Args(scenario="@menu", level=1))
    with pytest.raises(CliError, match="no scenario nope"):
        RUN.do_capture(G, Args(scenario="nope", level=0))
    with pytest.raises(CliError, match="there is none"):
        RUN.do_capture(G, Args(scenario="@menu", level=0))


def test_scenario_from_golden(d):
    G = FakeGame(d, scenarios={"menu": {"seconds": 30, "env": {"RECOMP_X": "1"}}})
    env, secs = RUN.scenario(G, "menu")
    assert secs == 30


# ── check's sheet ────────────────────────────────────────────────────────

LOG = """\
[hud.unlisted] 0x00012305 x -40..-20
[hud.unlisted] 0x00012305 x -40..-20
[hud.skew] 0x00012405
[hud.skip] 0x00012505 prim 3 is not the layout's 4
[aspect.program] 0000ABCD flip 300: 6 vertices 2 tris x -100..20 on 640x480 pad 107 at 0x0 pitch 2560, stage 0 tex -> clip lift
[aspect.program] 0000ABCE flip 300: 6 vertices 2 tris x 600..760 on 640x480 pad 107 at 0x0 pitch 2560, stage 0 tex -> clip lift
[aspect.program] 0000ABCF flip 300: 6 vertices 2 tris x -100..760 on 640x480 pad 107 at 0x0 pitch 2560, stage 0 tex -> clip
"""


def test_sheet_parse_log():
    got = S.parse_log(LOG.splitlines(True))
    assert got["unlisted"] == {0x12305: (-40.0, -20.0)}
    assert got["skew"] == [0x12405]
    assert got["skip"] == {0x12505: "prim 3 is not the layout's 4"}
    assert [x[0] for x in got["lifted"]] == ["0000ABCD", "0000ABCE"]


def test_sheet_explains_by_kind():
    assert S.explains({"left"}, -100, 20, 640) and not S.explains({"left"}, 600, 760, 640)
    assert S.explains({"proportional"}, -100, 760, 640)
    assert S.explains(set(), 0, 640, 640)


def run_dir(d, name, flips, log=""):
    rd = os.path.join(d, name)
    os.makedirs(os.path.join(rd, "frames"))
    for f in flips:
        open(os.path.join(rd, "frames", "flip_%05d.bmp" % f), "wb").close()
    open(os.path.join(rd, "game-stdio.log"), "w").write(log)
    return rd


def test_sheet_write(d):
    on = (run_dir(d, "on", [300, 420], LOG), 0, "done")
    off = (run_dir(d, "off", [300]), 0, "done")
    known = {"site": [{"ret_va": 1, "element": "a", "kind": "left"}]}
    path, failed = S.write(os.path.join(d, "out"), "@menu", "16:9", on, off, known)
    # The skip and the right-hand lift no row explains.
    assert failed == 2 and path.endswith("check-16x9.html")
    text = open(path).read()
    assert "../off/frames/flip_00300.bmp" in text and "no dump" in text
    assert "0x00012505" in text and "0000ABCE" in text
    _, failed = S.write(os.path.join(d, "out"), "@menu", "16:9", on, (off[0], 1, "exited"), {})
    assert failed == 4


def test_check_run(d, no_lock, monkeypatch):
    G = FakeGame(d, hud={"mod": "hud-anchors", "scenarios": ["@menu"]})
    exe = os.path.join(d, "game")
    open(exe, "w").close()
    envs = []

    def popen(argv, cwd, env, stdout, **k):
        envs.append(env)
        stdout.write(b"[METAL] flip 423 batches 3\n")
        stdout.flush()
        return FakeProc(lambda: None)

    out = os.path.join(d, "check")
    rc = RUN.do_check(
        G,
        Args(scenario=[], ratios="16:9,21:9", at="300,420", exe=exe, out=out),
        say=lambda s: None,
        popen=popen,
    )
    assert rc == 0 and len(envs) == 4
    mods = [tomllib.load(open(e["RECOMP_CONFIG"], "rb")) for e in envs]
    assert [(m["display"]["aspect"], m["mods"]["hud-anchors"]) for m in mods] == [
        ("16:9", True),
        ("16:9", False),
        ("21:9", True),
        ("21:9", False),
    ]
    assert os.path.exists(os.path.join(out, "menu", "check-21x9.html"))


def test_check_needs_mod_and_scenarios(d):
    with pytest.raises(CliError, match="hud\\] mod"):
        RUN.do_check(FakeGame(d), Args(scenario=[], ratios="16:9"))
    with pytest.raises(CliError, match="no scenario"):
        RUN.do_check(FakeGame(d, hud={"mod": "m"}), Args(scenario=[], ratios="16:9"))


# ── manifest and the command line ────────────────────────────────────────


def test_manifest_hud_table_is_optional():
    assert "hud" not in manifest.current().m and "hud" in manifest.OPTIONAL
    doc = tomllib.load(open(os.path.join(manifest.current().root, "game.toml"), "rb"))
    doc["hud"] = {"mod": "hud-anchors"}
    m = manifest.validate(doc)
    assert m["hud"] == {"table": "config/hud.toml", "scenarios": [], "mod": "hud-anchors"}
    doc["hud"]["tabel"] = "x"
    with pytest.raises(manifest.ManifestError, match="hud.tabel"):
        manifest.validate(doc)


def test_hud_map_parses():
    ap, _ = main.make_parser()
    a = ap.parse_args(["hud-map", "check", "@menu", "--ratios", "21:9"])
    assert (a.action, a.scenario, a.ratios, a.lock_wait) == ("check", ["@menu"], "21:9", 3600)
    a = ap.parse_args(["hud-map", "emit", "--check"])
    assert a.proposal == "" and a.check and not a.no_analysis
    assert ap.parse_args(["hud-map", "emit", "p.toml", "--no-analysis"]).no_analysis
    a = ap.parse_args(["hud-map", "capture", "@menu", "--level", "0"])
    assert a.level == 0 and a.frames == "600:120"
    text = ap.format_help()
    assert "hud-map <action>" in text.split("Developer commands:")[1]


# ── review fixes: the C reader's limits, names, analysis, malformed rows ──


def test_table_form_draws_are_capture_draws_blocks():
    row = {"fn": 0x11000, "name": "quad", "vtx": "stack:0", "count": "stack:4", "stride": 28}
    doc = table(capture={"draws": [row, "sig:Draw"]})
    text = T.dumps(doc)
    assert "[capture]" not in text and text.count("[[capture.draws]]") == 2
    assert 'fn = 0x00011000\nname = "quad"\nvtx = "stack:0"' in text and "{" not in text
    assert "stride = 28" in text and T.check_draws(tomllib.loads(text)) == []
    assert T.c_readable(text) == []
    assert tomllib.loads(text)["capture"]["draws"][1] == {"fn": "sig:Draw"}
    # Names and VAs only: one [capture] with a flat array.
    flat = T.dumps(table(capture={"draws": ["sig:Draw", 0x11000]}))
    assert '[capture]\ndraws = ["sig:Draw", 0x00011000]' in flat


def test_merge_into_capture_draws_blocks():
    src = (
        "version = 1\n\n# what the level-0 run records\n"
        "[[capture.draws]]\nfn = 0x00011000\n\n"
        '[[site]]\nret_va = 0x00012005\nelement = "heart"\nkind = "left"\n'
    )
    cap = {"draws": [{"fn": 0x11000, "vtx": "ecx", "count": 4}, {"va": 0x13000}]}
    text, report = T.merge(src, [("capture", cap)])
    assert report == [("capture", None, "changed")]
    assert tomllib.loads(text)["capture"] == cap and T.c_readable(text) == []
    assert "# what the level-0 run records\n[[capture.draws]]" in text
    assert text.index("[[capture.draws]]") < text.index("[[site]]")
    # The same capture again changes nothing.
    assert T.merge(text, [("capture", cap)]) == (text, [])
    # A flat capture replaces the blocks with one [capture].
    text, _ = T.merge(text, [("capture", {"draws": ["sig:Draw"]})])
    assert "[[capture.draws]]" not in text and T.c_readable(text) == []
    assert tomllib.loads(text)["capture"] == {"draws": ["sig:Draw"]}


def test_c_readable_refuses_what_the_reader_refuses():
    text = (
        'a = { x = 1 }\nb = [[1], 2]\nc = [1, "[", 2] # [x]\n'
        "d = [\n  1, # one\n  2,\n]\n[capture]\n[[capture.draws]]\nfn = 1\n"
    )
    assert T.c_readable(text) == [
        "line 1: a: inline tables are not supported",
        "line 2: b: nested arrays and inline tables are not supported",
        "line 9: [[capture.draws]] beside the table [capture] (line 8)",
    ]


def test_emit_refuses_text_the_reader_refuses(d):
    an = write_analysis(d)
    tp = os.path.join(d, "hud.toml")
    open(tp, "w").write(T.dumps(emit_doc()) + "\n[capture]\ndraws = [{ fn = 0x11000 }]\n")
    rc, msgs, _ = E.emit(tp, None, E.Hooked(["0x11000", "0x13000"]), an, dry=True)
    assert rc == 1 and msgs[0].endswith("draws: nested arrays and inline tables are not supported")


def test_emit_writes_capture_draws_the_reader_takes(d):
    an = write_analysis(d)
    hooked = E.Hooked(["0x11000", "0x13000"])
    tp = os.path.join(d, "hud.toml")
    open(tp, "w").write(T.dumps(emit_doc()))
    prop = os.path.join(d, "p.toml")
    open(prop, "w").write(T.dumps({"version": 1, "capture": {"draws": [{"fn": 0x11000}]}}))
    rc, msgs, report = E.emit(tp, prop, hooked, an)
    assert (rc, msgs, report) == (0, [], [("capture", None, "added")])
    text = open(tp).read()
    assert "[[capture.draws]]\nfn = 0x00011000" in text and T.c_readable(text) == []
    assert E.emit(tp, None, hooked, an) == (0, [], [])


def test_emit_without_analysis_needs_no_analysis(d):
    tp = os.path.join(d, "hud.toml")
    prop = os.path.join(d, "p.toml")
    open(prop, "w").write(T.dumps(emit_doc()))
    hooked = E.Hooked(["0x11000", "0x13000"])
    rc, msgs, _ = E.emit(tp, prop, hooked, None)
    assert rc == 1 and msgs == [E.NO_ANALYSIS] and not os.path.exists(tp)
    assert E.emit(tp, prop, hooked, None, dry=True)[0] == 0
    assert E.emit(tp, prop, hooked, None, no_analysis=True)[0] == 0 and os.path.exists(tp)


def test_merge_keeps_comments_inside_a_changed_block():
    src = "version = 1\n\n[canvas]\n# the guest's units\nw = 640 # width\n\n# height\nh = 480\n"
    text, report = T.merge(src, [("canvas", {"w": 1280, "h": 480, "origin": "centre"})])
    assert report == [("canvas", None, "changed")]
    assert text == (
        "version = 1\n\n[canvas]\n# the guest's units\nw = 1280 # width\n\n"
        '# height\nh = 480\norigin = "centre"\n'
    )


def test_str_escapes_control_characters():
    s = T._str('a\tb\nc\x7f"\\')
    assert s == '"a\\tb\\nc\\u007F\\"\\\\"'
    assert tomllib.loads("k = " + s)["k"] == 'a\tb\nc\x7f"\\'
    s = T._str("\b\f\r\x01")
    assert s == '"\\b\\f\\r\\u0001"'
    assert tomllib.loads("k = " + s)["k"] == "\b\f\r\x01"


def test_group_names_fit_the_parser():
    long = "CHudManager::DrawPlayerStatusPanelAndEverythingElse"
    assert len(P.fit_name(long).encode()) == T.NAME_MAX - 1
    assert P.fit_name(long, "_2").endswith("_2")
    assert len(P.fit_name(long, "_2").encode()) == T.NAME_MAX - 1
    # A cut never splits a UTF-8 character.
    cut = P.fit_name("é" * 40)
    assert cut == "é" * 23 and T._name_ok(cut)


def test_propose_truncates_long_group_names(d):
    sprites = []
    for f in (0, 1, 2):
        sprites += [
            sprite(f, 0x12005, [-310, -230, -300, -220]),
            sprite(f, 0x12015, [-299, -230, -290, -220]),
        ]
    cap = C.Capture(write_capture(d, sprites))
    name = "x" * 60
    an = FakeAnalysis([(0x12000, 0x12100, name)])
    known = table(group=[{"name": "x" * 47, "kind": "left"}], site=[])
    doc, _ = P.propose([cap], known, an)
    got = [g["name"] for g in doc["group"] if g.get("proposed")]
    assert got == ["x" * 45 + "_2"]
    assert T.validate(doc, proposal=True) == []


def test_malformed_rows_are_errors_not_crashes():
    doc = table(group=["x"], site=[{"ret_va": [1]}, 5])
    errs = T.validate(doc)
    assert "unknown key group" in errs and "site 0: ret_va is not an address" in errs
    assert E.check(doc, E.Hooked([]), None) == errs
    assert S.table_kinds(doc) == set() and R.rows_by_site(doc) == {}


def test_capture_needs_a_canvas(d):
    with open(os.path.join(d, "capture.jsonl"), "w") as f:
        f.write(json.dumps({"type": "frame", "frame": 0}) + "\n")
    with pytest.raises(CliError, match="no frame line with a canvas"):
        C.Capture(d)


def test_capture_skips_a_last_line_cut_inside_a_utf8_name(d, capsys):
    # A run killed mid-write can stop inside a multibyte character.
    path = write_capture(d, [sprite(0, 0x12005, [0, 0, 10, 10])])
    with open(os.path.join(path, "capture.jsonl"), "ab") as f:
        f.write('{"type": "sprite", "fn": "Dibujar\u00e9'.encode()[:-1])
    cap = C.Capture(path)
    assert len(cap.sprites) == 1
    assert "partial last line skipped" in capsys.readouterr().err


def test_capture_clears_an_earlier_runs_frames(d, no_lock):
    G = FakeGame(d, hud={"mod": "hud-anchors"})
    exe = os.path.join(d, "game")
    open(exe, "w").close()
    out = os.path.join(d, "cap")
    os.makedirs(out)
    for n in ("frame_00099.bmp", "capture.jsonl", "notes.txt", "flip_00001.bmp"):
        open(os.path.join(out, n), "w").close()

    def popen(argv, cwd, env, **k):
        def start():
            with open(os.path.join(out, "capture.jsonl"), "w") as f:
                for fr in (10, 11, 12):
                    f.write(json.dumps({"type": "frame", "frame": fr}) + "\n")

        return FakeProc(start)

    RUN.do_capture(G, Args(scenario="@menu", exe=exe, out=out), say=lambda s: None, popen=popen)
    left = sorted(os.listdir(out))
    assert "frame_00099.bmp" not in left and "notes.txt" in left
    assert "flip_00001.bmp" in left


def test_proposal_head_lists_captures_one_per_line(d):
    a = os.path.join(d, "cap, one")
    b = os.path.join(d, "cap2")
    os.makedirs(a)
    os.makedirs(b)
    prop = os.path.join(d, "proposal.toml")
    P.write(table(), [], prop, [a, b])
    assert R.sources(prop) == [a, b]


# ── the toolkit's capture.draws rules and the capture's table copy ──────


@pytest.mark.parametrize(
    "row,err",
    [
        ({"fn": 0x11000, "vtx": "ecx"}, "vtx and count go together"),
        ({"fn": 0x11000, "vtx": "eax", "count": 4}, "vtx is not ecx, edx or stack:N"),
        ({"fn": 0x11000, "vtx": 4, "count": 4}, "vtx is not ecx, edx or stack:N"),
        ({"fn": 0x11000, "vtx": "stack:6", "count": 4}, "vtx is not ecx, edx or stack:N"),
        ({"fn": 0x11000, "vtx": "edx", "count": 0x10000}, "count is not ecx"),
        ({"fn": 0x11000, "stride": "stack:4096"}, "stride is not ecx"),
        ({"fn": 0x11000, "prim": 65}, "prim is not 1..64"),
        ({"fn": 0x11000, "colour": 1}, "unknown key colour"),
        ({"fn": 0}, "fn is not an address"),
        ({"name": "x"}, "fn is not an address"),
        ({"fn": "draw"}, "fn is not an address"),
        ({"fn": 0x11000, "vtx": "stack:08", "count": 4}, "vtx is not ecx, edx or stack:N"),
        ({"fn": 0x11000, "vtx": "stack:\u0664", "count": 4}, "vtx is not ecx, edx or stack:N"),
        ({"fn": 0x11000, "name": "x" * 48}, "name is not a string of 1..47 bytes"),
        ({"fn": "0o17"}, "fn is not an address"),
        ({"fn": "0b1"}, "fn is not an address"),
        ({"fn": "1_000"}, "fn is not an address"),
        ({"fn": "0x10 "}, "fn is not an address"),
        ({"fn": "\u0664\u0664"}, "fn is not an address"),
        ({"fn": "-1"}, "fn is not an address"),
        ({"fn": "0x100000000"}, "fn is not an address"),
    ],
)
def test_check_draws(row, err):
    errs = T.check_draws({"capture": {"draws": [row]}})
    assert len(errs) == 1 and errs[0].startswith("capture.draws 0: " + err)


def test_check_draws_takes_the_toolkit_spellings():
    draws = [
        0x11000,
        "0x00011000",
        "sig:Draw",
        {"va": "0x11000", "vtx": "stack:0x10", "count": "edx", "stride": "stack:8", "prim": 3},
        {"sig": "Draw", "name": "quad"},
        {"fn": "010", "vtx": "stack:010", "count": 4},
        " +0x10",
        "69632",
    ]
    doc = {"capture": {"draws": draws}}
    assert T.check_draws(doc) == []
    assert [e for e, _ in T.draws(doc)] == [
        "0x00011000",
        "0x00011000",
        "sig:Draw",
        "0x00011000",
        "sig:Draw",
        "0x00000008",
        "0x00000010",
        "0x00011000",
    ]


@pytest.mark.parametrize(
    "s,v",
    [
        ("0", 0),
        ("010", 8),
        ("0x1F", 31),
        ("0X1f", 31),
        (" \t+42", 42),
        ("08", None),
        ("0x", None),
        ("", None),
        ("+", None),
        ("-4", None),
        ("4 ", None),
        ("1_0", None),
        ("0o7", None),
        ("\u0664", None),
        (7, None),
    ],
)
def test_c_ulong_reads_like_strtoul_base_0(s, v):
    assert T.c_ulong(s) == v


def test_hooked_reads_a_draw_address_like_the_toolkit():
    h = E.Hooked(["0x11000"])
    assert h.has("0x00011000") and h.has("69632") and h.has(0x11000)
    assert not E.Hooked(["0x69632"]).has("69632")
    assert not h.has("sig:Draw") and not h.has("draw")
    doc = {"capture": {"draws": ["69632"]}}
    assert E.check_hooks(doc, h) == []


def test_draws_given_as_a_table_or_too_many_are_refused():
    doc = {"capture": {"draws": {"fn": 0x11000}}}
    assert T.draws(doc) == []
    assert T.check_draws(doc) == ["capture.draws: draws is a table; use [[capture.draws]]"]
    doc = {"capture": {"draws": [0x11000] * 65}}
    assert T.check_draws(doc) == ["capture.draws: 65 draws, the toolkit reads at most 64"]
    assert T.check_draws({"capture": {"draws": [0x11000] * 64}}) == []


def test_a_site_group_that_is_not_a_name_is_an_error():
    doc = table(site=[{"ret_va": 0x12005, "element": "heart", "group": ["x"]}])
    assert T.validate(doc) == ["site 0x00012005: group is not a name"]


def test_emit_refuses_draws_the_capture_skips(d):
    an = write_analysis(d)
    doc = emit_doc(capture={"draws": [{"fn": 0x11000, "vtx": "ecx"}]})
    errs = E.check(doc, E.Hooked(["0x11000", "0x13000"]), an)
    assert errs == ["capture.draws 0: vtx and count go together"]


def test_capture_table_resolves_sig_draws(d, monkeypatch):
    from xboxrecomp_cli import hooks

    G = FakeGame(d)
    G.hook_functions = os.path.join(d, "hook_functions.json")
    tp = os.path.join(d, "hud.toml")
    out = os.path.join(d, "cap")
    os.makedirs(out)
    slots = [{"entry": "sig:Draw", "va": "0x00011000"}, {"entry": "0x13000", "va": "0x13000"}]
    monkeypatch.setattr(hooks, "load_json", lambda G: slots)
    # Nothing to resolve: the game's own table.
    open(tp, "w").write(T.dumps(table(capture={"draws": [0x13000]})))
    assert RUN.capture_table(G, tp, out) == tp
    draws = ["sig:Draw", {"sig": "Draw", "vtx": "ecx", "count": 4}, {"fn": "sig:Draw"}, 0x13000]
    open(tp, "w").write(T.dumps(table(capture={"draws": draws})))
    got = RUN.capture_table(G, tp, out)
    assert got == os.path.join(out, RUN.CAPTURE_TABLE)
    text = open(got).read()
    assert "sig:" not in text.split("\n", 1)[1] and T.c_readable(text) == []
    assert tomllib.loads(text)["capture"]["draws"] == [
        {"fn": 0x11000},
        {"fn": 0x11000, "vtx": "ecx", "count": 4},
        {"fn": 0x11000},
        {"fn": 0x13000},
    ]
    open(tp, "w").write(T.dumps(table(capture={"draws": ["sig:Other"]})))
    with pytest.raises(CliError, match="sig:Other: no VA"):
        RUN.capture_table(G, tp, out)
