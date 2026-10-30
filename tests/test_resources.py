"""Resource-aware builds: the memory, core and disk queries, the job
sizing (D3), the measured peaks, the gen/ pool in the build's argv, and the
disk refusal (D9) in build and package."""

import json
import os
import shutil
import subprocess
import sys
import types

import pytest
from test_enhance import dds_file, dxt1_blocks
from test_incremental import (
    bulk,
    first_record,
    game,  # noqa: F401 (a fixture)
    plan_of,
    recompile,
)
from test_manifest import MINIMAL
from test_second_game import PKG2, game2_tree, write

from xboxrecomp_cli import build, doctor, host, main, manifest, resources
from xboxrecomp_cli.enhance import dds, hud
from xboxrecomp_cli.package import incr, plib

MiB, GiB = resources.MiB, resources.GiB
G1220 = 1220 * MiB


@pytest.fixture
def state(d, monkeypatch):
    """The measured peaks in a scratch file."""
    p = os.path.join(d, "state", "resources.json")
    monkeypatch.setattr(resources, "state_path", lambda: p)
    return p


# ── queries ──────────────────────────────────────────────────────────────

MEMINFO = """MemTotal:       16303428 kB
MemFree:         1203400 kB
MemAvailable:    9437184 kB
Buffers:          123 kB
HugePages_Total:       0
"""

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           20000.
Pages speculative:                         5000.
Pages throttled:                              0.
Pages wired down:                         90000.
Pages purgeable:                           1000.
"""


def test_meminfo_and_vm_stat(d):
    m = resources.meminfo(MEMINFO)
    assert m["MemTotal"] == 16303428 * 1024 and m["MemAvailable"] == 9 * GiB
    assert "HugePages_Total" not in m
    assert resources.vm_stat_available(VM_STAT) == (10000 + 20000 + 5000 + 1000) * 16384
    assert resources.vm_stat_available("garbage") is None
    assert resources.vm_stat_available(None) is None
    p = os.path.join(d, "meminfo")
    write(p, MEMINFO)
    assert resources.mem_total("linux", p) == 16303428 * 1024
    assert resources.mem_available("linux", p, dirs=[]) == 9 * GiB
    assert resources.mem_total("linux", os.path.join(d, "none")) is None
    # macOS: sysctl and vm_stat through the run hook.
    out = {"sysctl": "17179869184\n", "vm_stat": VM_STAT}
    run = lambda argv: out.get(argv[0])  # noqa: E731
    assert resources.mem_total("macos", run=run) == 16 * GiB
    assert resources.mem_available("macos", run=run) == 36000 * 16384
    # A query that fails is unknown, never an error.
    assert resources.mem_total("macos", run=lambda argv: None) is None
    assert resources.mem_total("macos", run=lambda argv: "not a number") is None


def test_cgroup_limits(d):
    """A limit on a parent cgroup (a container, a slice) binds the build:
    the least headroom and quota over the folder and its ancestors."""
    root = os.path.join(d, "cg")
    leaf = os.path.join(root, "user.slice", "app.scope")
    os.makedirs(leaf)
    proc = os.path.join(d, "cgroup")
    write(proc, "0::/user.slice/app.scope\n")
    dirs = resources.cgroup_dirs(proc, root)
    assert dirs == [leaf, os.path.dirname(leaf), os.path.normpath(root)]
    assert resources.cgroup_mem_headroom(dirs) is None
    assert resources.cgroup_cpus(dirs) is None
    write(os.path.join(leaf, "memory.max"), "max\n")
    write(os.path.join(leaf, "memory.current"), "100\n")
    write(os.path.join(dirs[1], "memory.max"), str(4 * GiB) + "\n")
    write(os.path.join(dirs[1], "memory.current"), str(GiB) + "\n")
    assert resources.cgroup_mem_headroom(dirs) == 3 * GiB
    write(os.path.join(leaf, "cpu.max"), "250000 100000\n")
    write(os.path.join(dirs[1], "cpu.max"), "max 100000\n")
    assert resources.cgroup_cpus(dirs) == 3
    p = os.path.join(d, "meminfo")
    write(p, MEMINFO)
    assert resources.mem_available("linux", p, dirs=dirs) == 3 * GiB
    assert resources.cores("linux", dirs) <= 3
    # cgroup v1 or no cgroup file: no limits.
    write(proc, "12:memory:/user.slice\n")
    assert resources.cgroup_dirs(proc, root) == []
    assert resources.cgroup_dirs(os.path.join(d, "none"), root) == []


# ── sizing (D3) ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "avail,total,cores,pool,gen_jobs,jobs",
    [
        # The design's example: 16 GB, 9 GB free, 1.2 GB per gen/ job.
        (9 * GiB, 16 * GiB, 16, True, 6, 7),
        (9 * GiB, 16 * GiB, 16, False, 6, 6),
        # The tester's case, a small machine: one gen/ job, small ones beside it.
        (3 * GiB, 8 * GiB, 8, True, 1, 4),
        # Low memory: one job, never zero.
        (2 * GiB, 8 * GiB, 8, True, 1, 1),
        (0, 8 * GiB, 8, True, 1, 1),
        # Plenty: the cores bound it.
        (60 * GiB, 64 * GiB, 8, True, 8, 8),
    ],
)
def test_size_table(avail, total, cores, pool, gen_jobs, jobs):
    s = resources.size(avail, total, cores, G1220, pool)
    assert (s.gen_jobs, s.jobs) == (gen_jobs, jobs)
    assert s.jobs <= cores and s.gen_jobs >= 1
    low = [w for w in s.warnings if w.startswith("memory is low")]
    assert bool(low) == (s.budget < G1220)


def test_size_line_and_reserve():
    s = resources.size(9 * GiB, 16 * GiB, 16, G1220)
    assert s.line() == "7 jobs, generated code 6 at a time (9.7 GB available, about 1.3 GB per job)"
    assert resources.reserve_for(4 * GiB) == GiB
    assert resources.reserve_for(16 * GiB) == 16 * GiB // 10
    assert resources.reserve_for(64 * GiB) == 2 * GiB
    assert resources.reserve_for(None) == GiB


def test_size_unknown_memory():
    s = resources.size(None, None, 16, G1220)
    assert (s.gen_jobs, s.jobs) == (4, 4) and not s.warnings
    assert s.line() == "4 jobs (available memory unknown)"
    assert resources.size(None, None, 2, G1220).jobs == 2
    assert resources.size(None, None, None, G1220).jobs == 4


def test_size_forced_jobs():
    """--jobs N sets both counts, and warns past the formula's gen_jobs."""
    s = resources.size(9 * GiB, 16 * GiB, 16, G1220, jobs=12)
    assert (s.gen_jobs, s.jobs, s.formula) == (12, 12, 6)
    assert s.line() == "12 jobs (--jobs)"
    assert any(w.startswith("--jobs 12: more than the 6") for w in s.warnings)
    s = resources.size(9 * GiB, 16 * GiB, 16, G1220, jobs=2)
    assert (s.gen_jobs, s.jobs) == (2, 2) and not s.warnings
    import argparse

    for bad in ("0", "-1", "x"):
        with pytest.raises(argparse.ArgumentTypeError):
            resources.jobs_arg(bad)
    assert resources.jobs_arg("3") == 3


def test_gen_job_sources_and_record(d, state):
    """G: game.toml's figure, else the gen/ estimate, raised by a larger
    measured peak; the record keeps the largest ever seen."""
    with game2_tree(d) as (G, _):
        r = G.m["build"]["resources"]
        # gen/ has only a header: no estimate, the floor.
        assert resources.gen_job("windows", {}) == (resources.GEN_FLOOR, "estimated from gen/")
        write(os.path.join(G.gen, "recomp_0.c"), "x" * 10_000_000)
        assert resources.gen_job("windows", {}) == (1400 * MiB, "estimated from gen/")
        r["gen_job_mib"] = 1220
        assert resources.gen_job("windows", {}) == (G1220, "game.toml")
        resources.record("compile:windows", 1300 * MiB, 6)
        n, src = resources.gen_job("windows")
        assert n == 1300 * MiB and src.startswith("measured ")
        # An incremental build measures less: the record keeps the peak.
        resources.record("compile:windows", 200 * MiB, 6)
        assert resources.peak_of("compile:windows") == 1300 * MiB
        resources.record("compile:windows", None)
        assert json.load(open(state))["compile:windows"]["jobs"] == 6
        # The other target's peak does not count.
        assert resources.gen_job("macos")[1] == "game.toml"


def test_record_never_fails(d, monkeypatch):
    monkeypatch.setattr(resources, "state_path", lambda: os.path.join(d, "f", "x"))
    write(os.path.join(d, "f"), "a file where the folder goes")
    resources.record("tool:disasm", 5)
    assert resources.load() == {}


def test_tool_warning(d, state):
    with game2_tree(d) as (G, _):
        r = G.m["build"]["resources"]
        assert resources.tool_warning(2 * GiB, 8 * GiB) == ""
        r["tool_mib"], r["tool_step"] = 1722, "disasm"
        w = resources.tool_warning(2 * GiB, 8 * GiB)
        assert w == (
            "disasm needs about 1.8 GB in one process and 2.1 GB is available; "
            "close other programs first"
        )
        assert resources.tool_warning(8 * GiB, 16 * GiB) == ""
        resources.record("tool:analyze", 3 * GiB)
        assert resources.tool_need() == (3 * GiB, "analyze")


def test_hud_jobs(monkeypatch, state):
    monkeypatch.setattr(resources, "cores", lambda *a: 8)
    monkeypatch.setattr(resources, "mem_total", lambda *a: 8 * GiB)
    monkeypatch.setattr(resources, "mem_available", lambda *a: 2 * GiB)
    assert resources.hud_jobs(100) == 4  # (2 - 1 - 0.5 GiB) / 128 MiB
    assert resources.hud_jobs(2) == 2
    assert resources.hud_jobs(100, jobs=3) == 3
    monkeypatch.setattr(resources, "mem_available", lambda *a: GiB)
    assert resources.hud_jobs(100) == 1
    monkeypatch.setattr(resources, "mem_available", lambda *a: None)
    assert resources.hud_jobs(100) == 4


# ── measured runs ────────────────────────────────────────────────────────

ALLOC = "import sys; b = b'x' * (200 << 20); sys.exit(3)"


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="POSIX measurement")
def test_wait_measures_a_child():
    p = subprocess.Popen([sys.executable, "-c", ALLOC])
    rc, peak = resources.wait(p, resources.watch(p))
    assert rc == 3 and p.returncode == 3 and p.wait() == 3
    assert peak is not None and peak >= 190 * MiB
    with pytest.raises(host.CliError, match="exit 3"):
        host.run([sys.executable, "-c", ALLOC])
    assert host.LAST_PEAK >= 190 * MiB
    host.run([sys.executable, "-c", "pass"])
    assert host.LAST_PEAK < 190 * MiB


def test_wait_without_measurement(monkeypatch):
    """No wait4 (or a failed job): the exit code, and no peak."""
    monkeypatch.delattr(os, "wait4", raising=False)
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(2)"])
    assert resources.wait(p, None) == (2, None)

    class Broken:
        def peak(self):
            raise OSError("no job")

        def close(self):
            self.closed = True

    job = Broken()
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    assert resources.wait(p, job) == (0, None) and job.closed
    assert resources.watch(p, "linux") is None


# ── the build: the pool and the job count ────────────────────────────────


def fake_build(monkeypatch, G, ninja="ninja", version=(4, 4)):
    runs = []
    monkeypatch.setattr(build.pipeline, "refuse_stale_gen", lambda *a: None)
    monkeypatch.setattr(host, "refuse_while_regenerating", lambda: None)
    monkeypatch.setattr(build, "build_tools", lambda st: ("cmake", ninja))
    monkeypatch.setattr(build, "check_build_target", lambda t: None)
    monkeypatch.setattr(build, "cmake_version", lambda c, run=None: version)
    monkeypatch.setattr(G, "exe", lambda b, t: G.xbe)
    monkeypatch.setattr(
        build.resources,
        "plan",
        lambda t, j=0, p=True: resources.size(9 * GiB, 16 * GiB, 16, G1220, p, j),
    )

    def run(argv, **k):
        runs.append([str(x) for x in argv])
        host.LAST_PEAK = 900 * MiB if "--build" in argv else None

    monkeypatch.setattr(host, "run", run)
    return runs


def test_build_argv_pool(d, monkeypatch, state, capsys):
    with game2_tree(d) as (G, tk):
        write(os.path.join(tk, "CMakeLists.txt"), "")
        bdir = os.path.join(d, "build")
        runs = fake_build(monkeypatch, G)
        build.build("macos", bdir=bdir)
        cfg, bld = runs
        inc = os.path.join(bdir, "xbr", "xbr-jobs.cmake")
        assert "-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=" + inc in cfg
        assert "-DXBR_GEN_JOBS=6" in cfg
        assert "-DXBR_GEN_TARGETS=" + G.m["build"]["exe"] in cfg
        assert os.path.isfile(inc)
        assert bld == ["cmake", "--build", bdir, "--config", "Release", "-j", "7"]
        out = capsys.readouterr().out
        assert "build: 7 jobs, generated code 6 at a time" in out
        assert "build: largest compile job 943.7 MB" in out
        assert resources.peak_of("compile:macos") == 900 * MiB
        # A configured tree with the same pool: no reconfigure.
        write(
            os.path.join(bdir, "CMakeCache.txt"),
            "CMAKE_GENERATOR:INTERNAL=Ninja\n"
            "CMAKE_PROJECT_TOP_LEVEL_INCLUDES:UNINITIALIZED=%s\n"
            "XBR_GEN_JOBS:UNINITIALIZED=6\n"
            "XBR_GEN_TARGETS:UNINITIALIZED=%s\n" % (inc, G.m["build"]["exe"]),
        )
        runs.clear()
        build.build("macos", bdir=bdir)
        assert [r[1] for r in runs] == ["--build"]
        # --jobs 2 moves the pool: build.ninja is regenerated, nothing else.
        runs.clear()
        build.build("macos", bdir=bdir, jobs=2)
        assert runs[0][:5] == ["cmake", "-S", G.root, "-B", bdir]
        assert "-DXBR_GEN_JOBS=2" in runs[0]
        assert runs[1][-2:] == ["-j", "2"]
        # Extra cmake args on a configured tree reconfigure it too, and
        # name the source: from any other directory (a build host's ssh
        # home) -B alone made CMake take that directory.
        runs.clear()
        build.build("macos", ("-DFOO=1",), bdir=bdir, jobs=2)
        assert runs[0][:5] == ["cmake", "-S", G.root, "-B", bdir] and "-DFOO=1" in runs[0]


@pytest.mark.skipif(not shutil.which("cmake"), reason="no cmake on PATH")
def test_cmake_b_alone_ignores_the_cache(d, monkeypatch):
    """Why the reconfigure names -S: CMake 4 with -B alone takes the
    working directory as the source tree even when the build tree's cache
    names another (the ssh user's home on a PC build host)."""
    src, bdir, away = (os.path.join(d, x) for x in ("src", "b", "away"))
    write(
        os.path.join(src, "CMakeLists.txt"),
        "cmake_minimum_required(VERSION 3.20)\nproject(x NONE)\n",
    )
    os.makedirs(away)
    cmake = shutil.which("cmake")
    subprocess.run([cmake, "-S", src, "-B", bdir], check=True, capture_output=True)
    monkeypatch.chdir(away)
    alone = subprocess.run([cmake, "-B", bdir, "-DFOO=1"], capture_output=True, text=True)
    named = subprocess.run([cmake, "-S", src, "-B", bdir, "-DFOO=1"], capture_output=True)
    assert alone.returncode != 0
    assert named.returncode == 0


def test_build_argv_no_pool(d, monkeypatch, state):
    """Make, or a CMake before 3.24: no pool, and jobs = gen_jobs."""
    with game2_tree(d) as (G, tk):
        write(os.path.join(tk, "CMakeLists.txt"), "")
        for ninja, version in ((None, (4, 4)), ("ninja", (3, 22))):
            bdir = os.path.join(d, "build-%s" % (ninja or "make"))
            runs = fake_build(monkeypatch, G, ninja, version)
            build.build("macos", bdir=bdir)
            assert not any("XBR_" in x or "TOP_LEVEL" in x for x in runs[0])
            assert runs[1][-2:] == ["-j", "6"]
        # An existing Makefiles tree keeps its generator, so no pool.
        write(os.path.join(bdir, "CMakeCache.txt"), "CMAKE_GENERATOR:INTERNAL=Unix Makefiles\n")
        assert not build.pool_ok(bdir, "cmake", "ninja", (4, 4))
        assert build.pool_ok(os.path.join(d, "fresh"), "cmake", "ninja", (3, 24))


def test_compile_key_ignores_jobs(d, monkeypatch):
    """D6: the pool's values in the cache never reach the compile key or
    the version's options digest."""
    with game2_tree(d) as (G, tk):
        G.m["package"] = dict(PKG2, targets=["windows"])
        bdir = os.path.join(d, "b")
        monkeypatch.setattr(incr, "toolchain", lambda t, st: {"clang": "1"})
        lib = plib()
        a = types.SimpleNamespace(system_tools=False, allow_nonstock=True, allow_debug=False)
        keys = []
        for n in (1, 8):
            write(
                os.path.join(bdir, "CMakeCache.txt"),
                "CMAKE_BUILD_TYPE:STRING=Release\nXBR_GEN_JOBS:UNINITIALIZED=%d\n" % n,
            )
            inputs = incr.compile_inputs("windows", a, lib, [], bdir)
            inputs.pop("toolchain", None)
            cache = lib.read_cache(os.path.join(bdir, "CMakeCache.txt"))
            keys.append((json.dumps(inputs, sort_keys=True), lib.options_digest(cache)))
        assert keys[0] == keys[1]


def test_upscale_same_at_any_job_count(state):
    items = [
        types.SimpleNamespace(dds=dds.parse(dds_file(8, 8, b"DXT1", dxt1_blocks(8, 8, i % 4))))
        for i in range(5)
    ]
    one = list(hud.upscale_all(items, 2, 1))
    three = list(hud.upscale_all(items, 2, 3))
    assert one == three and len(one) == 5
    assert resources.peak_of("hud:worker")


# ── doctor (D5) ──────────────────────────────────────────────────────────


def test_doctor_resources(d, monkeypatch, state):
    with game2_tree(d) as (G, _):
        r = G.m["build"]["resources"]
        r["gen_job_mib"], r["tool_mib"], r["tool_step"] = 1220, 1722, "disasm"
        monkeypatch.setattr(resources, "cores", lambda *a: 16)
        monkeypatch.setattr(resources, "mem_total", lambda *a: 16 * GiB)
        monkeypatch.setattr(resources, "mem_available", lambda *a: 9 * GiB)
        monkeypatch.setattr(resources, "disk_free", lambda p: 200 * GiB)
        lines, j = doctor.resources_report("linux")
        assert lines[0] == "memory:     17.2 GB, 9.7 GB available now"
        assert lines[1] == "cores:      16"
        assert lines[2].startswith("disk:       214.7 GB free in ")
        assert "(a build needs about" in lines[2]
        assert (
            lines[3] == "build jobs: 7 (generated code 6 at a time, about 1.3 GB each: game.toml)"
        )
        assert j["jobs"] == 7 and j["gen_jobs"] == 6 and j["gen_job_source"] == "game"
        assert j["disk_need"] > GiB and j["warnings"] == []
        # Low memory and disk: warnings, never problems.
        monkeypatch.setattr(resources, "mem_available", lambda *a: 2 * GiB)
        monkeypatch.setattr(resources, "disk_free", lambda p: GiB)
        lines, j = doctor.resources_report("linux")
        w = [x for x in lines if x.startswith("warning:")]
        assert any("memory is low (2.1 GB available)" in x for x in w)
        assert any("disasm needs about 1.8 GB" in x for x in w)
        assert any("disk: 1.1 GB free" in x and "package will refuse" in x for x in w)
        assert len(j["warnings"]) == 3
        # Unknown memory: null.
        monkeypatch.setattr(resources, "mem_available", lambda *a: None)
        monkeypatch.setattr(resources, "mem_total", lambda *a: None)
        lines, j = doctor.resources_report("linux")
        assert lines[0] == "memory:     unknown" and j["mem_available"] is None
        assert lines[3] == "build jobs: 4 (available memory unknown)"
        # Before the first import: game.toml's game files, imported and bundled.
        monkeypatch.setattr(resources, "disk_free", lambda p: 200 * GiB)
        before = doctor.resources_report("linux")[1]["disk_need"]
        os.rename(G.game_files, G.game_files + ".away")
        r["game_files_mib"] = 3000
        after = doctor.resources_report("linux")[1]["disk_need"]
        os.rename(G.game_files + ".away", G.game_files)
        assert after - before >= 2 * 3000 * MiB - resources.tree_size(G.game_files)


# ── disk (D9) ────────────────────────────────────────────────────────────


def test_by_volume_and_text():
    needs = [
        resources.Need("generate", "/a/out", 2 * GiB, "analysis and generated code"),
        resources.Need("build", "/a/build", GiB, "build-pkg-win"),
        resources.Need("package", "/b/dist", 3 * GiB, "the bundle"),
        resources.Need("setup", "/a", 0, "nothing: current"),
    ]
    dev = lambda p: p.split("/")[1]  # noqa: E731
    free = {"a": 2 * GiB, "b": 100 * GiB}
    vols = resources.by_volume(needs, lambda p: free[dev(p)], dev)
    assert [v["need"] for v in vols] == [4 * GiB, 4 * GiB]
    assert vols[0]["stages"] == {"generate": 2 * GiB, "build": GiB}
    assert "setup" not in vols[0]["stages"]
    assert [v["path"] for v in resources.short(vols)] == ["/a/out"]
    text = resources.disk_text(vols)
    assert text.startswith("disk: this build needs 4.3 GB in /a/out and 2.1 GB is free")
    assert "generate" in text and "slack" in text and "(and 4.3 GB in /b/dist" in text
    assert "free 2.1 GB in /a/out" in text and "--ignore-disk-space" in text
    assert resources.disk_json(vols)[1] == {
        "path": "/b/dist",
        "free": 100 * GiB,
        "need": 4 * GiB,
        "stages": {"package": 3 * GiB},
    }


def test_check_disk_refuses_and_ignores(monkeypatch, capsys):
    monkeypatch.setattr(resources, "disk_free", lambda p: GiB)
    needs = [resources.Need("build", "/x", 512 * MiB, "b")]
    with pytest.raises(host.Refusal) as e:
        resources.check_disk(needs)
    assert e.value.code == "disk"
    capsys.readouterr()
    vols = resources.check_disk(needs, ignore=True)
    assert vols and capsys.readouterr().out.startswith("WARNING: disk: this build needs")
    monkeypatch.setattr(resources, "disk_free", lambda p: 100 * GiB)
    resources.check_disk(needs)
    # Free space unknown: never a refusal.
    monkeypatch.setattr(resources, "disk_free", lambda p: None)
    resources.check_disk(needs)


def test_build_refused_disk(d, monkeypatch, capsys):
    with game2_tree(d) as (G, _):
        monkeypatch.setattr(resources, "disk_free", lambda p: 100 * MiB)
        called = []
        monkeypatch.setattr(build, "build", lambda *a, **k: called.append(k) or G.xbe)
        assert main.main(["--game", G.root, "build", "macos"]) == 1
        err = capsys.readouterr().err
        assert "disk: this build needs" in err and err.rstrip().endswith("refused: disk")
        assert not called
        assert main.main(["--game", G.root, "build", "macos", "--ignore-disk-space"]) == 0
        assert called and called[0]["jobs"] == 0
        assert "WARNING: disk:" in capsys.readouterr().out
        argv = ["--game", G.root, "build", "macos", "--jobs", "3", "--ignore-disk-space"]
        assert main.main(argv) == 0
        assert called[-1]["jobs"] == 3


def test_package_refused_disk(game, monkeypatch):  # noqa: F811
    G = game["G"]
    from xboxrecomp_cli import package as pkg

    # The fixture's plan runs nothing before the compile; this one builds
    # and packages, as a first package does.
    monkeypatch.setattr(pkg, "package_plan", lambda a, imp=None: [("build", ""), ("package", "")])
    monkeypatch.setattr(host.VIEW, "set_stage", lambda s: None, raising=False)
    monkeypatch.setattr(resources, "disk_free", lambda p: 100 * MiB)
    with pytest.raises(host.Refusal) as e:
        game["run"]()
    assert e.value.code == "disk" and "this build needs" in str(e.value)
    assert game["calls"]["build"] == 0
    # The plan says so, with each volume's figures.
    doc = json.loads(game["run"](plan=True, json=True))
    assert doc["output"]["kind"] == "none" and doc["output"]["why_codes"] == ["disk"]
    assert doc["disk"] and doc["disk"][0]["free"] == 100 * MiB
    # --ignore-disk-space: a warning, and the bundle is made.
    out = game["run"](ignore_disk_space=True)
    assert "WARNING: disk:" in out and game["calls"]["build"] == 1
    assert any(n.endswith(".tar") for n in os.listdir(G.path("dist")))
    # With the space, a current build counts nothing but the bundle.
    monkeypatch.setattr(resources, "disk_free", lambda p: 100 * GiB)
    doc = json.loads(game["run"](plan=True, json=True))
    stages = {k for v in doc["disk"] for k in v["stages"]}
    assert "build" not in stages and "generate" not in stages


def test_package_disk_counts_what_runs(game, monkeypatch):  # noqa: F811
    """A run that is all current, or that makes a patch, counts no game
    files: with 5 GiB of them and 3 GiB free, only a full bundle refuses.
    The real run checks the same needs as its plan shows."""
    G = game["G"]
    monkeypatch.setattr(host.VIEW, "set_stage", lambda s: None, raising=False)
    monkeypatch.setattr(resources, "tree_size", lambda p: 5 * GiB)
    checked = []
    real_check = resources.check_disk

    def check(needs, ignore=False):
        checked.append(resources.disk_json(resources.by_volume(needs)))
        return real_check(needs, ignore)

    monkeypatch.setattr(resources, "check_disk", check)
    monkeypatch.setattr(resources, "disk_free", lambda p: 100 * GiB)
    bulk(G)
    game["run"]()
    base = first_record(game)
    monkeypatch.setattr(resources, "disk_free", lambda p: 3 * GiB)
    # All current: nothing to write, nothing refused.
    doc = plan_of(game)
    assert doc["disk"] == [] and doc["output"]["kind"] != "none", doc["output"]
    game["run"]()
    assert checked[-1] == doc["disk"]
    # A full bundle of a new build: the game files again, refused.
    recompile(game)
    doc = plan_of(game)
    assert doc["output"]["why_codes"] == ["disk"], doc["output"]
    with pytest.raises(host.Refusal) as e:
        game["run"]()
    assert e.value.code == "disk" and checked[-1] == doc["disk"]
    # The same build as a patch: the program and the payload only.
    game["a"].patch_from = base["sha256"][:12]
    try:
        doc = plan_of(game)
        assert doc["output"]["kind"] == "patch", doc["output"]
        assert all(v["need"] < 3 * GiB for v in doc["disk"]), doc["disk"]
        out = game["run"]()
    finally:
        del game["a"].patch_from
    assert "patch: from %s" % base["version"] in out and checked[-1] == doc["disk"]


# ── game.toml ────────────────────────────────────────────────────────────


def test_manifest_resources():
    m = manifest.parse_text(MINIMAL)
    r = m["build"]["resources"]
    assert r["gen_job_mib"] == 0 and r["tool_mib"] == 0 and r["gen_targets"] == ["some_recomp"]
    assert r["generate_mib"] == 1024 and r["tree_mib"] == 512 and r["game_files_mib"] == 0
    text = MINIMAL + (
        "[build.resources]\ngen_job_mib = 1220\ntool_mib = 1722\n"
        'tool_step = "disasm"\ngen_targets = ["a", "b"]\n'
    )
    r = manifest.parse_text(text)["build"]["resources"]
    assert (r["gen_job_mib"], r["tool_step"], r["gen_targets"]) == (1220, "disasm", ["a", "b"])
    with pytest.raises(manifest.ManifestError, match="must not be negative"):
        manifest.parse_text(MINIMAL + "[build.resources]\ntool_mib = -1\n")
    with pytest.raises(manifest.ManifestError, match="gen_targets"):
        manifest.parse_text(MINIMAL + "[build.resources]\ngen_targets = []\n")
