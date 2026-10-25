"""build: configure once, then compile incrementally, for the windows target
(llvm-mingw) or the macos one (Apple clang)."""

import glob
import os
import re
import shutil
import sys

from . import fetch, host, pipeline, resources, toolkit
from .host import CliError


def build_tools(system_tools, os_name=None):
    """(cmake, ninja or None): the venv's locked wheels, or with --system-tools
    the host's. Ninja is required on Windows (no make there, and CMake's
    default generator would be Visual Studio)."""
    os_name = os_name or host.host_os()
    if system_tools:
        cmake, ninja = shutil.which("cmake"), shutil.which("ninja")
        if not cmake:
            raise CliError("--system-tools: no cmake on PATH")
    else:
        b = host.venv_bin(os_name=os_name)
        cmake = os.path.join(b, "cmake" + host.exe_suffix(os_name))
        ninja = os.path.join(b, "ninja" + host.exe_suffix(os_name))
        if not os.path.isfile(cmake):
            raise CliError(
                "no CMake in .venv: run '%s setup' (or pass --system-tools)" % host.prog()
            )
        ninja = ninja if os.path.isfile(ninja) else None
    if not ninja and os_name == "windows":
        raise CliError("Ninja is required on Windows: run '%s setup'" % host.prog())
    return cmake, ninja


def default_build_target(os_name=None):
    """This host's own target when the game builds it, else the first one
    game.toml lists (a windows-only game cross-builds on a Mac)."""
    native = "macos" if (os_name or host.host_os()) == "macos" else "windows"
    targets = host.g().m["build"]["targets"]
    return native if native in targets or not targets else targets[0]


def check_build_target(target, os_name=None):
    os_name = os_name or host.host_os()
    if target not in host.g().m["build"]["targets"]:
        raise CliError("game.toml's build.targets has no %s" % target)
    if target == "macos" and os_name != "macos":
        raise CliError(
            "the macos target needs a macOS host (Apple clang, codesign, "
            "hdiutil); this host can build: windows (and package windows, steamos)"
        )


def build_dir(target):
    return host.g().build_dir(target)


def pkg_build_dir(target):
    """package's own build tree: a developer's build/ and build-win/ (with
    whatever options they keep there) are never read or changed by it."""
    return host.g().path("build-pkg-win" if target == "windows" else "build-pkg-macos")


def stock_cmake_args():
    """The stock options, passed on every packaging configure so a cache can
    not keep a non-stock value from an earlier run (build.stock_cmake)."""
    return ("-DCMAKE_BUILD_TYPE=Release",) + tuple(host.g().m["build"]["stock_cmake"])


def toolchain_file():
    return host.g().toolchain_file


TOOLCHAIN_INCLUDE = re.compile(r'^include\("([^"]+)"\)', re.M)


CACHED_TOOLCHAIN = re.compile(r"^CMAKE_TOOLCHAIN_FILE:[A-Z]+=(.*)$", re.M)


def same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def reset_stale_toolchain(bdir, toolchain=None):
    """A tree configured with a toolchain file that is gone (a game's own
    copy, deleted when the CLI's became the default) cannot reconfigure:
    CMakeSystem.cmake includes the old path on every run. Nor does a tree
    configured with another toolchain file take a new -DCMAKE_TOOLCHAIN_FILE:
    CMake keeps the first. Start its cache afresh; the next configure names
    the current file (toolchain, when given)."""
    stale = []
    for sysf in glob.glob(os.path.join(bdir, "CMakeFiles", "*", "CMakeSystem.cmake")):
        with open(sysf, encoding="utf-8", errors="replace") as f:
            stale += [p for p in TOOLCHAIN_INCLUDE.findall(f.read()) if not os.path.isfile(p)]
    cache = os.path.join(bdir, "CMakeCache.txt")
    if not stale and toolchain and os.path.isfile(cache):
        with open(cache, encoding="utf-8", errors="replace") as f:
            m = CACHED_TOOLCHAIN.search(f.read())
        if m and m.group(1).strip() and not same_file(m.group(1).strip(), toolchain):
            stale.append(m.group(1).strip())
    if not stale:
        return False
    print(
        "%s: configured with the toolchain file %s, not the current one; configuring afresh"
        % (bdir, stale[0]),
        file=sys.stderr,
    )
    start_afresh(bdir)
    return True


CACHED_TOOLKIT = re.compile(r"^XBOXRECOMP_DIR:[A-Z]+=(.*)$", re.M)


def reset_stale_toolkit(bdir, tk):
    """A tree configured against another toolkit (a game moved between the
    upstream and fork pins, or XBOXRECOMP_DIR changed) keeps the old one: a
    configure without -DXBOXRECOMP_DIR reads the cached path, so gen/ for
    one toolkit links against the other's libraries (undefined
    g_xbox_spin_sleep). Passing the new path is not enough either: the
    toolkit builds in the same add_subdirectory binary dir whichever source
    it comes from, so the old toolkit's cached options and find results
    would carry over. Start the cache afresh, as cmake --fresh does."""
    cache = os.path.join(bdir, "CMakeCache.txt")
    if not os.path.isfile(cache):
        return False
    with open(cache, encoding="utf-8", errors="replace") as f:
        m = CACHED_TOOLKIT.search(f.read())
    old = m.group(1).strip() if m else ""
    if not old or same_dir(old, tk):
        return False
    print(
        "%s: configured with the toolkit %s, not %s; configuring afresh" % (bdir, old, tk),
        file=sys.stderr,
    )
    start_afresh(bdir)
    return True


def same_dir(a, b, path=os.path):
    """One directory by both spellings. On Windows CMake caches a path with
    forward slashes and the drive letter as it saw it (d:/xbr/...), while
    toolkit_dir() may give D:\\xbr\\...: compare case-folded too, or every
    build would start afresh."""
    return path.normcase(path.realpath(a)) == path.normcase(path.realpath(b))


def start_afresh(bdir):
    """Drop a tree's CMakeCache.txt and CMakeFiles (what cmake --fresh
    drops), so the next configure starts from the arguments it is given."""
    cache = os.path.join(bdir, "CMakeCache.txt")
    if os.path.isfile(cache):
        os.remove(cache)
    shutil.rmtree(os.path.join(bdir, "CMakeFiles"), ignore_errors=True)


CACHE_LINE = r"^%s:[A-Z]+=(.*)$"
JOBS_INCLUDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmake", "xbr-jobs.cmake")
POOL_VARS = ("CMAKE_PROJECT_TOP_LEVEL_INCLUDES", "XBR_GEN_JOBS", "XBR_GEN_TARGETS")


def cached(bdir, var):
    """A CMakeCache.txt value, or None."""
    try:
        with open(os.path.join(bdir, "CMakeCache.txt"), encoding="utf-8", errors="replace") as f:
            m = re.search(CACHE_LINE % re.escape(var), f.read(), re.M)
    except OSError:
        return None
    return m.group(1).strip() if m else None


def cmake_version(cmake, run=None):
    """(major, minor) of a cmake, or None."""
    import subprocess

    try:
        out = (run or subprocess.check_output)([cmake, "--version"], stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"version (\d+)\.(\d+)", out.decode() if isinstance(out, bytes) else out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def pool_ok(bdir, cmake, ninja, version=None):
    """Whether this tree takes the gen/ pool: the Ninja generator (an
    existing tree keeps the one it was configured with) and CMake 3.24 or
    later (CMAKE_PROJECT_TOP_LEVEL_INCLUDES)."""
    gen = cached(bdir, "CMAKE_GENERATOR")
    if gen is None:
        gen = "Ninja" if ninja else ""
    if not gen.startswith("Ninja"):
        return False
    v = version if version is not None else cmake_version(cmake)
    return v is not None and v >= (3, 24)


def jobs_include(bdir):
    """The pool's include, copied into the tree: the cache keeps its path,
    and the CLI's own folder moves with each pin. Written only when it
    differs, since CMake reconfigures when an include changes."""
    dst = os.path.join(bdir, "xbr", "xbr-jobs.cmake")
    with open(JOBS_INCLUDE, "rb") as f:
        want = f.read()
    try:
        with open(dst, "rb") as f:
            same = f.read() == want
    except OSError:
        same = False
    if not same:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(want)
    return dst


def pool_args(bdir, gen_jobs):
    """The three -D values of the gen/ pool. They stay out of the compile
    key and the manifest: a pool orders Ninja's commands and changes none."""
    targets = host.g().m["build"]["resources"]["gen_targets"]
    return [
        "-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=" + jobs_include(bdir),
        "-DXBR_GEN_JOBS=%d" % gen_jobs,
        "-DXBR_GEN_TARGETS=" + ";".join(targets),
    ]


def pool_moved(bdir, args):
    """Whether a configured tree's cached pool differs from args."""
    for a in args:
        var, _, val = a[2:].partition("=")
        if cached(bdir, var) != val:
            return True
    return False


def build(
    target,
    cmake_args=(),
    system_tools=False,
    bdir=None,
    stock=False,
    reconfigure=False,
    stale_ok=False,
    jobs=0,
    command="build",
    lineage=None,
):
    """Configure once (the generator is fixed by the first configure; an
    existing tree keeps its own), then build incrementally. stock (package's
    own tree): configure on every run with the stock args; reconfigure
    starts that tree's cache afresh. A gen/ stale against its key is refused
    unless stale_ok: it compiles, then fails at link or misbehaves (a
    wrapped function's gen/ from before the wrap links twice). jobs: --jobs
    N, else 0 for as many as the free memory holds (resources.size).
    command and lineage go into its build-record.json (benchdb, D7)."""
    g = host.g()
    check_build_target(target)
    host.require(os.path.join(g.gen, "recomp_funcs.h"), "recomp")
    if not stale_ok:
        pipeline.refuse_stale_gen("--stale-gen-ok")
    host.refuse_while_regenerating()
    cmake, ninja = build_tools(system_tools)
    env = host.build_env()
    bdir = bdir or build_dir(target)
    tk = toolkit.toolkit_dir()
    if not os.path.isfile(os.path.join(tk, "CMakeLists.txt")):
        raise CliError("no toolkit at %s: run '%s setup' or set XBOXRECOMP_DIR" % (tk, host.prog()))
    if target == "windows":
        reset_stale_toolchain(bdir, toolchain_file())
    reset_stale_toolkit(bdir, tk)
    if reconfigure:
        start_afresh(bdir)
    if stock:
        cmake_args = list(stock_cmake_args()) + list(cmake_args)
    pool = pool_ok(bdir, cmake, ninja)
    sizing = resources.plan(target, jobs, pool)
    host.say("build: " + sizing.line())
    for w in sizing.warnings:
        host.say("WARNING: " + w)
    extra = pool_args(bdir, sizing.gen_jobs) if pool else []
    configured = os.path.isfile(os.path.join(bdir, "CMakeCache.txt"))
    timer = build_timer(g, target, bdir, tk, cmake, ninja, command, lineage, configured)
    timer.doc["jobs"] = sizing.jobs
    timer.doc["warm"] = (
        "cold" if not configured else "reconfigured" if (reconfigure or stock) else "warm"
    )
    if timer.doc["warm"] != "warm":
        timer.doc["kind"] = "full"
    edges0 = ninja_log_lines(bdir)
    try:
        exe = configure_and_compile(
            g,
            target,
            bdir,
            tk,
            cmake,
            ninja,
            env,
            cmake_args,
            extra,
            stock,
            configured,
            sizing,
            timer,
        )
    except BaseException:
        timer.doc["edges"] = edges_since(bdir, edges0)
        timer.finish(bdir, ok=False)
        raise
    timer.doc["edges"] = edges_since(bdir, edges0)
    timer.doc["ccache"] = ccache_state(bdir)
    timer.finish(bdir, ok=True, exe=exe)
    return exe


def configure_and_compile(
    g, target, bdir, tk, cmake, ninja, env, cmake_args, extra, stock, configured, sizing, timer
):
    if stock or not configured:
        cfg = [
            cmake,
            "-S",
            g.root,
            "-B",
            bdir,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DXBOXRECOMP_DIR=" + tk,
        ]
        if ninja:
            cfg += ["-G", "Ninja", "-DCMAKE_MAKE_PROGRAM=" + ninja]
        if target == "windows":
            cfg += [
                "-DCMAKE_TOOLCHAIN_FILE=" + toolchain_file(),
                "-DLLVM_MINGW_ROOT=" + fetch.mingw_root(),
            ]
        timer.timed("configure", lambda: host.run(cfg + list(cmake_args) + extra, env=env))
    elif cmake_args or (extra and pool_moved(bdir, extra)):
        # A new pool depth regenerates build.ninja only: no compile command
        # changes, so Ninja rebuilds nothing.
        timer.timed(
            "configure",
            lambda: host.run([cmake, "-B", bdir] + list(cmake_args) + extra, env=env),
            "reconfigure",
        )
    timer.timed(
        "cmake --build",
        lambda: host.run(
            [cmake, "--build", bdir, "--config", "Release", "-j", str(sizing.jobs)], env=env
        ),
    )
    if host.LAST_PEAK:
        resources.record("compile:" + target, host.LAST_PEAK, sizing.gen_jobs)
        host.say("build: largest compile job %s" % resources.gb(host.LAST_PEAK))
    exe = g.exe(bdir, target)
    if not os.path.isfile(exe):
        raise CliError("the build finished but %s is missing" % exe)
    return exe


# ---- the build's record (benchdb, D7) -------------------------------------------


def ninja_log_lines(bdir):
    """The entries in .ninja_log (one per edge run), or None without one."""
    try:
        with open(os.path.join(bdir, ".ninja_log"), "rb") as f:
            return sum(1 for line in f if not line.startswith(b"#"))
    except OSError:
        return None


def edges_since(bdir, before):
    """Edges the build ran: the lines .ninja_log gained. Ninja rewrites
    the log when it recompacts it, so a shrink reads as unknown."""
    after = ninja_log_lines(bdir)
    if after is None:
        return None
    if before is None:
        return after
    return after - before if after >= before else None


def ccache_state(bdir):
    """'off' when no compiler launcher is cached, 'on' when ccache is it,
    the launcher's name otherwise; None without a cache."""
    if not os.path.isfile(os.path.join(bdir, "CMakeCache.txt")):
        return None
    launch = cached(bdir, "CMAKE_C_COMPILER_LAUNCHER") or cached(
        bdir, "CMAKE_CXX_COMPILER_LAUNCHER"
    )
    if not launch:
        return "off"
    return "on" if "ccache" in os.path.basename(launch) else os.path.basename(launch)


def toolchain_line(target, cmake, ninja):
    """'llvm-mingw <tag>, cmake 3.31, ninja 1.12.1' or '<CLT|Xcode>, ...':
    builds compare only within one toolchain (D13)."""
    import subprocess

    parts = []
    if target == "windows":
        try:
            parts.append("llvm-mingw %s" % fetch.mingw_tag())
        except Exception:  # noqa: BLE001 (a record field, never a failure)
            parts.append("llvm-mingw ?")
    elif sys.platform == "darwin":
        dev = os.environ.get("DEVELOPER_DIR", "")
        if not dev:
            try:
                dev = subprocess.run(
                    ["xcode-select", "-p"], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                dev = ""
        parts.append("CLT" if "CommandLineTools" in dev else "Xcode" if dev else "clang ?")
    v = cmake_version(cmake)
    parts.append("cmake %s" % ("%d.%d" % v if v else "?"))
    if ninja:
        try:
            nv = subprocess.run(
                [ninja, "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            nv = "?"
        parts.append("ninja %s" % (nv or "?"))
    return ", ".join(parts)


def build_timer(g, target, bdir, tk, cmake, ninja, command, lineage, configured):
    from .benchdb import live
    from .cli_dir import cli_dir

    t = live.Timer(
        g.slug,
        target,
        command,
        # The host's own OS: a windows exe built on a Mac is a cross build.
        cross=int(target != ("macos" if host.host_os() == "macos" else "windows")),
        toolchain=toolchain_line(target, cmake, ninja),
        trees=live.trees(g.root, tk, cli_dir()),
    )
    if lineage:
        t.doc["lineage"] = lineage
    return t
