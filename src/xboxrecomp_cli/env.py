"""uv and the game's tools environment (.venv/, from the game's uv.lock)."""

import os
import re
import shutil
import subprocess

from . import host
from .host import CliError

# uv makes .venv/ from uv.lock. It is the user's own install, found on PATH:
# setup never downloads it. UV_MIN is the oldest uv checked against a game's
# uv.lock format and [dependency-groups]: on 2026-10-06,
# `uvx --from uv==0.5.31 uv lock --check` and `uv sync --locked --no-dev`
# (into a scratch UV_PROJECT_ENVIRONMENT) both passed on blinx2-recomp's
# lock, as did 0.6.17, 0.7.22, 0.8.0 and 0.9.0 for the check. The bootstrap
# (wrapper/game.py) carries the same floor and hints.
UV_MIN = (0, 5, 31)
UV_DOCS = "https://docs.astral.sh/uv/getting-started/installation/"
UV_HINT = {
    "macos": "brew install uv",
    # The Astral installer puts uv in ~/.local/bin, so it also works on an
    # immutable host (SteamOS and the like) where the package manager cannot.
    "linux": "curl -LsSf https://astral.sh/uv/install.sh | sh   (or your distribution's uv package)",
    "windows": "winget install --id=astral-sh.uv -e",
}


def uv_hint(os_name=None):
    return "%s   (other ways: %s)" % (UV_HINT[os_name or host.host_os()], UV_DOCS)


def uv_version(uv):
    """(major, minor, patch) from `uv --version` ('uv 0.12.21 (...)'), or None."""
    try:
        out = subprocess.run(
            [uv, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ).stdout.decode()
    except OSError:
        return None
    m = re.match(r"uv (\d+)\.(\d+)\.(\d+)", out.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def find_uv():
    """(path, version) of the uv on PATH; CliError with the install command
    for this host when there is none or it is too old."""
    uv = shutil.which("uv")
    if not uv:
        raise CliError("no uv on PATH; install it with: %s" % uv_hint())
    ver = uv_version(uv)
    if not ver or ver < UV_MIN:
        raise CliError(
            "%s is uv %s, older than %s; update it (%s)"
            % (
                uv,
                ".".join(map(str, ver)) if ver else "of an unknown version",
                ".".join(map(str, UV_MIN)),
                uv_hint(),
            )
        )
    return uv, ver


def venv_ready():
    """.venv has what the build and the tools need (cmake, ninja, Python)."""
    b = host.venv_bin()
    return os.path.isfile(host.venv_python()) and all(
        os.path.isfile(os.path.join(b, t + host.exe_suffix())) for t in ("cmake", "ninja")
    )


def uv_env():
    """uv's environment for the game: always its .venv/, whatever
    UV_PROJECT_ENVIRONMENT the developer's shell sets for other projects
    (this CLI's own `uv run` sets it too, for the CLI's environment)."""
    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = host.g().venv
    return env


def lock_in_step(uv):
    """(ok, uv's last error line). Offline: a matching lock checks without
    the network or a cache, so a failure is most likely a stale lock, but
    uv's own words go with it in case it is something else."""
    r = subprocess.run(
        [uv, "lock", "--check", "--offline", "--project", host.g().root],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=uv_env(),
    )
    lines = [x.strip() for x in r.stderr.decode(errors="replace").splitlines() if x.strip()]
    # uv ends with a "hint:" line; its "error:" line is the one that says why.
    errors = [x for x in lines if x.startswith("error:")]
    return r.returncode == 0, ((errors or lines)[-1] if lines else "")


def sync_venv(dev=False, required=True):
    """`uv sync --locked`: .venv/ holds exactly the locked versions, each
    verified by its sha256 in uv.lock; nothing is resolved at install time.
    Without --dev the sync is inexact so it does not strip a developer's
    dev group (`uv run` puts it back each time). With required False (the
    package plan's setup) a working .venv is left alone when there is no uv."""
    try:
        uv, ver = find_uv()
    except CliError:
        if required or not venv_ready():
            raise
        host.say(".venv: left as it is (no uv on PATH to check it against uv.lock)")
        return
    host.say("uv: %s %s" % (uv, ".".join(map(str, ver))))
    ok, why = lock_in_step(uv)
    if not ok:
        raise CliError(
            "uv lock --check failed; .venv was not changed. Most likely uv.lock is out of "
            "date with pyproject.toml (maintainers: '%s pins refresh' or 'uv lock', then "
            "commit uv.lock). uv said: %s" % (host.prog(), why or "nothing")
        )
    cmd = [uv, "sync", "--locked", "--project", host.g().root]
    cmd += [] if dev else ["--no-dev", "--inexact"]
    host.run(cmd, env=uv_env())
