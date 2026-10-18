"""The windows target: the launcher exe with the icon compiled in, the
root tools (the stub and apply.exe), and the NSIS installer beside the
game files, or a patch folder (D8)."""

import json
import os
import re
import shutil
import zipfile

from .. import doctor, fetch, host
from ..host import CliError
from . import app, g, render, template

LOCALAPPDATA = "%LOCALAPPDATA%\\"
# The root stub's contract with every installed version (stub.c's
# STUB_ABI): a patch is made only between equal ones (D5, D8).
STUB_ABI = 1
# Beside the payload in the stage, never in it: the root tools are not in
# the manifest's files[] (D8).
ROOT_TOOLS = "root"
STUB = "stub.exe"
APPLY = "apply.exe"
PATCH_LIST = "patch.lst"


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


def _icon_res(ico, work):
    """The icon as a COFF object (windres, from llvm-mingw), in the work dir."""
    a = app()
    rc, res = os.path.join(work, a + ".rc"), os.path.join(work, a + ".res.o")
    if not os.path.isfile(res):
        render(template("windows", "app.rc.in"), {"ICON": ico.replace("\\", "/")}, rc)
        host.run([fetch.mingw_tool("windres"), "-O", "coff", "-o", res, rc], env=host.build_env())
    return res


def _cc(dst, srcs, flags, libs):
    host.run(
        [fetch.mingw_tool("clang"), "-municode", "-O2", "-Wall"]
        + flags
        # No link timestamp: the same bytes on every build, so a reused
        # stage keeps them (D12).
        + ["-Wl,--no-insert-timestamp", "-o", dst]
        + srcs
        + libs,
        env=host.build_env(),
    )


def build_launcher_exe(dst, ico, work, name):
    """<app>.exe with the icon compiled in; the rendered launcher.c, the .rc
    and its object stay in the work dir, out of the payload."""
    res = _icon_res(ico, work)
    src = os.path.join(work, "launcher.c")
    render(template("windows", "launcher.c"), launcher_values(), src)
    _cc(
        dst,
        [src, res],
        ["-mwindows", '-DAPP_NAME=L"%s"' % c_escape(name)],
        ["-lshell32", "-lole32", "-luuid"],
    )


def build_root_tools(work, ico, name):
    """work/root/stub.exe (the root <app>.exe) and work/root/apply.exe; both
    carry the icon. Returns the folder."""
    res = _icon_res(ico, work)
    out = os.path.join(work, ROOT_TOOLS)
    os.makedirs(out, exist_ok=True)
    vals = {"APP": c_escape(app())}
    stub = os.path.join(work, "stub.c")
    render(template("windows", "stub.c"), vals, stub)
    _cc(
        os.path.join(out, STUB), [stub, res], ["-mwindows", '-DAPP_NAME=L"%s"' % c_escape(name)], []
    )
    apply_c = os.path.join(work, "apply.c")
    render(template("windows", "apply.c"), vals, apply_c)
    for n in ("apply_plan.c", "apply_plan.h"):
        shutil.copyfile(template("windows", n), os.path.join(work, n))
    _cc(
        os.path.join(out, APPLY),
        [apply_c, os.path.join(work, "apply_plan.c"), res],
        [],
        ["-lbcrypt", "-ladvapi32"],
    )
    return out


def root_tools_ready(work):
    return all(os.path.isfile(os.path.join(work, ROOT_TOOLS, n)) for n in (STUB, APPLY))


def root_block(work):
    """The manifest's root block (D8): the stub's contract and both tools'
    sha256 and size, kept out of files[]."""
    out = {"stub_abi": STUB_ABI}
    for key, n in (("stub", STUB), ("apply", APPLY)):
        p = os.path.join(work, ROOT_TOOLS, n)
        out[key] = {"sha256": host.sha256_path(p), "size": os.path.getsize(p)}
    return out


def nsis_escape(path):
    return path.replace("$", "$$")


def tree_kib(root):
    """The size of every file under root, in KiB, rounded up (NSIS AddSize)."""
    total = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
    return (total + 1023) // 1024


def installer_values(lib, version, setup, payload, ico, inst, uninst, game_kib=0, tools="root"):
    """The @KEY@ values of installer.nsi.in. tools: the folder of the root
    tools (stub.exe, apply.exe)."""
    return {
        "APPLY": nsis_escape(os.path.join(tools, APPLY)),
        "STUB": nsis_escape(os.path.join(tools, STUB)),
        "GAME_FILES_KB": str(game_kib),
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


def bundle_path(out, version):
    return os.path.join(out, "%s-%s-windows" % (app(), version))


def patch_path(out, version, base_version):
    from . import patch

    return os.path.join(
        out, "%s-%s-windows-patch-from-%s" % (app(), version, patch.short(base_version))
    )


def patch_list(doc, app_name, new_sha):
    """patch.lst, apply.exe's reading of patch.json (C has no JSON parser):
    a header, key lines, then "<kind> <size> <sha256> <path>" per entry,
    kind C, N, D or K."""
    for k in ("base_version", "version"):
        if not re.fullmatch(r"[^\s/\\:.][^\s/\\:]*", doc[k]):
            raise CliError("patch.lst: version %r cannot be a folder name" % doc[k])
    lines = [
        "xbr-patch 1",
        "app " + app_name,
        "version " + doc["version"],
        "base " + doc["base_version"],
        "base_sha " + doc["base_manifest_sha256"],
        "new_sha " + new_sha,
        "stub_abi %d" % STUB_ABI,
    ]
    for kind, key in (("C", "changed"), ("N", "new"), ("D", "deleted"), ("K", "kept")):
        for e in doc[key]:
            if "\n" in e["path"] or "\r" in e["path"]:
                raise CliError("patch.lst: a file name with a line break: %r" % e["path"])
            lines.append("%s %d %s %s" % (kind, e["size"], e["sha256"], e["path"]))
    return "\n".join(lines) + "\n"


def wrap_windows_patch(stage, payload, version, out, choice):
    """The patch folder: apply.exe, patch.lst, patch.json and files\\ (the
    changed and new files, the full manifest.json and SHA256SUMS). Players
    run apply.exe from it; NSIS is not used for patches (D8)."""
    from . import incr, patch

    man_path = os.path.join(payload, "manifest.json")
    with open(man_path, encoding="utf-8") as f:
        man = json.load(f)
    folder = patch_path(out, version, choice.base.version)
    part = folder + ".part"
    shutil.rmtree(part, ignore_errors=True)
    files = os.path.join(part, "files")
    os.makedirs(files)
    put = incr._linker(True)
    try:
        for e in patch.shipped(choice.diff):
            dst = os.path.join(files, *e["path"].split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            put(os.path.join(payload, *e["path"].split("/")), dst)
        for n in ("manifest.json", "SHA256SUMS"):
            shutil.copyfile(os.path.join(payload, n), os.path.join(files, n))
        shutil.copyfile(os.path.join(stage, ROOT_TOOLS, APPLY), os.path.join(part, APPLY))
        doc = patch.patch_doc(choice.base, man, choice.diff)
        patch.write_doc(os.path.join(part, patch.PATCH_JSON), doc)
        with open(os.path.join(part, PATCH_LIST), "w", newline="\n", encoding="utf-8") as f:
            f.write(patch_list(doc, app(), host.sha256_path(man_path)))
        shutil.rmtree(folder, ignore_errors=True)
        os.rename(part, folder)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return folder


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
        names, inst, uninst = lib.nsis_lists(payload, "versions/%s.partial" % version)
    except ValueError as e:
        raise CliError(str(e)) from e
    a = app()
    folder = bundle_path(out, version)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    setup = os.path.join(folder, "%s-%s-setup.exe" % (a, version))
    nsi = os.path.join(stage, "installer.nsi")
    render(
        template("windows", "installer.nsi.in"),
        installer_values(
            lib,
            version,
            setup,
            payload,
            os.path.join(icon_dir, a + ".ico"),
            inst,
            uninst,
            tree_kib(os.path.join(payload, "game_files")),
            os.path.join(stage, ROOT_TOOLS),
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
    # Linked, not moved: the staged payload keeps its game files for the
    # next run (clones on APFS, hard links elsewhere: no second 3 GB).
    from . import incr

    incr.link_tree(os.path.join(payload, "game_files"), os.path.join(folder, "game_files"))
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
