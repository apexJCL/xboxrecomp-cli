"""The steamos target: the payload's launch and install scripts, and one tar
that is the same from any build host."""

import os
import shutil
import tarfile

from .. import host
from ..host import CliError
from . import app, copy_lf, render, templates


def stage_steamos(payload, lib, icon_dir):
    """install.sh, launch.sh, install_lib.py, the game's running_game.py
    (the launcher asks it whether a copy already runs) and the icon."""
    t = os.path.join(templates(), "steamos")
    copy_lf(os.path.join(t, "install.sh"), os.path.join(payload, "install.sh"), 0o755)
    render(
        os.path.join(t, "launch.sh"),
        {"NAME": lib.PRODUCT_NAME},
        os.path.join(payload, "launch.sh"),
        0o755,
    )
    copy_lf(os.path.join(t, "install_lib.py"), os.path.join(payload, "install_lib.py"))
    copy_lf(
        host.g().path("scripts", "running_game.py"),
        os.path.join(payload, "running_game.py"),
    )
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
