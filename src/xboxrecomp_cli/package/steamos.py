"""The steamos target: the payload's launch and install scripts, and one tar
that is the same from any build host.

The installer (install.sh, install_lib.py), the launcher (launch.sh) and the
README's install section are this CLI's own, game-agnostic templates
(templates/steamos/), filled with the game's values from game.toml. A game
that needs its own copy of one puts it in <package.templates>/steamos/ (or
README.part in <package.content>/steamos/), and that file wins."""

import os
import shutil
import tarfile

from .. import host, pins
from ..host import CliError
from . import app, copy_lf, g, render

DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "steamos")
# Rendered with steamos_values(); install.sh is the one the installer runs.
FILES = (("install.sh", 0o755), ("launch.sh", 0o755), ("install_lib.py", 0o644))


def template(name):
    """The game's override of a steamos template, else the CLI's default."""
    G = g()
    dirs = [os.path.join(G.templates, "steamos")] if G.templates else []
    if name == "README.part" and G.content:
        dirs.append(os.path.join(G.content, "steamos"))
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return os.path.join(DEFAULTS, name)


def steamos_values(lib):
    """The @KEY@ values of the steamos templates."""
    G = g()
    umu = pins.UMU_LAUNCHER
    return {
        "NAME": lib.PRODUCT_NAME,
        "APP": app(),
        "PRODUCT": lib.PRODUCT,
        "EXE": G.exe_name,
        "SOURCE_NAME": lib.SOURCE_NAME,
        "DEFAULT_ROOT": G.m["data"]["steamos"],
        "ROOT_ENV": G.m["game"]["slug"].upper().replace("-", "_") + "_ROOT",
        "UMU_VERSION": umu["version"],
        "UMU_URL": umu["url"],
        "UMU_SHA256": umu["sha256"],
    }


def render_lf(src, values, dst, mode):
    """render(), with LF line endings whatever the checkout did."""
    render(src, values, dst, mode)
    with open(dst, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        with open(dst, "wb") as f:
            f.write(data.replace(b"\r\n", b"\n"))


def stage_steamos(payload, lib, icon_dir):
    """install.sh, launch.sh, install_lib.py, the game's running_game.py
    when it has one (install.sh status asks it whether a copy runs) and the
    icon."""
    values = steamos_values(lib)
    for name, mode in FILES:
        render_lf(template(name), values, os.path.join(payload, name), mode)
    # Without it, install.sh status looks for the exe with pgrep.
    rg = host.g().path("scripts", "running_game.py")
    if os.path.isfile(rg):
        copy_lf(rg, os.path.join(payload, "running_game.py"))
    shutil.copy2(os.path.join(icon_dir, "icon.png"), os.path.join(payload, "icon.png"))


def wrap_steamos(payload, version, exe, out):
    """One tar, written by tarfile with explicit modes, uid/gid 0 and no
    owner names (no account name leaks), the exe's mtime on every member and
    no symlinks: the same file from any build host, NTFS included."""
    dst = os.path.join(out, "%s-%s-steamos.tar" % (app(), version))
    part = dst + ".part"
    mtime = int(os.path.getmtime(exe))
    top = os.path.basename(payload)
    execs = {"install.sh", "launch.sh"}

    def info(arc, path, isdir):
        ti = tarfile.TarInfo(arc)
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mtime = mtime
        if isdir:
            ti.type, ti.mode = tarfile.DIRTYPE, 0o755
        else:
            ti.size = os.path.getsize(path)
            ti.mode = 0o755 if os.path.basename(arc) in execs and arc.count("/") == 1 else 0o644
        return ti

    with tarfile.open(part, "w", format=tarfile.PAX_FORMAT) as t:
        t.addfile(info(top, payload, True))
        for dirpath, dirnames, filenames in os.walk(payload):
            dirnames.sort()
            rel = os.path.relpath(dirpath, payload)
            for d in dirnames:
                p = os.path.join(dirpath, d)
                if os.path.islink(p):
                    raise CliError("symlink in the payload: %s" % p)
                t.addfile(info("/".join(x for x in (top, rel, d) if x != "."), p, True))
            for fn in sorted(filenames):
                p = os.path.join(dirpath, fn)
                if os.path.islink(p):
                    raise CliError("symlink in the payload: %s" % p)
                with open(p, "rb") as f:
                    t.addfile(info("/".join(x for x in (top, rel, fn) if x != "."), p, False), f)
    os.replace(part, dst)
    return dst
