"""benchdb plan's pending judge, short guard and --frames,
against the temp workspace of test_benchdb_clean: a citation pins only
while the work it belongs to is open, the fail safe keeps what cannot be
mapped, golden.json always pins, a live topic stays under the short age,
and --frames takes only the frame dumps of passing runs."""

import datetime
import json
import os
import time

import pytest
from test_benchdb_clean import DAY, NOW, WS, NoHost, age, git, write

from xboxrecomp_cli.benchdb import clean, config, pending
from xboxrecomp_cli.benchdb.store import DbError


@pytest.fixture
def ws(tmp_path, monkeypatch):
    d = os.path.realpath(str(tmp_path / "ws"))
    os.makedirs(d)
    monkeypatch.setenv("BENCHDB_AGENT", "tester")
    monkeypatch.setattr(clean, "run_host", NoHost())
    # a neutral tracker prefix: the default is the maintainer's project
    monkeypatch.setattr(config, "DEFAULT_PREFIXES", ("TRK",))
    return WS(d, monkeypatch)


def fetched(ago=0):
    t = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=ago)
    return t.isoformat(timespec="seconds")


def states(ws, **items):
    """A tracker states file: TRK_n=(state, group[, labels])."""
    doc = {"fetched": fetched(), "project": "TRK", "items": {}}
    for k, v in items.items():
        doc["items"][k.replace("_", "-")] = {
            "state": v[0],
            "group": v[1],
            "labels": list(v[2]) if len(v) > 2 else [],
        }
    p = ws.p("states.json")
    with open(p, "w") as f:
        json.dump(doc, f)
    return p


DONE = ("Done", "completed")
OPEN = ("In Progress", "started")
BACKLOG = ("Backlog", "backlog")


def runs(ws, *names):
    for n in names:
        ws.store_run(n)
        ws.run_row(n)
    return ["bench-logs/" + n for n in names]


def commit(ws):
    git(ws.game, "add", "-A")
    git(ws.game, "commit", "-qm", "cite")


def test_a_citation_pins_by_its_block_and_heading(ws):
    write(
        ws.p("notes/refs/t.md"),
        "## TRK-10 the fade (done)\n"
        "kept 20261001-200001 for the A/B\n"
        "\n"
        "## TRK-11 the menu\n"
        "kept 20261001-200002\n"
        "\n"
        "- TRK-99 says 20261001-200004\n"
        "- TRK-12 and 20261001-200006\n",
    )
    names = runs(ws, *("20261001-20000%d" % i for i in (1, 2, 4, 6)))
    plan = ws.plan(states=states(ws, TRK_10=DONE, TRK_11=OPEN, TRK_12=BACKLOG))
    assert ws.planned(plan) == [names[0]]
    assert "t.md:5 cites 20261001-200002 (pending: TRK-11 In Progress)" in ws.why(plan, names[1])
    # The bullet's own item is unknown, its heading's is open: any open ref pins.
    assert "(pending: TRK-11 In Progress)" in ws.why(plan, names[2])
    # Backlog is open work: what it cites stays.
    assert "(pending: TRK-12 Backlog, TRK-11 In Progress)" in ws.why(plan, names[3])
    pins = plan["pins"]
    assert pins["tracker"] and pins["pinned"] == 3 and pins["pinned_failsafe"] == 0
    assert pins["released"] == {"20261001-200001": ["notes/refs/t.md:2 (closed: TRK-10 Done)"]}


def test_a_table_row_does_not_take_its_heading(ws):
    """An index table's heading names one item while its rows keep other
    work's runs: a bare row is weighed by the whole file, never by the
    heading, so an open item anywhere in the file keeps it."""
    write(
        ws.p("notes/refs/t.md"),
        "## Kept evidence (TRK-10 index)\n"
        "| run | why |\n"
        "|---|---|\n"
        "| 20261001-200001 | TRK-10 fade |\n"
        "| 20261001-200002 | menu |\n"
        "\nSee also TRK-11.\n",
    )
    names = runs(ws, "20261001-200001", "20261001-200002")
    plan = ws.plan(states=states(ws, TRK_10=DONE, TRK_11=OPEN))
    assert ws.planned(plan) == [names[0]]
    assert "(pending: TRK-11 In Progress (anywhere in the file))" in ws.why(plan, names[1])
    assert plan["pins"]["pinned_failsafe"] == 1
    # The whole file only keeps: every item closed still pins the bare row.
    plan = ws.plan(states=states(ws, TRK_10=DONE, TRK_11=DONE))
    assert ws.planned(plan) == [names[0]]
    assert "fail-safe: only closed" in ws.why(plan, names[1])


def test_a_note_maps_by_its_folder_label_and_its_name(ws):
    """A note that names no item is weighed by its path: a trkNNN in it, or
    its notes folder named by an item's agent:<folder> label."""
    write(ws.p("notes/refs/plain.md"), "kept 20261001-200001\n")
    write(ws.p("notes/refs/trk77-menu.md"), "kept 20261001-200002\n")
    names = runs(ws, "20261001-200001", "20261001-200002")
    st = states(ws, TRK_5=DONE + (("agent:refs",),), TRK_77=OPEN)
    plan = ws.plan(states=st)
    assert ws.planned(plan) == [names[0]]
    assert "(pending: TRK-77 In Progress)" in ws.why(plan, names[1])
    st = states(ws, TRK_5=OPEN + (("agent:refs",),), TRK_77=DONE)
    plan = ws.plan(states=st)
    assert "(pending: TRK-5 In Progress)" in ws.why(plan, names[0])
    # Without a label the plain note maps to nothing: the fail safe.
    plan = ws.plan(states=states(ws, TRK_77=DONE))
    assert "(fail-safe: no item)" in ws.why(plan, names[0])


def test_specs_open_archived_and_golden(ws):
    write(ws.p("demo/openspec/changes/x/tasks.md"), "- kept 20261001-200001\n")
    write(
        ws.p("demo/openspec/changes/archive/2026-10-01-old/tasks.md"),
        "- kept 20261001-200002\n",
    )
    write(ws.p("demo/analysis/golden/golden.json"), '{"ref": "20261001-200003"}\n')
    write(ws.p("demo/TASKS.md"), "# tasks\n- [x] openspec/changes/gone/ 20261001-200004\n")
    commit(ws)
    names = runs(ws, *("20261001-20000%d" % i for i in range(1, 5)))
    plan = ws.plan(states=states(ws))
    assert "(pending: openspec x open)" in ws.why(plan, names[0])
    assert ws.why(plan, names[2]) and "(golden reference)" in ws.why(plan, names[2])
    # A change named nowhere on main lives on a branch: the fail safe.
    assert "(fail-safe: openspec gone not on main)" in ws.why(plan, names[3])
    assert names[1] in ws.planned(plan)
    assert plan["pins"]["pinned_golden"] == 1


def test_without_states_every_item_pins(ws):
    write(ws.p("notes/refs/t.md"), "## TRK-10\nkept 20261001-200001\n")
    names = runs(ws, "20261001-200001")
    plan = ws.plan(states=ws.p("missing.json"))
    assert "(fail-safe: TRK-10 (tracker not read))" in ws.why(plan, names[0])
    assert any("every citation that names an item keeps pinning" in w for w in plan["warnings"])
    # Without [pending] or --states the old judge and messages stay.
    plan = ws.plan()
    assert ws.why(plan, names[0]).endswith("t.md cites 20261001-200001")
    assert "pins" not in plan


def test_the_short_guard_and_live_topics(ws):
    st = states(
        ws,
        TRK_612=OPEN,
        TRK_9=("In Review", "started", ("agent:labelled",)),
        TRK_8=DONE + (("agent:finished",),),
    )
    ws.store_run("20261028-100000")
    ws.run_row("20261028-100000")
    age(ws.p("bench-logs/20261028-100000"), NOW - DAY)
    ws.store_run("20261029-100000")
    ws.run_row("20261029-100000")
    age(ws.p("bench-logs/20261029-100000"), NOW - 3600)
    for t in ("trk612-probe", "labelled", "finished", "has-wt"):
        write(ws.p("runs/%s/a.txt" % t))
        age(ws.p("runs/" + t), NOW - 1.5 * DAY)
    os.makedirs(ws.p("wt/has-wt"))
    plan = ws.plan(states=st, guard_hours=6)
    assert plan["guard_hours"] == 6
    assert sorted(ws.planned(plan)) == ["bench-logs/20261028-100000", "runs/finished"]
    assert ws.why(plan, "bench-logs/20261029-100000") == "written within 6 hours"
    assert ws.why(plan, "runs/trk612-probe") == "live: TRK-612 In Progress"
    assert ws.why(plan, "runs/labelled") == "live: TRK-9 In Review is agent:labelled"
    assert ws.why(plan, "runs/has-wt") == "live: wt/has-wt exists"
    rules = {i["path"]: i["rule"] for i in plan["items"]}
    # The runs root's floor is a day: a topic dir may be a live agent's.
    assert rules[ws.p("runs/finished")] == "older than 1 days"
    # No tracker: the guard is dropped and the roots' own days apply.
    plan = ws.plan(guard_hours=6)
    assert ws.planned(plan) == [] and "guard_hours" not in plan
    assert any("guard_hours 6 not used" in w for w in plan["warnings"])
    with pytest.raises(DbError, match="at least 1 hour"):
        ws.plan(states=st, guard_hours=0.5)


def frame_run(ws, name, words, exit_code=0):
    d = ws.store_run(name, files=("game-stdio.log", "frames/flip_1.bmp"))
    rid = ws.run_row(name, status="ok", exit_code=exit_code)
    with ws.db.write() as w:
        w.artifact(rid, "store", name + "/frames", "frames", True, bytes=10)
        for i, word in enumerate(words):
            s = w.session("s-%s-%d" % (name, i), "demo", "golden", "2026-10-01T10:00:00Z")
            w.verdict(s, "sc%d" % i, word, run=rid)
    age(d)
    return "bench-logs/%s/frames" % name


def test_frames_of_passing_runs_only(ws):
    f = [
        frame_run(ws, "20261001-210001", ["pass"]),
        frame_run(ws, "20261001-210002", ["pass", "REGRESSION"]),
        frame_run(ws, "20261001-210003", ["pass-with-known"]),
        frame_run(ws, "20261001-210004", []),
        frame_run(ws, "20261001-210005", ["pass"], exit_code=None),
        frame_run(ws, "20261001-210007", ["pass"], exit_code=-2),
        frame_run(ws, "20261001-210006", ["pass"]),
    ]
    write(ws.p("notes/refs/t.md"), "## TRK-3\nkept 20261001-210006\n")
    pid, plan = ws.saved(states=states(ws, TRK_3=OPEN), frames=True)
    assert ws.planned(plan) == [f[0], f[5]]
    assert plan["items"][0]["rule"] == "frames of a passing run"
    assert ws.why(plan, f[1]) == "frames kept: verdicts REGRESSION/pass"
    assert ws.why(plan, f[2]) == "frames kept: verdicts pass-with-known"
    assert ws.why(plan, f[3]) == "frames kept: no verdict"
    assert ws.why(plan, f[4]) == "frames kept: no exit code parsed"
    assert "(pending: TRK-3 In Progress)" in ws.why(plan, f[6])
    # clean re-plans with the saved args: the same one item goes.
    out, _ = ws.clean(pid)
    assert out["deleted"] == 2
    assert not os.path.exists(ws.p(f[0]))
    assert os.path.isfile(ws.p("bench-logs/20261001-210001/game-stdio.log"))
    # An explicit run bounds --frames; a worktree root is refused.
    plan = ws.plan(ws.p("bench-logs/20261001-210002"), "wt", frames=True)
    assert ws.planned(plan) == []
    assert ws.why(plan, "wt") == "--frames covers the store and runs roots only"


def test_pending_and_clean_config_keys(ws):
    def load(extra):
        with open(ws.p(config.NAME), "a") as f:
            f.write(extra)
        return config.load(ws.p(config.NAME))

    base = open(ws.p(config.NAME)).read()
    cfg = load('\n[pending]\nstates = ["true"]\nprefixes = ["trk"]\n[clean]\nguard_hours = 6\n')
    assert cfg.pending["prefixes"] == ["TRK"] and cfg.pending["cache_minutes"] == 10
    assert cfg.guard_hours == 6
    for bad, msg in (
        ("\n[pending]\nstate = 1\n", "state"),
        ("\n[clean]\nguard_hours = 0.5\n", "guard_hours"),
    ):
        with open(ws.p(config.NAME), "w") as f:
            f.write(base)
        with pytest.raises(Exception, match=msg):
            load(bad)


def test_states_are_fetched_once_and_cached(ws, monkeypatch):
    doc = {
        "fetched": fetched(),
        "items": {"TRK-1": {"state": "Done", "group": "completed", "labels": []}},
    }
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = json.dumps(doc).encode()
            stderr = b""

        return R()

    monkeypatch.setattr(pending.subprocess, "run", fake_run)
    ws.cfg.pending = dict(states=["fetch"], states_file=None, prefixes=["TRK"], cache_minutes=10)
    t = time.time()
    items, src, warn = pending.load_states(ws.cfg, now=t)
    assert items["TRK-1"]["state"] == "Done" and warn is None
    items, src, warn = pending.load_states(ws.cfg, now=t + 60)
    assert len(calls) == 1 and src.endswith("tracker-states.json")
    pending.load_states(ws.cfg, now=t + 11 * 60)
    assert len(calls) == 2


# ---- the review's cases --------------------------------------------------------


def test_a_closed_block_ref_does_not_hide_an_open_heading(ws):
    write(
        ws.p("notes/refs/t.md"),
        "## TRK-20 Deck slow draws\n\n- baseline: TRK-14's run 20261001-200001, fixed there\n",
    )
    names = runs(ws, "20261001-200001")
    plan = ws.plan(states=states(ws, TRK_20=OPEN, TRK_14=DONE))
    assert ws.planned(plan) == []
    assert "(pending: TRK-20 In Progress)" in ws.why(plan, names[0])


def test_the_whole_file_only_keeps(ws):
    write(
        ws.p("notes/refs/r.md"),
        "# Resume\n\nKeep 20261001-200002 as the Deck baseline.\n\n## History\n\nTRK-14 landed.\n",
    )
    names = runs(ws, "20261001-200002")
    plan = ws.plan(states=states(ws, TRK_14=DONE))
    assert ws.planned(plan) == []
    assert "(fail-safe: only closed: TRK-14 Done (anywhere in the file))" in ws.why(plan, names[0])
    assert plan["pins"]["released"] == {}


def test_a_comment_in_a_code_fence_is_no_heading(ws):
    write(
        ws.p("notes/refs/c.md"),
        "## TRK-20 open work\n\n```sh\n# TRK-14 repro\nblinx2 golden check 20261001-200003\n```\n",
    )
    names = runs(ws, "20261001-200003")
    plan = ws.plan(states=states(ws, TRK_20=OPEN, TRK_14=DONE))
    assert ws.planned(plan) == []
    assert "(pending: TRK-20 In Progress)" in ws.why(plan, names[0])


def test_clean_replans_against_the_states_it_reads(ws):
    write(ws.p("notes/refs/t.md"), "## TRK-10 x\nkept 20261001-200009\n")
    names = runs(ws, "20261001-200009")
    st = states(ws, TRK_10=DONE)
    pid, plan = ws.saved(states=st)
    assert ws.planned(plan) == names
    states(ws, TRK_10=OPEN)
    assert ws.clean(pid)[0]["deleted"] == 0
    os.remove(st)
    assert ws.clean(pid)[0]["deleted"] == 0
    assert os.path.isdir(ws.p(names[0]))


@pytest.mark.parametrize(
    "doc,msg",
    [
        ({"fetched": None, "items": {}}, "no items"),
        ({"items": {"TRK-1": {"state": "Done", "group": "completed"}}}, "no fetched time"),
        (None, "over 60 minutes ago"),
    ],
)
def test_an_empty_or_stale_states_doc_is_refused(ws, doc, msg):
    """An empty or stale doc would release what an item reopened since
    cites: it reads as no tracker, so the guard drops and every item pins."""
    if doc is None:
        doc = {
            "fetched": fetched(2 * 3600),
            "items": {"TRK-10": {"state": "Done", "group": "completed"}},
        }
    p = ws.p("st.json")
    with open(p, "w") as f:
        json.dump(doc, f)
    write(ws.p("notes/refs/t.md"), "## TRK-10\nkept 20261001-200001\n")
    write(ws.p("runs/topic/a.txt"))
    age(ws.p("runs/topic"), NOW - 1.5 * DAY)
    names = runs(ws, "20261001-200001")
    plan = ws.plan(states=p, guard_hours=6)
    assert ws.planned(plan) == [] and "guard_hours" not in plan
    assert "(tracker not read)" in ws.why(plan, names[0])
    assert any(msg in w for w in plan["warnings"]), plan["warnings"]


def test_the_states_cache_is_refetched_when_stale(ws, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = json.dumps(
                {"fetched": fetched(len(calls) == 1 and 7200), "items": {"TRK-1": {}}}
            ).encode()
            stderr = b""

        return R()

    monkeypatch.setattr(pending.subprocess, "run", fake_run)
    ws.cfg.pending = dict(states=["fetch"], states_file=None, prefixes=["TRK"], cache_minutes=10)
    items, _src, warn = pending.load_states(ws.cfg)
    assert items is None and "over 60 minutes ago" in warn
    items, _src, warn = pending.load_states(ws.cfg)
    assert items and warn is None and len(calls) == 2


def test_a_failing_run_keeps_the_games_days_under_the_guard(ws):
    """guard_hours is for passes: a REGRESSION, INCOMPLETE or FAIL-RUN run
    is evidence, so the store and the retention keep it the game's days."""
    st = states(ws, TRK_1=DONE)
    ids = {}
    for name, word in (("20261028-100001", "pass"), ("20261028-100002", "REGRESSION")):
        ws.store_run(name)
        ids[name] = ws.run_row(name, started="2026-10-28T10:00:00Z")
        with ws.db.write() as w:
            s = w.session("s-" + name, "demo", "golden", "2026-10-28T10:00:00Z")
            w.verdict(s, "sc", word, run=ids[name])
        age(ws.p("bench-logs/" + name), NOW - DAY)
    plan = ws.plan(states=st, guard_hours=6)
    assert ws.planned(plan) == ["bench-logs/20261028-100001"]
    assert ws.why(plan, "bench-logs/20261028-100002") == "written within 7 days"
    from xboxrecomp_cli.benchdb import retain

    now = retain.parse_time("2026-10-29T10:00:00Z")
    got = retain.decide(ws.db, ws.cfg, now=now, young_days=0.25)
    assert got[ids["20261028-100001"]][1] != "younger than 7d"
    assert got[ids["20261028-100002"]] == (True, "younger than 7d")


def test_an_archived_spec_matches_its_name_exactly(ws):
    write(ws.p("demo/openspec/changes/archive/2026-10-01-fps-pacing/tasks.md"), "- x\n")
    write(ws.p("demo/TASKS.md"), "# tasks\n- openspec/changes/pacing/ 20261001-200001\n")
    commit(ws)
    names = runs(ws, "20261001-200001")
    plan = ws.plan(states=states(ws, TRK_1=DONE))
    assert "(fail-safe: openspec pacing not on main)" in ws.why(plan, names[0])
    write(ws.p("demo/openspec/changes/archive/2026-10-02-pacing/tasks.md"), "- x\n")
    commit(ws)
    plan = ws.plan(states=states(ws, TRK_1=DONE))
    assert ws.planned(plan) == names


def test_a_trk1_path_names_the_game_not_an_item(ws):
    write(ws.p("notes/refs/trk1-menu.md"), "kept 20261001-200001\n")
    write(ws.p("notes/refs/trk12-menu.md"), "kept 20261001-200002\n")
    names = runs(ws, "20261001-200001", "20261001-200002")
    plan = ws.plan(states=states(ws, TRK_1=DONE, TRK_12=DONE))
    assert "(fail-safe: no item)" in ws.why(plan, names[0])
    assert ws.planned(plan) == [names[1]]


def test_ready_is_live_and_the_runs_floor_is_a_day(ws):
    st = states(ws, TRK_611=("Ready", "unstarted"), TRK_2=DONE)
    for t, ago in (("trk611-build", 1.5 * DAY), ("fresh", 12 * 3600), ("old", 1.5 * DAY)):
        write(ws.p("runs/%s/a.txt" % t))
        age(ws.p("runs/" + t), NOW - ago)
    plan = ws.plan(states=st, guard_hours=6)
    assert ws.planned(plan) == ["runs/old"]
    assert ws.why(plan, "runs/trk611-build") == "live: TRK-611 Ready"
    assert ws.why(plan, "runs/fresh") == "written within 1 days"
