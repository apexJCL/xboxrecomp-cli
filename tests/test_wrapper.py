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
# A git with a remote in env: FAKE_MAIN is the remote's main, FAKE_HAS the
# commits it holds (space separated), FAKE_OFFLINE no network. HEAD lives in
# .git/fake-head.
FAKE_GIT = """#!/bin/sh
echo "$@" >> "$GIT_LOG"
fail() { echo "fatal: $1" >&2; exit 128; }
if [ "$1" = clone ]; then
  [ -n "$FAKE_OFFLINE" ] && fail "unable to access '$4': Could not resolve host"
  /bin/mkdir -p "$5/.git/info"; : > "$5/pyproject.toml"
  echo "$FAKE_MAIN" > "$5/.git/fake-head"; exit 0
fi
[ "$1" = -C ] || exit 0
dir="$2"; shift 2
case "$1" in
  rev-parse)
    if [ "$2" = HEAD ]; then
      [ -f "$dir/.git/fake-head" ] || fail "not a git repository"
      /bin/cat "$dir/.git/fake-head"
    else echo .git/info/exclude; fi ;;
  fetch) [ -n "$FAKE_OFFLINE" ] && fail "unable to access: Could not resolve host" ;;
  checkout)
    case " $FAKE_HAS " in
      *" $3 "*) echo "$3" > "$dir/.git/fake-head" ;;
      *) fail "reference is not a tree: $3" ;;
    esac ;;
esac
exit 0
"""
PIN = "0" * 40  # tests/testdata/game/game.toml [cli] commit
OLD = "1" * 40


def fake_clone(path, sha, marked):
    """A checkout of the CLI at sha, as the fake git sees it."""
    os.makedirs(os.path.join(path, ".git", "info"))
    open(os.path.join(path, "pyproject.toml"), "w").close()
    with open(os.path.join(path, ".git", "fake-head"), "w") as f:
        f.write(sha + "\n")
    if marked:
        with open(os.path.join(path, ".xbr-pin"), "w") as f:
            f.write(sha + "\n")


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
        [python, os.path.join(root, "blinx2.py")] + args,
        env=e,
        cwd=os.path.dirname(root),
        capture_output=True,
        text=True,
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
    for p in (beside, override):
        os.makedirs(p)
        open(os.path.join(p, "pyproject.toml"), "w").close()
    fake_clone(external, PIN, marked=False)
    tool(bindir, "git", FAKE_GIT)

    def project(**env):
        r = run(root, bindir, ["doctor"], UV_LOG=log, GIT_LOG=os.path.join(d, "git.log"), **env)
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


def git_env(d, **kw):
    e = {"UV_LOG": os.path.join(d, "uv.log"), "GIT_LOG": os.path.join(d, "git.log")}
    e.update({"FAKE_MAIN": OLD, "FAKE_HAS": "%s %s" % (OLD, PIN)})
    e.update(kw)
    return e


def git_calls(d):
    with open(os.path.join(d, "git.log")) as f:
        return f.read().splitlines()


def test_clone_at_the_pin(d):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    tool(bindir, "git", FAKE_GIT)
    r = run(root, bindir, ["doctor"], **git_env(d))
    assert r.returncode == 0, r.stderr
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    tmp = dest + ".partial"
    pin = tomllib.load(open(os.path.join(root, "game.toml"), "rb"))["cli"]
    calls = git_calls(d)
    assert calls[0] == "clone --quiet --no-checkout %s %s" % (pin["url"], tmp), calls
    assert calls[1] == "-C %s checkout --quiet %s" % (tmp, pin["commit"]), calls
    with open(os.path.join(dest, ".xbr-pin")) as f:
        assert f.read() == pin["commit"] + "\n"
    with open(os.path.join(dest, ".git", "info", "exclude")) as f:
        assert "/.xbr-pin" in f.read()
    assert not os.path.exists(tmp)
    with open(os.path.join(d, "uv.log")) as f:
        argv = f.read().splitlines()
    assert argv[argv.index("--project") + 1] == dest


def refused(r, d, *why):
    """The friendly no-CLI message, no traceback, nothing run, nothing left
    in external/."""
    assert r.returncode == 1, r
    assert "Traceback" not in r.stderr, r.stderr
    for w in why:
        assert w in r.stderr, r.stderr
    assert "no xboxrecomp-cli: clone it beside this checkout, or set XBOXRECOMP_CLI_DIR" in (
        r.stderr
    )
    assert not os.path.exists(os.path.join(d, "uv.log"))


@pytest.mark.parametrize("python", PYTHONS)
def test_pin_not_on_the_remote(d, python):
    """A pin the remote does not have (a commit never pushed): no traceback,
    no clone left at the remote's main for the next run to pick up."""
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    tool(bindir, "git", FAKE_GIT)
    for _ in range(2):  # the second run is no better off than the first
        r = run(root, bindir, ["doctor"], python, **git_env(d, FAKE_HAS=OLD))
        refused(r, d, "git checkout failed: fatal: reference is not a tree")
        assert not os.listdir(os.path.join(root, "external"))


@pytest.mark.parametrize("python", PYTHONS)
def test_no_network(d, python):
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    tool(bindir, "git", FAKE_GIT)
    r = run(root, bindir, ["doctor"], python, **git_env(d, FAKE_OFFLINE="1"))
    refused(r, d, "git clone failed: fatal: unable to access")
    assert not os.listdir(os.path.join(root, "external"))


@pytest.mark.parametrize("python", PYTHONS)
def test_stale_clone(d, python):
    """A clone this script made, left at an older pin, moves to the new pin
    (fetching when it lacks it); an unmarked checkout there at another commit
    is refused, never run."""
    root, bindir = scratch(d)
    tool(bindir, "uv", FAKE_UV % "0.9.0")
    tool(bindir, "git", FAKE_GIT)
    dest = os.path.join(root, "external", "xboxrecomp-cli")
    fake_clone(dest, OLD, marked=True)
    r = run(root, bindir, ["doctor"], python, **git_env(d))
    assert r.returncode == 0, r.stderr
    assert "the pin moved" in r.stderr
    with open(os.path.join(dest, ".git", "fake-head")) as f:
        assert f.read().strip() == PIN
    with open(os.path.join(dest, ".xbr-pin")) as f:
        assert f.read() == PIN + "\n"
    # Offline, and the old clone lacks the new pin: refused, left as it was.
    shutil.rmtree(dest)
    os.remove(os.path.join(d, "uv.log"))
    fake_clone(dest, OLD, marked=True)
    r = run(root, bindir, ["doctor"], python, **git_env(d, FAKE_HAS=OLD, FAKE_OFFLINE="1"))
    refused(r, d, "git fetch failed")
    # Unmarked: someone else's checkout.
    shutil.rmtree(dest)
    fake_clone(dest, OLD, marked=False)
    r = run(root, bindir, ["doctor"], python, **git_env(d))
    refused(r, d, "is at 111111111111, not the pin 000000000000, and this script did not clone it")
    with open(os.path.join(dest, ".git", "fake-head")) as f:
        assert f.read().strip() == OLD


@pytest.mark.parametrize("python", PYTHONS)
def test_no_uv(d, python):
    """Without uv, --help still prints (with a CLI to read it from) and
    anything else prints the hint."""
    root, bindir = scratch(d)
    cli = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(helptext.__file__))))
    # Neither uv nor a CLI: --help says how to get both.
    r = run(root, bindir, ["--help"], python)
    assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr
    assert "no xboxrecomp-cli: clone it beside" in r.stderr, r.stderr
    assert "no uv 0.5.31+ on PATH" in r.stderr, r.stderr
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
    # A mistyped XBOXRECOMP_CLI_DIR says so, rather than failing inside uv.
    r = run(root, bindir, ["doctor"], XBOXRECOMP_CLI_DIR=os.path.join(d, "nope"))
    assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr
    assert "is not an xboxrecomp-cli checkout" in r.stderr, r.stderr


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
