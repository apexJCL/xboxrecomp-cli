"""benchdb plan and clean (design D11), against a temp workspace: every
guard's refusal, the pins over every worktree, the intersection with a
fresh plan, the journal, and the host scripts (the POSIX apply run on a
temp tree through shims, the PowerShell one checked by its text). Hosts
are faked at clean.run_host; nothing here reaches ssh or a real tree."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import pytest

from xboxrecomp_cli.benchdb import clean, cli, config, refs, store
from xboxrecomp_cli.benchdb.store import DbError

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDATA_GAME = os.path.join(HERE, "testdata", "game")
DAY = 86400
NOW = time.time()
OLD = NOW - 30 * DAY

TOML = r"""
db = "bench-logs/bench.db"

[games]
demo = "demo"

[refs]
paths = ["notes/refs"]
exclude = ["notes/refs/export"]

[roots.store]
path = "bench-logs"
kind = "store"
pattern = ['[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?']

[roots.runs]
path = "runs"
kind = "runs"
pattern = ['[A-Za-z0-9_.-]+']

[roots.wt]
path = "wt"
kind = "worktrees"
pattern = ['[A-Za-z0-9_.-]+']

[roots.scratch]
path = "scratch"
kind = "scratch"
pattern = ['[a-z0-9-]+/scratchpad']

[roots.notes]
path = "notes"
kind = "notes"

[roots.game]
path = "demo"
kind = "checkout"

[hosts.hostA]
os = "linux-proton"
ssh = "hosta"

[hosts.hostB]
os = "windows"
ssh = "hostb"

[roots.hosta]
host = "hostA"
path = "/srv/u/xbr-bench/demo/runs"
kind = "host-scratch"
pattern = ['[A-Za-z0-9_.-]+']
lock = "flock"

[roots.hosttmp]
host = "hostA"
path = "/srv/u/tmp"
kind = "host-scratch"
pattern = ['[a-z0-9-]+-bench']
own = true

[roots.pc]
host = "hostB"
path = 'D:\xbr'
kind = "host-scratch"
pattern = ['[a-z0-9-]+-bench']
lock = "bench-lock"

[roots.pcruns]
host = "hostB"
path = 'D:\xbr\bench\demo\runs'
kind = "host-scratch"
pattern = ['[A-Za-z0-9_.-]+']
lock = "bench-lock"

[never]
local = ["runs/keep-me"]
hostA = ["/srv/u/xbr-bench/demo/runs/base"]
"""


def git(d, *args):
    return subprocess.run(
        ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"),
    )


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def age(top, t=OLD):
    """Every mtime under top set to t, links by their own lstat."""
    paths = [top]
    if os.path.isdir(top) and not os.path.islink(top):
        for dp, dirs, files in os.walk(top, followlinks=False):
            paths += [os.path.join(dp, n) for n in dirs + files]
    for p in reversed(paths):
        os.utime(p, (t, t), follow_symlinks=False)


class WS:
    """The temp workspace: its dir, Config, db and the paths of its roots."""

    def __init__(self, d, monkeypatch):
        self.d = d
        self.game = os.path.join(d, "demo")
        os.makedirs(self.game)
        with open(os.path.join(TESTDATA_GAME, "game.toml")) as f:
            toml = f.read()
        toml = toml.replace('slug = "blinx2"', 'slug = "demo"')
        write(os.path.join(self.game, "game.toml"), toml)
        write(os.path.join(self.game, "analysis/golden/golden.json"), '{"scenarios": {}}\n')
        write(os.path.join(self.game, "analysis/golden/audio.json"), "{}\n")
        write(os.path.join(self.game, "TASKS.md"), "# tasks\n")
        write(os.path.join(self.game, "openspec/changes/x/tasks.md"), "- none\n")
        write(
            os.path.join(self.game, "config/seed_functions.json"),
            '[{"addr": "0x10000", "observed": true, "source": "bench 20261001-100003"}]\n',
        )
        write(os.path.join(self.game, "config/spin_waits.json"), "[]\n")
        git(self.game, "init", "-q", "-b", "main")
        git(self.game, "add", "-A")
        git(self.game, "commit", "-q", "-m", "x")
        for n in ("bench-logs", "runs", "wt", "scratch", "notes/refs/export", "outside"):
            os.makedirs(os.path.join(d, n), exist_ok=True)
        write(os.path.join(d, "notes/refs/log.md"), "kept 20261001-100005 for the A/B\n")
        write(os.path.join(d, "notes/refs/export/runs.jsonl"), '{"stamp": "20261001-100004"}\n')
        write(os.path.join(d, config.NAME), TOML)
        self.cfg = config.load(os.path.join(d, config.NAME))
        monkeypatch.setattr(store, "FS_CHECK", lambda p: (True, ""))
        self.db = store.open(cfg=self.cfg)
        self.store = os.path.join(d, "bench-logs")
        self.runs = os.path.join(d, "runs")

    def p(self, rel):
        return os.path.join(self.d, rel)

    def plan(self, *targets, **kw):
        return clean.make_plan(self.cfg, self.db, targets, **kw)

    def why(self, plan, rel):
        p = rel if os.path.isabs(rel) or ":" in rel else self.p(rel)
        hits = [r["why"] for r in plan["refused"] if r["path"] == p]
        return hits[0] if hits else None

    def planned(self, plan, host="local"):
        return sorted(
            os.path.relpath(i["path"], self.d) if host == "local" else i["path"]
            for i in plan["items"]
            if i["host"] == host
        )

    def saved(self, *targets, **kw):
        plan = self.plan(*targets, **kw)
        pid, _p = clean.save_plan(self.cfg, plan)
        return pid, plan

    def clean(self, pid, **kw):
        lines = []
        out = clean.clean(self.cfg, self.db, pid, say=lines.append, **kw)
        return out, lines

    def run_row(self, rel, status="unparsed", exit_code=0, build=None, started=None):
        with self.db.write() as w:
            return w.run(
                "demo",
                "store",
                rel,
                rel[:15],
                "golden",
                status=status,
                exit_code=exit_code,
                build=build,
                started=started or "2026-10-01T10:00:00Z",
                scenario="menu",
                host=None,
                backend="d3d11",
            )

    def store_run(self, name, exit_code=True, files=("game-stdio.log",), info="host: x\n"):
        d = os.path.join(self.store, name)
        write(os.path.join(d, "run-info.txt"), info)
        if exit_code:
            write(os.path.join(d, "exit-code"), "0\n")
        for f in files:
            write(os.path.join(d, f), "x\n")
        age(d)
        return d

    def worktree(self, name, branch=None):
        top = self.p("wt/" + name)
        os.makedirs(top)
        git(self.game, "worktree", "add", "-q", "-b", branch or name, os.path.join(top, "demo"))
        return top


@pytest.fixture
def ws(tmp_path, monkeypatch):
    d = os.path.realpath(str(tmp_path / "ws"))
    os.makedirs(d)
    monkeypatch.setenv("BENCHDB_AGENT", "tester")
    monkeypatch.setattr(clean, "run_host", NoHost())
    return WS(d, monkeypatch)


class NoHost:
    def __call__(self, h, cmd_stdin, timeout=clean.LIST_TIMEOUT):
        raise AssertionError("no host is reached in this test")


class FakeHost:
    """run_host's stand-in: the listing a host's list script would print,
    and the lines of its apply; it records every (host, command, stdin)."""

    def __init__(self, listing="", apply=None, rc=0, apply_rc=0):
        self.listing, self.apply, self.rc, self.apply_rc = listing, apply, rc, apply_rc
        self.calls = []

    def __call__(self, h, cmd_stdin, timeout=clean.LIST_TIMEOUT):
        cmd, script = cmd_stdin
        self.calls.append((h.key, cmd, script, timeout))
        is_apply = "$Items =" in script if h.os == "windows" else "\nP=(" in script
        if is_apply:
            if self.apply is not None:
                return self.apply_rc, self.apply(script)
            return self.apply_rc, ""
        return self.rc, self.listing


def listing(*lines, uid="501", now=NOW, home="/srv/u"):
    head = ["H\t%s\t%d" % (uid, now), "Z\t%s" % home]
    return "\n".join(head + list(lines)) + "\n"


def entry(path, kind="d", uid="501", mtime=OLD, kb=4, real=None):
    return "E\t%s\t%s\t%s\t%s\t%s\t%d\t%d" % (
        path,
        path,
        "-" if kind == "l" else (real or path),
        kind,
        uid,
        mtime,
        kb,
    )


HOSTA_ROOTS = (
    "T\t0\t/srv/u/xbr-bench/demo/runs\t1000000\t500000",
    "T\t1\t/srv/u/tmp\t1000000\t500000",
)


# ---- guards 1, 2 and 5: the local refusals ----------------------------------------


def test_every_local_refusal_names_its_rule(ws):
    write(ws.p("outside/ro/f"))
    write(ws.p("notes/x/a.md"))
    write(ws.p("runs/keep-me/a"))
    age(ws.p("runs/keep-me"))
    write(ws.p("runs/fresh/a"))
    write(ws.p("bench-logs/topic/a"))
    plan = ws.plan(
        ws.p("outside/ro"),
        ws.p("runs"),
        ws.p("runs/keep-me"),
        ws.p("runs/fresh"),
        ws.p("runs/../outside/ro"),
        ws.p("runs/nothing"),
        ws.p("runs/a b"),
        ws.p("notes/x"),
        ws.p("demo/TASKS.md"),
        ws.p("bench-logs/bench.db"),
        ws.p("bench-logs/topic"),
    )
    assert plan["items"] == []
    assert ws.why(plan, "outside/ro") == "outside the deletable roots"
    assert ws.why(plan, "runs") == "the root runs itself"
    assert ws.why(plan, "runs/keep-me").startswith("under or over the never-path")
    assert ws.why(plan, "runs/fresh") == "written within 2 days"
    assert ws.why(plan, "runs/../outside/ro") == "a .. in the path"
    assert ws.why(plan, "runs/nothing") == "does not exist"
    assert ws.why(plan, "runs/a b") in (
        "does not exist",
        "a b does not match the runs root's pattern",
    )
    assert ws.why(plan, "notes/x") == "under the reference-only root notes (notes)"
    assert ws.why(plan, "demo/TASKS.md") == "under the reference-only root game (checkout)"
    assert ws.why(plan, "bench-logs/bench.db") == "the store's own bench.db"
    assert ws.why(plan, "bench-logs/topic") == "topic does not match the store root's pattern"


def test_pattern_and_age_with_a_real_name(ws):
    write(ws.p("runs/a b/x"))
    age(ws.p("runs/a b"))
    plan = ws.plan(ws.p("runs/a b"))
    assert ws.why(plan, "runs/a b") == "a b does not match the runs root's pattern"
    write(ws.p("runs/topic/x"))
    age(ws.p("runs/topic"), NOW - 3 * DAY)
    assert ws.planned(ws.plan(ws.p("runs/topic"))) == ["runs/topic"]
    # --older-than moves guard 5 for every root.
    assert ws.planned(ws.plan(ws.p("runs/topic"), older_than=5)) == []


def test_a_parent_through_a_link_is_judged_by_its_real_path(ws):
    write(ws.p("outside/ro/f"))
    age(ws.p("outside"))
    os.symlink(ws.p("outside"), ws.p("runs/out"))
    plan = ws.plan(ws.p("runs/out/ro"))
    assert ws.why(plan, "runs/out/ro") == "outside the deletable roots"


def test_worktree_and_scratch_roots_need_explicit_paths(ws):
    plan = ws.plan("wt", "scratch", "notes")
    assert ws.why(plan, "wt") == "worktrees roots are cleaned by explicit path only"
    assert ws.why(plan, "scratch") == "scratch roots are cleaned by explicit path only"
    assert ws.why(plan, "notes") == "the notes root notes is reference-only"


def test_scratch_pattern_is_matched_on_the_whole_path(ws):
    write(ws.p("scratch/sess-1/scratchpad/a.log"))
    age(ws.p("scratch/sess-1"))
    plan = ws.plan(ws.p("scratch/sess-1"), ws.p("scratch/sess-1/scratchpad"))
    assert ws.why(plan, "scratch/sess-1") == "sess-1 does not match the scratch root's pattern"
    assert ws.planned(plan) == ["scratch/sess-1/scratchpad"]


# ---- guard 3: the pins -------------------------------------------------------------


def test_pins_over_every_worktree_seed_hhmmss_and_refs(ws):
    top = ws.worktree("agent1")
    wt_tasks = os.path.join(top, "demo", "TASKS.md")
    write(wt_tasks, "# tasks\n- golden 20261001-100001 shows the fade\n- see 100002\n")
    git(os.path.join(top, "demo"), "commit", "-qam", "cite")
    names = ["20261001-1000%02d" % i for i in range(1, 8)]
    for n in names:
        ws.store_run(n)
        ws.run_row(n)
    plan = ws.plan()
    assert "TASKS.md cites 20261001-100001" in ws.why(plan, "bench-logs/" + names[0])
    assert "cites 100002 (HHMMSS)" in ws.why(plan, "bench-logs/" + names[1])
    assert "seed_functions.json cites 20261001-100003" in ws.why(plan, "bench-logs/" + names[2])
    # An export under [refs] exclude pins nothing; the rest of [refs] does.
    assert "log.md cites 20261001-100005" in ws.why(plan, "bench-logs/" + names[4])
    assert ws.planned(plan) == sorted("bench-logs/" + n for n in (names[3], names[5], names[6]))


def test_a_missing_required_ref_refuses_the_whole_plan(ws):
    os.unlink(os.path.join(ws.game, "TASKS.md"))
    with pytest.raises(DbError, match="reference sources missing"):
        ws.plan()


def test_a_game_without_tasks_or_openspec_still_plans(ws):
    """Only some games keep a TASKS.md or openspec/: one whose repo never
    tracked them is read without them, or with an untracked one on the disk;
    a tracked one gone still refuses."""
    git(ws.game, "rm", "-q", "-r", "TASKS.md", "openspec")
    git(ws.game, "commit", "-q", "-m", "no tasks")
    ws.plan()
    # One kept out of git is still read.
    write(ws.p("runs/20261001-100001/a.png"))
    age(ws.p("runs/20261001-100001"))
    write(os.path.join(ws.game, "TASKS.md"), "kept 20261001-100001\n")
    assert "TASKS.md cites 20261001-100001" in ws.why(
        ws.plan(ws.p("runs/20261001-100001")), "runs/20261001-100001"
    )
    git(ws.game, "add", "TASKS.md")
    git(ws.game, "commit", "-q", "-m", "tasks again")
    os.unlink(os.path.join(ws.game, "TASKS.md"))
    with pytest.raises(DbError, match="reference sources missing"):
        ws.plan()


def test_a_failing_worktree_list_refuses_the_whole_plan(ws):
    os.rename(os.path.join(ws.game, ".git"), os.path.join(ws.game, ".git-off"))
    with pytest.raises(DbError, match="cannot list the worktrees"):
        ws.plan()


def test_a_missing_refs_path_refuses_the_whole_plan(ws):
    shutil.rmtree(ws.p("notes/refs"))
    with pytest.raises(DbError, match="reference sources missing"):
        ws.plan()


def test_a_worktree_named_in_the_text_is_pinned(ws):
    top = ws.worktree("agent2")
    age(top)
    write(ws.p("notes/refs/handoff.md"), "resume from wt/agent2 tomorrow\n")
    plan = ws.plan(top)
    assert "handoff.md names wt/agent2" in ws.why(plan, "wt/agent2")


def test_a_bare_name_pins_nothing_but_a_path_to_it_does(ws, monkeypatch):
    """A topic or scratch name is a word of prose (ssx3, cli): only a path
    to it pins, from the root, the workspace, in full or from ~ on a host."""
    for n in ("ssx3", "free-cam"):
        write(ws.p("runs/%s/a.png" % n))
        age(ws.p("runs/" + n))
    write(ws.p("notes/refs/prose.md"), "the ssx3 menu fades; x-bench and y-bench are cold\n")
    write(ws.p("notes/refs/cite.md"), "frames in runs/free-cam/ for the A/B\n")
    plan = ws.plan(ws.p("runs/ssx3"), ws.p("runs/free-cam"))
    assert ws.planned(plan) == ["runs/ssx3"]
    assert "cite.md names runs/free-cam" in ws.why(plan, "runs/free-cam")
    lines = HOSTA_ROOTS + tuple(entry("/srv/u/tmp/%s-bench" % n) for n in ("x", "y", "z", "w"))
    monkeypatch.setattr(clean, "run_host", FakeHost(listing(*lines)))
    write(ws.p("notes/refs/host.md"), "BENCH_DIR=/srv/u/tmp/z-bench, then ~/tmp/w-bench\n")
    plan = ws.plan("hosttmp")
    assert ws.planned(plan, "hostA") == ["/srv/u/tmp/x-bench", "/srv/u/tmp/y-bench"]
    assert "names /srv/u/tmp/z-bench" in ws.why(plan, "/srv/u/tmp/z-bench")
    assert "names ~/tmp/w-bench" in ws.why(plan, "/srv/u/tmp/w-bench")


def test_a_root_name_wins_over_a_cwd_entry(ws, monkeypatch):
    """plan runs from the workspace sweeps the runs root; ./runs is the
    path."""
    write(ws.p("runs/old/a.png"))
    age(ws.p("runs/old"))
    monkeypatch.chdir(ws.d)
    assert clean.split_targets(ws.cfg, ["runs", "./runs"]) == (["runs"], ["./runs"], {})
    assert ws.planned(ws.plan("runs")) == ["runs/old"]
    assert ws.why(ws.plan("./runs"), "runs") == "the root runs itself"


def test_a_never_path_on_a_host_is_reported_as_one(ws, monkeypatch):
    """A never-path the pattern would also refuse says never, not pattern."""
    lines = HOSTA_ROOTS + (entry("/srv/u/tmp/keep_me"),)
    monkeypatch.setattr(clean, "run_host", FakeHost(listing(*lines)))
    ws.cfg.never["hostA"] = ["/srv/u/tmp/keep_me"]
    plan = ws.plan("hosttmp")
    assert ws.why(plan, "/srv/u/tmp/keep_me") == "under or over the never-path /srv/u/tmp/keep_me"
    write(ws.p("runs/Keep Me/a.png"))
    age(ws.p("runs/Keep Me"))
    ws.cfg.never["local"] = [ws.p("runs/Keep Me")]
    plan = ws.plan(ws.p("runs/Keep Me"))
    assert ws.why(plan, "runs/Keep Me").startswith("under or over the never-path")


def test_a_host_run_dir_pins_by_the_endings_of_its_path(ws, monkeypatch):
    """A Deck run dir is cited as runs/<sha>, <game>/runs/<x> or
    xbr-bench/<game>/runs/<x>, and a build sha alone pins its dir (in full
    where the dir is a short sha). Any other bare word still pins nothing."""
    base = "/srv/u/xbr-bench/demo/runs/"
    names = ("3ae6eaaf5407", "abc1234def", "task-a", "task-b", "plainword", "task-c")
    lines = HOSTA_ROOTS + tuple(entry(base + n) for n in names)
    monkeypatch.setattr(clean, "run_host", FakeHost(listing(*lines)))
    write(
        ws.p("notes/refs/deck.md"),
        "golden at runs/3ae6eaaf5407 passed; build abc1234def0123456789abcd shipped\n"
        "frames in xbr-bench/demo/runs/task-a and demo/runs/task-b; plainword\n",
    )
    plan = ws.plan("hosta")
    assert ws.planned(plan, "hostA") == [base + "plainword", base + "task-c"]
    assert "names runs/3ae6eaaf5407" in ws.why(plan, base + "3ae6eaaf5407")
    assert "names abc1234def" in ws.why(plan, base + "abc1234def")
    assert "names xbr-bench/demo/runs/task-a" in ws.why(plan, base + "task-a")
    assert "names demo/runs/task-b" in ws.why(plan, base + "task-b")


def test_a_windows_run_dir_pins_by_its_backslash_paths(ws, monkeypatch):
    r"""runs\<task>, the full D:\ path (in any case) and its / form all pin."""
    base = "D:\\xbr\\bench\\demo\\runs\\"
    lines = ("T\t0\tD:\\xbr\t1000000\t500000", "T\t1\tD:\\xbr\\bench\\demo\\runs\t1000000\t500000")
    lines += tuple(entry(base + n) for n in ("t-one", "t-two", "t-three", "t-four"))
    lines += (entry("D:\\xbr\\a-bench"), entry("D:\\xbr\\b-bench"))
    monkeypatch.setattr(clean, "run_host", FakeHost(listing(*lines)))
    write(
        ws.p("notes/refs/pc.md"),
        "Runs\\t-one; d:\\XBR\\bench\\demo\\runs\\t-two; D:/xbr/bench/demo/runs/t-three\n"
        "and D:\\xbr\\a-bench\n",
    )
    plan = ws.plan("pc", "pcruns")
    assert ws.planned(plan, "hostB") == ["D:\\xbr\\b-bench", base + "t-four"]
    assert "names runs\\t-one" in ws.why(plan, base + "t-one")
    assert "names D:\\xbr\\bench\\demo\\runs\\t-two" in ws.why(plan, base + "t-two")
    assert "names D:/xbr/bench/demo/runs/t-three" in ws.why(plan, base + "t-three")
    assert "names D:\\xbr\\a-bench" in ws.why(plan, "D:\\xbr\\a-bench")


def test_a_topic_dir_kept_for_nested_pins_keeps_only_those(ws):
    """The user, 2026-10-25: runs/fork-prs, kept only for the one run a note
    cites, keeps that run; its other children go one by one under their own
    guards (young here), and the topic dir stays until it is empty. Cited as
    a whole, it stays whole."""
    top = "runs/fork-prs/"
    for rel in (
        "mac-r1/golden-skates/20261024-221209-metal-x/a.png",
        "mac-r1/golden-skates/20261024-221500-metal-x/a.png",
        "mac-r1/other/20261024-200000-metal/a.png",
        "mac-r1/notes.txt",
        "pc-r2/x.png",
        "deck-cold/bench.log",
        "deck-cold/bench-logs/20261024-200829/run-info.txt",
    ):
        write(ws.p(top + rel))
    age(ws.p("runs/fork-prs"))
    write(ws.p(top + "fresh/x.png"))
    write(ws.p("notes/refs/perf.md"), "the skates golden at 221209; cold at 200829\n")
    for plan in (ws.plan(ws.p("runs/fork-prs")), ws.plan()):
        assert ws.planned(plan) == [
            top + "mac-r1/golden-skates/20261024-221500-metal-x",
            top + "mac-r1/notes.txt",
            top + "mac-r1/other",
            top + "pc-r2",
        ]
        for d in ("runs/fork-prs", top + "mac-r1", top + "mac-r1/golden-skates"):
            assert ws.why(plan, d).startswith("kept until empty: ")
        assert "perf.md cites 221209" in ws.why(
            plan, top + "mac-r1/golden-skates/20261024-221209-metal-x"
        )
        assert ws.why(plan, top + "fresh").startswith("written within")
        # One bench's output dir stays whole: its bench.log tells the run.
        assert "perf.md cites 200829" in ws.why(plan, top + "deck-cold")
    write(ws.p("notes/refs/topic.md"), "the PR frames are in runs/fork-prs\n")
    plan = ws.plan(ws.p("runs/fork-prs"))
    assert ws.planned(plan) == []
    assert "topic.md names runs/fork-prs" in ws.why(plan, "runs/fork-prs")


def test_a_saved_plan_table_is_not_a_reference(ws):
    """benchdb plan > notes/x.txt names every path it weighed; read as a
    reference it would pin them all. A note quoting one row still pins."""
    write(ws.p("runs/20261001-100006/a.png"))
    age(ws.p("runs/20261001-100006"))
    lines = []
    clean.table(ws.plan(ws.p("runs/20261001-100006")), lines.append)
    write(ws.p("notes/refs/plan-dry.txt"), "\n".join(lines) + "\n")
    assert refs.is_listing("\n".join(lines))
    plan = ws.plan(ws.p("runs/20261001-100006"))
    assert ws.planned(plan) == ["runs/20261001-100006"]
    assert any("notes/refs/plan-dry.txt" in w for w in plan["warnings"])
    write(ws.p("notes/refs/plan-dry.txt"), "keep this one:\n" + "\n".join(lines) + "\n")
    assert ws.why(ws.plan(ws.p("runs/20261001-100006")), "runs/20261001-100006").startswith(
        "pinned: "
    )


def test_a_plan_id_pins_nothing():
    pid = clean.plan_id()
    assert clean.PLAN_ID_RE.match(pid)
    assert not refs.STAMP_RE.search(pid) and not refs.SIX_RE.search(pid)


def test_a_case_or_normalisation_variant_is_refused(ws):
    d = ws.runs + "/B3-Fizz"
    write(d + "/x.png", "x")
    nfc = ws.p("notes/caf\u00e9")
    write(nfc + "/x.md", "x")
    age(d)
    if not os.path.exists(ws.runs + "/b3-fizz"):
        pytest.skip("a case-sensitive file system: no variant to spell")
    assert ws.why(ws.plan(ws.runs + "/b3-fizz"), "runs/b3-fizz") == "spelled %s on disk" % d
    nfd = ws.p("notes/cafe\u0301")
    assert ws.why(ws.plan(nfd), nfd) == "spelled %s on disk" % nfc
    assert ws.planned(ws.plan(d)) == ["runs/B3-Fizz"]


# ---- guard 4: worktrees ------------------------------------------------------------


def test_worktree_with_untracked_notes_then_copied(ws):
    top = ws.worktree("agent3")
    write(os.path.join(top, "demo", "resume.md"), "where I stopped\n")
    age(top)
    plan = ws.plan(top)
    assert ws.why(plan, "wt/agent3") == "untracked files not under notes/agent3/: demo/resume.md"
    write(ws.p("notes/agent3/resume.md"), "where I stopped\n")
    pid, plan = ws.saved(top)
    assert ws.planned(plan) == ["wt/agent3"]
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1 and not os.path.lexists(top)
    listed = git(ws.game, "worktree", "list", "--porcelain").stdout.decode()
    assert "agent3" not in listed
    # The branch stays.
    assert git(ws.game, "rev-parse", "--verify", "agent3").returncode == 0


def test_a_failed_worktree_remove_is_journalled(ws):
    top = ws.worktree("agent8")
    # A locked worktree refuses one --force: the rmtree still clears it.
    git(ws.game, "worktree", "lock", os.path.join(top, "demo"))
    age(top)
    pid, plan = ws.saved(top)
    assert ws.planned(plan) == ["wt/agent8"]
    out, lines = ws.clean(pid)
    assert out["deleted"] == 1 and not os.path.lexists(top)
    assert any(x.startswith("WARNING: git worktree remove") for x in lines)
    row = ws.db.one("SELECT reason, result FROM deletions WHERE relpath LIKE '%agent8'")
    assert row["result"] == "deleted" and "git worktree remove" in row["reason"]


def test_worktree_with_uncommitted_changes(ws):
    top = ws.worktree("agent4")
    write(os.path.join(top, "demo", "TASKS.md"), "# edited\n")
    age(top)
    why = ws.why(ws.plan(top), "wt/agent4")
    assert why.startswith("uncommitted changes in") and "TASKS.md" in why
    assert ws.planned(ws.plan(top, force_dirty=True)) == ["wt/agent4"]


def test_a_quoted_name_is_read_as_its_bytes(ws):
    # git C-quotes a non-ASCII name; -z keeps it as it is on disk.
    top = ws.worktree("agent5")
    write(os.path.join(top, "demo", "r\u00e9sum\u00e9.md"), "notes\n")
    age(top)
    assert ws.why(ws.plan(top), "wt/agent5") == (
        "untracked files not under notes/agent5/: demo/r\u00e9sum\u00e9.md"
    )
    write(ws.p("notes/agent5/r\u00e9sum\u00e9.md"), "notes\n")
    assert ws.planned(ws.plan(top)) == ["wt/agent5"]


def test_files_beside_the_checkouts_and_ignored_work_are_kept(ws):
    top = ws.worktree("agent6")
    demo = os.path.join(top, "demo")
    write(os.path.join(demo, ".gitignore"), "*.log\nbuild/\nlocal.cfg\n")
    git(demo, "add", ".gitignore")
    git(demo, "commit", "-q", "-m", "ignore")
    write(os.path.join(top, "NOTES.md"), "beside the checkout\n")
    write(os.path.join(demo, "probe.log"), "an ignored finding\n")
    # Build output goes; an ignored file the main checkout has the same goes.
    write(os.path.join(demo, "build/CMakeCache.txt"), "x\n")
    write(os.path.join(demo, "__pycache__/a.pyc"), "x\n")
    write(os.path.join(ws.game, "local.cfg"), "same\n")
    write(os.path.join(demo, "local.cfg"), "same\n")
    age(top)
    why = ws.why(ws.plan(top), "wt/agent6")
    assert why == "untracked files not under notes/agent6/: NOTES.md, demo/probe.log"
    write(ws.p("notes/agent6/NOTES.md"), "beside the checkout\n")
    write(ws.p("notes/agent6/probe.log"), "an ignored finding\n")
    assert ws.planned(ws.plan(top)) == ["wt/agent6"]


def test_a_detached_commit_keeps_the_worktree(ws):
    top = ws.worktree("agent7")
    demo = os.path.join(top, "demo")
    git(demo, "checkout", "-q", "--detach")
    write(os.path.join(demo, "fix.c"), "int x;\n")
    git(demo, "add", "fix.c")
    git(demo, "commit", "-q", "-m", "detached work")
    age(top)
    why = ws.why(ws.plan(top), "wt/agent7")
    assert why and "is on no branch or tag" in why
    git(demo, "branch", "keep-it")
    assert ws.planned(ws.plan(top)) == ["wt/agent7"]


# ---- the store: guard 6 and retention -----------------------------------------------


def test_store_runs_by_their_rows(ws):
    ws.store_run("20261001-110000")  # no row: import first
    ws.store_run("20261001-110001")
    ws.run_row("20261001-110001", status="ok")  # retention's
    ws.store_run("20261001-110002")
    ws.run_row("20261001-110002")  # unparsed: may go whole
    ws.store_run("20261001-110003", exit_code=False)
    ws.run_row("20261001-110003")
    write(ws.p("bench-logs/topic-ab/20261001-110004/run-info.txt"))
    age(ws.p("bench-logs/topic-ab"))
    plan = ws.plan(*(ws.p("bench-logs/20261001-11000%d" % i) for i in range(4)))
    assert ws.why(plan, "bench-logs/20261001-110000") == "a run not in the db: benchdb import first"
    assert ws.why(plan, "bench-logs/20261001-110001").startswith("a run in the db")
    assert ws.why(plan, "bench-logs/20261001-110003") == "a run with no exit-code (being written)"
    assert ws.planned(plan) == ["bench-logs/20261001-110002"]
    assert plan["items"][0]["rule"] == "an unparsed run older than 7 days"
    # A sweep leaves the parsed run to retention and skips non-run names.
    plan = ws.plan()
    assert ws.planned(plan) == ["bench-logs/20261001-110002"]
    assert ws.why(plan, "bench-logs/20261001-110001") is None


def build(ws, started):
    with ws.db.write() as w:
        return w.build(
            "u" + started, "demo", "windows", "full", lineage="integration", started=started
        )


def test_retention_takes_only_superseded_heavy_files(ws):
    rels = []
    for i in range(4):
        rel = "20261001-12000%d" % i
        d = ws.store_run(rel, files=("game-stdio.log", "frames/f1.png", "hdd.img"))
        rid = ws.run_row(
            rel,
            status="ok",
            build=build(ws, "2026-10-0%dT10:00:00Z" % (i + 1)),
            started="2026-10-0%dT10:00:00Z" % (i + 1),
        )
        with ws.db.write() as w:
            w.artifact(rid, "store", rel + "/frames", "frames", True, bytes=10)
            w.artifact(rid, "store", rel + "/hdd.img", "disk", True, bytes=10)
            w.artifact(rid, "store", rel + "/game-stdio.log", "log", False, bytes=2)
        age(d)
        rels.append(rel)
    pid, plan = ws.saved()
    # internal_keep 3: the oldest build's run is superseded.
    assert ws.planned(plan) == ["bench-logs/%s/frames" % rels[0], "bench-logs/%s/hdd.img" % rels[0]]
    assert all(i["rule"].startswith("superseded (D10)") for i in plan["items"])
    out, _ = ws.clean(pid)
    assert out["deleted"] == 2
    assert os.path.isfile(os.path.join(ws.store, rels[0], "game-stdio.log"))
    assert not os.path.exists(os.path.join(ws.store, rels[0], "frames"))
    states = {
        a["relpath"]: a["state"]
        for a in ws.db.sql(
            "SELECT relpath, state FROM artifacts WHERE relpath LIKE ?", (rels[0] + "/%",)
        )
    }
    assert states == {
        rels[0] + "/frames": "deleted",
        rels[0] + "/hdd.img": "deleted",
        rels[0] + "/game-stdio.log": "present",
    }
    assert ws.db.one("SELECT count(*) AS n FROM runs")["n"] == 4


def test_retention_keeps_a_run_written_today(ws):
    rels = []
    for i in range(4):
        rel = "20261001-14000%d" % i
        d = ws.store_run(rel, files=("game-stdio.log", "frames/f1.png"))
        rid = ws.run_row(
            rel,
            status="ok",
            build=build(ws, "2026-10-0%dT10:00:00Z" % (i + 1)),
            started="2026-10-0%dT10:00:00Z" % (i + 1),
        )
        with ws.db.write() as w:
            w.artifact(rid, "store", rel + "/frames", "frames", True, bytes=10)
        age(d)
        rels.append(rel)
    # A re-judge rewrote the oldest run's log: its start is old, its files new.
    os.utime(os.path.join(ws.store, rels[0], "game-stdio.log"))
    plan = ws.plan()
    assert ws.planned(plan) == []
    assert ws.why(plan, "bench-logs/%s/frames" % rels[0]) == ("its run was written within 7 days")


def test_older_than_has_a_floor(ws, capsys):
    d = ws.runs + "/fresh"
    write(d + "/x.png", "x")
    for days in (0, 0.5, -1):
        with pytest.raises(DbError, match="floor|0 or more"):
            ws.plan(d, older_than=days)
    with pytest.raises(DbError, match="0 or more"):
        ws.plan(d, older_than=-1, force_young=True)
    with pytest.raises(DbError, match="0 or more"):
        ws.plan(d, older_than=float("nan"), force_young=True)
    pid, plan = ws.saved(d, older_than=0, force_young=True)
    assert ws.planned(plan) == ["runs/fresh"]
    assert plan["args"]["force_young"] is True
    clean.table(plan)
    assert "WARNING: --force-young" in capsys.readouterr().out
    # The plan file carries the flag into clean's fresh plan.
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1
    with pytest.raises(config.ConfigError, match="days"):
        bad = TOML.replace('kind = "runs"', 'kind = "runs"\ndays = 0', 1)
        write(ws.p("bad.toml"), bad)
        config.load(ws.p("bad.toml"))


def test_a_retention_decision_edited_into_the_plan_is_ignored(ws):
    """Only the paths of the plan file count: a path it adds that the fresh
    plan does not hold stays, whatever rule the file gives it."""
    keep = ws.store_run("20261001-130000", files=("frames/f.png",))
    rid = ws.run_row("20261001-130000", status="ok")
    with ws.db.write() as w:
        w.artifact(rid, "store", "20261001-130000/frames", "frames", True)
    write(ws.p("runs/fresh/a"))
    write(ws.p("runs/old/a"))
    age(ws.p("runs/old"))
    pid, plan = ws.saved()
    assert ws.planned(plan) == ["runs/old"]
    p = os.path.join(ws.store, clean.PLANS, pid + ".json")
    with open(p) as f:
        doc = json.load(f)
    st = os.lstat(os.path.join(keep, "frames"))
    for path, kind in ((os.path.join(keep, "frames"), "heavy"), (ws.p("runs/fresh"), "tree")):
        doc["items"].append(
            dict(
                root="store",
                host="local",
                path=path,
                rel="x",
                kind=kind,
                bytes=0,
                mtime=int(st.st_mtime),
                rule="superseded (D10)",
                age=0,
            )
        )
    with open(p, "w") as f:
        json.dump(doc, f)
    out, lines = ws.clean(pid)
    assert out == dict(deleted=1, skipped=2, failed=0, bytes=out["bytes"])
    assert os.path.isdir(os.path.join(keep, "frames")) and os.path.isdir(ws.p("runs/fresh"))
    assert not os.path.exists(ws.p("runs/old"))
    assert any("written within 2 days" in x for x in lines)


# ---- links (finding 5) ---------------------------------------------------------------


def test_links_are_only_unlinked(ws):
    os.symlink("nowhere", os.path.join(ws.store, "bench-logs"))  # the dangling one
    write(ws.p("runs/target/a"))
    write(ws.p("outside/kept/a"))
    age(ws.p("runs/target"))
    os.symlink(ws.p("runs/target"), ws.p("runs/in"))
    os.symlink(ws.p("outside/kept"), ws.p("runs/out"))
    for n in ("bench-logs/bench-logs", "runs/in", "runs/out"):
        age(ws.p(n))
    pid, plan = ws.saved(ws.p("bench-logs/bench-logs"), ws.p("runs/in"), ws.p("runs/out"))
    kinds = {os.path.relpath(i["path"], ws.d): i["kind"] for i in plan["items"]}
    assert kinds == {"bench-logs/bench-logs": "link", "runs/in": "link", "runs/out": "link"}
    out, _ = ws.clean(pid)
    assert out["deleted"] == 3
    for n in ("bench-logs/bench-logs", "runs/in", "runs/out"):
        assert not os.path.lexists(ws.p(n))
    assert os.path.isfile(ws.p("runs/target/a")) and os.path.isfile(ws.p("outside/kept/a"))


def test_a_cited_or_fresh_link_stays(ws):
    write(ws.p("runs/target/a"))
    os.symlink(ws.p("runs/target"), ws.p("runs/20261001-100005"))  # cited in notes/refs
    age(ws.p("runs/20261001-100005"))
    os.symlink(ws.p("runs/target"), ws.p("runs/fresh"))
    plan = ws.plan(ws.p("runs/20261001-100005"), ws.p("runs/fresh"))
    assert ws.planned(plan) == []
    assert ws.why(plan, "runs/20261001-100005").startswith("pinned: ")
    assert ws.why(plan, "runs/fresh") == "a link made within 2 days"


def test_read_only_trees_go_and_links_out_keep_their_targets(ws):
    ro_out = ws.p("outside/ro")
    write(os.path.join(ro_out, "f"), "x")
    os.chmod(os.path.join(ro_out, "f"), 0o444)
    os.chmod(ro_out, 0o555)
    tree = ws.p("runs/oldtree")
    write(os.path.join(tree, "game_files/default.xbe"), "x")
    os.symlink(ro_out, os.path.join(tree, "ext"))
    os.chmod(os.path.join(tree, "game_files/default.xbe"), 0o444)
    os.chmod(os.path.join(tree, "game_files"), 0o555)
    age(tree)
    try:
        plan = ws.plan(ro_out)
        assert ws.why(plan, "outside/ro") == "outside the deletable roots"
        pid, plan = ws.saved(tree)
        out, _ = ws.clean(pid)
        assert out["deleted"] == 1 and not os.path.lexists(tree)
        assert stat.S_IMODE(os.stat(ro_out).st_mode) == 0o555
        assert stat.S_IMODE(os.stat(os.path.join(ro_out, "f")).st_mode) == 0o444
    finally:
        os.chmod(ro_out, 0o755)


# ---- clean: the intersection, the age of a plan, --large, the journal ----------------


def test_stale_plan_and_changed_path(ws):
    write(ws.p("runs/a/x"))
    write(ws.p("runs/b/x"))
    age(ws.p("runs/a"))
    age(ws.p("runs/b"))
    pid, _ = ws.saved()
    with pytest.raises(DbError, match="over an hour old"):
        clean.clean(ws.cfg, ws.db, pid, now=NOW + 2 * 3600, say=lambda s: None)
    # b changed since the plan (still old): skipped; a goes.
    os.utime(ws.p("runs/b/x"), (OLD + 100, OLD + 100))
    out, lines = ws.clean(pid)
    assert (out["deleted"], out["skipped"]) == (1, 1)
    assert os.path.isdir(ws.p("runs/b")) and not os.path.exists(ws.p("runs/a"))
    assert any("changed since the plan" in x for x in lines)
    with pytest.raises(DbError, match="not a plan id"):
        clean.clean(ws.cfg, ws.db, "../x", say=lambda s: None)


def test_large_threshold(ws, monkeypatch):
    write(ws.p("runs/big/x"), "x" * 5000)
    age(ws.p("runs/big"))
    pid, _ = ws.saved()
    monkeypatch.setattr(clean, "LARGE_BYTES", 100)
    with pytest.raises(DbError, match="pass --large"):
        ws.clean(pid)
    assert os.path.isdir(ws.p("runs/big"))
    out, _ = ws.clean(pid, large=True)
    assert out["deleted"] == 1
    # The share of a disk: 20% of a tiny disk.
    monkeypatch.setattr(clean, "LARGE_BYTES", 10**15)
    monkeypatch.setattr(clean, "disk", lambda p: (1000, 10))
    it = dict(host="local", root="runs", bytes=300)
    assert "over 20% of its disk" in clean.over_cap(ws.cfg, [it], {})
    assert clean.over_cap(ws.cfg, [dict(it, bytes=100)], {}) is None
    assert "over 20%" in clean.over_cap(
        ws.cfg, [dict(host="hostA", root="hosta", bytes=300)], {"hostA:hosta": (1000, 0)}
    )


def test_journal_rows_and_log(ws):
    write(ws.p("runs/j1/x"), "abc")
    write(ws.p("runs/j2/x"), "abc")
    age(ws.p("runs/j1"))
    age(ws.p("runs/j2"))
    pid, _ = ws.saved()
    shutil.rmtree(ws.p("runs/j2"))  # gone before the clean: skipped, journalled
    ws.clean(pid)
    rows = ws.db.sql("SELECT plan, agent, root, relpath, result FROM deletions ORDER BY relpath")
    assert [(r["plan"], r["agent"], r["root"], r["relpath"]) for r in rows] == [
        (pid, "tester", "runs", "j1"),
        (pid, "tester", "runs", "j2"),
    ]
    assert rows[0]["result"] == "deleted" and rows[1]["result"].startswith("refused:")
    with open(os.path.join(ws.store, clean.CLEAN_LOG)) as f:
        log = f.read().splitlines()
    assert len(log) == 2 and all(line.split("\t")[1] == pid for line in log)


def test_old_plans_are_swept(ws):
    pid, _ = ws.saved()
    p = os.path.join(ws.store, clean.PLANS, pid + ".json")
    age(p)
    plan = ws.plan()
    assert ws.planned(plan) == ["bench-logs/%s/%s.json" % (clean.PLANS, pid)]


# ---- hosts ---------------------------------------------------------------------------


def test_host_tree_waits_for_a_whole_local_copy(ws, monkeypatch):
    """Finding 2: a host tree whose bench-logs run is not in the store
    whole is refused, and allowed once it is."""
    tree = "/srv/u/xbr-bench/demo/runs/tree1"
    info = "host: x\n"
    sha = __import__("hashlib").sha256(info.encode()).hexdigest()
    lines = HOSTA_ROOTS + (
        entry(tree),
        "S\t%s\t20261001-140000\t%s\t1" % (tree, sha),
        "F\t%s\t20261001-140000\trun-info.txt" % tree,
        "F\t%s\t20261001-140000\texit-code" % tree,
        "F\t%s\t20261001-140000\tgame-stdio.log" % tree,
    )
    fake = FakeHost(listing(*lines), apply=lambda s: "deleted\t%s\n" % tree)
    monkeypatch.setattr(clean, "run_host", fake)
    plan = ws.plan("hosta:" + tree)
    assert ws.why(plan, tree) == "runs not pulled whole to the store: 20261001-140000"
    ws.store_run("20261001-140000", files=())
    plan = ws.plan("hosta:" + tree)
    assert ws.why(plan, tree) == "runs not pulled whole to the store: 20261001-140000"
    ws.store_run("20261001-140000", info=info)
    pid, plan = ws.saved("hosta:" + tree)
    assert ws.planned(plan, "hostA") == [tree]
    out, _ = ws.clean(pid)
    assert out["deleted"] == 1
    host, cmd, script, timeout = fake.calls[-1]
    assert (host, cmd, timeout) == ("hostA", "bash -s", clean.APPLY_TIMEOUT)
    # The apply takes the run lock and is sent the resolved path.
    assert "flock -x -n 9" in script and "P=('%s')" % tree in script
    rows = ws.db.sql("SELECT root, relpath, result FROM deletions")
    assert [(r["root"], r["relpath"], r["result"]) for r in rows] == [("hosta", "tree1", "deleted")]


def test_host_refusals_and_patterns(ws, monkeypatch):
    """Finding 16: /tmp's own entries and another user's never match; a
    never-path, a run being written and a young tree stay."""
    lines = HOSTA_ROOTS + (
        entry("/srv/u/tmp/.X11-unix"),
        entry("/srv/u/tmp/systemd-private-x"),
        entry("/srv/u/tmp/a-bench", uid="1001"),
        entry("/srv/u/tmp/b-bench"),
        entry("/srv/u/tmp/c-bench", mtime=NOW - DAY),
        entry("/srv/u/xbr-bench/demo/runs/base"),
        entry("/srv/u/xbr-bench/demo/runs/busy"),
        "S\t/srv/u/xbr-bench/demo/runs/busy\t20261001-150000\t-\t0",
        entry("/srv/u/xbr-bench/demo/runs/lnk", kind="l"),
        "M\t/srv/u/xbr-bench/demo/runs/gone",
    )
    monkeypatch.setattr(clean, "run_host", FakeHost(listing(*lines)))
    plan = ws.plan(
        "hosttmp",
        "hosta:/srv/u/xbr-bench/demo/runs/base",
        "hosta:/srv/u/xbr-bench/demo/runs/busy",
        "hosta:/srv/u/xbr-bench/demo/runs/lnk",
        "hosta:/srv/u/xbr-bench/demo/runs/gone",
        "hosta:/srv/u/xbr-bench/demo/runs/../etc",
    )
    assert ws.planned(plan, "hostA") == ["/srv/u/tmp/b-bench", "/srv/u/xbr-bench/demo/runs/lnk"]
    w = ws.why
    assert w(plan, "/srv/u/tmp/.X11-unix") == ".X11-unix does not match the hosttmp root's pattern"
    assert "does not match" in w(plan, "/srv/u/tmp/systemd-private-x")
    assert w(plan, "/srv/u/tmp/a-bench") == "owned by uid 1001, not this user"
    assert w(plan, "/srv/u/tmp/c-bench") == "written within 7 days"
    assert w(plan, "/srv/u/xbr-bench/demo/runs/base").startswith("under or over the never-path")
    assert "has no exit-code" in w(plan, "/srv/u/xbr-bench/demo/runs/busy")
    assert w(plan, "/srv/u/xbr-bench/demo/runs/gone") == "does not exist"
    assert w(plan, "/srv/u/xbr-bench/demo/runs/../etc") == "a .. in the path"


def test_windows_patterns_refuse_the_bench_tree_and_its_lock(ws, monkeypatch):
    lines = (
        "T\t0\tD:\\xbr\t1000000\t500000",
        entry("D:\\xbr\\bench"),
        entry("D:\\xbr\\spartan"),
        entry("D:\\xbr\\.bench-lock", kind="f"),
        entry("D:\\xbr\\x-bench"),
        entry("D:\\xbr\\y-bench", real="D:\\other\\y-bench"),
    )
    fake = FakeHost(listing(*lines))
    monkeypatch.setattr(clean, "run_host", fake)
    plan = ws.plan("pc")
    assert ws.planned(plan, "hostB") == ["D:\\xbr\\x-bench"]
    # The bench base and its lock are the built-in floor, said as such.
    for n in ("bench", ".bench-lock"):
        assert (
            ws.why(plan, "D:\\xbr\\" + n)
            == "under or over the never-path D:\\xbr\\%s (built in)" % n
        )
    assert ws.why(plan, "D:\\xbr\\spartan") == "spartan does not match the pc root's pattern"
    assert ws.why(plan, "D:\\xbr\\y-bench").startswith("its real path is")
    _host, cmd, script, _t = fake.calls[0]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert "$Roots = @('D:\\xbr', 'D:\\xbr\\bench\\demo\\runs')" in script
    assert "$Sweep = @(1, 0)" in script


def test_an_unreachable_host_refuses_its_paths(ws, monkeypatch):
    monkeypatch.setattr(clean, "run_host", FakeHost(rc=255))
    plan = ws.plan("hosta:/srv/u/xbr-bench/demo/runs/t")
    assert ws.why(plan, "/srv/u/xbr-bench/demo/runs/t").startswith("host hostA not reached")


def test_apply_lines_and_the_held_lock(ws, monkeypatch):
    lines = HOSTA_ROOTS + (
        entry("/srv/u/xbr-bench/demo/runs/t1"),
        entry("/srv/u/xbr-bench/demo/runs/t2"),
    )
    fake = FakeHost(
        listing(*lines),
        apply=lambda s: (
            "deleted\t/srv/u/xbr-bench/demo/runs/t1\nskipped\t/srv/u/xbr-bench/demo/runs/t2\tgone\n"
        ),
    )
    monkeypatch.setattr(clean, "run_host", fake)
    pid, _ = ws.saved("hosta:/srv/u/xbr-bench/demo/runs/t1", "hosta:/srv/u/xbr-bench/demo/runs/t2")
    out, _ = ws.clean(pid)
    assert (out["deleted"], out["skipped"], out["failed"]) == (1, 1, 0)
    fake.apply_rc = 75
    pid, _ = ws.saved("hosta:/srv/u/xbr-bench/demo/runs/t1")
    out, lines = ws.clean(pid)
    assert out["failed"] == 1 and "the run lock was held" in lines[0]


# ---- the host scripts ----------------------------------------------------------------

BASH = shutil.which("bash", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")

SHIMS = {
    # flock: the lock is logged, never taken (the Mac has no flock).
    "flock": '#!/bin/sh\necho "flock $*" >> "$SHIM_LOG"\nexit 0\n',
    # rm: logged; BSD rm has no --one-file-system.
    "rm": '#!/bin/sh\necho "rm $*" >> "$SHIM_LOG"\n'
    'args=""\nfor a in "$@"; do [ "$a" = --one-file-system ] || set -- "$@" "$a"; shift; done\n'
    'exec /bin/rm "$@"\n',
    # mv: logged; with SHIM_SHIP set, a ship writes into the dir it moved
    # (one that cd'd in before the rename), so the re-read sees it.
    "mv": '#!/bin/sh\necho "mv $*" >> "$SHIM_LOG"\n/bin/mv "$@" || exit 1\n'
    'for a in "$@"; do last=$a; done\n'
    'case "$last" in *"/.$SHIM_SHIP.deleting."*) : > "$last/shipped.exe" ;; esac\n',
    # stat -c %u: GNU's, on the lstat.
    "stat": "#!%s\nimport os, sys\n"
    "assert sys.argv[1:3] == ['-c', '%%u'], sys.argv\n"
    "print(os.lstat(sys.argv[-1]).st_uid)\n" % sys.executable,
    # realpath and find: GNU's, as far as the scripts use them.
    "realpath": "#!%s\nimport os, sys\n"
    "a = [x for x in sys.argv[1:] if x != '--']\n"
    "p = a[0]\n"
    "if not os.path.lexists(os.path.dirname(os.path.abspath(p)) or '/'): sys.exit(1)\n"
    "print(os.path.realpath(p))\n" % sys.executable,
    "find": "#!%s\nimport os, stat, sys\n"
    "a = [x for x in sys.argv[1:] if x not in ('-P', '-xdev')]\n"
    "top = a[0]\n"
    "paths = [top]\n"
    "if os.path.isdir(top) and not os.path.islink(top):\n"
    "    for d, ds, fs in os.walk(top):\n"
    "        paths += [os.path.join(d, n) for n in ds + fs]\n"
    "if '-printf' in a:\n"
    "    for p in paths: print('%%f' %% os.lstat(p).st_mtime)\n"
    "elif '-exec' in a:\n"
    "    for p in paths:\n"
    "        s = os.lstat(p)\n"
    "        if not stat.S_ISLNK(s.st_mode): os.chmod(p, s.st_mode | stat.S_IWUSR)\n"
    "else: sys.exit(2)\n" % sys.executable,
}


@pytest.fixture
def shim(tmp_path):
    b = tmp_path / "shim"
    b.mkdir()
    for n, text in SHIMS.items():
        (b / n).write_text(text)
        (b / n).chmod(0o755)
    log = tmp_path / "shim.log"
    log.write_text("")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(
        os.environ, PATH="%s:%s" % (b, os.environ["PATH"]), SHIM_LOG=str(log), HOME=str(home)
    )
    return env, log


@pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="bash")
def test_clean_apply_sh_on_a_temp_tree(tmp_path, shim):
    """Finding 6: a link leaf is rm'd and its target kept; a read-only tree
    with a link to a read-only dir in it goes and the dir stays read-only;
    a changed tree, a never-path and a trailing / are skipped; rm gets
    --one-file-system and no trailing /, under the flock."""
    env, log = shim
    root = os.path.realpath(str(tmp_path / "hroot"))
    ro = os.path.realpath(str(tmp_path / "ro"))
    write(os.path.join(ro, "f"), "x")
    os.chmod(os.path.join(ro, "f"), 0o444)
    os.chmod(ro, 0o555)
    write(os.path.join(root, "target/a"))
    os.symlink(os.path.join(root, "target"), os.path.join(root, "lnk"))
    write(os.path.join(root, "rotree/game_files/x"))
    os.symlink(ro, os.path.join(root, "rotree/ext"))
    os.chmod(os.path.join(root, "rotree/game_files/x"), 0o444)
    os.chmod(os.path.join(root, "rotree/game_files"), 0o555)
    write(os.path.join(root, "changed/a"))
    write(os.path.join(root, "base/a"))
    for n in ("rotree", "changed", "base", "target", "lnk"):
        age(os.path.join(root, n))
    r = config.Root(
        "hr",
        root,
        "hostA",
        "host-scratch",
        True,
        [__import__("re").compile("[a-z]+")],
        None,
        False,
        "flock",
        "",
    )
    h = config.Host("hostA", "linux-proton", "hosta", [], [], "", "")

    def item(n, m=None):
        p = os.path.join(root, n)
        return dict(
            root="hr", path=p, mtime=int(m if m is not None else refs.newest_mtime(p)), age=DAY
        )

    items = [
        item("lnk"),
        item("rotree"),
        item("changed", OLD - 5),
        item("base"),
        dict(item("target"), path=os.path.join(root, "target") + "/"),
    ]
    cmd, script = clean.apply_script(h, items, [r], [os.path.join(root, "base")], "o")
    assert cmd == "bash -s" and not script.startswith("#@")
    try:
        p = subprocess.run(
            [BASH, "-s"], input=script.encode(), capture_output=True, env=env, timeout=60
        )
        got = {path: (res, why) for res, path, why in clean.parse_apply(p.stdout.decode())}
        assert got[os.path.join(root, "lnk")][0] == "deleted"
        assert got[os.path.join(root, "rotree")][0] == "deleted"
        assert got[os.path.join(root, "changed")][0] == "skipped"
        assert "changed since the plan" in got[os.path.join(root, "changed")][1]
        assert got[os.path.join(root, "base")] == ("skipped", "never-path %s/base" % root)
        assert got[os.path.join(root, "target") + "/"][0] == "skipped"
        assert p.returncode == 0, p.stderr.decode()
        assert not os.path.lexists(os.path.join(root, "lnk"))
        assert os.path.isfile(os.path.join(root, "target/a"))
        assert not os.path.lexists(os.path.join(root, "rotree"))
        assert stat.S_IMODE(os.stat(ro).st_mode) == 0o555
        assert os.path.isfile(os.path.join(ro, "f"))
        text = log.read_text()
        assert "flock -x -n 9" in text
        rms = [x for x in text.splitlines() if x.startswith("rm ")]
        assert any(
            re.fullmatch(r"rm -rf --one-file-system -- %s/\.rotree\.deleting\.\d+" % root, x)
            for x in rms
        ), rms
        assert "rm -f -- %s/lnk" % root in rms
        assert not any(x.endswith("/") for x in rms)
    finally:
        os.chmod(ro, 0o755)


@pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="bash")
def test_clean_apply_sh_gives_way_to_a_ship(tmp_path, shim):
    """M9: the dir is renamed before the rm; a ship that wrote into it
    meanwhile shows in the re-read and the dir goes back where it was."""
    env, log = shim
    root = os.path.realpath(str(tmp_path / "hroot"))
    for n in ("old", "ship"):
        write(os.path.join(root, n, "a"))
        age(os.path.join(root, n))
    # own: the user's dirs pass the owner check (stat -c %u on the lstat).
    r = config.Root(
        "hr", root, "hostA", "host-scratch", True, [re.compile("[a-z]+")], None, True, "flock", ""
    )
    h = config.Host("hostA", "linux-proton", "hosta", [], [], "", "")
    items = [
        dict(root="hr", path=os.path.join(root, n), mtime=int(OLD), age=DAY)
        for n in ("old", "ship")
    ]
    _cmd, script = clean.apply_script(h, items, [r], [], "o")
    p = subprocess.run(
        [BASH, "-s"],
        input=script.encode(),
        capture_output=True,
        env=dict(env, SHIM_SHIP="ship"),
        timeout=60,
    )
    got = {path: (res, why) for res, path, why in clean.parse_apply(p.stdout.decode())}
    assert got[os.path.join(root, "old")][0] == "deleted"
    assert got[os.path.join(root, "ship")][0] == "skipped"
    assert "shipped to during the clean" in got[os.path.join(root, "ship")][1]
    assert not os.path.lexists(os.path.join(root, "old"))
    assert os.path.isfile(os.path.join(root, "ship", "shipped.exe"))
    assert sorted(os.listdir(root)) == ["ship"]
    assert p.returncode == 0, p.stderr.decode()


def test_clean_apply_sh_owner_is_the_links_own(tmp_path, shim):
    """L3: the owner check reads the leaf itself: the user's link to a
    root-owned dir is the user's and is unlinked (test -O followed it)."""
    env, _log = shim
    root = os.path.realpath(str(tmp_path / "hroot"))
    os.makedirs(root)
    os.symlink("/", os.path.join(root, "lnk"))
    r = config.Root(
        "hr", root, "hostA", "host-scratch", True, [re.compile("[a-z]+")], None, True, "flock", ""
    )
    h = config.Host("hostA", "linux-proton", "hosta", [], [], "", "")
    items = [dict(root="hr", path=os.path.join(root, "lnk"), mtime=int(OLD), age=DAY)]
    _cmd, script = clean.apply_script(h, items, [r], [], "o")
    p = subprocess.run(
        [BASH, "-s"], input=script.encode(), capture_output=True, env=env, timeout=60
    )
    got = {path: (res, why) for res, path, why in clean.parse_apply(p.stdout.decode())}
    assert got[os.path.join(root, "lnk")][0] == "deleted", got
    assert not os.path.lexists(os.path.join(root, "lnk"))


def ps_code(text):
    """The lines of a PowerShell script that are not comments."""
    return "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))


def test_powershell_apply_text(ws):
    """Finding 6, by the builder's text: links are deleted as links, the
    walk is by hand, nothing recurses; the lock is the runs' file; the
    mtime check and the never-path are on the resolved path."""
    h = ws.cfg.hosts["hostB"]
    roots = [ws.cfg.roots["pc"]]
    it = dict(root="pc", path="D:\\xbr\\x-bench", mtime=1700000000, age=7 * DAY)
    cmd, script = clean.apply_script(h, [it], roots, ["D:\\xbr\\bench"], "bench benchdb-clean p")
    code = ps_code(script)
    assert "[IO.Directory]::Delete($p, $false)" in code
    assert "EnumerateFileSystemEntries" in code
    assert "-Recurse" not in code and "Remove-Item -Recurse" not in code
    assert "$Lock = 'D:\\xbr\\.bench-lock'" in script
    assert "$Never = @('D:\\xbr\\bench', 'C:\\xbr\\.bench-lock'" in script
    # The floor goes with every apply, whatever the config's [never] says.
    assert "'D:\\xbr\\.bench-lock'" in script.split("$Never = ")[1].split("\n")[0]
    assert "$Bench = @('C:\\xbr\\bench', 'D:\\xbr\\bench')" in script
    assert "'D:\\xbr'" in script.split("$NeverSelf = ")[1].split("\n")[0]
    assert (
        "@{p='D:\\xbr\\x-bench'; r=0; pat='[a-z0-9-]+-bench'; m=1700000000; age=604800}" in script
    )
    assert "changed since the plan" in code and "Resolve-Leaf $p $root" in code
    assert "Test-Under $res" in code
    # The lock text and takeover match the runs' own (pid= start= ticks).
    assert "'^bench .* pid=(\\d+) start=(\\d+)\\s*$'" in code
    assert cmd == clean.ps_stdin()


def test_powershell_list_text(ws):
    h = ws.cfg.hosts["hostB"]
    cmd, script = clean.list_script(h, [ws.cfg.roots["pc"]], [], ["D:\\xbr\\x'y"])
    assert "$Paths = @('D:\\xbr\\x''y')" in script and "$Sweep = @(0)" in script
    assert "-Recurse" not in ps_code(script)


def test_sh_list_quotes_home_and_paths():
    assert clean.sh_path("~/xbr-bench") == "\"$HOME\"/'xbr-bench'"
    assert clean.sh_path("/srv/u/it's") == "'/srv/u/it'\\''s'"
    assert clean.sh_path("~") == '"$HOME"'


def test_parse_listing_skips_bad_lines():
    got = clean.parse_listing("H\t501\t100\nE\tbad\nT\tx\t/p\t1\t2\nM\t/srv/u/g\nB\t/srv/u/a?b\n")
    assert got["uid"] == "501" and got["now"] == 100.0
    assert got["entries"] == [] and got["roots"] == {}
    assert got["missing"] == ["/srv/u/g"] and got["bad"] == ["/srv/u/a?b"]


# ---- pins, gc and the verbs ----------------------------------------------------------


def test_refresh_pins(ws):
    n = clean.refresh_pins(ws.cfg, ws.db)
    rows = {(r["stamp"], r["source"]) for r in ws.db.sql("SELECT stamp, source FROM pins")}
    assert n == len(rows)
    assert ("20261001-100003", "demo/config/seed_functions.json:1") in rows
    assert ("20261001-100005", "notes/refs/log.md:1") in rows
    assert not any(s == "20261001-100004" for s, _ in rows)


def test_gc_marks_vanished_artifacts(ws):
    d = ws.store_run("20261001-160000", files=("hdd.img",))
    rid = ws.run_row("20261001-160000", status="ok")
    with ws.db.write() as w:
        w.artifact(rid, "store", "20261001-160000/hdd.img", "disk", True)
        w.artifact(rid, "store", "20261001-160000/run-info.txt", "info", False)
    os.unlink(os.path.join(d, "hdd.img"))
    assert clean.gc(ws.cfg, ws.db, say=lambda s: None) == 1
    st = {a["relpath"]: a["state"] for a in ws.db.sql("SELECT relpath, state FROM artifacts")}
    assert st["20261001-160000/hdd.img"] == "missing"
    assert st["20261001-160000/run-info.txt"] == "present"


def test_plan_and_clean_verbs(ws, monkeypatch, capsys):
    write(ws.p("runs/v1/x"))
    age(ws.p("runs/v1"))
    monkeypatch.chdir(ws.d)
    assert cli.main(["plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [os.path.relpath(i["path"], ws.d) for i in plan["items"]] == ["runs/v1"]
    assert cli.main(["clean", plan["id"]]) == 0
    assert "1 deleted" in capsys.readouterr().out
    assert not os.path.exists(ws.p("runs/v1"))
    assert cli.main(["pins", "--refresh"]) == 0


def test_the_clean_verbs_need_a_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(config.ENV, raising=False)
    for argv in (["plan"], ["clean", "20261025000000-abcdef"]):
        assert cli.main(argv) != 0


# ---- review fixes: the nested pins, the floor, the guards' gaps -------------------------


def test_a_run_nested_in_a_topic_dir_is_pinned(ws):
    """H1: a topic dir under runs/ or a store copy whose child run is cited
    (by stamp or HHMMSS) stays, though its own name cites nothing."""
    write(ws.p("runs/topic-x/20261001-170000/a.png"))
    age(ws.p("runs/topic-x"))
    write(ws.p("notes/refs/x.md"), "the fade shows at 170000\n")
    plan = ws.plan(ws.p("runs/topic-x"))
    assert "x.md cites 170000 (HHMMSS)" in ws.why(plan, "runs/topic-x")
    assert "x.md cites 170000 (HHMMSS)" in ws.why(plan, "runs/topic-x/20261001-170000")
    assert ws.planned(plan) == []
    d = ws.p("bench-logs/20261001-171000-ab/20261001-171500")
    write(os.path.join(d, "run-info.txt"))
    write(os.path.join(d, "exit-code"), "0\n")
    age(ws.p("bench-logs/20261001-171000-ab"))
    ws.run_row("20261001-171000-ab/20261001-171500")
    write(ws.p("notes/refs/y.md"), "see 20261001-171500\n")
    plan = ws.plan(ws.p("bench-logs/20261001-171000-ab"))
    assert "y.md cites 20261001-171500" in ws.why(plan, "bench-logs/20261001-171000-ab")


def test_the_built_in_floor_holds_without_a_never_line(ws, monkeypatch):
    """H2: a home's Games and bench base, the db and its backups and the
    checkouts are refused with no [never] line; a bench base gives up only
    its <game>/runs/<x>."""
    monkeypatch.setenv("HOME", ws.p("runs"))
    for n in ("Games", "xbr-bench", "other"):
        write(ws.p("runs/%s/a" % n))
        age(ws.p("runs/" + n))
    plan = ws.plan(ws.p("runs/Games"), ws.p("runs/xbr-bench"), ws.p("runs/other"))
    assert ws.why(plan, "runs/Games").endswith("runs/Games (built in)")
    assert ws.why(plan, "runs/xbr-bench").endswith("runs/xbr-bench (built in)")
    assert ws.planned(plan) == ["runs/other"]
    # The db's own dir is never a target, nor the config file.
    assert clean.floor_hit(clean.local_floor(ws.cfg), ws.p("bench-logs/db-backup/x.db"))
    assert clean.floor_hit(clean.local_floor(ws.cfg), ws.p("benchdb.toml"))
    assert clean.floor_hit(clean.local_floor(ws.cfg), ws.d)
    assert clean.floor_hit(clean.local_floor(ws.cfg), ws.p("demo/x"))
    assert not clean.floor_hit(clean.local_floor(ws.cfg), ws.p("bench-logs/20261001-100000"))
    fl = [("bench", "/h/xbr-bench")]
    assert clean.floor_hit(fl, "/h/xbr-bench/blinx2/game_files")
    assert clean.floor_hit(fl, "/h/xbr-bench/blinx2/runs")
    assert not clean.floor_hit(fl, "/h/xbr-bench/blinx2/runs/abc123")
    assert clean.floor_hit(fl, "/h")


def test_a_broad_host_pattern_cannot_take_the_bench_base(ws, monkeypatch):
    """H2's scenario: a home root whose pattern matches xbr-bench, with no
    [never] line for it."""
    r = config.Root(
        "home",
        "/srv/u",
        "hostA",
        "host-scratch",
        True,
        [__import__("re").compile("[a-z0-9-]+-bench")],
        None,
        False,
        "flock",
        "",
    )
    ws.cfg.roots = dict(ws.cfg.roots, home=r)
    ws.cfg.never = {"local": ws.cfg.never["local"]}
    fake = FakeHost(
        listing(
            *HOSTA_ROOTS,
            "T\t2\t/srv/u\t1000000\t500000",
            entry("/srv/u/xbr-bench"),
            entry("/srv/u/old-bench"),
        )
    )
    monkeypatch.setattr(clean, "run_host", fake)
    plan = ws.plan("hosta:/srv/u/xbr-bench", "hosta:/srv/u/old-bench")
    why = ws.why(plan, "/srv/u/xbr-bench")
    assert why.startswith("under or over the never-path /srv/u/xbr-bench") and "built in" in why
    assert ws.planned(plan, "hostA") == ["/srv/u/old-bench"]


def test_unknown_config_keys_are_refused(tmp_path):
    for body, err in (
        ('[roots.r]\npath = "r"\nkind = "runs"\npaterns = ["x"]\n', "roots.r.paterns"),
        ('[hosts.h]\nos = "windows"\nsshh = "h"\n', "hosts.h.sshh"),
        ('[never]\nnope = ["x"]\n', "never.nope"),
        ('[refs]\npath = ["x"]\n', "refs.path"),
        ('[release]\ntoolkits = "x"\n', "release.toolkits"),
    ):
        p = tmp_path / "benchdb.toml"
        p.write_text(body)
        with pytest.raises(config.ConfigError, match=err.replace(".", r"\.")):
            config.load(str(p))


@pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="bash")
def test_clean_apply_sh_keeps_the_floor(tmp_path, shim):
    """H2 on the host: with a root at $HOME whose pattern takes anything,
    the apply still keeps ~/Games, the bench base and its game_files, and
    deletes only a bench run dir and a plain scratch dir."""
    env, log = shim
    home = os.path.realpath(env["HOME"])
    names = (
        "Games",
        "xbr-bench",
        "xbr-bench/demo/game_files",
        "xbr-bench/demo/runs/abc",
        "a-bench",
    )
    for n in names:
        write(os.path.join(home, n, "f"))
    age(home)
    r = config.Root(
        "home",
        "~",
        "hostA",
        "host-scratch",
        True,
        [__import__("re").compile("[A-Za-z_-]+(/[a-z_]+)*")],
        None,
        False,
        "flock",
        "",
    )
    h = config.Host("hostA", "linux-proton", "hosta", [], [], "", "")
    items = [
        dict(
            root="home",
            path=os.path.join(home, n),
            age=DAY,
            mtime=int(refs.newest_mtime(os.path.join(home, n))),
        )
        for n in names
    ]
    cmd, script = clean.apply_script(h, items, [r], [], "o")
    p = subprocess.run(
        [BASH, "-s"], input=script.encode(), capture_output=True, env=env, timeout=60
    )
    got = {path: res for res, path, _why in clean.parse_apply(p.stdout.decode())}
    assert {os.path.relpath(k, home): v for k, v in got.items()} == {
        "Games": "skipped",
        "xbr-bench": "skipped",
        "xbr-bench/demo/game_files": "skipped",
        "xbr-bench/demo/runs/abc": "deleted",
        "a-bench": "deleted",
    }
    assert os.path.isfile(os.path.join(home, "xbr-bench/demo/game_files/f"))


def test_a_game_bench_env_adds_its_bench_base_to_the_floor(ws, monkeypatch):
    """H2: a game benching to a BENCH_DIR other than ~/xbr-bench (the Deck's
    ~/blx-bench) has that base, its game files and its prefix kept too."""
    write(
        os.path.join(ws.game, "scripts/bench.env"),
        "BENCH_HOST=hosta\nBENCH_DIR='~/blx-bench'\n"
        "BENCH_GAME_FILES='~/Games/demo/game_files'\nBENCH_PREFIX='~/blx-pfx'\n",
    )
    r = config.Root(
        "home",
        "/srv/u",
        "hostA",
        "host-scratch",
        True,
        [__import__("re").compile("[a-z0-9-]+(-bench|-pfx)(/.*)?")],
        None,
        False,
        "flock",
        "",
    )
    ws.cfg.roots = dict(ws.cfg.roots, home=r)
    paths = ("/srv/u/blx-bench", "/srv/u/blx-bench/demo/runs/x1", "/srv/u/blx-pfx")
    fake = FakeHost(
        listing(*HOSTA_ROOTS, "T\t2\t/srv/u\t1000000\t500000", *(entry(p) for p in paths))
    )
    monkeypatch.setattr(clean, "run_host", fake)
    plan = ws.plan(*("hosta:" + p for p in paths))
    assert "/srv/u/blx-bench (built in)" in ws.why(plan, "/srv/u/blx-bench")
    assert "/srv/u/blx-pfx (built in)" in ws.why(plan, "/srv/u/blx-pfx")
    assert ws.planned(plan, "hostA") == ["/srv/u/blx-bench/demo/runs/x1"]
    h = ws.cfg.hosts["hostA"]
    _cmd, script = clean.apply_script(h, [], [r], [], "o", clean.bench_env_floor(ws.cfg)["hostA"])
    assert "BENCH=(\"$HOME\"/'blx-bench'" in script
    write(os.path.join(ws.game, "scripts/bench.env"), "BENCH_DIR=$X\n")
    with pytest.raises(DbError, match="bench floor"):
        ws.plan("hosta:/srv/u/blx-pfx")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_pins_fail_closed_on_unreadable_or_big_sources(ws, monkeypatch):
    """M1: an unreadable ref dir or file, or one over the size cap, refuses
    the plan; a build-* dir that is not a CMake tree is read."""
    write(ws.p("runs/old/a"))
    age(ws.p("runs/old"))
    d = ws.p("notes/refs/locked")
    write(os.path.join(d, "a.md"), "x\n")
    os.chmod(d, 0)
    try:
        with pytest.raises(DbError, match="reference sources missing.*locked"):
            ws.plan(ws.p("runs/old"))
    finally:
        os.chmod(d, 0o755)
    f = write(ws.p("notes/refs/secret.md"), "x\n")
    os.chmod(f, 0)
    try:
        with pytest.raises(DbError, match="secret.md"):
            ws.plan(ws.p("runs/old"))
    finally:
        os.chmod(f, 0o644)
    write(ws.p("notes/refs/big.md"), "x" * 200)
    monkeypatch.setattr(refs, "MAX_REF_FILE", 100)
    with pytest.raises(DbError, match="big.md.*over"):
        ws.plan(ws.p("runs/old"))
    monkeypatch.undo()
    os.unlink(ws.p("notes/refs/big.md"))
    write(os.path.join(ws.game, "openspec/changes/build-resources/tasks.md"), "keep runs/old\n")
    write(os.path.join(ws.game, "build-win/CMakeCache.txt"), "runs/old\n")
    write(os.path.join(ws.game, "build-win/notes.md"), "runs/old\n")
    plan = ws.plan(ws.p("runs/old"))
    assert "build-resources/tasks.md names" in ws.why(plan, "runs/old")
    assert refs.build_dir(ws.game, "build-win")
    assert not refs.build_dir(os.path.join(ws.game, "openspec/changes"), "build-resources")
