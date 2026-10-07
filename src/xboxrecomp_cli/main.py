"""xbr: the command line. A game's wrapper runs

  uv run --project <this checkout> --locked xbr --game <root> --prog <slug> ARGS

so every message and the help read `<slug> ...` (blinx2 for BLiNX 2), as
the game's single-file CLI did. The commands and their help are below; the
top-level help is helptext.py's.

Developer tools outside the help's command list (the game's former
scripts): `golden`, `pacing-stats`, `benchlog-retention`, `audio-check`
(each passes the game's paths from game.toml and takes the script's own
arguments), and `wrapper --check` (the game's bootstrap against this CLI's
template).
"""

import argparse
import os
import sys

from . import build as build_mod
from . import doctor as doctor_mod
from . import helptext, host, manifest, pins, pipeline, scaffold, setup
from .host import CliError

COMMANDS = []  # (name, help, configure(parser), func(args)), in --help order


def command(name, help, configure=None):
    def wrap(func):
        COMMANDS.append((name, help, configure, func))
        return func

    return wrap


def _build_target_help():
    targets = host.g().m["build"]["targets"]
    if "windows" in targets and "macos" in targets:
        return "default: macos on a macOS host, windows elsewhere"
    return "default: %s (game.toml's build.targets)" % (targets[0] if targets else "windows")


def _cfg_build(p):
    p.add_argument(
        "target",
        nargs="?",
        choices=("windows", "macos"),
        help=_build_target_help(),
    )
    p.add_argument(
        "--system-tools",
        action="store_true",
        help="use the host's cmake and ninja instead of the venv's",
    )
    p.add_argument(
        "--stale-gen-ok",
        action="store_true",
        help="build even when gen/ is stale against its key (analyze and recomp to fix)",
    )
    p.set_defaults(passthrough="extra CMake arguments")


def _help_build():
    b = host.g().m["build"]
    return "compile the executable: %s/ (windows) or %s/ (macos)" % (
        b["windows_dir"],
        b["macos_dir"],
    )


def cmd_build(a):
    target = a.target or build_mod.default_build_target()
    args = [x for x in a.extra if x != "--"]
    host.step("build %s" % target)
    host.say("built %s" % build_mod.build(target, args, a.system_tools, stale_ok=a.stale_gen_ok))


def _stage_cmd(fn):
    def cmd(a):
        fn(a.extra)

    return cmd


def _cfg_stage(p):
    p.set_defaults(passthrough="extra arguments for the stage's tool")


def _cfg_all(p):
    p.add_argument(
        "--system-tools",
        action="store_true",
        help="all: use the host's cmake and ninja instead of the venv's",
    )


def cmd_analyze(a):
    pipeline.analyze()


def cmd_all(a):
    cmd_analyze(a)
    pipeline.stage_recomp()
    host.step("build")
    build_mod.build(build_mod.default_build_target(), (), a.system_tools)


def _cfg_setup(p):
    p.add_argument(
        "--dev",
        action="store_true",
        help="also the dev group: pytest, ruff and the toolkit's test dependencies",
    )
    p.add_argument("--no-toolkit", action="store_true", help="do not clone the toolkit")
    p.add_argument("--force", action="store_true", help="fetch the toolchain again")


def cmd_setup(a):
    setup.run_setup(a.dev, a.force, a.no_toolkit)
    return cmd_doctor(a)


def cmd_doctor(a):
    lines, targets, blocked = doctor_mod.doctor_report()
    host.step("doctor")
    for line in lines:
        host.say(line)
    host.say()
    host.say("can package: %s" % (", ".join(targets) or "nothing yet"))
    for t, why in blocked.items():
        host.say("  %s: %s" % (t, why))
    if "steamos" in host.g().m.get("package", {}).get("targets", []):
        return 0 if "steamos" in targets else 1
    return 0 if not doctor_mod.PROBLEMS else 1


def _cfg_pins(p):
    p.add_argument("action", choices=("refresh",))


def cmd_pins(a):
    pins.pins_refresh()


def _cfg_package(p):
    from .package import lib as pkg_lib

    b = host.g().m["build"]
    nonstock = pkg_lib.nonstock_help(b["nonstock_vars"], b["stock_cmake"])
    p.add_argument("target", choices=("windows", "steamos", "macos"))
    p.add_argument("--no-build", action="store_true", help="package the existing build as it is")
    p.add_argument("--allow-debug", action="store_true", help="package a non-Release build")
    p.add_argument(
        "--allow-nonstock",
        action="store_true",
        help="package with %s (recorded)" % " or ".join(nonstock),
    )
    p.add_argument(
        "--archive", action="store_true", help="windows: also a stored .zip of the folder"
    )
    p.add_argument("--out", default=host.g().dist, help="output directory (default dist/)")
    p.add_argument("--keep-stage", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--plain",
        action="store_true",
        help="plain line output, no live progress (also when not a terminal, or CI=1)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="show every tool's output as it runs (implies --plain); "
        "it always goes to build-logs/ too",
    )
    p.add_argument(
        "--no-setup",
        action="store_true",
        help="never run setup, even when something is missing (own toolchain)",
    )
    p.add_argument(
        "--reconfigure", action="store_true", help="configure the packaging build tree afresh"
    )
    p.add_argument(
        "--system-tools",
        action="store_true",
        help="use the host's cmake and ninja instead of the venv's",
    )


def cmd_package(a):
    from . import package

    package.cmd_package(a)


def register():
    """The command table, in --help order (the old blinx2.py's)."""
    COMMANDS.clear()
    command("build", _help_build(), _cfg_build)(cmd_build)
    for name, fn, help_ in pipeline.stages():
        command(name, help_, _cfg_stage)(_stage_cmd(fn))
    command("analyze", "parse, disasm, funcid, abi, then names if a Ghidra export exists")(
        cmd_analyze
    )
    command("all", "analyze, recomp, then build for this host's default target", _cfg_all)(cmd_all)
    command(
        "setup",
        "fetch the pinned toolchain for this host (venv, llvm-mingw, NSIS on Windows)",
        _cfg_setup,
    )(cmd_setup)
    command("doctor", "what this host has, and which targets it can package")(cmd_doctor)
    command("pins", "maintainers: rewrite config/setup-pins.json and uv.lock", _cfg_pins)(cmd_pins)
    command(
        "package",
        "a private bundle for windows, steamos or macos in dist/; runs setup, "
        "generation and the build first when they are needed",
        _cfg_package,
    )(cmd_package)


class Parser(argparse.ArgumentParser):
    """The top level prints helptext's text: the same bytes whichever Python
    runs it (and the same as the bootstrap prints without uv)."""

    def format_help(self):
        G = host.g()
        b = G.m["build"]
        return helptext.top_help(self.prog, G.name, b["windows_dir"], b["macos_dir"])


def make_parser(prog=None):
    G = host.g()
    prog = prog or G.slug
    doc = helptext.render(prog, G.name, G.m["build"]["windows_dir"], G.m["build"]["macos_dir"])
    ap = Parser(
        prog=prog,
        description=doc.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=doc.split("\n\n", 1)[1],
    )
    # The sub-parsers are plain argparse: only the top level prints helptext.
    sub = ap.add_subparsers(dest="cmd", metavar="command", parser_class=argparse.ArgumentParser)
    sub.required = True
    register()
    for name, help_, configure, func in COMMANDS:
        # No help= on the sub-parsers: the epilog lists the commands, players'
        # first and the developers' apart.
        p = sub.add_parser(name, description=help_)
        if configure:
            configure(p)
        p.set_defaults(func=func)
    return ap, sub


def native_target(os_name=None):
    """What the wrapper with no arguments packages: the bundle for this host
    when the game makes it, else the first of package.targets this host can
    make (a game without macos bundles packages windows on a Mac)."""
    os_name = os_name or host.host_os()
    native = {"macos": "macos", "windows": "windows"}.get(os_name, "steamos")
    targets = host.g().m.get("package", {}).get("targets")
    if not targets or native in targets:
        return native
    for t in targets:
        if t != "macos" or os_name == "macos":
            return t
    return native


def cmd_bench(argv):
    """The bench controller is loaded only here; its own help and argument
    handling are bench's."""
    from . import bench

    return bench.main(argv, host.g())


def tool_argv(name, argv):
    """The developer tools, with the game's paths from game.toml."""
    G = host.g()
    golden = (
        ["--golden-json", G.golden_json, "--golden-frames", G.golden_frames, "--game-root", G.root]
        if G.golden_json
        else []
    )
    if name == "golden":
        from . import golden as mod

        return mod.main(golden + argv)
    if name == "pacing-stats":
        from . import pacing_stats as mod

        return mod.main(golden + argv)
    if name == "benchlog-retention":
        from . import benchlog_retention as mod

        gj = G.m["golden"]
        extra = ["--root", G.root] if "--root" not in argv else []
        extra += ["--golden-json", gj["json"]] if gj["json"] else []
        extra += ["--golden-audio", gj["audio"]] if gj["audio"] else []
        return mod.main(extra + argv)
    if name == "audio-check":
        # numpy is the game's tools environment's, not this CLI's: the
        # script runs there, as it did from the game's scripts/.
        import subprocess

        from . import toolkit

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_check.py")
        return subprocess.run([toolkit.tool_python(), script] + argv).returncode
    if name == "wrapper":
        from . import wrapper

        return wrapper.main(argv, G)
    raise AssertionError(name)


TOOLS = ("golden", "pacing-stats", "benchlog-retention", "audio-check", "wrapper")


def clean_environ():
    """`uv run` puts this CLI's own venv first on PATH and in VIRTUAL_ENV;
    the game's tools, uv calls and builds must not see it (uv would warn
    that VIRTUAL_ENV is not the game's .venv)."""
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv or os.path.realpath(venv) != os.path.realpath(sys.prefix):
        return
    del os.environ["VIRTUAL_ENV"]
    b = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
    parts = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        p for p in parts if os.path.realpath(p) != os.path.realpath(b)
    )


def split_global(argv):
    """(game root, prog, rest) from a leading --game DIR --prog NAME."""
    root, prog = None, None
    while argv[:1] and argv[0] in ("--game", "--prog"):
        if len(argv) < 2:
            raise SystemExit("xbr: %s needs a value" % argv[0])
        if argv[0] == "--game":
            root = argv[1]
        else:
            prog = argv[1]
        argv = argv[2:]
    return root or os.getcwd(), prog, argv


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    root, prog, argv = split_global(argv)
    clean_environ()
    if argv[:1] == ["new"]:
        # The one command without a game: it writes the game.toml the
        # others load.
        return scaffold.main(argv[1:], prog or "xbr")
    try:
        G = manifest.use(manifest.load(root))
    except manifest.ManifestError as e:
        print("%s: %s" % (prog or "xbr", e), file=sys.stderr)
        return 1
    name = prog or G.slug
    if argv[:1] == ["bench"]:
        return cmd_bench(argv[1:])
    if argv[:1] and argv[0] in TOOLS:
        try:
            return tool_argv(argv[0], argv[1:]) or 0
        except CliError as e:
            print("%s: %s" % (name, e), file=sys.stderr)
            return 1
    ap, sub = make_parser(name)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["package", native_target()] + argv
    a, extra = ap.parse_known_args(argv)
    if extra and not getattr(a, "passthrough", None):
        ap.error("unrecognized arguments: %s" % " ".join(extra))
    a.extra = extra
    try:
        return a.func(a) or 0
    except (CliError, manifest.ManifestError) as e:
        print("%s: %s" % (name, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
