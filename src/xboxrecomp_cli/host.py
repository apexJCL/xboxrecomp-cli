"""The host: which OS and architecture this is, the venv layout, output (the
progress view or plain lines), and running a command."""

import hashlib
import os
import platform
import subprocess

from . import manifest

CLT = "/Library/Developer/CommandLineTools"


class CliError(Exception):
    pass


# The progress view (package/progress.py) while `package` runs, else None:
# say, step and run go through it, and child output goes to build-logs/.
VIEW = None


def g():
    return manifest.current()


def prog():
    """The command name players type: the game's wrapper (`blinx2`)."""
    return g().slug


def cli_name():
    return prog() if host_os() == "windows" else "./" + prog()


def say(msg=""):
    if VIEW:
        VIEW.say(msg)
    else:
        print(msg, flush=True)


def step(msg):
    if VIEW:
        VIEW.begin(msg)
        return
    say()
    say("== %s ==" % msg)


def mark(name):
    """The plan step now running: '[3/4] build' at the head of the view."""
    if VIEW and name in VIEW.plan:
        VIEW.set_stage("[%d/%d] %s" % (VIEW.plan.index(name) + 1, len(VIEW.plan), name))


def progress_lib():
    from .package import progress

    return progress


def host_os(system=None):
    s = (system or platform.system()).lower()
    if s.startswith("win") or s.startswith("cygwin") or s.startswith("msys"):
        return "windows"
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    return s


def host_arch(machine=None):
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64", "armv8", "armv8l"):
        return "aarch64"
    return m


def exe_suffix(os_name=None):
    return ".exe" if (os_name or host_os()) == "windows" else ""


def venv_bin(venv=None, os_name=None):
    return os.path.join(
        venv or g().venv, "Scripts" if (os_name or host_os()) == "windows" else "bin"
    )


def venv_python(venv=None, os_name=None):
    return os.path.join(venv_bin(venv, os_name), "python" + exe_suffix(os_name))


def build_env(env=None, os_name=None):
    """The environment for CMake and the compilers: the venv's tools first
    on PATH, and on macOS the Command Line Tools as the developer dir
    (the selected Xcode's linker may not read the newer SDK)."""
    e = dict(os.environ if env is None else env)
    e.setdefault("NINJA_STATUS", "[%f/%t] ")  # what the progress view parses
    b = venv_bin(os_name=os_name)
    if os.path.isdir(b):
        e["PATH"] = b + os.pathsep + e.get("PATH", "")
    if (os_name or host_os()) == "macos" and not e.get("DEVELOPER_DIR") and os.path.isdir(CLT):
        e["DEVELOPER_DIR"] = CLT
    return e


def run(cmd, cwd=None, env=None, check=True, parser=None):
    """Argument lists only, never a shell string: paths with spaces stay
    one argument on every host. Under the progress view the output goes to
    the step's log and the parser (by default chosen from the command)."""
    if VIEW:
        rc = progress_lib().run_step(VIEW, cmd, cwd, env, parser, VIEW.verbose)
    else:
        rc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env).returncode
    if check and rc != 0:
        raise CliError("%s failed (exit %d)" % (os.path.basename(str(cmd[0])), rc))
    return rc


def require(path, stage):
    if not os.path.exists(path):
        raise CliError(
            "missing %s: run the '%s' stage first" % (os.path.relpath(path, g().root), stage)
        )


def refuse_while_regenerating():
    if os.path.exists(g().regen_marker):
        raise CliError(
            "gen/ is being regenerated (or the last recomp failed): wait, "
            "or run '%s recomp' again" % prog()
        )


def sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def git_head(d):
    r = subprocess.run(
        ["git", "-C", d, "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return r.stdout.decode().strip() if r.returncode == 0 else ""
