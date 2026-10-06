"""The windows target: the launcher exe with the icon compiled in, and the
NSIS installer beside the game files."""

import os
import shutil
import zipfile

from .. import doctor, fetch, host
from ..host import CliError
from . import app, render, templates


def build_launcher_exe(dst, ico, work, name):
    """<app>.exe with the icon compiled in (windres, from llvm-mingw); the
    .rc and its object stay in the work dir, out of the payload. The
    launcher's name macro is <APP>_NAME, as the game's launcher.c reads it."""
    a = app()
    rc, res = os.path.join(work, a + ".rc"), os.path.join(work, a + ".res.o")
    render(os.path.join(templates(), "windows", a + ".rc.in"), {"ICON": ico.replace("\\", "/")}, rc)
    env = host.build_env()
    host.run([fetch.mingw_tool("windres"), "-O", "coff", "-o", res, rc], env=env)
    macro = "".join(c if c.isalnum() else "_" for c in a.upper()) + "_NAME"
    host.run(
        [
            fetch.mingw_tool("clang"),
            "-municode",
            "-mwindows",
            "-O2",
            "-Wall",
            "-o",
            dst,
            '-D%s=L"%s"' % (macro, name.replace("\\", "\\\\").replace('"', '\\"')),
            os.path.join(templates(), "windows", "launcher.c"),
            res,
            "-lshell32",
            "-lole32",
            "-luuid",
        ],
        env=env,
    )


def nsis_escape(path):
    return path.replace("$", "$$")


def wrap_windows(stage, payload, version, out, lib, icon_dir):
    mk = doctor.makensis_path()
    if not mk:
        raise CliError(
            "no makensis: %s"
            % (
                doctor.makensis_hint()
                if host.host_os() != "windows"
                else "run '%s setup'" % host.prog()
            )
        )
    try:
        names, inst, uninst = lib.nsis_lists(payload)
    except ValueError as e:
        raise CliError(str(e)) from e
    a = app()
    folder = os.path.join(out, "%s-%s-windows" % (a, version))
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    setup = os.path.join(folder, "%s-%s-setup.exe" % (a, version))
    nsi = os.path.join(stage, "installer.nsi")
    render(
        os.path.join(templates(), "windows", "installer.nsi.in"),
        {
            "NAME": lib.PRODUCT_NAME,
            "VERSION": version,
            "OUTFILE": nsis_escape(setup),
            "STAGE": nsis_escape(payload),
            "ICON": nsis_escape(os.path.join(icon_dir, a + ".ico")),
            "INSTALL_FILES": inst.rstrip("\n"),
            "UNINSTALL_FILES": uninst.rstrip("\n"),
        },
        nsi,
    )
    flag = "/" if host.host_os() == "windows" else "-"
    try:
        nfiles = len(names) + 4  # + the uninstaller and the registry/shortcut steps
        host.run(
            [mk, flag + ("V3" if host.VIEW else "V2"), nsi],
            parser=host.progress_lib().MakensisParser(nfiles) if host.VIEW else None,
        )
        if os.path.getsize(setup) >= 2 * 1024**3:
            raise CliError("%s is over 2 GB" % setup)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # no half-made bundle in dist/
        raise
    os.rename(os.path.join(payload, "game_files"), os.path.join(folder, "game_files"))
    return folder


def archive(result):
    """--archive: a stored .zip of the folder."""
    z = result + ".zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for dirpath, _, files in os.walk(result):
            for fn in files:
                p = os.path.join(dirpath, fn)
                zf.write(p, os.path.relpath(p, os.path.dirname(result)))
    return z
