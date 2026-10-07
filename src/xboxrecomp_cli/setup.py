"""setup: the game's tools environment, llvm-mingw, NSIS on Windows, the
toolkit at its pin; and what setup would fetch or cannot fix (the package
plan and doctor ask)."""

import os

from . import cli_dir, doctor, env, fetch, host, toolkit
from .host import CliError


def run_setup(dev=False, force=False, no_toolkit=False, need_uv=True):
    """Every step skips what is already in place, so package can run it
    whenever something is missing (with need_uv False: a working .venv
    does not make a missing uv an error there)."""
    host.step("setup: Python venv (.venv, from uv.lock)")
    env.sync_venv(dev, required=need_uv)
    if host.g().m["toolchain"]["llvm_mingw"]:
        # The pins are read here, not first: a game without the windows
        # target has none to read.
        pins = fetch.load_pins()
        host.step("setup: llvm-mingw")
        fetch.fetch_mingw(pins, force)
        if host.host_os() == "windows":
            step_nsis(pins, force)
    if not no_toolkit:
        host.step("setup: toolkit")
        toolkit.clone_toolkit()
    cli_dir.checkout_pin()


def step_nsis(pins, force):
    host.step("setup: NSIS")
    fetch.fetch_nsis(pins, force)


def setup_needs(target, system_tools=False):
    """What `setup` would fetch for this target: [] when nothing."""
    needs = []
    if not system_tools:
        for t in ("cmake", "ninja"):
            if not os.path.isfile(os.path.join(host.venv_bin(), t + host.exe_suffix())):
                needs.append("no %s" % t)
    try:
        toolkit.tool_python()
    except CliError:
        needs.append("no tools Python")
    if needs:
        try:
            env.find_uv()
        except CliError:
            needs.append("no uv")
    if not os.path.isdir(os.path.join(toolkit.toolkit_dir(), "tools")):
        needs.append("no toolkit")
    if target in ("windows", "steamos"):
        try:
            fetch.mingw_root()
        except CliError:
            needs.append("no llvm-mingw")
    if target == "windows" and host.host_os() == "windows" and not doctor.makensis_path():
        needs.append("no NSIS")
    return needs


def host_blockers(target):
    """What stops this target on this host that setup cannot install, each
    with its one-line fix: [] when nothing."""
    g = host.g()
    out = []
    if not os.path.isfile(g.xbe):
        out.append("no %s: dump the disc into %s/" % (g.rel(g.xbe), g.rel(g.game_files)))
    os_name = host.host_os()
    if target == "macos":
        if os_name != "macos":
            out.append("the macos target needs a macOS host (Apple clang, codesign, hdiutil)")
        elif not os.path.isdir(host.CLT):
            out.append("Command Line Tools: xcode-select --install")
        else:
            missing = doctor.brew_missing()
            if missing:
                out.append("Homebrew: brew install %s" % " ".join(missing))
    if target == "windows" and os_name != "windows" and not doctor.makensis_path():
        out.append("makensis: " + doctor.makensis_hint())
    return out
