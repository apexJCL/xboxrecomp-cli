#!/usr/bin/env python3
"""The game's command: finds uv and the xboxrecomp-cli commit game.toml pins,
then runs it. A copy of xboxrecomp-cli's wrapper/game.py, named after the
game (`<this> wrapper --check` compares the two); standard library only,
Python 3.9 or newer, so a host with only the system Python still gets the
help and the uv hint. Docs: docs/packaging.md."""

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SLUG = os.path.splitext(os.path.basename(__file__))[0]
UV_MIN = (0, 5, 31)  # xboxrecomp-cli's env.UV_MIN
UV_HINT = {
    "darwin": "brew install uv",
    "linux": "curl -LsSf https://astral.sh/uv/install.sh | sh   (or your distribution's uv package)",
    "win32": "winget install --id=astral-sh.uv -e",
}
UV_DOCS = "https://docs.astral.sh/uv/getting-started/installation/"
MARK = ".xbr-pin"


def cli_table():
    """[cli] commit and url; game.toml keeps them plain one-line strings."""
    vals, cur = {}, None
    with open(os.path.join(ROOT, "game.toml"), encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("["):
                cur = line.strip("[]").strip()
            elif cur == "cli":
                m = re.match(r'^(commit|url)\s*=\s*"([^"\\]*)"\s*(#.*)?$', line)
                if m:
                    vals[m.group(1)] = m.group(2)
    return vals


def find_uv():
    uv = shutil.which("uv")
    if not uv:
        return None
    out = subprocess.run([uv, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    m = re.match(r"uv (\d+)\.(\d+)\.(\d+)", out.stdout.decode().strip())
    return uv if m and tuple(int(x) for x in m.groups()) >= UV_MIN else None


def find_cli(cli, clone=True):
    """$XBOXRECOMP_CLI_DIR, else external/xboxrecomp-cli, else
    ../xboxrecomp-cli (the toolkit's order), else a clone at the pin."""
    if os.environ.get("XBOXRECOMP_CLI_DIR"):
        return os.path.abspath(os.environ["XBOXRECOMP_CLI_DIR"])
    for d in (
        os.path.join(ROOT, "external", "xboxrecomp-cli"),
        os.path.join(ROOT, "..", "xboxrecomp-cli"),
    ):
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            return os.path.abspath(d)
    if not clone or not cli.get("url") or not cli.get("commit") or not shutil.which("git"):
        return None
    dest = os.path.join(ROOT, "external", "xboxrecomp-cli")
    print(
        "%s: cloning %s %s into external/xboxrecomp-cli" % (SLUG, cli["url"], cli["commit"][:12]),
        file=sys.stderr,
    )
    subprocess.run(["git", "clone", "--quiet", cli["url"], dest], check=True)
    subprocess.run(["git", "-C", dest, "checkout", "--quiet", cli["commit"]], check=True)
    with open(os.path.join(dest, MARK), "w") as f:
        f.write(cli["commit"] + "\n")
    excl = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--git-path", "info/exclude"],
        stdout=subprocess.PIPE,
        check=True,
    )
    with open(os.path.join(dest, excl.stdout.decode().strip()), "a") as f:
        f.write("\n/%s\n" % MARK)  # out of `git status`: no -dirty versions
    return dest


def main(argv):
    cli = cli_table()
    uv = find_uv()
    if not uv:
        d = find_cli(cli, clone=False)
        if argv in (["-h"], ["--help"]) and d:
            helptext = os.path.join(d, "src", "xboxrecomp_cli", "helptext.py")
            return subprocess.run([sys.executable, helptext, ROOT, SLUG]).returncode
        hint = UV_HINT.get(sys.platform, UV_HINT["linux"])
        print(
            "%s: no uv %s on PATH; install it with: %s   (other ways: %s)"
            % (SLUG, ".".join(map(str, UV_MIN)) + "+", hint, UV_DOCS),
            file=sys.stderr,
        )
        return 1
    d = find_cli(cli)
    if not d:
        print(
            "%s: no xboxrecomp-cli: clone it beside this checkout, or set XBOXRECOMP_CLI_DIR"
            % SLUG,
            file=sys.stderr,
        )
        return 1
    env = dict(os.environ)
    for k in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):  # the CLI's own .venv, not the shell's
        env.pop(k, None)
    cmd = [
        uv,
        "run",
        "--quiet",
        "--project",
        d,
        "--locked",
        "--no-dev",
        "xbr",
        "--game",
        ROOT,
        "--prog",
        SLUG,
    ] + argv
    if os.name == "nt":
        return subprocess.run(cmd, env=env).returncode
    os.execve(uv, cmd, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
