"""The bootstrap a game vendors (wrapper/game.py, as <slug>.py): its [cli]
parse against tomllib, where it looks for the CLI, the clone at the pin,
the no-uv path, and the uv command it runs. Each case copies it into a
scratch game as blinx2.py and runs it as a script, with a fake git and uv
on PATH where one is needed.

  uv run pytest tests/test_wrapper.py
"""

import os
import shutil
import subprocess
import sys
import tomllib

import pytest

from xboxrecomp_cli import helptext, manifest, wrapper

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "testdata", "game")
# The bootstrap's floor is the system Python of a fresh Mac (3.9): run it
# there too when this host has one.
SYSTEM_PYTHON = "/usr/bin/python3" if os.path.isfile("/usr/bin/python3") else None
PYTHONS = [sys.executable] + ([SYSTEM_PYTHON] if SYSTEM_PYTHON else [])

FAKE_UV = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "uv %s"; exit 0; fi
printf '%%s\\n' "$@" > "$UV_LOG"
"""
FAKE_GIT = """#!/bin/sh
echo "$@" >> "$GIT_LOG"
case "$1" in
  clone) /bin/mkdir -p "$4/.git/info"; : > "$4/pyproject.toml" ;;
  -C) [ "$3" = rev-parse ] && echo .git/info/exclude ;;
esac
exit 0
"""


def scratch(d):
    """d/game: the test game with the bootstrap as blinx2.py; d/bin: PATH."""
    root = os.path.join(d, "game")
    os.makedirs(root)
    shutil.copy(os.path.join(GAME, "game.toml"), root)
    shutil.copy(wrapper.TEMPLATE, os.path.join(root, "blinx2.py"))
    os.makedirs(os.path.join(d, "bin"))
    return root, os.path.join(d, "bin")


def tool(bindir, name, text):
    p = os.path.join(bindir, name)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, 0o755)


def run(root, bindir, args, python=sys.executable, **env):
    # PATH is the fakes alone: the host's own git or uv must not answer.
    e = {"PATH": bindir, "HOME": os.environ.get("HOME", "/")}
    e.update(env)
    return subprocess.run(
        [python, os.path.join(root, "blinx2.py")] + args, env=e, capture_output=True, text=True
    )


def cli_values(text):
    """The bootstrap's [cli] parse, on text."""
    out = {}
    for k in ("commit", "url"):
        v = manifest.line_value(text, "cli", k)
        if v is not None:
            out[k] = v
    return out


def test_cli_parse_agrees_with_tomllib():
    with open(os.path.join(GAME, "game.toml"), encoding="utf-8") as f:
        text = f.read()
    assert cli_values(text) == tomllib.loads(text)["cli"]
    # The bootstrap's own parser, run on the same file, reads the same.
    import importlib.util

    spec = importlib.util.spec_from_file_location("boot", wrapper.TEMPLATE)
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    boot.ROOT = GAME
    assert boot.cli_table() == tomllib.loads(text)["cli"]
    # helptext reads [game] name and [build] dirs the same way.
    t = tomllib.loads(text)
    assert helptext.line_value(text, "game", "name") == t["game"]["name"]
    assert helptext.line_value(text, "build", "windows_dir") == t["build"]["windows_dir"]


def test_multiline_cli_value_refused(d):
    """A [cli] value tomllib accepts but the line parser cannot read (a
    literal string, a value on two lines) stops every command."""
    with open(os.path.join(GAME, "game.toml"), encoding="utf-8") as f:
        text = f.read()
    bad = text.replace('url = "https://', "url = 'https://", 1).replace('.git"\n', ".git'\n", 1)
    assert bad != text
    with pytest.raises(manifest.ManifestError, match="cli.url: write as one plain line"):
        manifest.parse_text(bad)


def test_search_order(d):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    log = os.path.join(d, "uv.log")
    beside = os.path.join(d, "xboxrecomp-cli")
    external = os.path.join(root, "external", "xboxrecomp-cli")
    override = os.path.join(d, "elsewhere")
    for p in (beside, external, override):
        os.makedirs(p)
        open(os.path.join(p, "pyproject.toml"), "w").close()

    def project(**env):
        r = run(root, bindir, ["doctor"], UV_LOG=log, **env)
        assert r.returncode == 0, r.stderr
        with open(log) as f:
            argv = f.read().splitlines()
        return argv[argv.index("--project") + 1]

    assert project(XBOXRECOMP_CLI_DIR=override) == override
    assert project() == external
    shutil.rmtree(external)
    assert os.path.realpath(project()) == os.path.realpath(beside)


def test_uv_command(d):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    log = os.path.join(d, "uv.log")
    cli = os.path.join(d, "xboxrecomp-cli")
    os.makedirs(cli)
    open(os.path.join(cli, "pyproject.toml"), "w").close()
    r = run(root, bindir, ["package", "two words"], UV_LOG=log, VIRTUAL_ENV="/somewhere")
    assert r.returncode == 0, r.stderr
    with open(log) as f:
        argv = f.read().splitlines()
    assert argv == [
        "run",
        "--quiet",
        "--project",
        os.path.abspath(cli),
        "--locked",
        "--no-dev",
        "xbr",
        "--game",
        root,
        "--prog",
        "blinx2",
        "package",
        "two words",
    ], argv


def test_clone_at_the_pin(d):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    tool(bindir, "git", FAKE_GIT)
    log = os.path.join(d, "git.log")
    r = run(root, bindir, ["doctor"], UV_LOG=os.path.join(d, "uv.log"), GIT_LOG=log)
    assert r.returncode == 0, r.stderr
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    pin = tomllib.load(open(os.path.join(root, "game.toml"), "rb"))["cli"]
    with open(log) as f:
        calls = f.read().splitlines()
    assert calls[0] == "clone --quiet %s %s" % (pin["url"], dest), calls
    assert calls[1] == "-C %s checkout --quiet %s" % (dest, pin["commit"]), calls
    with open(os.path.join(dest, ".xbr-pin")) as f:
        assert f.read() == pin["commit"] + "\n"
    with open(os.path.join(dest, ".git", "info", "exclude")) as f:
        assert "/.xbr-pin" in f.read()


@pytest.mark.parametrize("python", PYTHONS)
def test_no_uv(d, python):
    """Without uv, --help still prints (with a CLI to read it from) and
    anything else prints the hint."""
    root, bindir = scratch(d)
    cli = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(helptext.__file__))))
    r = run(root, bindir, ["--help"], python, XBOXRECOMP_CLI_DIR=cli)
    assert r.returncode == 0, r.stderr
    assert r.stdout == helptext.top_help("blinx2", "BLiNX 2"), r.stdout
    r = run(root, bindir, ["doctor"], python, XBOXRECOMP_CLI_DIR=cli)
    assert r.returncode == 1 and "no uv 0.5.31+ on PATH; install it with: " in r.stderr, r.stderr
    tool(bindir, "uv", FAKE_UV % "0.4.0")
    r = run(root, bindir, ["doctor"], python, XBOXRECOMP_CLI_DIR=cli)
    assert r.returncode == 1 and "no uv 0.5.31+" in r.stderr, r.stderr


def test_no_cli(d):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    # No git on this PATH either: nothing to clone with.
    r = run(root, bindir, ["doctor"])
    assert r.returncode == 1, r
    assert "no xboxrecomp-cli: clone it beside this checkout, or set XBOXRECOMP_CLI_DIR" in (
        r.stderr
    )


def test_wrapper_check(d, capsys):
    root, _ = scratch(d)
    game = manifest.load(root)
    assert wrapper.main(["--check"], game) == 0
    with open(os.path.join(root, "blinx2.py"), "a") as f:
        f.write("# local edit\n")
    assert wrapper.main(["--check"], game) == 1
    assert "+# local edit" in capsys.readouterr().out
    # CRLF (a Windows checkout) is the same file.
    with open(wrapper.TEMPLATE, "rb") as f:
        crlf = f.read().replace(b"\n", b"\r\n")
    with open(os.path.join(root, "blinx2.py"), "wb") as f:
        f.write(crlf)
    assert wrapper.check(game) == (True, [])


def test_template_is_py39():
    """The template and helptext.py parse as Python 3.9 (the bootstrap's
    floor): no match statements, no 3.10+ syntax."""
    import ast

    for p in (wrapper.TEMPLATE, helptext.__file__):
        with open(p) as f:
            ast.parse(f.read(), p, feature_version=(3, 9))
