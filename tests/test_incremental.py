"""Incremental package runs (buildstate, package/incr.py): each stage's key,
what reuses it and what reruns it. Synthetic game2 tree in git, a fake
compile, the real steamos tar; no toolchain, no network.

  uv run pytest tests/test_incremental.py
"""

import json
import os
import re
import shutil
import subprocess
import tarfile

import pytest
from test_second_game import PKG2, game2_tree, write

from xboxrecomp_cli import build as build_mod
from xboxrecomp_cli import buildstate, host, manifest, pipeline
from xboxrecomp_cli import package as pkg
from xboxrecomp_cli.package import incr, steamos
from xboxrecomp_cli.package import lib as pl


def git(d, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", d] + list(args), check=True, stdout=subprocess.PIPE, env=env
    ).stdout.decode()


def commit_all(d, msg="c"):
    git(d, "add", "-A")
    git(d, "commit", "-q", "--allow-empty", "-m", msg)


# ── buildstate ───────────────────────────────────────────────────────────


def test_key_chains_upstream():
    a = buildstate.stage_key({"x": 1})
    assert a == buildstate.stage_key({"x": 1})
    assert a != buildstate.stage_key({"x": 2})
    assert buildstate.stage_key({"x": 1}, {"up": "k1"}) != buildstate.stage_key(
        {"x": 1}, {"up": "k2"}
    )


def test_check_reasons_and_outputs(d):
    st = buildstate.State(d, os.path.join(d, "state"))
    out = os.path.join(d, "out.bin")
    write(out, "abc")
    inputs = {"a": 1, "b": 2}
    key = buildstate.stage_key(inputs)
    assert st.check("s", key, inputs) == ["not built yet"]
    st.record("s", key, inputs, [out])
    assert st.check("s", key, inputs) == []
    moved = {"a": 1, "b": 3}
    assert st.check("s", buildstate.stage_key(moved), moved) == ["b changed"]
    assert st.check("s", "other", None) == ["an earlier stage ran"]
    # A touch with the same bytes is still current; new bytes are not.
    os.utime(out, ns=(1, 1))
    assert st.check("s", key, inputs) == []
    write(out, "abd")
    assert st.check("s", key, inputs) == ["out.bin changed"]
    os.remove(out)
    assert st.check("s", key, inputs) == ["out.bin is gone"]


def test_verify_hashes_a_same_size_same_mtime_change(d):
    st = buildstate.State(d, os.path.join(d, "state"))
    out = os.path.join(d, "out.bin")
    write(out, "abc")
    key = buildstate.stage_key({})
    st.record("s", key, {}, [out])
    t = os.stat(out).st_mtime_ns
    write(out, "xyz")
    os.utime(out, ns=(t, t))
    assert st.check("s", key, {}) == []  # size and mtime: trusted
    st.verify = True
    assert st.check("s", key, {}) == ["out.bin changed"]


def test_directory_output_skips_the_manifest_files(d):
    st = buildstate.State(d, os.path.join(d, "state"))
    p = os.path.join(d, "payload")
    write(os.path.join(p, "a.txt"), "a")
    st.record("s", "k", {}, [p], skip=incr.MANIFEST_FILES)
    write(os.path.join(p, "manifest.json"), "{}")
    write(os.path.join(p, "SHA256SUMS"), "")
    assert st.check("s", "k", {}) == []
    write(os.path.join(p, "b.txt"), "b")
    assert st.check("s", "k", {}) == ["payload changed"]


def test_corrupt_state_reads_empty(d):
    sdir = os.path.join(d, "state")
    os.makedirs(sdir)
    with open(os.path.join(sdir, buildstate.STATE_NAME), "w") as f:
        f.write("{not json")
    st = buildstate.State(d, sdir)
    assert st.check("s", "k", {}) == ["not built yet"]
    st.record("s", "k", {})
    assert buildstate.State(d, sdir).check("s", "k", {}) == []
    # Another schema is foreign: read as empty too.
    with open(os.path.join(sdir, buildstate.STATE_NAME), "w") as f:
        json.dump({"schema": 99, "stages": {"s": {"key": "k"}}}, f)
    assert buildstate.State(d, sdir).check("s", "k", {}) == ["not built yet"]


def test_begin_drops_the_entry(d):
    """A crash between begin and record leaves no key: the next run redoes
    the stage."""
    sdir = os.path.join(d, "state")
    st = buildstate.State(d, sdir)
    st.record("s", "k", {})
    st.begin("s")
    assert buildstate.State(d, sdir).check("s", "k", {}) == ["not built yet"]


def test_paths_move_with_the_folder(d):
    root = os.path.join(d, "game")
    write(os.path.join(root, "build", "x.exe"), "exe")
    st = buildstate.State(root, os.path.join(root, ".xbr"))
    st.record("s", "k", {}, [os.path.join(root, "build", "x.exe")])
    assert list(st.get("s")["outputs"]) == ["$ROOT/build/x.exe"]
    moved = os.path.join(d, "moved")
    shutil.copytree(root, moved)
    assert buildstate.State(moved, os.path.join(moved, ".xbr")).check("s", "k", {}) == []


def test_state_dir_in_git_dir(d):
    root = os.path.join(d, "g")
    os.makedirs(root)
    assert buildstate.state_dir(root) == os.path.join(root, ".xbr")
    git(root, "init", "-q")
    assert buildstate.state_dir(root) == os.path.join(os.path.realpath(root), ".git", "xbr")


def test_hash_cache(d, monkeypatch):
    p = os.path.join(d, "f")
    write(p, "abc")
    hc = buildstate.HashCache(os.path.join(d, "state"))
    sha = hc.sha256("game_files/f", p)
    hc.save()
    calls = []
    real = buildstate.sha256_file
    monkeypatch.setattr(buildstate, "sha256_file", lambda q: calls.append(q) or real(q))
    hc2 = buildstate.HashCache(os.path.join(d, "state"))
    assert hc2.sha256("game_files/f", p) == sha and calls == []
    write(p, "abcd")
    assert hc2.sha256("game_files/f", p) != sha and calls == [p]


# ── payload sync ─────────────────────────────────────────────────────────


def test_sync_tree_keeps_equal_files(d):
    src, dst = os.path.join(d, "src"), os.path.join(d, "dst")
    write(os.path.join(src, "same.txt"), "same")
    write(os.path.join(src, "new.txt"), "new")
    write(os.path.join(src, "sub", "x.txt"), "x2")
    write(os.path.join(dst, "same.txt"), "same")
    write(os.path.join(dst, "sub", "x.txt"), "x1")
    write(os.path.join(dst, "old.txt"), "old")
    write(os.path.join(dst, "gone", "y.txt"), "y")
    write(os.path.join(dst, "game_files", "keep.bin"), "keep")
    write(os.path.join(dst, "manifest.json"), "{}")
    ino = os.stat(os.path.join(dst, "same.txt")).st_ino
    incr.sync_tree(src, dst)
    assert os.stat(os.path.join(dst, "same.txt")).st_ino == ino
    assert open(os.path.join(dst, "sub", "x.txt")).read() == "x2"
    assert os.path.isfile(os.path.join(dst, "new.txt"))
    assert not os.path.exists(os.path.join(dst, "old.txt"))
    assert not os.path.exists(os.path.join(dst, "gone"))
    assert os.path.isfile(os.path.join(dst, "game_files", "keep.bin"))
    assert os.path.isfile(os.path.join(dst, "manifest.json"))


def test_sync_game_files(d, monkeypatch):
    monkeypatch.setattr(pl, "GAME_FILES_EXCLUDE", (".DS_Store", "skip.me"))
    src, dst = os.path.join(d, "gf"), os.path.join(d, "payload", "game_files")
    write(os.path.join(src, "a.bin"), "a")
    write(os.path.join(src, "dir", "b.bin"), "b")
    write(os.path.join(src, "skip.me"), "s")
    assert incr.sync_game_files(src, dst, pl) == 2
    assert sorted(pl.list_files(dst)) == ["a.bin", "dir/b.bin"]
    ino = os.stat(os.path.join(dst, "dir", "b.bin")).st_ino
    write(os.path.join(src, "a.bin"), "a2")
    os.remove(os.path.join(src, "dir", "b.bin"))
    write(os.path.join(src, "c.bin"), "c")
    incr.sync_game_files(src, dst, pl)
    assert sorted(pl.list_files(dst)) == ["a.bin", "c.bin"]
    assert open(os.path.join(dst, "a.bin")).read() == "a2"
    del ino


# ── package, end to end on steamos ───────────────────────────────────────


class View:
    plan = []

    def say(self, msg):
        print(msg)

    def begin(self, msg):
        pass

    def end(self, ok):
        pass


@pytest.fixture
def game(d, monkeypatch, capsys):
    """game2 in git with a toolkit in git, packaged for steamos with a fake
    compile (a counter in the exe) and the real tar."""
    with game2_tree(d) as (G, tk):
        host.g().m["package"] = dict(PKG2, targets=["windows", "steamos"])
        G.content = G.path("packaging")
        write(os.path.join(G.content, "launch.env.default.windows"), "LOG_KEEP=10\n")
        write(os.path.join(G.content, "config.default.toml"), "# none\n")
        write(G.path("LICENSE"), "MIT\n")
        write(
            G.path(".gitignore"),
            "/build-pkg-win/\n/dist/\n/Second Game Files/\n/src/game/recomp/gen/\n/build/\n"
            "/src/game/recomp/gen.key.json\n/build-logs/\n",
        )
        os.chmod(G.game_files, 0o755)
        write(os.path.join(G.game_files, "media", "a.bin"), "media")
        # An imported dump's marker: without it the plan asks for the disc.
        write(os.path.join(G.game_files, ".xbr-dump.json"), "{}\n")
        write(os.path.join(tk, "CMakeLists.txt"), "# tk\n")
        write(os.path.join(tk, "src", "runtime.c"), "int x;\n")
        for t in (G.root, tk):
            git(t, "init", "-q")
            commit_all(t)
        calls = {"build": 0, "afresh": 0, "tar": 0}
        tc = {"clang": "clang 1"}
        cache = {"type": "Release"}
        bdir = build_mod.pkg_build_dir("windows")

        def fake_build(target, *args, **kw):
            calls["build"] += 1
            write(
                os.path.join(bdir, "CMakeCache.txt"),
                "CMAKE_BUILD_TYPE:STRING=%s\n" % cache["type"],
            )
            exe = G.exe(bdir, "windows")
            write(exe, "exe %d" % calls["build"])
            return exe

        def fake_icon(b):
            out = os.path.join(b, "icon")
            write(os.path.join(out, "icon.png"), "png")
            write(os.path.join(out, "icon.key"), json.dumps({"key": {"v": 1}}))
            return out

        def stage_steamos(payload, lib, icon_dir):
            write(os.path.join(payload, "install.sh"), "#!/bin/sh\n")
            write(os.path.join(payload, "launch.sh"), "#!/bin/sh\n")

        real_tar = steamos.wrap_steamos

        def tar(*a, **k):
            calls["tar"] += 1
            return real_tar(*a, **k)

        def afresh(b):
            calls["afresh"] += 1

        monkeypatch.setattr(pkg.setup, "host_blockers", lambda t, importing=False: [])
        monkeypatch.setattr(pkg.toolkit, "exclude_pin_mark", lambda t: None)
        monkeypatch.setattr(pkg, "package_plan", lambda a, imp=None: [])
        monkeypatch.setattr(pkg, "run_plan_prefix", lambda a, plan: None)
        monkeypatch.setattr(pkg, "reads_config", lambda: False)
        monkeypatch.setattr(pkg, "check_content", lambda *a: None)
        monkeypatch.setattr(pkg, "readme", lambda t, v, lib, dst, enhance=True: write(dst, v))
        monkeypatch.setattr(pkg, "make_icon", fake_icon)
        monkeypatch.setattr(pkg.doctor, "dump_state", lambda: ("sha", True))
        monkeypatch.setattr(pl, "xbe_title_id", lambda data: pl.TITLE_ID)
        monkeypatch.setattr(build_mod, "build", fake_build)
        monkeypatch.setattr(build_mod, "start_afresh", afresh)
        monkeypatch.setattr(incr, "toolchain", lambda t, s: dict(tc))
        monkeypatch.setattr(steamos, "stage_steamos", stage_steamos)
        monkeypatch.setattr(steamos, "wrap_steamos", tar)
        monkeypatch.setattr(host, "VIEW", View())
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
        # The plan's view of the stages the fixture fakes.
        from xboxrecomp_cli.package import icon as icon_mod

        monkeypatch.setattr(icon_mod, "icon_key", lambda xbe: {"v": 1})
        monkeypatch.setattr(pkg.setup, "setup_needs", lambda t, s=False: [])
        monkeypatch.setattr(
            pipeline,
            "gen_stale_reasons",
            lambda extra=None: [] if os.path.isfile(G.gen_key) else ["no key"],
        )
        write(G.gen_key, json.dumps({"inputs": {}}))
        a = type(
            "A",
            (),
            {
                "target": "steamos",
                "out": G.path("dist"),
                "hud_pack": None,
                "seed_config": None,
                "archive": False,
                "no_build": False,
                "reconfigure": False,
                "system_tools": False,
                "allow_debug": False,
                "allow_nonstock": False,
                "verify": False,
            },
        )()

        def run(**kw):
            capsys.readouterr()
            for k, v in kw.items():
                setattr(a, k, v)
            try:
                pkg.package(a)
            finally:
                for k in kw:
                    delattr(a, k)
            return capsys.readouterr().out

        yield {
            "G": G,
            "tk": tk,
            "run": run,
            "calls": calls,
            "tc": tc,
            "a": a,
            "bdir": bdir,
            "cache": cache,
        }


def bundles(G):
    return sorted(n for n in os.listdir(G.path("dist")) if n.endswith(".tar"))


def tar_manifest(path):
    with tarfile.open(path) as t:
        name = next(n for n in t.getnames() if n.endswith("/manifest.json"))
        return json.load(t.extractfile(name))


def test_second_run_reuses_everything(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    assert calls == {"build": 1, "afresh": 0, "tar": 1}
    (b,) = bundles(G)
    mt = os.stat(G.path("dist", b)).st_mtime_ns
    out = game["run"]()
    assert calls == {"build": 1, "afresh": 0, "tar": 1}, out
    assert "is current" in out and "stage: current" in out and "bundle: current" in out
    assert os.stat(G.path("dist", b)).st_mtime_ns == mt
    # The state lives in .git: the game tree stays clean (the CLI's own
    # checkout may be dirty while it is worked on, so the version is not
    # asserted here).
    assert not pl.tree_state(G.root)["dirty"]
    assert not pl.tree_state(game["tk"])["dirty"]
    assert os.path.isfile(os.path.join(G.root, ".git", "xbr", buildstate.STATE_NAME))


def test_reproducible_built_time(game):
    G = game["G"]
    game["run"]()
    (b,) = bundles(G)
    m = tar_manifest(G.path("dist", b))
    ct = int(git(G.root, "log", "-1", "--format=%ct"))
    assert m["built"] == pl.build_time((G.root, game["tk"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert pl.build_time((G.root,)).timestamp() == ct
    assert pl.build_time((G.root,), env={"SOURCE_DATE_EPOCH": "86400"}).timestamp() == 86400
    with pytest.raises(ValueError):
        pl.build_time((G.root,), env={"SOURCE_DATE_EPOCH": "soon"})


def test_touched_game_file_restages_and_keeps_the_bundle(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    p = os.path.join(G.game_files, "media", "a.bin")
    os.utime(p, ns=(10**18, 10**18))
    out = game["run"]()
    assert "stage: " in out and "stage: current" not in out
    assert calls["build"] == 1 and calls["tar"] == 1, out
    assert "bundle: current" in out


def test_changed_game_file_rewraps(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    write(os.path.join(G.game_files, "media", "a.bin"), "other media")
    game["run"]()
    assert calls["build"] == 1 and calls["tar"] == 2
    (b,) = bundles(G)
    files = {f["path"]: f for f in tar_manifest(G.path("dist", b))["files"]}
    assert files["game_files/media/a.bin"]["size"] == len("other media")


def test_toolkit_runtime_change_recompiles_and_keeps_generate(game):
    """A src/-only toolkit commit: the toolkit's tools/ (what generate
    reads) are unchanged, so the gen key holds; the compile reruns."""
    G, tk, calls = game["G"], game["tk"], game["calls"]
    game["run"]()
    gen_before = pipeline.gen_key()
    gen_rec = buildstate.State(G.root).get("generate")
    write(os.path.join(tk, "src", "runtime.c"), "int y;\n")
    commit_all(tk, "runtime")
    assert pipeline.gen_key() == gen_before
    out = game["run"]()
    assert calls["build"] == 2, out
    assert buildstate.State(G.root).get("generate")["key"] == gen_rec["key"]
    # A new toolkit commit is a new version, so a new bundle.
    assert calls["tar"] == 2 and len(bundles(G)) == 2


def test_toolchain_change_resets_cmake(game):
    calls = game["calls"]
    game["run"]()
    game["tc"]["clang"] = "clang 2"
    out = game["run"]()
    assert calls["afresh"] == 1 and calls["build"] == 2, out
    assert "toolchain changed" in out


def test_deleted_outputs_rerun_their_stage(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    os.remove(G.exe(game["bdir"], "windows"))
    game["run"]()
    assert calls["build"] == 2
    (b,) = bundles(G)
    os.remove(G.path("dist", b))
    out = game["run"]()
    assert calls["build"] == 2 and calls["tar"] == 3, out
    assert bundles(G) == [b]


def test_corrupt_state_reruns_once(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    with open(os.path.join(G.root, ".git", "xbr", buildstate.STATE_NAME), "w") as f:
        f.write("garbage")
    game["run"]()
    assert calls["build"] == 2
    game["run"]()
    assert calls["build"] == 2


def test_crash_mid_stage_reruns_it(game, monkeypatch):
    G = game["G"]
    game["run"]()
    write(os.path.join(G.game_files, "media", "a.bin"), "changed")

    real = incr.sync_game_files

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(incr, "sync_game_files", boom)
    with pytest.raises(OSError):
        game["run"]()
    assert buildstate.State(G.root).get("stage:steamos") is None
    monkeypatch.setattr(incr, "sync_game_files", real)
    out = game["run"]()
    assert "stage: not built yet" in out, out
    assert buildstate.State(G.root).get("stage:steamos") is not None


def test_moved_folder_keeps_the_keys(game, d):
    """The records name paths under the game as $ROOT/...: the folder moved
    whole (its .git with it) reuses the build and the stage."""
    G, calls = game["G"], game["calls"]
    game["run"]()
    st = buildstate.State(G.root)
    assert all(p.startswith("$ROOT/") for rec in st.data["stages"].values() for p in rec["outputs"])
    assert G.root not in open(st.path).read()
    moved = os.path.join(d, "moved")
    shutil.copytree(G.root, moved, symlinks=True)
    G2 = manifest.load(moved)
    manifest.use(G2)
    G2.content = G2.path("packaging")
    host.g().m["package"] = dict(G.m["package"])
    game["a"].out = G2.path("dist")
    bdir = build_mod.pkg_build_dir("windows")
    assert bdir.startswith(moved)
    out = game["run"]()
    assert calls["build"] == 1, out
    assert "stage: current" in out, out


def test_rebase_keeping_the_tree_rewraps(game):
    """A new commit with the same tree: the compile is reused (its key holds
    the tree, not the commit), but the version and the manifest name the new
    commit, so the stage and the wrap run again."""
    G, calls = game["G"], game["calls"]
    game["run"]()
    (old,) = bundles(G)
    git(G.root, "commit", "-q", "--amend", "-m", "reworded")
    head = git(G.root, "rev-parse", "HEAD").strip()
    out = game["run"]()
    assert calls["build"] == 1 and calls["tar"] == 2, out
    new = [b for b in bundles(G) if b != old]
    assert len(new) == 1 and head[:7] in new[0]
    m = tar_manifest(G.path("dist", new[0]))
    assert m["sources"][pl.SOURCE_NAME]["commit"] == head
    assert m["version"] in new[0]


def test_a_rewritten_gen_recompiles(game):
    """gen/ written again (a recomp by hand) moves the generate key, and the
    compile chained on it reruns."""
    G, calls = game["G"], game["calls"]
    game["run"]()
    write(os.path.join(G.gen, "recomp_0001.c"), "/* new */\n")
    out = game["run"]()
    assert calls["build"] == 2, out


# ── phase 2: the plan, clean, the bundle records ─────────────────────────


def plan_of(game):
    from xboxrecomp_cli.package import plan as plan_mod

    return plan_mod.make_plan(game["a"], pkg.plib(), None, False, None)


def actions(plan):
    return {r["stage"]: (r["action"], r["why"]) for r in plan["stages"]}


def test_plan_before_any_run_runs_everything(game):
    p = plan_of(game)
    acts = actions(p)
    assert acts["compile:windows"][0] == "run" and acts["wrap:steamos"][0] == "run"
    assert acts["import"] == ("reuse", []) and acts["generate"] == ("reuse", [])
    assert p["bases"] == []
    # Nothing was recorded.
    assert not os.path.exists(os.path.join(G_state(game), buildstate.STATE_NAME))


def G_state(game):
    return buildstate.state_dir(game["G"].root)


def test_plan_after_a_run_reuses_everything(game):
    game["run"]()
    p = plan_of(game)
    assert {r["action"] for r in p["stages"]} == {"reuse"}, p["stages"]
    assert p["estimate_s"] == 0 and p["version"]
    (b,) = p["bases"]
    assert b["version"] == p["version"] and b["on_disk"] and b["size"] > 0
    # --plan --json through package(): one JSON document, nothing run.
    doc = json.loads(game["run"](plan=True, json=True))
    text = game["run"](plan=True)
    assert "plan for steamos" in text and "compile:windows  reuse" in text, text
    assert "base: %s" % p["version"] in text
    assert doc["stages"] == p["stages"] and game["calls"]["build"] == 1


def test_plan_names_the_toolkit_and_matches_the_run(game):
    """The spec's code-only change: import, generate and icon reused, the
    compile, stage and wrap run, the compile naming the tree."""
    tk = game["tk"]
    game["run"]()
    write(os.path.join(tk, "src", "runtime.c"), "int y;\n")
    acts = actions(plan_of(game))
    assert acts["import"][0] == acts["generate"][0] == acts["icon"][0] == "reuse"
    assert acts["compile:windows"] == ("run", ["toolkit changed"])
    assert acts["stage:steamos"][0] == acts["wrap:steamos"][0] == "run"
    out = game["run"]()
    assert game["calls"]["build"] == 2 and game["calls"]["tar"] == 2, out
    assert {r["action"] for r in plan_of(game)["stages"]} == {"reuse"}


def test_plan_sees_a_rebase_rewrap(game):
    G = game["G"]
    game["run"]()
    git(G.root, "commit", "-q", "--amend", "-m", "reworded")
    acts = actions(plan_of(game))
    assert acts["compile:windows"][0] == "reuse"
    # The version names the commit: the stage reruns, and the wrap with it.
    assert acts["stage:steamos"] == ("run", ["version changed"])
    assert acts["wrap:steamos"][0] == "run"


def test_plan_estimates_from_timings(game):
    G = game["G"]
    from xboxrecomp_cli.package import progress

    progress.save_timing(G.logs, "build windows (build-pkg-win)", 25.04)
    progress.save_timing(G.logs, "tar", 2.5)
    p = plan_of(game)
    est = {r["stage"]: r["estimate_s"] for r in p["stages"]}
    assert est["compile:windows"] == 25.0 and est["wrap:steamos"] == 2.5
    assert est["import"] is None  # reused: no estimate
    assert "stage:steamos" in p["unknown"]


def test_plan_hud_default_estimate(game):
    """--hud-pack before enhance hud ever ran here: the measured 4 to 8 s."""
    from xboxrecomp_cli.package import plan as plan_mod

    game["run"]()
    game["a"].hud_pack = ""
    monkey_hud = {"scale": 2, "languages": {}}
    from xboxrecomp_cli.enhance import hud

    orig = hud.hud_config
    hud.hud_config = lambda: dict(monkey_hud)
    try:
        p = plan_of(game)
    finally:
        hud.hud_config = orig
        game["a"].hud_pack = None
    acts = {r["stage"]: r for r in p["stages"]}
    assert acts["hud"]["action"] == "run"
    assert acts["hud"]["estimate_s"] == plan_mod.HUD_DEFAULT_S
    assert acts["stage:steamos"]["why"] == ["the HUD pack is made again"]


def test_clean_keeps_gen_and_clean_all_drops_it(game):
    G, calls = game["G"], game["calls"]
    game["run"]()
    os.makedirs(G.gen, exist_ok=True)
    write(os.path.join(G.gen, "recomp_funcs.h"), "x")
    game["run"]()
    builds = calls["build"]
    out = game["run"](clean="build")
    assert calls["build"] == builds + 1 and calls["tar"] >= 2, out
    assert os.path.isfile(G.gen_key) and os.path.isdir(G.gen)
    # The plan of a --clean run reuses generate (its key is current).
    game["a"].clean = "build"
    acts = actions(plan_of(game))
    game["a"].clean = "all"
    acts_all = actions(plan_of(game))
    del game["a"].clean
    assert acts["generate"][0] == "reuse" and acts["compile:windows"][0] == "run"
    assert acts_all["generate"] == ("run", ["--clean=all"])
    from xboxrecomp_cli import clean

    clean.package_clean("all", G.path("dist"))
    assert not os.path.exists(G.gen) and not os.path.exists(G.gen_key)
    assert os.path.isdir(G.game_files)


def test_clean_all_keeps_the_mark(game):
    from types import SimpleNamespace

    from xboxrecomp_cli import clean

    G = game["G"]
    game["run"]()
    sdir = G_state(game)
    write(os.path.join(sdir, pl.MARK_NAME), "{}")
    a = SimpleNamespace(all=True, bundles=False, build=False, yes=False, json=False)
    clean.cmd_clean(a)
    assert bundles(G)  # listed only
    a.yes = True
    clean.cmd_clean(a)
    assert os.listdir(sdir) == [pl.MARK_NAME]
    assert not bundles(G) and not os.path.exists(game["bdir"])
    assert os.path.isfile(os.path.join(G.game_files, "media", "a.bin"))


def test_clean_refuses_paths_outside_the_game(game, d):
    from types import SimpleNamespace

    from xboxrecomp_cli import clean

    G = game["G"]
    game["run"]()
    elsewhere = os.path.join(d, "elsewhere")
    shutil.move(G.path("dist"), elsewhere)
    os.symlink(elsewhere, G.path("dist"))
    a = SimpleNamespace(all=False, bundles=True, build=False, yes=True, json=False)
    with pytest.raises(host.CliError, match="outside the game folder"):
        clean.cmd_clean(a)
    assert os.listdir(elsewhere)


def test_a_third_bundle_evicts_the_first(game):
    G = game["G"]
    shas = []
    for i in range(3):
        write(G.path("notes%d.txt" % i), "n")
        commit_all(G.root, "c%d" % i)
        game["run"]()
        shas.append(sorted(bundles(G)))
    assert len(bundles(G)) == 2
    st = buildstate.State(G.root)
    b = incr.bases(st, "steamos")
    assert len(b) == 3 and [x["on_disk"] for x in b] == [True, True, False]
    assert all(
        os.path.isfile(os.path.join(incr.bundles_dir(st, "steamos"), x["sha256"] + ".json"))
        for x in b
    )
    # --keep-bundles 1: only the newest.
    write(G.path("notes9.txt"), "n")
    commit_all(G.root, "c9")
    game["run"](keep_bundles=1)
    assert len(bundles(G)) == 1


def test_a_debug_build_gets_its_own_version(game):
    """D12: the build time and the commits no longer tell two builds of
    one commit apart, so a non-stock build (here --allow-debug on a Debug
    tree) carries -o<6 hex>; the stock build keeps today's format."""
    G = game["G"]
    game["run"]()
    (stock,) = bundles(G)
    v_stock = tar_manifest(G.path("dist", stock))["version"]
    assert "-o" not in v_stock
    game["cache"]["type"] = "Debug"
    plan = json.loads(game["run"](plan=True, json=True, reconfigure=True, allow_debug=True))
    game["run"](reconfigure=True, allow_debug=True)
    new = [b for b in bundles(G) if b != stock]
    (debug,) = new
    v_debug = tar_manifest(G.path("dist", debug))["version"]
    assert v_debug != v_stock and re.search(r"-g[0-9a-f]{8}-o[0-9a-f]{6}(-dirty|$)", v_debug), (
        v_debug
    )
    # The plan, made before the rebuild, could not know the Debug cache
    # yet; one made now names the run's version.
    assert plan["version"] == v_stock
    after = json.loads(game["run"](plan=True, json=True, allow_debug=True))
    assert after["version"] == v_debug


# ── phase 4: patches ─────────────────────────────────────────────────────


def patches(G):
    return sorted(n for n in os.listdir(G.path("dist")) if "-patch-from-" in n)


def tar_names(path):
    with tarfile.open(path) as t:
        return sorted(n.split("/", 1)[1] for n in t.getnames() if "/" in n)


def recompile(game):
    write(os.path.join(game["tk"], "src", "runtime.c"), "int y;\n")
    commit_all(game["tk"], "runtime")


def first_record(game):
    st = buildstate.State(game["G"].root)
    (b,) = incr.bases(st, "steamos")
    return b


def bulk(G):
    """Game data that dwarfs the program, as on a real disc: a patch of the
    program alone is then under half the full bundle (strict --patch-from)."""
    write(os.path.join(G.game_files, "media", "big.bin"), "m" * 100000)


def test_patch_from_a_record(game):
    """--patch-from <record sha>: the tar holds the new exe, the applier,
    the manifest, the sums and patch.json, and no game file; the plan's
    base row knows the patch's size."""
    G = game["G"]
    bulk(G)
    game["run"]()
    base = first_record(game)
    recompile(game)
    out = game["run"](patch_from=base["sha256"][:12])
    assert "patch: from %s (--patch-from)" % base["version"] in out, out
    (p,) = patches(G)
    names = tar_names(G.path("dist", p))
    assert "patch.json" in names and "manifest.json" in names and "SHA256SUMS" in names
    assert any(n.endswith(".exe") for n in names), names
    assert not any(n.startswith("game_files") for n in names), names
    with tarfile.open(G.path("dist", p)) as t:
        doc = json.load(t.extractfile(next(n for n in t.getnames() if n.endswith("/patch.json"))))
    assert doc["base_version"] == base["version"] and not doc["game_data"]
    # The exe and README.txt (it names the version).
    changed = sorted(e["path"] for e in doc["changed"])
    assert changed == sorted(["README.txt"] + [n for n in names if n.endswith(".exe")])
    rows = {b["version"]: b for b in plan_of(game)["bases"]}
    assert rows[base["version"]]["patch_size"] > 0


def test_patch_from_an_unusable_base_is_refused(game, d):
    G = game["G"]
    game["run"]()
    m = tar_manifest(G.path("dist", bundles(G)[0]))
    m["target"] = "windows-x86_64"
    p = os.path.join(d, "other.json")
    write(p, json.dumps(m))
    recompile(game)
    builds = game["calls"]["build"]
    with pytest.raises(host.Refusal, match="another target.*--full") as e:
        game["run"](patch_from=p)
    # Refused on the manifests alone, before the build (D7).
    assert e.value.code == "target" and game["calls"]["build"] == builds


def test_default_patch_against_the_installed_version(game, d, monkeypatch):
    """On the host's own platform the current install is the base: a patch
    when it is under half the full bundle, else the full bundle; --full
    always makes the full one."""
    G = game["G"]
    # Game data big enough that a code-only patch is the small part.
    write(os.path.join(G.game_files, "media", "a.bin"), "m" * 100000)
    game["run"]()
    (full,) = bundles(G)
    root = os.path.join(d, "deck")
    m = tar_manifest(G.path("dist", full))
    os.makedirs(os.path.join(root, "versions", m["version"]))
    with tarfile.open(G.path("dist", full)) as t:
        data = t.extractfile(next(n for n in t.getnames() if n.endswith("/manifest.json"))).read()
    with open(os.path.join(root, "versions", m["version"], "manifest.json"), "wb") as f:
        f.write(data)
    os.symlink(os.path.join("versions", m["version"]), os.path.join(root, "current"))
    monkeypatch.setattr(host, "host_os", lambda system=None: "linux")
    monkeypatch.setenv(host.g().m["game"]["slug"].upper().replace("-", "_") + "_ROOT", root)
    recompile(game)
    out = game["run"]()
    assert "patch: from the installed %s" % m["version"] in out, out
    assert len(patches(G)) == 1
    out = game["run"](full=True)
    assert "bundle: full (--full)" in out, out
    # A bigger change than half the bundle: the full one.
    from xboxrecomp_cli.package import patch as patch_mod

    monkeypatch.setattr(patch_mod, "DEFAULT_SHARE", 0.0)
    write(G.path("notes.txt"), "n")
    commit_all(G.root, "notes")
    out = game["run"]()
    assert "bundle: full (a patch would be" in out, out


class FakeImport:
    """dump.prepare's result for a --dump whose files are already in
    game_files/ (the test writes them): the run counts as an import."""

    writes = False

    class src:
        @staticmethod
        def close():
            pass

    def run(self, warn_unknown=True):
        pass

    def plan_why(self):
        return "--dump"


def with_dump(game, monkeypatch):
    from xboxrecomp_cli import dump

    monkeypatch.setattr(dump, "prepare", lambda path, replace=False: FakeImport())
    game["a"].replace = False


def test_game_data_patch_warns(game, monkeypatch):
    """A changed game file is refused without --dump (the base was built
    from other game files); with it, the file is shipped with D10's
    warning. (A game-file change alone keeps the version, and a base at
    this version is refused: the version would be the current one.)"""
    G = game["G"]
    bulk(G)
    game["run"]()
    base = first_record(game)
    write(os.path.join(G.game_files, "media", "a.bin"), "other media")
    recompile(game)
    with pytest.raises(host.Refusal, match="built from other game files") as e:
        game["run"](patch_from=base["sha256"])
    assert e.value.code == "game_files"
    with_dump(game, monkeypatch)
    out = game["run"](patch_from=base["sha256"], dump="disc.iso")
    assert "patch: this update also changes game data" in out, out
    (p,) = patches(G)
    assert "game_files/media/a.bin" in tar_names(G.path("dist", p))


def test_patch_removing_game_files_needs_the_flag(game, monkeypatch, d):
    """A patch that would remove game files is refused, even after an
    import, unless --allow-game-file-deletes; a base that records no game
    tree is refused even with it. The plan counts the base's game-file
    changes and says why with a code."""
    G = game["G"]
    bulk(G)
    write(os.path.join(G.game_files, "media", "gone.bin"), "g" * 10)
    game["run"]()
    base = first_record(game)
    os.remove(os.path.join(G.game_files, "media", "gone.bin"))
    recompile(game)
    with_dump(game, monkeypatch)
    with pytest.raises(host.Refusal, match="remove 1 game files") as e:
        game["run"](patch_from=base["sha256"], dump="disc.iso")
    assert e.value.code == "game_deletes"
    # A manifest made before game_tree: no flag lets it remove game files.
    m = tar_manifest(G.path("dist", bundles(G)[0]))
    del m["game_tree"]
    old = os.path.join(d, "old.json")
    write(old, json.dumps(m))
    with pytest.raises(host.Refusal, match="records no game tree"):
        game["run"](patch_from=old, dump="disc.iso", allow_game_file_deletes=True)
    out = game["run"](patch_from=base["sha256"], dump="disc.iso", allow_game_file_deletes=True)
    assert "patch: this update also changes game data" in out, out
    # The plan, from the new record against the first one.
    rows = {b["version"]: b for b in plan_of(game)["bases"]}
    row = rows[base["version"]]
    assert row["game_files"] == {"changed": 0, "new": 0, "deleted": 1}, row
    assert row["why_codes"] == ["game_files"] and not row["usable"], row


def test_manifest_records_its_game_tree(game):
    """The manifest names the game files it was built from: the XBE's
    sha256, the count and the listing's digest, the same as computed from
    its entries."""
    from xboxrecomp_cli.package import patch as patch_mod

    G = game["G"]
    game["run"]()
    m = tar_manifest(G.path("dist", bundles(G)[0]))
    tree = m["game_tree"]
    games = [f for f in m["files"] if f["path"].startswith("game_files/")]
    assert tree["files"] == len(games) > 0
    xbe = next(f for f in games if f["path"] == patch_mod.xbe_path())
    assert tree["xbe_sha256"] == xbe["sha256"]
    del m["game_tree"]
    assert patch_mod.game_tree(m) == tree


def test_patches_are_listed_apart_from_bundles(tmp_path):
    """A patch never counts as one of the kept bundles (it is no use
    without its base), and the bundles never include a patch."""
    names = {
        "steamos": ("A-v1-steamos.tar", "A-v2-steamos-patch-from-c1-g1.tar"),
        "macos": ("A-v1.dmg", "A-v2-patch-from-c1-g1.dmg"),
        "windows": ("A-v1-windows", "A-v2-windows-patch-from-c1-g1"),
    }
    for target, (full, patch) in names.items():
        d = tmp_path / target
        d.mkdir()
        for n in (full, patch):
            (d / n).mkdir() if target == "windows" else (d / n).write_text("x")
        assert [os.path.basename(p) for p, _ in incr.bundles_on_disk(str(d), target, "A")] == [full]
        got = incr.bundles_on_disk(str(d), target, "A", patches=True)
        assert [os.path.basename(p) for p, _ in got] == [patch]


def test_plan_output_line(game, d, monkeypatch):
    """The plan's output prediction (D13): full with no install, a patch
    from the installed version on the host's own platform, the base a
    --patch-from names, --full; and full_size from the last bundle."""
    G = game["G"]
    p = plan_of(game)
    assert p["output"]["kind"] == "full" and p["output"]["why"] == "no install found"
    assert p["output"]["installed"] is None and p["full_size"] is None
    game["run"]()
    (full,) = bundles(G)
    base = first_record(game)
    p = plan_of(game)
    assert p["full_size"] == base["size"] > 0
    # An install of the newest bundle, current on this host.
    root = os.path.join(d, "deck")
    m = tar_manifest(G.path("dist", full))
    os.makedirs(os.path.join(root, "versions", m["version"]))
    with tarfile.open(G.path("dist", full)) as t:
        data = t.extractfile(next(n for n in t.getnames() if n.endswith("/manifest.json"))).read()
    with open(os.path.join(root, "versions", m["version"], "manifest.json"), "wb") as f:
        f.write(data)
    os.symlink(os.path.join("versions", m["version"]), os.path.join(root, "current"))
    monkeypatch.setattr(host, "host_os", lambda system=None: "linux")
    monkeypatch.setenv(host.g().m["game"]["slug"].upper().replace("-", "_") + "_ROOT", root)
    # Nothing changed: the run would make the installed version again.
    out = plan_of(game)["output"]
    assert out["kind"] == "full" and "this version already" in out["why"], out
    assert out["installed"]["version"] == m["version"] and not out["installed"]["usable"]
    # The folder the wizard passes back as --patch-from (D2).
    assert out["installed"]["path"] == os.path.join(root, "versions", m["version"])
    recompile(game)
    out = plan_of(game)["output"]
    assert out["kind"] == "patch" and out["why"] == "from the installed %s" % m["version"], out
    assert out["base"]["version"] == m["version"] and out["installed"]["usable"]
    game["a"].full = True
    try:
        full_out = plan_of(game)["output"]
    finally:
        del game["a"].full
    assert full_out == dict(out, kind="full", why="--full", base=None, patch_size=None)
    game["a"].patch_from = base["sha256"][:12]
    try:
        out = plan_of(game)["output"]
    finally:
        del game["a"].patch_from
    assert out["kind"] == "patch" and "(--patch-from)" in out["why"], out
    assert out["base"]["sha256"] == base["sha256"]


PLAN_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "wizard",
    "internal",
    "runner",
    "testdata",
    "plan-update.json",
)


def shape(v):
    """A JSON value's shape: the keys of each object and the type of each
    value, a list by the union of its items' shapes; None matches any."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        out = {}
        for x in v:
            sx = shape(x)
            out = merge(out, sx) if isinstance(sx, dict) else sx
        return [out]
    if v is None:
        return None
    return "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else type(v).__name__


def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {
            k: merge(a.get(k), b.get(k)) if k in a and k in b else a.get(k, b.get(k)) for k in a | b
        }
    return a if b is None else b


def same_shape(a, b, at="plan"):
    """[] when shape a and b agree (a None on either side is any type)."""
    if a is None or b is None:
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        out = ["%s.%s: only in one" % (at, k) for k in sorted(set(a) ^ set(b))]
        for k in sorted(set(a) & set(b)):
            out += same_shape(a[k], b[k], "%s.%s" % (at, k))
        return out
    if isinstance(a, list) and isinstance(b, list):
        return same_shape(a[0] if a else None, b[0] if b else None, at + "[]")
    return [] if a == b else ["%s: %s, the fixture has %s" % (at, a, b)]


def test_plan_json_contract(game, d, monkeypatch):
    """The wizard's ParsePlan reads wizard/internal/runner/testdata/
    plan-update.json (runner_test.go): an Update plan as `package --plan
    --json` writes it, with an install on this host and a bundle record.
    This keeps that fixture the CLI's: the same keys and value types.
    XBR_WRITE_PLAN_FIXTURE=1 writes it again."""
    G = game["G"]
    bulk(G)
    game["run"]()
    (full,) = bundles(G)
    root = os.path.join(d, "deck")
    m = tar_manifest(G.path("dist", full))
    os.makedirs(os.path.join(root, "versions", m["version"]))
    with tarfile.open(G.path("dist", full)) as t:
        data = t.extractfile(next(n for n in t.getnames() if n.endswith("/manifest.json"))).read()
    with open(os.path.join(root, "versions", m["version"], "manifest.json"), "wb") as f:
        f.write(data)
    os.symlink(os.path.join("versions", m["version"]), os.path.join(root, "current"))
    monkeypatch.setattr(host, "host_os", lambda system=None: "linux")
    monkeypatch.setenv(host.g().m["game"]["slug"].upper().replace("-", "_") + "_ROOT", root)
    recompile(game)
    doc = json.loads(game["run"](plan=True, json=True))
    assert doc["output"]["kind"] == "patch" and doc["bases"], doc
    assert all(isinstance(r["why"], list) for r in doc["stages"])
    if os.environ.get("XBR_WRITE_PLAN_FIXTURE"):
        text = json.dumps(doc, indent=1).replace(str(d), "/tmp/game") + "\n"
        with open(PLAN_FIXTURE, "w", newline="\n") as f:
            f.write(text)
    with open(PLAN_FIXTURE, encoding="utf-8") as f:
        fixture = json.load(f)
    assert same_shape(shape(doc), shape(fixture)) == []


def without_volatile(m):
    """The manifest minus the fields it declares volatile."""
    m = json.loads(json.dumps(m))
    for field in m["volatile"]:
        head, _, tail = field.partition(".*.")
        if not tail:
            m.pop(field, None)
            continue
        for v in m.get(head, {}).values():
            v.pop(tail, None)
    return m


def test_incremental_equals_clean(game, d, monkeypatch):
    """D12: a folder built at the previous commit and then incrementally
    after one code change makes the same bundle as a clean folder built at
    the new commit, apart from the declared volatile fields. The compile is
    faked as a function of its inputs, as a reproducible link is."""
    G, tk, calls = game["G"], game["tk"], game["calls"]

    def det_build(target, *args, **kw):
        calls["build"] += 1
        g = manifest.current()
        b = build_mod.pkg_build_dir("windows")
        write(os.path.join(b, "CMakeCache.txt"), "CMAKE_BUILD_TYPE:STRING=Release\n")
        exe = g.exe(b, "windows")
        write(exe, "exe of " + open(os.path.join(tk, "src", "runtime.c")).read())
        return exe

    monkeypatch.setattr(build_mod, "build", det_build)
    game["run"]()
    write(os.path.join(tk, "src", "runtime.c"), "int y;\n")
    commit_all(tk, "runtime")
    out = game["run"]()
    assert calls["build"] == 2 and "toolkit changed" in out, out
    (inc,) = [b for b in bundles(G) if git(tk, "rev-parse", "--short=7", "HEAD").strip() in b]
    m_inc = tar_manifest(G.path("dist", inc))

    # The clean folder: the same commits, none of the first folder's build
    # state, build trees or bundles.
    fresh = os.path.join(d, "fresh")
    shutil.copytree(G.root, fresh, symlinks=True)
    for p in (".git/xbr", "build-pkg-win", "dist"):
        shutil.rmtree(os.path.join(fresh, p), ignore_errors=True)
    G2 = manifest.load(fresh)
    manifest.use(G2)
    G2.content = G2.path("packaging")
    host.g().m["package"] = dict(G.m["package"])
    game["a"].out = G2.path("dist")
    out = game["run"]()
    assert calls["build"] == 3, out
    (clean_b,) = bundles(G2)
    assert clean_b == inc
    m_clean = tar_manifest(G2.path("dist", clean_b))
    assert m_clean["files"] == m_inc["files"]
    assert without_volatile(m_clean) == without_volatile(m_inc)
