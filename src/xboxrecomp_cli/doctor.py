"""doctor: what this host has, and which targets it can package."""

import os
import platform
import shutil
import subprocess

from . import cli_dir, env, fetch, host, toolkit
from .host import CliError


def makensis_hints():
    slug = host.prog()
    return {
        "macos": "brew install makensis",
        "debian": "sudo apt install nsis",
        "fedora": "sudo dnf install mingw32-nsis",
        "arch": "sudo pacman -S nsis   (or the AUR package)",
        "immutable": (
            f"in a toolbox or distrobox: distrobox create -n {slug}-build -i fedora:42, "
            f"then distrobox enter {slug}-build -- sudo dnf install -y mingw32-nsis; "
            f"build on the host ('{slug} package steamos' or '{slug} build'), then run "
            f"'{slug} package windows --no-build' inside the box (the build needs the "
            "host's .venv and llvm-mingw; packaging in the box needs only makensis)"
        ),
    }


def makensis_path():
    if host.host_os() == "windows":
        try:
            ver = fetch.load_pins()["nsis"]["version"]
        except (CliError, KeyError, ValueError):
            ver = None
        if ver:
            p = os.path.join(host.g().third_party, "nsis-%s" % ver, "makensis.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("makensis")


def linux_flavour():
    try:
        with open("/etc/os-release") as f:
            info = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return "debian"
    ids = (info.get("ID", "") + " " + info.get("ID_LIKE", "")).replace('"', "").lower()
    if os.path.exists("/run/ostree-booted") or "steamos" in ids:
        return "immutable"
    for k in ("fedora", "arch", "debian", "ubuntu"):
        if k in ids:
            return "debian" if k == "ubuntu" else k
    return "debian"


def makensis_hint():
    return makensis_hints()["macos" if host.host_os() == "macos" else linux_flavour()]


def brew_formulae():
    """The Homebrew formulae the macos target links (package.brew)."""
    return list(host.g().m.get("package", {}).get("brew", []))


def brew_missing(pkgs=None):
    pkgs = brew_formulae() if pkgs is None else pkgs
    if not shutil.which("brew"):
        return list(pkgs)
    missing = []
    for p in pkgs:
        r = subprocess.run(
            ["brew", "--prefix", "--installed", p],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if r.returncode != 0:
            missing.append(p)
    return missing


def quarantined(path):
    if host.host_os() != "macos" or not os.path.exists(path):
        return False
    r = subprocess.run(
        ["xattr", "-p", "com.apple.quarantine", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def windows_long_paths_enabled():
    try:
        import winreg

        k = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        )
        return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except (ImportError, OSError):
        return None


def windows_path_warnings(root=None, long_paths=None):
    root = host.g().root if root is None else root
    w = []
    if len(root) > 60:
        w.append(
            "the checkout path is %d characters; the build nests deep, so use a short "
            "path such as C:\\b2" % len(root)
        )
    if long_paths is False:
        w.append("LongPathsEnabled is off; enable it, and run 'git config core.longpaths true'")
    return w


def dump_state():
    """(sha256 or None, known): the dump against xbe.sha256. An empty list
    means every dump is known (the check is off)."""
    g = host.g()
    if not os.path.isfile(g.xbe):
        return None, False
    sha = host.sha256_path(g.xbe)
    known = g.m["xbe"]["sha256"]
    return sha, (not known or sha in known)


def unknown_dump_note(sha):
    known = host.g().m["xbe"]["sha256"]
    return "unknown dump: sha256 %s is not one game.toml lists (%s)" % (
        sha,
        ", ".join(k[:12] for k in known),
    )


# What the last doctor_report found missing for any build (setup can fix
# most of it): doctor's exit status when the game packages no steamos.
PROBLEMS = []


def doctor_report():
    """(lines, packageable targets, blocked {target: reason})."""
    g = host.g()
    slug = host.prog()
    os_name, arch = host.host_os(), host.host_arch()
    lines = ["host:       %s %s, Python %s" % (os_name, arch, platform.python_version())]
    blocked = {}
    problems = []
    try:
        uv, ver = env.find_uv()
        lines.append("uv:         %s %s" % (uv, ".".join(map(str, ver))))
        ok, why = env.lock_in_step(uv)
        lines.append(
            "lock:       "
            + (
                "uv.lock in step with pyproject.toml"
                if ok
                else "uv lock --check failed (stale uv.lock? maintainers: %s pins refresh): " % slug
                + (why or "no message")
            )
        )
    except CliError as e:
        lines.append("uv:         %s" % e)
        # A working .venv still builds and packages; only setup needs uv.
        if not env.venv_ready():
            problems.append("no uv (%s)" % env.UV_HINT[os_name])
    # venv tools
    b = host.venv_bin()
    for t in ("cmake", "ninja"):
        p = os.path.join(b, t + host.exe_suffix())
        lines.append(
            "%-11s %s"
            % (
                t + ":",
                p
                if os.path.isfile(p)
                else "missing in .venv (run '%s setup'); system: %s"
                % (slug, shutil.which(t) or "none"),
            )
        )
        if not os.path.isfile(p):
            problems.append("no %s in .venv (%s setup; or --system-tools)" % (t, slug))
    try:
        lines.append("tools py:   %s" % toolkit.tool_python())
    except CliError as e:
        lines.append("tools py:   %s" % e)
        problems.append("no tools Python (%s setup)" % slug)
    lines.append(cli_dir.doctor_line())
    w = cli_dir.drift_warning()
    if w:
        lines.append("warning:    " + w)
    tk = toolkit.toolkit_dir()
    if os.path.isdir(os.path.join(tk, "tools")):
        head = host.git_head(tk) or "?"
        note = toolkit.pin_note(tk, g.m["toolkit"])
        lines.append("toolkit:    %s @ %s%s" % (tk, head[:12], note))
        w = toolkit.drift_warning("toolkit", tk, g.m["toolkit"])
        if w:
            lines.append("warning:    " + w)
    else:
        lines.append("toolkit:    missing at %s (run '%s setup')" % (tk, slug))
        problems.append("no toolkit (%s setup)" % slug)
    try:
        root = fetch.mingw_root()
        cc = (
            os.path.join(root, "bin", "x86_64-w64-mingw32-clang" + host.exe_suffix())
            if root
            else shutil.which("x86_64-w64-mingw32-clang")
        )
        lines.append("llvm-mingw: %s" % (root or cc))
        if quarantined(cc or ""):
            lines.append(
                "            quarantined: macOS will refuse it; clear with "
                "xattr -dr com.apple.quarantine '%s'" % root
            )
            problems.append("llvm-mingw quarantined")
    except CliError as e:
        lines.append("llvm-mingw: %s" % e)
        problems.append("no llvm-mingw (%s setup)" % slug)
    gen_ok = os.path.isfile(os.path.join(g.gen, "recomp_funcs.h"))
    sha, known = dump_state()
    xbe_rel, gf_rel = g.rel(g.xbe), g.rel(g.game_files)
    lines.append(
        "game files: %s"
        % (
            (xbe_rel + ("" if known else " (%s)" % unknown_dump_note(sha)))
            if sha
            else "MISSING: put your dump in %s/" % gf_rel
        )
    )
    lines.append(
        "gen/:       %s"
        % ("present" if gen_ok else "not generated yet (%s analyze, %s recomp)" % (slug, slug))
    )
    envs = [g.m["game"][k] for k in ("env_header", "env_doc") if g.m["game"][k]]
    if envs:
        lines.append(
            "game env:   %s"
            % ", ".join(e + ("" if os.path.isfile(g.path(e)) else " (missing)") for e in envs)
        )
    if os_name == "macos":
        lines.append("DEVELOPER_DIR: %s" % host.build_env().get("DEVELOPER_DIR", "(Xcode default)"))
    if os_name == "windows":
        for w in windows_path_warnings(g.root, windows_long_paths_enabled()):
            lines.append("warning:    " + w)
    if not sha:
        problems.append("no %s" % xbe_rel)
    PROBLEMS[:] = problems
    ok = not problems
    why = "; ".join(problems)
    if not ok:
        blocked["steamos"] = why
    mk = makensis_path()
    lines.append("makensis:   %s" % (mk or "missing (windows target only): " + makensis_hint()))
    if not ok:
        blocked["windows"] = why
    elif not mk:
        blocked["windows"] = "makensis: " + makensis_hint()
    if os_name != "macos":
        blocked["macos"] = "needs a macOS host (Apple clang, codesign, hdiutil)"
    else:
        missing = brew_missing()
        clt = os.path.isdir(host.CLT)
        if not clt:
            blocked["macos"] = "Command Line Tools: xcode-select --install"
        elif missing:
            blocked["macos"] = "Homebrew: brew install %s" % " ".join(missing)
        elif not ok:
            blocked["macos"] = why
    wanted = g.m["package"]["targets"] if "package" in g.m else []
    blocked = {t: w for t, w in blocked.items() if t in wanted}
    targets = [t for t in ("windows", "steamos", "macos") if t in wanted and t not in blocked]
    if "package" not in g.m:
        lines.append("package:    game.toml has no [package]: nothing to package")
    lines.append("next:       " + next_step(problems, gen_ok, gf_rel))
    return lines, targets, blocked


def next_step(problems, gen_ok, game_files):
    """One line for a fresh tree, where everything is missing at once: the
    first fix in setup order (uv, then setup, then the dump, then the
    pipeline), so the eight 'missing' lines above it end in one thing to
    do."""
    cmd = host.cli_name()
    if any(p.startswith("no uv") for p in problems):
        return "install uv: " + env.uv_hint()
    fixes = [p for p in problems if "setup" in p or "quarantined" in p]
    if fixes:
        return "'%s setup' (%s)" % (cmd, "; ".join(p.split(" (")[0] for p in fixes))
    if any(p.startswith("no ") and p.endswith(".xbe") for p in problems):
        return "put your dump (default.xbe and the game's files) in %s/" % game_files
    if not gen_ok:
        return "'%s analyze', then '%s recomp' ('%s all' builds too)" % (cmd, cmd, cmd)
    return "'%s build', or '%s package <target>'" % (cmd, cmd)
