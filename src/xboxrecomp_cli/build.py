"""build: configure once, then compile incrementally, for the windows target
(llvm-mingw) or the macos one (Apple clang)."""

import os
import shutil

from . import fetch, host, toolkit
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
    return "macos" if (os_name or host.host_os()) == "macos" else "windows"


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
    tf = host.g().toolchain_file
    if not tf:
        raise CliError("game.toml sets no build.toolchain_file (the windows target needs it)")
    return tf


def build(target, cmake_args=(), system_tools=False, bdir=None, stock=False, reconfigure=False):
    """Configure once (the generator is fixed by the first configure; an
    existing tree keeps its own), then build incrementally. stock (package's
    own tree): configure on every run with the stock args; reconfigure
    starts that tree's cache afresh."""
    g = host.g()
    check_build_target(target)
    host.require(os.path.join(g.gen, "recomp_funcs.h"), "recomp")
    host.refuse_while_regenerating()
    cmake, ninja = build_tools(system_tools)
    env = host.build_env()
    bdir = bdir or build_dir(target)
    tk = toolkit.toolkit_dir()
    if not os.path.isfile(os.path.join(tk, "CMakeLists.txt")):
        raise CliError(
            "no toolkit at %s: run '%s setup' or set XBOXRECOMP_DIR" % (tk, host.prog())
        )
    if reconfigure:
        if os.path.isfile(os.path.join(bdir, "CMakeCache.txt")):
            os.remove(os.path.join(bdir, "CMakeCache.txt"))
        shutil.rmtree(os.path.join(bdir, "CMakeFiles"), ignore_errors=True)
    if stock:
        cmake_args = list(stock_cmake_args()) + list(cmake_args)
    if stock or not os.path.isfile(os.path.join(bdir, "CMakeCache.txt")):
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
        host.run(cfg + list(cmake_args), env=env)
    elif cmake_args:
        host.run([cmake, "-B", bdir] + list(cmake_args), env=env)
    host.run(
        [cmake, "--build", bdir, "--config", "Release", "-j", str(os.cpu_count() or 4)], env=env
    )
    exe = g.exe(bdir, target)
    if not os.path.isfile(exe):
        raise CliError("the build finished but %s is missing" % exe)
    return exe
