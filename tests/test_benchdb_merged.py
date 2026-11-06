"""benchdb plan and clean of a merged worktree: its work is on
its repo's integration branch, so it passes guard 4's untracked files and
guard 5's age without --force-dirty or --force-young. Each merged form
(an ancestor, a squash merge naming the tip, a dir with no checkout left,
an old detached checkout of a merged commit), the notes and scripts it
copies to notes/<name>/, and each refusal (a tracked edit, an unmerged,
fresh or caught-up branch, a clone, an equal tree alone, one unmerged
checkout of several, a hidden .git, a young detached checkout, a live
agent), on temp repos only."""

import os
import subprocess
import sys
import time

import pytest
from test_benchdb_clean import WS, NoHost, git, write
from test_benchdb_pending import DONE, OPEN, states

from xboxrecomp_cli.benchdb import clean, config


@pytest.fixture
def ws(tmp_path, monkeypatch):
    d = os.path.realpath(str(tmp_path / "ws"))
    os.makedirs(d)
    monkeypatch.setenv("BENCHDB_AGENT", "tester")
    monkeypatch.setattr(clean, "run_host", NoHost())
    monkeypatch.setattr(config, "DEFAULT_PREFIXES", ("TRK",))
    w = WS(d, monkeypatch)
    # A second repo whose integration branch is not main (the toolkit's
    # posix-host/portability), named in [clean.integration].
    w.tool = w.p("tool")
    write(os.path.join(w.tool, "lib.c"), "int lib;\n")
    git(w.tool, "init", "-q", "-b", "main")
    git(w.tool, "add", "-A")
    git(w.tool, "commit", "-q", "-m", "upstream")
    git(w.tool, "branch", "dev")
    git(w.tool, "checkout", "-q", "dev")
    w.cfg.integration = {w.tool: "dev"}
    return w


def head(d):
    return git(d, "rev-parse", "HEAD").stdout.decode().strip()


def add(ws, name, repo=None, base=None):
    """wt/<name>/<repo>: a worktree of repo (the game by default) on a new
    branch <name> off base, with one commit of its own."""
    repo = repo or ws.game
    top = ws.p("wt/" + name)
    os.makedirs(top, exist_ok=True)
    d = os.path.join(top, os.path.basename(repo))
    git(repo, "worktree", "add", "-q", "-b", name, d, *([base] if base else []))
    write(os.path.join(d, "fix.c"), "int %s;\n" % name.replace("-", "_"))
    git(d, "add", "fix.c")
    git(d, "commit", "-q", "-m", "fix: %s" % name)
    return top, d


def squash(repo, branch, body):
    git(repo, "merge", "-q", "--squash", branch)
    git(repo, "commit", "-q", "-m", "area: what changed\n\n" + body)


def plan(ws, top, **kw):
    kw.setdefault("states", states(ws, TRK_1=DONE))
    return ws.plan(top, **kw)


def rule(p):
    assert len(p["items"]) == 1, p["refused"]
    return p["items"][0]["rule"]


# ---- the merged forms ----------------------------------------------------------------


def test_an_ancestor_of_main_goes_young_and_with_build_output(ws):
    top, d = add(ws, "ff")
    git(ws.game, "merge", "-q", "--ff-only", "ff")
    write(os.path.join(d, "build-mac.log"), "cc ...\n")
    write(os.path.join(top, "pc3.out"), "a probe dump\n")
    r = rule(plan(ws, top))
    assert r.startswith("a merged worktree, its notes kept (demo: ff ")
    # The files that go are counted on the row.
    assert r.endswith(" is in main); 2 untracked files go (20 B): demo/build-mac.log, pc3.out")
    # Without the tracker it is judged as before: young, untracked files.
    why = ws.why(ws.plan(top), "wt/ff")
    assert why.startswith("untracked files not under notes/ff/")


def test_a_squash_merge_naming_the_tip_by_a_short_sha(ws):
    top, d = add(ws, "sq")
    tip = head(d)
    # main moves on first, so neither ancestry nor the tree tells it.
    write(os.path.join(ws.game, "other.c"), "int other;\n")
    git(ws.game, "add", "other.c")
    git(ws.game, "commit", "-q", "-m", "other")
    p = plan(ws, top)
    assert (
        ws.why(p, "wt/sq")
        == "written within 7 days; not merged: demo: sq %s is not in main" % (tip[:12])
    )
    squash(ws.game, "sq", "Branch sq, tip %s.\n" % tip[:7])
    assert "demo: sq squash-merged, tip %s in main" % tip[:7] in rule(plan(ws, top))


def test_a_tip_line_of_another_sha_is_not_a_merge(ws):
    top, d = add(ws, "near")
    write(os.path.join(ws.game, "other.c"), "int other;\n")
    git(ws.game, "add", "other.c")
    git(ws.game, "commit", "-q", "-m", "other")
    git(ws.game, "commit", "-q", "--allow-empty", "-m", "x\n\ntip %s\n" % ("0" * 7))
    # Six hex digits are no tip.
    git(ws.game, "commit", "-q", "--allow-empty", "-m", "x\n\ntip %s\n" % head(d)[:6])
    assert "not merged" in ws.why(plan(ws, top), "wt/near")


def test_an_equal_tree_alone_is_not_a_merge(ws):
    # A squash body here always names the tip; a tree equal to main's is
    # also what a revert or an empty commit leaves.
    top, d = add(ws, "eq")
    squash(ws.game, "eq", "No tip line in this body.\n")
    assert "demo: eq %s is not in main" % head(d)[:12] in ws.why(plan(ws, top), "wt/eq")
    top, d = add(ws, "rv")
    git(d, "revert", "--no-edit", "HEAD")
    assert "demo: rv %s is not in main" % head(d)[:12] in ws.why(plan(ws, top), "wt/rv")
    top = ws.worktree("empty")
    d = os.path.join(top, "demo")
    git(d, "commit", "-q", "--allow-empty", "-m", "nothing")
    assert "demo: empty %s is not in main" % head(d)[:12] in ws.why(plan(ws, top), "wt/empty")


def test_a_clone_is_never_merged(ws):
    # Its commits may live only in the dir: an ancestor of its own main
    # tells nothing.
    top = ws.p("wt/cl")
    d = os.path.join(top, "demo")
    git(ws.d, "clone", "-q", ws.game, d)
    write(os.path.join(d, "fix.c"), "int cl;\n")
    git(d, "add", "fix.c")
    git(d, "commit", "-q", "-m", "only here")
    assert ws.why(plan(ws, top), "wt/cl") == (
        "written within 7 days; not merged: demo is no linked worktree"
    )


@pytest.mark.parametrize(
    "how",
    [
        ("merge", "-q", "--ff-only", "main"),
        ("rebase", "-q", "main"),
        ("pull", "-q", "--ff-only", ".", "main"),
    ],
)
def test_a_worktree_that_caught_up_with_main_is_not_merged(ws, how):
    top = ws.worktree("up")
    d = os.path.join(top, "demo")
    write(os.path.join(ws.game, "other.c"), "int other;\n")
    git(ws.game, "add", "other.c")
    git(ws.game, "commit", "-q", "-m", "other")
    git(d, *how)
    assert head(d) == head(ws.game)
    write(os.path.join(d, "new.c"), "int new_work;\n")
    why = ws.why(plan(ws, top), "wt/up")
    assert why.startswith("untracked files not under notes/up/: demo/new.c; not merged: ")
    assert why.endswith("demo: HEAD %s is no commit made in it" % head(d)[:12])


def test_a_rebase_with_work_of_its_own_is_its_commit(ws):
    top, d = add(ws, "rb")
    write(os.path.join(ws.game, "other.c"), "int other;\n")
    git(ws.game, "add", "other.c")
    git(ws.game, "commit", "-q", "-m", "other")
    git(d, "rebase", "-q", "main")
    git(ws.game, "merge", "-q", "--ff-only", "rb")
    assert "demo: rb %s is in main" % head(d)[:12] in rule(plan(ws, top))


def test_a_checkout_under_an_ignored_dir_is_judged_too(ws):
    top, d = add(ws, "ign")
    write(os.path.join(d, ".gitignore"), "scratch/\n")
    git(d, "add", ".gitignore")
    git(d, "commit", "-q", "-m", "ignore scratch")
    git(ws.game, "merge", "-q", "--ff-only", "ign")
    t = os.path.join(d, "scratch", "tool")
    git(ws.tool, "worktree", "add", "-q", "-b", "scratchwork", t)
    write(os.path.join(t, "w.c"), "int w;\n")
    git(t, "add", "w.c")
    git(t, "commit", "-q", "-m", "unmerged work")
    why = ws.why(plan(ws, top), "wt/ign")
    assert (
        why
        == "written within 7 days; not merged: demo/scratch/tool: scratchwork %s is not in dev"
        % (head(t)[:12])
    )


def test_the_configured_integration_branch_is_the_one_read(ws):
    top, d = add(ws, "tk", repo=ws.tool)
    git(ws.tool, "checkout", "-q", "main")
    git(ws.tool, "merge", "-q", "--ff-only", "tk")
    git(ws.tool, "checkout", "-q", "dev")
    # Merged into main, which is not this repo's integration branch.
    assert "tool: tk %s is not in dev" % head(d)[:12] in ws.why(plan(ws, top), "wt/tk")
    git(ws.tool, "merge", "-q", "--ff-only", "tk")
    assert "tool: tk " in rule(plan(ws, top)) and " is in dev" in rule(plan(ws, top))


def test_every_checkout_of_the_dir_is_named_when_all_are_merged(ws):
    top, _d = add(ws, "both")
    add(ws, "both", repo=ws.tool)
    git(ws.game, "merge", "-q", "--ff-only", "both")
    git(ws.tool, "merge", "-q", "--ff-only", "both")
    r = rule(plan(ws, top))
    assert "demo: both " in r and "tool: both " in r


# ---- the refusals --------------------------------------------------------------------


def test_one_unmerged_checkout_of_several_keeps_the_dir(ws):
    top, _d = add(ws, "half")
    _top, t = add(ws, "half", repo=ws.tool)
    git(ws.game, "merge", "-q", "--ff-only", "half")
    why = ws.why(plan(ws, top), "wt/half")
    assert why == "written within 7 days; not merged: tool: half %s is not in dev" % head(t)[:12]


def test_a_tracked_edit_keeps_a_merged_worktree(ws):
    top, d = add(ws, "edit")
    git(ws.game, "merge", "-q", "--ff-only", "edit")
    write(os.path.join(d, "TASKS.md"), "# edited\n")
    why = ws.why(plan(ws, top), "wt/edit")
    assert why.startswith("uncommitted changes in") and "TASKS.md" in why


def test_a_fresh_worktree_is_not_merged(ws):
    # Its HEAD is main's tip with main's tree, and no commit of its own:
    # an agent is about to work in it.
    top = ws.worktree("fresh")
    why = ws.why(plan(ws, top), "wt/fresh")
    assert (
        why
        == "written within 7 days; not merged: demo: HEAD %s is no commit made in it"
        % (head(ws.game)[:12])
    )


def age(top, days):
    """Backdates everything in top, top included, by days."""
    t = time.time() - days * 86400
    for dp, dirs, files in os.walk(top, topdown=False):
        for n in dirs + files:
            os.utime(os.path.join(dp, n), (t, t), follow_symlinks=False)
    os.utime(top, (t, t))


def test_notes_and_scripts_are_copied_and_the_rest_listed(ws):
    top, d = add(ws, "notes")
    git(ws.game, "merge", "-q", "--ff-only", "notes")
    keep = {
        "demo/resume.md": "where I stopped\n",
        "NOTES.TXT": "beside, upper case\n",
        "demo/scratch/probe.Py": "print(1)\n",
        "run.sh": "blinx2 bench\n",
        "a.markdown": "a\n",
        "b.rst": "b\n",
    }
    for rel, text in keep.items():
        write(os.path.join(top, rel), text)
    write(os.path.join(d, "save.bin"), "a game save\n")
    write(os.path.join(top, "logs/run.log"), "frame 1\n")
    # A CMake build tree's .txt and .py files are its own: they go.
    write(os.path.join(top, "ctest-tk/CMakeCache.txt"), "c\n")
    write(os.path.join(top, "ctest-tk/_deps/x/tool.py"), "p\n")
    # A patch may be the only copy of some work.
    keep["fix.patch"] = "--- a\n+++ b\n"
    write(os.path.join(top, "fix.patch"), keep["fix.patch"])
    # A copy already under notes/<name>/ (by sha) is not copied again.
    write(os.path.join(top, "done.md"), "kept already\n")
    write(ws.p("notes/notes/old/done.md"), "kept already\n")
    r = rule(plan(ws, top))
    assert r.startswith("a merged worktree, its notes kept (demo: notes ")
    assert r.endswith(
        "; keeps 7 notes or scripts (73 B) in notes/notes/"
        "; 4 untracked files go (24 B): ctest-tk/CMakeCache.txt, ctest-tk/_deps/x/tool.py,"
        " demo/save.bin, logs/run.log"
    )
    pid, _p = ws.saved(top, states=states(ws, TRK_1=DONE))
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1 and not os.path.lexists(top)
    for rel, text in keep.items():
        with open(ws.p("notes/notes/" + rel), encoding="utf-8") as f:
            assert f.read() == text
    assert not os.path.lexists(ws.p("notes/notes/done.md"))
    assert not os.path.lexists(ws.p("notes/notes/demo/save.bin"))


def test_a_note_whose_place_in_notes_holds_other_bytes_keeps_the_dir(ws):
    top, d = add(ws, "clash")
    git(ws.game, "merge", "-q", "--ff-only", "clash")
    write(os.path.join(d, "resume.md"), "the worktree's\n")
    write(ws.p("notes/clash/demo/resume.md"), "an older copy\n")
    why = ws.why(plan(ws, top), "wt/clash")
    assert why == "notes/clash/ already has other bytes at: demo/resume.md"


def test_clean_deletes_nothing_when_a_copy_cannot_be_made(ws):
    top, d = add(ws, "late")
    git(ws.game, "merge", "-q", "--ff-only", "late")
    write(os.path.join(d, "resume.md"), "mine\n")
    it = plan(ws, top)["items"][0]
    assert it["copy"] == [[os.path.join(d, "resume.md"), ws.p("notes/late/demo/resume.md")]]
    write(ws.p("notes/late/demo/resume.md"), "written since the plan\n")
    with pytest.raises(OSError, match="other bytes: nothing deleted"):
        clean.remove_local(ws.cfg, it)
    assert os.path.isfile(os.path.join(d, "resume.md"))


def test_a_dir_with_no_checkout_left_goes(ws):
    top = ws.p("wt/gone")
    write(os.path.join(top, "NOTES.md"), "what the agent learnt\n")
    write(os.path.join(top, "build/out.o"), "obj\n")
    r = rule(plan(ws, top))
    assert r == (
        "a worktree dir with no checkout left, its notes kept"
        "; keeps 1 note or script (22 B) in notes/gone/"
        "; 1 untracked file goes (4 B): build/out.o"
    )
    # Without the tracker (no live check) it is judged as before.
    assert ws.why(ws.plan(top), "wt/gone").startswith("untracked files not under notes/gone/")
    pid, _p = ws.saved(top, states=states(ws, TRK_1=DONE))
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1 and not os.path.lexists(top)
    assert os.path.isfile(ws.p("notes/gone/NOTES.md"))


def test_a_started_item_keeps_a_dir_with_no_checkout(ws):
    top = ws.p("wt/trk-7")
    write(os.path.join(top, "out.bin"), "x\n")
    st = states(ws, TRK_7=OPEN)
    assert ws.why(ws.plan(top, states=st), "wt/trk-7").startswith("live: TRK-7 ")


def test_a_git_hidden_under_an_ignored_dir_keeps_a_dir_with_no_checkout(ws):
    # A clone under build/ (what a .gitignore would hide): its commits may
    # live only there.
    top = ws.p("wt/hidden")
    write(os.path.join(top, "NOTES.md"), "n\n")
    lib = os.path.join(top, "build/_deps/lib")
    write(os.path.join(lib, "lib.c"), "int lib;\n")
    git(lib, "init", "-q", "-b", "main")
    git(lib, "add", "-A")
    git(lib, "commit", "-q", "-m", "only here")
    why = ws.why(plan(ws, top), "wt/hidden")
    assert why.endswith("not merged: build/_deps/lib is no linked worktree")


def test_a_git_name_in_any_case_or_an_unread_dir_keeps_the_dir(ws):
    top = ws.p("wt/odd")
    write(os.path.join(top, "logs/.GIT"), "gitdir: /nowhere\n")
    why = ws.why(plan(ws, top), "wt/odd")
    # APFS is case-insensitive: it is a checkout to judge, and git
    # cannot read it.
    assert why.startswith("git status failed in ") and why.endswith("/wt/odd/logs")
    os.unlink(os.path.join(top, "logs/.GIT"))
    os.makedirs(os.path.join(top, "locked"))
    os.chmod(os.path.join(top, "locked"), 0)
    try:
        why = ws.why(plan(ws, top), "wt/odd")
    finally:
        os.chmod(os.path.join(top, "locked"), 0o755)
    assert why == "cannot read %s" % os.path.join(top, "locked")


def detached(ws, name, at):
    top = ws.p("wt/" + name)
    d = os.path.join(top, "demo")
    os.makedirs(top)
    git(ws.game, "worktree", "add", "-q", "--detach", d, at)
    return top, d


def test_an_old_detached_checkout_of_a_merged_commit_goes(ws):
    top, _d = detached(ws, "probe", "main")
    age(top, 2)
    r = rule(plan(ws, top))
    assert r == (
        "a merged worktree, its notes kept (demo: detached HEAD %s is in main, "
        "nothing written for 2 days)" % head(ws.game)[:12]
    )


def test_a_young_detached_checkout_of_a_merged_commit_stays(ws):
    top, _d = detached(ws, "probe2", "main")
    why = ws.why(plan(ws, top), "wt/probe2")
    assert why == (
        "written within 7 days; not merged: demo: HEAD %s is no commit made in it, "
        "and the dir was written within a day" % head(ws.game)[:12]
    )


def test_an_old_detached_checkout_off_main_stays(ws):
    top, d = add(ws, "side")
    tip = head(d)
    other, _o = detached(ws, "side-probe", tip)
    age(other, 2)
    why = ws.why(plan(ws, other), "wt/side-probe")
    assert why == (
        "written within 7 days; not merged: demo: HEAD %s is no commit made in it" % tip[:12]
    )


def test_an_old_detached_checkout_with_a_tracked_edit_stays(ws):
    top, d = detached(ws, "probe3", "main")
    write(os.path.join(d, "TASKS.md"), "# edited\n")
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/probe3")
    assert why is not None and "TASKS.md" in why


def test_a_started_item_keeps_the_worktree(ws):
    top, _d = add(ws, "agentx")
    git(ws.game, "merge", "-q", "--ff-only", "agentx")
    st = states(ws, TRK_5=OPEN + (["agent:agentx"],))
    assert ws.why(ws.plan(top, states=st), "wt/agentx") == "live: TRK-5 In Progress is agent:agentx"
    # An item number in the name is its item.
    top2, _d = add(ws, "trk-6")
    git(ws.game, "merge", "-q", "--ff-only", "trk-6")
    st = states(ws, TRK_6=("In Review", "started"))
    assert ws.why(ws.plan(top2, states=st), "wt/trk-6") == "live: TRK-6 In Review"
    st = states(ws, TRK_6=DONE)
    assert rule(ws.plan(top2, states=st)).startswith("a merged worktree")


def test_a_running_process_keeps_the_worktree(ws):
    top, d = add(ws, "busy")
    git(ws.game, "merge", "-q", "--ff-only", "busy")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=d)
    try:
        why = ws.why(plan(ws, top), "wt/busy")
        assert why.startswith("live: pid %d (" % proc.pid) and why.endswith(" has demo open")
    finally:
        proc.kill()
        proc.wait()
    assert rule(plan(ws, top)).startswith("a merged worktree")


def test_unlisted_processes_turn_the_merged_rule_off(ws, monkeypatch):
    top, _d = add(ws, "blind")
    git(ws.game, "merge", "-q", "--ff-only", "blind")
    monkeypatch.setattr(clean, "processes", lambda: None)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=_d)
    try:
        why = ws.why(plan(ws, top), "wt/blind")
    finally:
        proc.kill()
        proc.wait()
    assert why == "written within 7 days; not merged: the processes could not be listed"


# ---- clean ---------------------------------------------------------------------------


def test_clean_removes_and_prunes_every_repo(ws):
    top, d = add(ws, "done")
    _top, t = add(ws, "done", repo=ws.tool)
    git(ws.game, "merge", "-q", "--ff-only", "done")
    squash(ws.tool, "done", "Branch done, tip %s.\n" % head(t))
    write(os.path.join(d, "build/CMakeCache.txt"), "x\n")
    write(os.path.join(t, "probe.out"), "x\n")
    st = states(ws, TRK_1=DONE)
    pid, p = ws.saved(top, states=st)
    assert ws.planned(p) == ["wt/done"]
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1 and not os.path.lexists(top)
    for repo in (ws.game, ws.tool):
        listed = git(repo, "worktree", "list", "--porcelain").stdout.decode()
        assert "wt/done" not in listed
        admin = os.path.join(repo, ".git", "worktrees")
        assert not os.path.isdir(admin) or not os.listdir(admin)
        # The branch stays, so the shas the merge body cites stay reachable.
        git(repo, "rev-parse", "--verify", "done")
    row = ws.db.one("SELECT reason, result FROM deletions WHERE relpath LIKE '%done'")
    assert row["result"] == "deleted" and row["reason"].startswith("a merged worktree")


def test_clean_prunes_a_nested_worktree(ws):
    top, d = add(ws, "nest")
    git(ws.game, "merge", "-q", "--ff-only", "nest")
    # A merged worktree of the tool repo inside the game checkout.
    inner = os.path.join(d, "sub", "tool")
    git(ws.tool, "worktree", "add", "-q", "-b", "inner", inner)
    write(os.path.join(inner, "in.c"), "int in_;\n")
    git(inner, "add", "in.c")
    git(inner, "commit", "-q", "-m", "inner work")
    git(ws.tool, "merge", "-q", "--ff-only", "inner")
    st = states(ws, TRK_1=DONE)
    pid, p = ws.saved(top, states=st)
    assert "demo/sub/tool: inner " in rule(p)
    out, _lines = ws.clean(pid)
    assert out["deleted"] == 1
    assert "sub/tool" not in git(ws.tool, "worktree", "list").stdout.decode()


def test_a_relative_gitdir_finds_its_main_checkout(tmp_path):
    d = tmp_path / "wt" / "x" / "demo"
    d.mkdir(parents=True)
    (d / ".git").write_text("gitdir: ../../../demo/.git/worktrees/demo\n")
    assert clean.main_of(str(d)) == str(tmp_path / "demo")


def test_the_integration_table_is_checked(tmp_path):
    from benchdb_util import workspace

    with pytest.raises(config.ConfigError, match="clean.integration.tk"):
        workspace(str(tmp_path), '\n[clean.integration]\ntk = "a..b"\n')
    cfg = workspace(str(tmp_path / "ok"), '\n[clean.integration]\ntk = "posix-host/portability"\n')
    assert cfg.integration == {str(tmp_path / "ok" / "tk"): "posix-host/portability"}


# ---- Opus review 2 probes ------------------------------------------------------------


def bare(ws, path):
    git(ws.game, "clone", "-q", "--bare", ws.game, path)


@pytest.mark.parametrize("where", ["backup.git", "logs/deep/mirror"])
def test_a_bare_clone_keeps_a_dir(ws, where):
    top = ws.p("wt/bare")
    os.makedirs(top)
    bare(ws, os.path.join(top, where))
    why = ws.why(plan(ws, top), "wt/bare")
    assert why.endswith("not merged: no checkout judged, yet %s is a git dir" % where)


def test_a_separate_git_dir_keeps_a_merged_worktree(ws):
    top, _d = add(ws, "sep")
    git(ws.game, "merge", "-q", "--ff-only", "sep")
    # git init --separate-git-dir: the repo is a plain dir, its checkout
    # elsewhere (here outside the dir).
    other = ws.p("outside/sepwork")
    os.makedirs(other)
    git(other, "init", "-q", "--separate-git-dir", os.path.join(top, "repo-data"))
    why = ws.why(plan(ws, top), "wt/sep")
    assert why.endswith("not merged: repo-data is a git dir")


def test_a_gitdir_file_keeps_a_merged_worktree(ws):
    top, _d = add(ws, "ptr")
    git(ws.game, "merge", "-q", "--ff-only", "ptr")
    write(os.path.join(top, "saved/dotgit"), "gitdir: /elsewhere/.git/worktrees/x\n")
    why = ws.why(plan(ws, top), "wt/ptr")
    assert why.endswith("not merged: saved/dotgit points at a git dir")


def test_a_bundle_keeps_a_dir(ws):
    top = ws.p("wt/bun")
    os.makedirs(top)
    git(ws.game, "bundle", "create", "-q", os.path.join(top, "work.BUNDLE"), "main")
    why = ws.why(plan(ws, top), "wt/bun")
    assert why.endswith("not merged: no checkout judged, yet work.BUNDLE is a git bundle")


def test_an_upper_case_git_dir_is_a_checkout(ws):
    top, d = add(ws, "upper")
    git(ws.game, "merge", "-q", "--ff-only", "upper")
    clone = os.path.join(top, "scratch/clone")
    git(ws.game, "clone", "-q", ws.game, clone)
    os.rename(os.path.join(clone, ".git"), os.path.join(clone, ".GIT"))
    assert os.path.normpath(clone) in clean.git_dirs(top)
    assert os.path.normpath(clone) in clean.all_git_dirs(top)
    assert ws.why(plan(ws, top), "wt/upper") is not None


@pytest.mark.parametrize("back", ["main", "branch"])
def test_a_commit_only_in_the_head_reflog_keeps_the_worktree(ws, back):
    top, d = add(ws, "orph")
    git(ws.game, "merge", "-q", "--ff-only", "orph")
    git(d, "checkout", "-q", "--detach")
    write(os.path.join(d, "lost.c"), "int lost;\n")
    git(d, "add", "lost.c")
    git(d, "commit", "-q", "-m", "only here")
    lost = head(d)
    if back == "main":
        # Detached at main's tip again (main is out in its own checkout).
        git(d, "checkout", "-q", "--detach", "main")
    else:
        git(d, "checkout", "-q", "orph")
    why = ws.why(plan(ws, top), "wt/orph")
    assert why.endswith("demo: commit %s is only in its HEAD reflog" % lost[:12])
    # Once a branch holds it, the worktree is merged as before.
    git(ws.game, "branch", "keep-it", lost)
    if back == "branch":
        assert rule(plan(ws, top)).startswith("a merged worktree")


def test_an_amended_commit_is_not_lost_work(ws):
    top, d = add(ws, "amend")
    git(d, "commit", "-q", "--amend", "-m", "fix: amend, reworded")
    git(ws.game, "merge", "-q", "--ff-only", "amend")
    assert rule(plan(ws, top)).startswith("a merged worktree")


def test_a_note_over_the_ref_file_limit_keeps_the_dir(ws):
    top, d = add(ws, "huge")
    git(ws.game, "merge", "-q", "--ff-only", "huge")
    with open(os.path.join(top, "trace.txt"), "wb") as f:
        f.write(b"x" * (9 << 20))
    why = ws.why(plan(ws, top), "wt/huge")
    assert why == "notes or scripts over 8 MB, too big for notes/: trace.txt"


def test_a_cmake_cache_at_the_top_or_a_checkout_root_hides_no_notes(ws):
    top, d = add(ws, "root")
    git(ws.game, "merge", "-q", "--ff-only", "root")
    write(os.path.join(top, "CMakeCache.txt"), "stray\n")
    write(os.path.join(top, "NOTES.md"), "n\n")
    write(os.path.join(d, "CMakeCache.txt"), "stray\n")
    write(os.path.join(d, "resume.md"), "r\n")
    r = rule(plan(ws, top))
    # Both CMakeCache.txt are notes too (a .txt): 4 kept.
    assert "; keeps 4 notes or scripts (" in r, r


def test_a_venv_beside_the_checkouts_is_build_output(ws):
    top, _d = add(ws, "venv")
    git(ws.game, "merge", "-q", "--ff-only", "venv")
    write(os.path.join(top, ".venv/lib/site.py"), "x\n")
    write(os.path.join(top, "tools/node_modules/a/README.md"), "x\n")
    r = rule(plan(ws, top))
    assert r.endswith(" is in main)"), r
    # Without the tracker it no longer counts as an untracked file either.
    assert ws.why(ws.plan(top), "wt/venv").startswith("written within")


# ---- Opus review 3 probes ------------------------------------------------------------


def env_git(d, *a, **env):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, env=dict(os.environ, **env))


def own(d, name="w.c", text="int w;\n", msg="only here"):
    write(os.path.join(d, name), text)
    git(d, "add", name)
    git(d, "commit", "-q", "-m", msg)
    return head(d)


def test_an_aborted_rebase_exempts_nothing(ws):
    top, d = detached(ws, "ab", "main")
    lost = own(d, "work.c")
    # main moves on with a conflicting change, so the rebase stops.
    write(os.path.join(ws.game, "work.c"), "int other;\n")
    git(ws.game, "add", "work.c")
    git(ws.game, "commit", "-q", "-m", "m")
    assert env_git(d, "rebase", "main").returncode != 0
    git(d, "rebase", "--abort")
    git(d, "checkout", "-q", "--detach", "main")
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/ab")
    assert why.endswith("demo: commit %s is only in its HEAD reflog" % lost[:12])


def test_a_merge_made_in_a_detached_worktree_is_its_work(ws):
    top, d = add(ws, "side")
    git(ws.game, "merge", "-q", "--ff-only", "side")
    git(ws.game, "checkout", "-q", "-b", "other")
    own(ws.game, "o.c", msg="o")
    git(ws.game, "checkout", "-q", "main")
    git(d, "checkout", "-q", "--detach", "main")
    git(d, "merge", "-q", "--no-ff", "-m", "resolution work", "other")
    m = head(d)
    assert clean.OWN_WORK_RE.match(
        git(d, "log", "-g", "-1", "--format=%gs", "HEAD").stdout.decode().strip()
    )
    git(d, "checkout", "-q", "--detach", "main")
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/side")
    assert why.endswith("demo: commit %s is only in its HEAD reflog" % m[:12])


def test_a_rebase_under_way_keeps_the_worktree(ws):
    top, d = detached(ws, "rip", "main")
    own(d)
    own(ws.game, "z.c", msg="m")
    editor = 'sh -c \'printf "break\\n" | cat - "$1" > "$1.n" && mv "$1.n" "$1"\' -'
    env_git(d, "rebase", "-i", "main", GIT_SEQUENCE_EDITOR=editor)
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/rip")
    assert why.endswith("demo: rebase-merge in its git dir")


@pytest.mark.parametrize("op", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG", "sequencer"])
def test_an_operation_under_way_keeps_the_worktree(ws, op):
    top, d = add(ws, "op")
    git(ws.game, "merge", "-q", "--ff-only", "op")
    gd = git(d, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
    if op == "sequencer":
        os.makedirs(os.path.join(gd, op))
    else:
        write(os.path.join(gd, op), head(d) + "\n")
    why = ws.why(plan(ws, top), "wt/op")
    assert why is not None and why.endswith("demo: %s in its git dir" % op)


def test_a_locked_worktree_stays(ws):
    top, d = add(ws, "lk")
    git(ws.game, "merge", "-q", "--ff-only", "lk")
    git(ws.game, "worktree", "lock", "--reason", "in use on usb", d)
    why = ws.why(plan(ws, top), "wt/lk")
    assert why.endswith("demo: locked in its git dir")


def test_a_ref_of_the_worktree_alone_does_not_keep_a_commit(ws):
    top, d = add(ws, "wr")
    git(ws.game, "merge", "-q", "--ff-only", "wr")
    git(d, "checkout", "-q", "-b", "tmp")
    q = own(d, "q.c")
    git(d, "update-ref", "refs/worktree/keep", q)
    git(d, "checkout", "-q", "wr")
    git(d, "branch", "-q", "-D", "tmp")
    why = ws.why(plan(ws, top), "wt/wr")
    assert why.endswith("demo: commit %s is only in its HEAD reflog" % q[:12])


def test_a_stash_is_a_shared_ref(ws):
    top, d = add(ws, "st")
    git(ws.game, "merge", "-q", "--ff-only", "st")
    write(os.path.join(d, "fix.c"), "int changed;\n")
    git(d, "stash", "-q")
    assert rule(plan(ws, top)).startswith("a merged worktree")


@pytest.mark.parametrize("reset_back", [False, True])
def test_an_amend_exempts_only_its_predecessor(ws, reset_back):
    top, d = detached(ws, "am", "main")
    c1 = own(d, msg="v1")
    write(os.path.join(d, "w.c"), "int w2;\n")
    git(d, "commit", "-qa", "--amend", "-m", "v2")
    c2 = head(d)
    if reset_back:
        git(d, "reset", "-q", "--hard", c1)
    git(d, "checkout", "-q", "--detach", "main")
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/am")
    # v2 is lost either way; with the reset v1 is checked again too.
    assert why is not None and why.endswith("is only in its HEAD reflog")
    assert c2[:12] in why or c1[:12] in why


def test_links_out_of_the_dir_are_never_followed(ws, tmp_path):
    top, d = add(ws, "ln")
    git(ws.game, "merge", "-q", "--ff-only", "ln")
    out = tmp_path / "precious"
    out.mkdir()
    (out / "a.c").write_text("x")
    os.symlink(str(out), os.path.join(top, "link"))
    os.symlink(str(out), os.path.join(d, "link2"))
    os.symlink(str(out / "a.c"), os.path.join(top, "NOTES.md"))
    pid, _p = ws.saved(top, states=states(ws, TRK_1=DONE))
    ws.clean(pid)
    assert (out / "a.c").read_text() == "x"


def test_windows_and_shebang_scripts_are_kept(ws):
    top, _d = add(ws, "scr")
    git(ws.game, "merge", "-q", "--ff-only", "scr")
    for n in ("run.ps1", "go.BAT", "x.cmd"):
        write(os.path.join(top, n), "echo\n")
    write(os.path.join(top, "bin/probe"), "#!/bin/sh\necho\n")
    write(os.path.join(top, "bin/blob"), "\x7fELF\n")
    r = rule(plan(ws, top))
    assert "; keeps 4 notes or scripts (" in r and r.endswith("goes (5 B): bin/blob"), r


# ---- Opus review 4 probes ------------------------------------------------------------


def git_dir(d):
    return git(d, "rev-parse", "--absolute-git-dir").stdout.decode().strip()


@pytest.mark.parametrize("where", ["top", "checkout"])
def test_a_fifo_never_hangs_plan_or_clean(ws, where):
    import signal

    top, d = add(ws, "fifo")
    git(ws.game, "merge", "-q", "--ff-only", "fifo")
    os.mkfifo(os.path.join(top if where == "top" else d, "probe"))
    write(os.path.join(top, "run"), "#!/bin/sh\necho\n")

    def hung(*_a):
        raise TimeoutError("opened a FIFO")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(60)
    try:
        r = rule(plan(ws, top))
        assert "; keeps 1 note or script (" in r, r
        pid, _p = ws.saved(top, states=states(ws, TRK_1=DONE))
        out, _lines = ws.clean(pid)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert out["deleted"] == 1 and not os.path.lexists(top)


@pytest.mark.parametrize("how", ["deleted", "expired", "never written"])
def test_a_missing_head_reflog_keeps_the_worktree(ws, how):
    top, d = detached(ws, "nolog", "main")
    if how == "never written":
        git(ws.game, "config", "core.logAllRefUpdates", "false")
        os.remove(os.path.join(git_dir(d), "logs", "HEAD"))
    own(d, "a.c")
    git(d, "checkout", "-q", "--detach", "main")
    if how == "deleted":
        os.remove(os.path.join(git_dir(d), "logs", "HEAD"))
    elif how == "expired":
        git(d, "reflog", "expire", "--expire=now", "HEAD")
    age(top, 2)
    why = ws.why(plan(ws, top), "wt/nolog")
    assert why.endswith("demo: its HEAD reflog or refs cannot be read")


def test_a_branch_rebased_then_merged_is_merged(ws):
    # Two commits, rebased onto a main that moved, then fast-forwarded:
    # the first one's old sha is in the branch's own reflog, which stays.
    top, d = add(ws, "mr")
    own(d, "b.c")
    own(ws.game, "z.c", msg="z")
    git(d, "rebase", "-q", "main")
    git(ws.game, "merge", "-q", "--ff-only", "mr")
    assert rule(plan(ws, top)).startswith("a merged worktree")
