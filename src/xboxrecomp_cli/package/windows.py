"""The windows target: the launcher exe with the icon compiled in, and the
NSIS installer beside the game files."""

import os
import re
import shutil
import zipfile

from .. import doctor, fetch, host
from ..host import CliError
from . import app, g, render, template

LOCALAPPDATA = "%LOCALAPPDATA%\\"


def data_dir():
    """data.windows below %LOCALAPPDATA%: the launcher finds that folder with
    SHGetKnownFolderPath, so it can be nowhere else."""
    d = g().m["data"]["windows"]
    if not d.upper().startswith(LOCALAPPDATA) or len(d) == len(LOCALAPPDATA):
        raise CliError("game.toml's data.windows (%s) must be %%LOCALAPPDATA%%\\<folder>" % d)
    return d[len(LOCALAPPDATA) :]


def c_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def link_name(name):
    """The product name as a shortcut file name: ':' and the other characters
    Windows refuses in a file name become ' -' or go."""
    name = re.sub(r"\s*:\s*", " - ", name)
    return re.sub(r'[<>"/\\|?*]', "", name).strip(" .")


def launcher_values():
    """The C-escaped @KEY@ values of launcher.c."""
    G = g()
    return {
        "APP": c_escape(app()),
        "EXE": c_escape(G.exe_name + ".exe"),
        "DATA_DIR": c_escape(data_dir()),
        "DATA_DIR_ENV": c_escape(G.m["data"]["dir_env"]),
    }


def build_launcher_exe(dst, ico, work, name):
    """<app>.exe with the icon compiled in (windres, from llvm-mingw); the
    rendered launcher.c, the .rc and its object stay in the work dir, out of
    the payload."""
    a = app()
    rc, res = os.path.join(work, a + ".rc"), os.path.join(work, a + ".res.o")
    src = os.path.join(work, "launcher.c")
    render(template("windows", "app.rc.in"), {"ICON": ico.replace("\\", "/")}, rc)
    render(template("windows", "launcher.c"), launcher_values(), src)
    env = host.build_env()
    host.run([fetch.mingw_tool("windres"), "-O", "coff", "-o", res, rc], env=env)
    host.run(
        [
            fetch.mingw_tool("clang"),
            "-municode",
            "-mwindows",
            "-O2",
            "-Wall",
            "-o",
            dst,
            '-DAPP_NAME=L"%s"' % c_escape(name),
            src,
            res,
            "-lshell32",
            "-lole32",
            "-luuid",
        ],
        env=env,
    )


def nsis_escape(path):
    return path.replace("$", "$$")


def installer_values(lib, version, setup, payload, ico, inst, uninst):
    """The @KEY@ values of installer.nsi.in."""
    return {
        "NAME": lib.PRODUCT_NAME,
        "LINK_NAME": nsis_escape(link_name(lib.PRODUCT_NAME)),
        "APP": nsis_escape(app()),
        "DATA_DIR": nsis_escape(data_dir()),
        "VERSION": version,
        "OUTFILE": nsis_escape(setup),
        "STAGE": nsis_escape(payload),
        "ICON": nsis_escape(ico),
        "INSTALL_FILES": inst.rstrip("\n"),
        "UNINSTALL_FILES": uninst.rstrip("\n"),
    }


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
        template("windows", "installer.nsi.in"),
        installer_values(
            lib, version, setup, payload, os.path.join(icon_dir, a + ".ico"), inst, uninst
        ),
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
