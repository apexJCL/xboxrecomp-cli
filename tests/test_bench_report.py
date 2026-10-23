"""bench-matrix report (bench-report D1-D8) on synthetic scenes and runs."""

import json
import os
import re
import subprocess
import tomllib

import pytest

from xboxrecomp_cli import bench_matrix as B
from xboxrecomp_cli import bench_report as R
from xboxrecomp_cli import bench_report_html as HTML
from xboxrecomp_cli import bench_report_public as P
from xboxrecomp_cli import bench_report_svg as S
from xboxrecomp_cli import public

SCENE = """
arm_id = "s{scale}-{aspect}"

[scene]
name = "t"
run_s = 30
duration_s = 10
warmup_s = 2

[scene.start]
min_batches = 100
hold_flips = 5
fallback_s = 3

[profile]
hz = 250
delay_s = 1

[[axis]]
name = "scale"
values = [1, 2]
config = "render.scale"
stock = 1
display = ["1x", "2x"]

[[axis]]
name = "aspect"
values = ["4:3", "16:9"]
labels = ["43", "169"]
config = "display.aspect"
stock = "4:3"
display = ["4:3", "16:9"]

[report]
target_fps = 60
label = "Test scene"

[report.best]
split = ["aspect"]
prefer = { scale = [2, 1] }

[report.public]
hosts.mac = { label = "Mac (Metal)", slug = "mac" }
hosts.deck = { label = "Steam Deck (Proton)", slug = "steam-deck" }

[hosts.mac]
kind = "local"
backend = "metal"

[hosts.deck]
kind = "proton"
backend = "d3d11"
"""


def load(tmp_path, text=SCENE, name="scene.toml"):
    p = tmp_path / name
    p.write_text(text)
    return B.load_scene(str(p))


@pytest.fixture
def scene(tmp_path):
    return load(tmp_path)


def flip_log(scene_ms, n=800, t0_us=1_000_000_000):
    t, lines = t0_us, []
    for k in range(1, 101):
        lines.append("[PACING] flip %d t_us %d batches 0" % (k, t))
        t += 20000
    for i in range(n):
        lines.append("[PACING] flip %d t_us %d batches 500" % (101 + i, t))
        t += int(scene_ms[i % len(scene_ms)] * 1000)
    return "\n".join(lines) + "\n"


def make_run(root, scene, host, arm_id, ms, suffix="", info_extra=None):
    d = os.path.join(root, host, arm_id + suffix)
    os.makedirs(d)
    with open(os.path.join(d, "game.log"), "w") as f:
        f.write(flip_log(ms))
    a = B.find_arm(scene, host, arm_id)
    info = B.base_info(scene, dict(scene["hosts"][host], _name=host), a, True, "/none", {}, 0, [])
    info.update(info_extra or {})
    B.write_info(d, info)
    return B.collect(d)


def row(arm, fps, low1=None, hitches=0, verdict="", host="deck", prof=0, **axes):
    r = {
        "host": host,
        "arm": arm,
        "prof": prof,
        "disturbed": False,
        "verdict": verdict,
        "steady_fps": fps,
        "steady_low1_fps": fps if low1 is None else low1,
        "steady_hitches": hitches,
        "steady_p99_ms": 20.0,
        "steady_worst_s_fps": fps,
        "run_dir": "%s/%s" % (host, arm),
    }
    r.update({"axis_" + k: v for k, v in axes.items()})
    return r


# --- D2 -----------------------------------------------------------------------


def test_worst_second_starts_at_the_first_whole_steady_second():
    fps = [10, 20, 55, 60, 58, 61]
    # A 2.5 s warm-up: bucket 2 holds 0.5 s of it, so bucket 3 is first.
    assert B.worst_second(fps, 2.5) == 58
    assert B.worst_second(fps, 2) == 55
    assert B.worst_second(fps[:2], 2) is None
    assert B.worst_second(None, 0) is None


def test_steady_worst_second_column(scene, tmp_path):
    root = str(tmp_path / "out")
    make_run(root, scene, "mac", "s1-43", [16.0])
    rows, _, arms = B.analyse(root)
    assert rows[0]["steady_worst_s_fps"] > 55
    assert "steady_worst_s_fps" in B.COLUMNS
    assert any(k.startswith("steady_worst_s_fps") for k in arms[0])


def test_sampler_group_two_rates():
    rows = [
        row("a", 60, prof=100),
        row("a", 60, prof=0),
        row("b", 60, prof=0),
        row("b", 60, prof=250),
    ]
    assert R.sampler_group(rows)["hz"] == 0
    g = R.sampler_group([rows[0], rows[3]])
    assert g["mixed"] and g["per_arm"] == {"a": 100, "b": 250}


# --- D6 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "edit, msg",
    [
        (('stock = "4:3"\n', ""), "axis aspect has no stock"),
        (("target_fps = 60", "target_fps = -1"), "positive"),
        (('split = ["aspect"]', 'split = ["nope"]'), "no axis 'nope'"),
        (('split = ["aspect"]', 'split = ["aspect"]\nmin_pass = "3/2"'), "not in (0, 1]"),
        (('split = ["aspect"]', 'split = ["aspect"]\nfoo = 1'), "unknown key foo"),
        (('slug = "mac"', 'slug = "Mac!"'), "is not [a-z0-9-]"),
        (('slug = "steam-deck"', 'slug = "mac"'), "slug mac twice"),
        (('display = ["1x", "2x"]', 'display = ["1x"]'), "display and values differ"),
        (("prefer = { scale = [2, 1] }", "prefer = { scale = [3] }"), "not one of its values"),
    ],
)
def test_report_schema_errors(tmp_path, edit, msg):
    with pytest.raises(B.MatrixError, match=re.escape(msg)):
        load(tmp_path, SCENE.replace(*edit, 1))


# --- D4 -----------------------------------------------------------------------


def model_of(scene, rows):
    return R.host_model(scene, "deck", rows, 60, R.steady_minutes(scene))


def test_min_pass_is_a_fraction(scene):
    rows = [
        # 2 of 3 runs hold: qualifies at 2/3.
        row("s2-43", 60, scale=2, aspect="4:3"),
        row("s2-43", 60, scale=2, aspect="4:3"),
        row("s2-43", 40, scale=2, aspect="4:3"),
        # 1 of 2 does not.
        row("s2-169", 60, scale=2, aspect="16:9"),
        row("s2-169", 40, scale=2, aspect="16:9"),
        row("s1-169", 59, scale=1, aspect="16:9"),
        row("s1-43", 59, scale=1, aspect="4:3"),
    ]
    hm = model_of(scene, rows)
    by = {e["id"]: e["judged"] for e in hm["arms"]}
    assert by["s2-43"]["qualifies"] and by["s2-43"]["passes"] == 2
    assert not by["s2-169"]["qualifies"]
    picks = {p["split_display"]: p for p in hm["best"]["picks"]}
    assert picks["4:3"]["arm"] == "s2-43" and not picks["4:3"]["single_run"]
    # 16:9 falls back to the next preference, on a single run.
    assert picks["16:9"]["arm"] == "s1-169" and picks["16:9"]["single_run"]
    assert picks["16:9"]["stock"] == "s1-169"


def test_nothing_holds_names_the_closest(scene):
    rows = [
        row("s1-43", 50, low1=30, scale=1, aspect="4:3"),
        row("s2-43", 40, low1=20, scale=2, aspect="4:3"),
    ]
    p = model_of(scene, rows)["best"]["picks"][0]
    assert p["arm"] is None and p["closest"] == "s1-43"
    assert p["text"].startswith("No tested setting holds 60 fps without frame drops at 4:3.")
    assert "(needs 58.2)" in p["text"] and "(needs 36)" in p["text"]
    # 16:9 has no runs at all.
    assert model_of(scene, rows)["best"]["picks"][1]["text"].startswith("No tested setting")


def test_slow_mode_unrelated_to_settings_judges_good_runs_only(scene):
    rows = []
    for arm, sc, asp in (("s1-43", 1, "4:3"), ("s2-43", 2, "4:3")):
        rows += [row(arm, 60, verdict="good", scale=sc, aspect=asp) for _ in range(4)]
        rows += [row(arm, 42, verdict="bad", scale=sc, aspect=asp) for _ in range(4)]
    best = model_of(scene, rows)["best"]
    assert best["basis"].startswith("good runs only")
    assert best["unstable"]["k"] == 8 and best["unstable"]["n"] == 16
    assert best["unstable"]["lo"] < 0.5 < best["unstable"]["hi"]
    assert best["picks"][0]["arm"] == "s2-43"


def test_slow_mode_tied_to_a_setting(scene):
    rows = []
    rows += [row("s1-43", 60, verdict="good", scale=1, aspect="4:3") for _ in range(8)]
    rows += [row("s2-43", 42, verdict="bad", scale=2, aspect="4:3") for _ in range(7)]
    rows += [row("s2-43", 60, verdict="good", scale=2, aspect="4:3")]
    best = model_of(scene, rows)["best"]
    assert best["tied"]["values"] == {"scale": ["2"]}
    assert "slow mode in 7 of 8 runs at 2x" in best["tied"]["text"]
    # Judged on all its runs, the slow ones failing: s2 does not qualify.
    assert best["picks"][0]["arm"] == "s1-43"


def find_up(rel):
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        p = os.path.join(d, rel)
        if os.path.exists(p):
            return p
        d = os.path.dirname(d)
    return None


TITLE_RUNS = find_up("runs/bench-matrix/jsrf-title")
TITLE_REPORT = """
[report]
target_fps = 60
[report.best]
priority = ["scale", "dsp"]
split = ["aspect"]
prefer = { scale = [4, 3, 2, 1], dsp = [1, 0] }
"""


@pytest.mark.skipif(
    not (TITLE_RUNS and os.path.isfile(os.path.join(TITLE_RUNS, "summary.json"))),
    reason="the jsrf-title runs are not on this machine",
)
def test_title_runs_picks():
    scene = R.find_scene(TITLE_RUNS)
    scene["report"] = tomllib.loads(TITLE_REPORT)["report"]
    for a in scene["axis"]:
        a.setdefault("stock", a["values"][0])
    model, _ = R.build_model(TITLE_RUNS, scene)
    picks = {
        h: [(p.get("arm"), p.get("closest")) for p in hm["best"]["picks"]]
        for h, hm in model["hosts"].items()
    }
    assert picks["deck"] == [("s3-dsp1-43", None), ("s3-dsp1-169", None)]
    assert picks["mac"] == [(None, "s1-dsp0-43"), (None, "s1-dsp1-169")]
    assert picks["windows"] == [("s4-dsp1-43", None), ("s4-dsp1-169", None)]
    u = model["hosts"]["deck"]["best"]["unstable"]
    assert (u["k"], u["n"]) == (18, 36)


# --- D3 -----------------------------------------------------------------------


@pytest.mark.parametrize("pal, bg", [(S.PUBLIC_LIGHT, "#ffffff"), (S.PUBLIC_DARK, "#0d1117")])
def test_public_palette_contrast(pal, bg):
    for k in S.MARK_KEYS:
        assert S.contrast(pal[k], bg) >= 3.0, k
    for k in S.TEXT_KEYS:
        assert S.contrast(pal[k], bg) >= 4.5, k
    for c in pal["hosts"]:
        assert S.contrast(c, bg) >= 3.0, c


# --- the private report (D7) and the public export (D8) ---------------------------


def clean_info(model="Valve Jupiter", fullscreen="1"):
    """A run-info with a plant-free hw block, as a run after D5 records it."""
    return {
        "hw": {
            "schema": 1,
            "device": {"model": model},
            "os": {"name": "SteamOS", "version": "3.7"},
            "cpu": {"model": "AMD Custom APU 0405", "cores": 4, "threads": 8},
            "gpu": [{"model": "AMD Custom GPU 0405"}],
            "ram_gb": 15.6,
            "backend": {"name": "d3d11"},
        },
        "present_fullscreen": fullscreen,
    }


@pytest.fixture
def result(scene, tmp_path):
    root = str(tmp_path / "out")
    for host in ("mac", "deck"):
        make_run(root, scene, host, "s1-43", [16.6], info_extra=clean_info())
        make_run(root, scene, host, "s2-43", [16.0], info_extra=clean_info())
        make_run(root, scene, host, "s2-169", [25.0], info_extra=clean_info())
    B.analyse(root)
    return root


@pytest.fixture
def result_no_hw(scene, tmp_path):
    root = str(tmp_path / "out-nohw")
    for host in ("mac", "deck"):
        make_run(root, scene, host, "s1-43", [16.6])
        make_run(root, scene, host, "s2-43", [16.0])
        make_run(root, scene, host, "s2-169", [25.0])
    B.analyse(root)
    return root


def test_private_report_sections(scene, result, tmp_path):
    model, s = R.build_model(result, scene)
    page = HTML.render(model, scene, s, result)
    for ident in ("verdict", "hosts", "matrix", "range", "detail", "hot", "headroom", "method"):
        assert 'id="%s"' % ident in page, ident
    assert re.findall(r"https?://[^\"' ]+", page) == ["http://www.w3.org/2000/svg"] * len(
        re.findall(r"https?://", page)
    )
    with open(os.path.join(result, "summary.json")) as f:
        rows = json.load(f)["rows"]
    mean = next(r for r in rows if r["host"] == "mac" and r["arm"] == "s2-43")["steady_fps"]
    assert "%.1f" % mean in page
    assert "not recorded" in page


def test_report_cli_writes_html_and_json(scene, result, tmp_path):
    out = str(tmp_path / "rep")
    rc = B.main(["report", result, "--scene", str(tmp_path / "scene.toml"), "--out", out])
    assert rc == 0
    assert os.path.isfile(os.path.join(out, "index.html"))
    with open(os.path.join(out, "report.json")) as f:
        assert json.load(f)["schema"] == 1


def git_repo(tmp_path, readme):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text(readme)
    return str(repo)


MARKED = "# Game\n\n<!-- xbr:bench t begin -->\nold\n<!-- xbr:bench t end -->\n\nTail.\n"


@pytest.fixture
def audit(tmp_path, monkeypatch):
    d = tmp_path / "pubdir"
    d.mkdir()
    (d / "audit-private").write_text("secretbox\n")
    monkeypatch.setenv("XBR_PUBLIC_DIR", str(d))
    monkeypatch.delenv("XBR_AUDIT_PRIVATE", raising=False)
    return d


PLANTS = [
    "myhost-7",
    "/Us" + "ers/someone/",
    "/ho" + "me/someone",
    "someone@example.org",
    "cla" + "ude.ai/chat",
    "C02XK1ABCDEF",
    "a4:83:e7:12:34:56",
    "192.0.2.17",
]


def poisoned_info():
    """The fields a title-scene run-info.json holds, each carrying a plant."""
    return {
        "hw": {
            "schema": 1,
            "device": {"model": "Valve Jupiter", "serial": "C02XK1ABCDEF"},
            "os": {"name": "SteamOS", "version": "3.7", "hostname": "myhost-7"},
            "cpu": {"model": "AMD Custom APU 0405", "cores": 4, "threads": 8},
            "gpu": [{"model": "AMD Custom GPU 0405", "mac": "a4:83:e7:12:34:56"}],
            "net": {"ip": "192.0.2.17"},
            "ram_gb": 15.6,
            "backend": {"name": "d3d11", "translation": "GE-Proton10-25"},
        },
        "build": {
            "version": "20261023.0049-c5d8e7b0-t4ec1a73",
            "game_sha": "c5d8e7b0",
            "toolkit_sha": "4ec1a73",
        },
        "remote_dir": "/ho" + "me/someone/xbr/run",
        "remote_exe": "/Us" + "ers/someone/game.exe",
        "env": {"RECOMP_NOTE": "someone@example.org cla" + "ude.ai/chat"},
        "busy_during": [],
        "present_fullscreen": "1",
        "refresh_hz": 60,
    }


def test_public_export_allow_list(scene, tmp_path, audit):
    root = str(tmp_path / "out")
    for arm in ("s1-43", "s2-43", "s2-169"):
        make_run(root, scene, "deck", arm, [16.0], info_extra=poisoned_info())
        make_run(root, scene, "mac", arm, [16.0], info_extra=clean_info("Mac mini"))
    B.analyse(root)
    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(root, scene)
    code, written = P.export(model, scene, repo)
    assert code == 0
    names = sorted(os.path.relpath(w, repo) for w in written)
    assert "README.md" in names
    assert "docs/images/bench/t/steam-deck-light.svg" in names
    text = "".join(open(w, encoding="utf-8").read() for w in written)
    for p in PLANTS:
        assert p not in text, p
    assert "Tail." in text and "old" not in text.split("xbr:bench t begin")[1]
    # The public build is the date and sequence only.
    assert "20261023.0049" in text and "c5d8e7b0" not in text
    assert "GE-Proton10-25" in text and "fullscreen" in text


def test_public_export_template_plant_fails(scene, result, tmp_path, audit):
    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(result, scene)
    model["hosts"]["mac"]["label"] = "x"
    scene["report"]["public"]["hosts"]["mac"]["label"] = "Mac, see someone@example.org"
    with pytest.raises(P.LeakError):
        P.export(model, scene, repo)
    assert open(os.path.join(repo, "README.md")).read() == MARKED
    assert not os.path.exists(os.path.join(repo, "docs"))


@pytest.mark.parametrize(
    "label, leaks",
    [
        ("Steam Deck (Proton)", False),
        ("Deck results", False),
        ("de" + "ck:~/x", True),
        ("run via ssh de" + "ck", True),
        ("logs at /deck/", True),
        ("Mac secretbox", True),
    ],
)
def test_public_labels(scene, result, tmp_path, audit, label, leaks):
    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(result, scene)
    scene["report"]["public"]["hosts"]["deck"]["label"] = label
    if leaks:
        with pytest.raises(P.LeakError):
            P.export(model, scene, repo)
    else:
        assert P.export(model, scene, repo)[0] == 0


def test_public_export_without_markers(scene, result, tmp_path, audit, capsys):
    repo = git_repo(tmp_path, "# Game\n")
    rc = B.main(
        ["report", result, "--scene", str(tmp_path / "scene.toml"), "--public", repo]
        + ["--out", str(tmp_path / "rep")]
    )
    assert rc == 1
    assert "### Performance: Test scene" in capsys.readouterr().out
    assert open(os.path.join(repo, "README.md")).read() == "# Game\n"
    assert not os.path.exists(os.path.join(repo, "docs"))


def test_public_export_without_patterns(scene, result, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XBR_PUBLIC_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("XBR_AUDIT_PRIVATE", raising=False)
    repo = git_repo(tmp_path, MARKED)
    rc = B.main(
        ["report", result, "--scene", str(tmp_path / "scene.toml"), "--public", repo]
        + ["--out", str(tmp_path / "rep")]
    )
    assert rc == 2
    assert open(os.path.join(repo, "README.md")).read() == MARKED


def test_private_patterns_cannot(tmp_path, monkeypatch):
    monkeypatch.setenv("XBR_PUBLIC_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("XBR_AUDIT_PRIVATE", raising=False)
    repo = git_repo(tmp_path, "")
    with pytest.raises(public.Cannot):
        P.leak_patterns(repo, ["deck"])


@pytest.mark.parametrize("rule", ["Steam Deck\tHandheld\n", "!fail Proton\\)\n", ""])
def test_public_export_meets_no_rewrite_rule(scene, result, tmp_path, audit, rule):
    # `public export` rewrites with these rules and fails on the checks: the
    # fragment and the SVGs must pass it unchanged (D8, task 4.3).
    (audit / "rewrite").write_text("# rules\n" + rule)
    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(result, scene)
    if rule:
        with pytest.raises(P.LeakError, match="rewrite"):
            P.export(model, scene, repo)
    else:
        assert P.export(model, scene, repo)[0] == 0


def test_public_paths_pass_the_export_refusals(scene, result, tmp_path, audit):
    from xboxrecomp_cli import layout

    class Game:
        m = {"data": {"game_files": "game_files"}, "pipeline": {"gen": "src/recomp/gen"}}

    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(result, scene)
    _, written = P.export(model, scene, repo)
    for w in written:
        rel = os.path.relpath(w, repo).replace(os.sep, "/")
        assert not layout.private(Game, rel), rel
        assert not layout.refused_path(Game, rel), rel
        assert not layout.data_ext(rel), rel
        assert not layout.binary(open(w, "rb").read()), rel


# --- review fixes ---------------------------------------------------------------


def test_public_export_refuses_a_host_without_hw(scene, result_no_hw, tmp_path, audit):
    """D5: runs from before the hw block cannot publish their host."""
    repo = git_repo(tmp_path, MARKED)
    model, _ = R.build_model(result_no_hw, scene)
    with pytest.raises(P.Refused, match="no hardware recorded"):
        P.export(model, scene, repo)
    assert open(os.path.join(repo, "README.md")).read() == MARKED
    assert not os.path.exists(os.path.join(repo, "docs"))
    rc = B.main(
        ["report", result_no_hw, "--scene", str(tmp_path / "scene.toml"), "--public", repo]
        + ["--out", str(tmp_path / "rep")]
    )
    assert rc == 1


def test_public_best_row_shows_the_judged_figures(scene):
    """On a host with a slow start mode the pick is judged on its good runs:
    the best row shows those, not the medians over every run."""
    rows = []
    for arm, sc in (("s1-43", 1), ("s2-43", 2)):
        rows += [row(arm, 60, verdict="good", scale=sc, aspect="4:3") for _ in range(4)]
        rows += [row(arm, 42, verdict="bad", scale=sc, aspect="4:3") for _ in range(4)]
    hm = model_of(scene, rows)
    assert hm["best"]["picks"][0]["arm"] == "s2-43"
    h = {
        "label": "Deck",
        "arms": [P.public_arm(e) for e in hm["arms"]],
        "best": hm["best"],
        "sampler": {"mixed": False, "hz": 250},
    }
    out, _ = P.table_rows({"target_fps": 60}, h, False)
    best = next(r for r in out if r[1].startswith("best"))
    assert best[3] == "60.0" and best[4] == "60"
    assert best[6] == "4 of 8"
    # The stock row keeps every run's medians.
    stock = next(r for r in out if r[1].startswith("stock"))
    assert stock[3] == "51.0" and stock[6] == "8"


def test_closest_arm_passing_on_medians_says_why_it_misses(scene):
    rows = [row("s2-43", fps, scale=2, aspect="4:3") for fps in (60, 60, 60, 40, 40)]
    p = model_of(scene, rows)["best"]["picks"][0]
    assert p["arm"] is None and p["closest"] == "s2-43"
    assert "fps average (needs" not in p["text"] and "low 60 (needs" not in p["text"]
    assert "holds on the medians, but only 3 of 5 runs pass (needs 4)" in p["text"]


def drop_window_field(root):
    """Make runs look like ones from before present_fullscreen and the run block."""
    for dirpath, _, files in os.walk(root):
        for name in ("run.json", "run-info.json"):
            if name in files:
                p = os.path.join(dirpath, name)
                with open(p) as f:
                    info = json.load(f)
                info.pop("present_fullscreen", None)
                info.pop("run", None)
                with open(p, "w") as f:
                    json.dump(info, f)


def test_baseline_window_mode_caveats_the_deltas(scene, result, tmp_path, audit):
    """A baseline from before present_fullscreen against a fullscreen rerun:
    compare() alone cannot see the change, so the host-level check marks it."""
    base = str(tmp_path / "base")
    for host in ("mac", "deck"):
        for arm, ms in (("s1-43", 16.6), ("s2-43", 16.0), ("s2-169", 25.0)):
            make_run(base, scene, host, arm, [ms], info_extra=clean_info())
    drop_window_field(base)
    B.analyse(base)
    model, s = R.build_model(result, scene, baseline=base)
    bh = model["baseline"]["hosts"]["deck"]
    assert bh["present_differs"] and bh["window"] == {
        "baseline": ["unknown"],
        "now": ["fullscreen"],
    }
    deltas = [e["delta"] for e in model["hosts"]["deck"]["arms"] if e.get("delta")]
    assert deltas and all(d["present_differs"] for d in deltas)
    page = HTML.render(model, scene, s, result)
    assert R.PRESENT_MARK in page and "the window mode differs" in page
    repo = git_repo(tmp_path, MARKED)
    code, written = P.export(model, scene, repo)
    assert code == 0
    readme = open(os.path.join(repo, "README.md"), encoding="utf-8").read()
    assert R.PRESENT_MARK in readme and "the window mode differs" in readme


def test_baseline_same_window_mode_has_no_caveat(scene, result, tmp_path):
    base = str(tmp_path / "base")
    for host in ("mac", "deck"):
        make_run(base, scene, host, "s2-43", [16.0], info_extra=clean_info())
    B.analyse(base)
    model, _ = R.build_model(result, scene, baseline=base)
    assert not model["baseline"]["hosts"]["deck"]["present_differs"]
    d = next(e["delta"] for e in model["hosts"]["deck"]["arms"] if e.get("delta"))
    assert not d.get("present_differs")


def test_a_host_gone_from_the_scene_is_left_out(scene, result, tmp_path):
    del scene["hosts"]["deck"]
    del scene["report"]["public"]["hosts"]["deck"]
    model, s = R.build_model(result, scene)
    assert list(model["hosts"]) == ["mac"]
    assert model["hosts_not_in_scene"] == ["deck"]
    page = HTML.render(model, scene, s, result)
    assert "not in the scene: deck" in page


def test_radar_desc_names_the_pick(scene, result):
    model, _ = R.build_model(result, scene)
    svg = S.radar(model, "mac", S.PUBLIC_LIGHT, "r")
    desc = re.search(r"<desc[^>]*>(.*?)</desc>", svg).group(1)
    assert "arms run hold" not in desc
    picks = [p for p in model["hosts"]["mac"]["best"]["picks"] if p.get("arm")]
    if picks:
        assert "Best: " in desc
    else:
        assert "No tested setting holds the target." in desc


def test_radar_desc_with_a_pick(scene):
    rows = [row("s2-43", 60, scale=2, aspect="4:3") for _ in range(3)]
    rows += [row("s1-169", 30, scale=1, aspect="16:9")]
    hm = model_of(scene, rows)
    hm["label"] = "Deck"
    model = {"target_fps": 60, "hosts": {"deck": hm}}
    svg = S.radar(model, "deck", S.PUBLIC_LIGHT, "r")
    desc = re.search(r"<desc[^>]*>(.*?)</desc>", svg).group(1)
    assert desc.startswith("Target 60 fps. Best: 2x")
    assert "at 4:3" in desc and "arms run hold" not in desc


def test_private_page_never_prints_build_none(scene, result_no_hw):
    model, s = R.build_model(result_no_hw, scene)
    page = HTML.render(model, scene, s, result_no_hw)
    assert "build None" not in page and "None" not in re.sub(r"<[^>]+>", " ", page)
