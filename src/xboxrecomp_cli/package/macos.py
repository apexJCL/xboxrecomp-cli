"""The macos target: <app>.app with the game files and every non-system
dylib inside, relinked to @rpath and signed ad hoc, then a DMG."""

import json
import os
import re
import shutil
import subprocess

from .. import host
from ..host import CliError
from . import DEFAULTS, app, render, templates, write_manifest

DEFAULTS_MACOS = os.path.join(DEFAULTS, "macos")


def _otool(path):
    return subprocess.run(["otool", "-L", path], stdout=subprocess.PIPE, check=True).stdout.decode()


def brew_origin(src):
    """Where a bundled dylib came from, without the host's paths: the
    Homebrew formula and version when it is a Cellar file."""
    parts = src.split("/")
    if "Cellar" in parts and len(parts) > parts.index("Cellar") + 2:
        i = parts.index("Cellar")
        return "%s %s" % (parts[i + 1], parts[i + 2])
    return os.path.basename(src)


def brew_prefix_lib(formula, name):
    if not shutil.which("brew"):
        return None
    r = subprocess.run(
        ["brew", "--prefix", formula], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    p = os.path.join(r.stdout.decode().strip(), "lib", name)
    return p if r.returncode == 0 and os.path.isfile(p) else None


def companions():
    """package.dylib_companions (NAME=brew:FORMULA): libraries the game
    loads with dlopen, so otool cannot see them, as NAME=PATH for the plan.
    One whose formula is not installed is left out, as before."""
    out = []
    for c in host.g().package["dylib_companions"]:
        name, src = c.split("=", 1)
        if not src.startswith("brew:"):
            raise CliError("package.dylib_companions: %s: only brew:FORMULA is supported" % c)
        p = brew_prefix_lib(src[len("brew:") :], name)
        if p:
            out.append("%s=%s" % (name, p))
    return out


def bundle_path(out, version):
    return os.path.join(out, "%s-%s.dmg" % (app(), version))


def patch_path(out, version, base_version):
    from . import patch

    return os.path.join(
        out, "%s-%s-patch-from-%s.dmg" % (app(), version, patch.short(base_version))
    )


# What a macOS patch ships whole, beside the changed files of Resources/:
# the manifest lists only Resources/, and these are small (the program and
# its dylibs, about 45 MB for BLiNX 2) next to the game files there.
WHOLE = ("Info.plist", "MacOS", "Frameworks", "_CodeSignature")


def build_swap(dst, app_dir):
    """xbr-swap for the patch: universal, for the app's own minimum macOS
    (LSMinimumSystemVersion), signed ad hoc."""
    import plistlib

    with open(os.path.join(app_dir, "Contents", "Info.plist"), "rb") as f:
        minimum = plistlib.load(f).get("LSMinimumSystemVersion", "13.0")
    src = os.path.join(DEFAULTS_MACOS, "xbr-swap.c")
    host.run(
        ["clang", "-O2", "-Wall", "-arch", "arm64", "-arch", "x86_64"]
        + ["-mmacosx-version-min=%s" % minimum, "-o", dst, src]
    )
    host.run(["codesign", "--force", "--sign", "-", dst])


def data_env():
    """patch.env's DATA_DIR and DATA_DIR_ENV: where the game keeps
    config/config.toml (data.macos, a leading ~/ left for the script to
    expand) and the variable that moves it. apply.command reads that file
    only to name a textures.pack line that overrides the new build (D8)."""
    d = host.g().m["data"]
    env = d.get("dir_env") or ""
    if env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
        raise CliError("game.toml's data.dir_env (%s) must be a variable name" % env)
    return {"DATA_DIR": d["macos"], "DATA_DIR_ENV": env}


def patch_tree(dst, app_dir, choice, version):
    """The patch DMG's folder: apply.command, xbr-swap and patch/, which
    holds the changed and new files of Resources/ and the WHOLE parts at
    their paths under Contents/, with what apply.command reads (patch.env,
    kept.list, deleted.list, whole.list) and patch.json."""
    from . import patch

    shutil.rmtree(dst, ignore_errors=True)
    p = os.path.join(dst, "patch")
    contents = os.path.join(app_dir, "Contents")
    res = os.path.join(contents, "Resources")
    with open(os.path.join(res, "manifest.json"), encoding="utf-8") as f:
        new = json.load(f)
    d = choice.diff
    for e in patch.shipped(d):
        _copy(os.path.join(res, e["path"]), os.path.join(p, "Contents", "Resources", e["path"]))
    for name in ("manifest.json", "SHA256SUMS"):
        _copy(os.path.join(res, name), os.path.join(p, "Contents", "Resources", name))
    for part in WHOLE:
        src = os.path.join(contents, part)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(p, "Contents", part), symlinks=True)
        elif os.path.isfile(src):
            _copy(src, os.path.join(p, "Contents", part))
    doc = patch.patch_doc(choice.base, new, d)
    doc["whole"] = list(WHOLE)
    patch.write_doc(os.path.join(p, patch.PATCH_JSON), doc)
    env = {
        "APP": app(),
        "VERSION": version,
        "BASE_VERSION": choice.base.version,
        "BASE_SHA": choice.base.sha256,
        "NEW_SHA": host.sha256_path(os.path.join(res, "manifest.json")),
        **data_env(),
    }
    for k, v in env.items():
        if "'" in v or "\n" in v:
            raise CliError("%s: %r cannot go in patch.env" % (k, v))
    _text(os.path.join(p, "patch.env"), "".join("%s='%s'\n" % kv for kv in env.items()))
    _text(
        os.path.join(p, "kept.list"),
        "".join("%d %s %s\n" % (e["size"], e["sha256"], e["path"]) for e in d["kept"]),
    )
    _text(os.path.join(p, "deleted.list"), "".join(e["path"] + "\n" for e in d["deleted"]))
    _text(os.path.join(p, "whole.list"), "".join(w + "\n" for w in WHOLE))
    shutil.copyfile(
        os.path.join(DEFAULTS_MACOS, "apply.command"), os.path.join(dst, "apply.command")
    )
    os.chmod(os.path.join(dst, "apply.command"), 0o755)
    build_swap(os.path.join(dst, "xbr-swap"), app_dir)
    return dst


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def _text(path, text):
    with open(path, "w", newline="\n") as f:
        f.write(text)


def hdiutil(srcfolder, volname, dmg):
    # hdiutil create refuses an existing file: a same-version re-run (the
    # wizard's Retry) replaces it, as the Windows folder is replaced.
    if os.path.exists(dmg):
        os.remove(dmg)
    host.run(
        ["hdiutil", "create"]
        + (["-puppetstrings"] if host.VIEW else ["-quiet"])
        + ["-srcfolder", srcfolder, "-format", "UDZO", "-volname", volname, dmg]
    )


def wrap_macos(
    stage,
    payload,
    version,
    exe,
    out,
    overrides,
    lib,
    icns,
    reuse=None,
    choose=None,
):
    """The app in stage/dmg (made afresh each run from the staged payload,
    which stays in place), then the DMG. reuse(manifest path, dmg path,
    choice): True when the DMG on disk already holds this app (the wrap's
    key), and hdiutil, the slow part, is skipped. choose(manifest path):
    the patch.Choice; a patch DMG (stage/patch-dmg) in place of the full
    one."""
    from . import incr

    a = app()
    exe_name = host.g().exe_name
    t = os.path.join(templates(), "macos")
    shutil.rmtree(os.path.join(stage, "dmg"), ignore_errors=True)
    bundle = os.path.join(stage, "dmg", a + ".app")
    c = os.path.join(bundle, "Contents")
    for d in ("MacOS", "Frameworks"):
        os.makedirs(os.path.join(c, d))
    # Clones (APFS) or copies, never hard links: codesign and the
    # manifest write into the app, and the payload must not change.
    incr.link_tree(payload, os.path.join(c, "Resources"), hard=False)
    build_sha = lib.sha256_file(exe)
    main_exe = os.path.join(c, "MacOS", exe_name)
    shutil.copy2(exe, main_exe)
    render(
        os.path.join(t, a + ".in"),
        {"NAME": lib.PRODUCT_NAME},
        os.path.join(c, "MacOS", a),
        0o755,
    )
    shutil.copy2(icns, os.path.join(c, "Resources", a + ".icns"))
    short = version.split("-")[0]
    render(
        os.path.join(t, "Info.plist.in"),
        {"VERSION": version, "SHORT_VERSION": short, "NAME": lib.PRODUCT_NAME},
        os.path.join(c, "Info.plist"),
    )
    plan = lib.dylib_plan(exe, companions())
    for dylib in plan["libs"]:
        dst = os.path.join(c, "Frameworks", dylib["name"])
        shutil.copy2(dylib["src"], dst)
        os.chmod(dst, 0o755)
        host.run(["install_name_tool", "-id", "@rpath/" + dylib["name"], dst])
    for ch in plan["changes"]:
        f = main_exe if ch["file"] == "@exe" else os.path.join(c, "Frameworks", ch["file"])
        host.run(["install_name_tool", "-change", ch["old"], ch["new"], f])
    host.run(["install_name_tool", "-add_rpath", "@executable_path/../Frameworks", main_exe])
    leftovers = []
    for f in [main_exe] + [os.path.join(c, "Frameworks", dylib["name"]) for dylib in plan["libs"]]:
        for dep in lib.parse_otool(_otool(f)):
            if not dep.startswith(lib.SYSTEM_PREFIXES) and not dep.startswith("@"):
                leftovers.append("%s -> %s" % (os.path.basename(f), dep))
    if leftovers:
        raise CliError("unbundled libraries remain: " + "; ".join(leftovers))
    write_manifest(
        os.path.join(c, "Resources"),
        "macos",
        version,
        overrides,
        lib,
        {
            "program": {"build_sha256": build_sha, "sealed_by": "codesign"},
            "host_deps": [
                {"name": dylib["name"], "from": brew_origin(dylib["src"])} for dylib in plan["libs"]
            ],
        },
    )
    for dylib in plan["libs"]:
        host.run(
            ["codesign", "--force", "--sign", "-", os.path.join(c, "Frameworks", dylib["name"])]
        )
    host.run(["codesign", "--force", "--sign", "-", main_exe])
    host.run(["codesign", "--force", "--sign", "-", bundle])
    host.run(["codesign", "--verify", "--deep", "--strict", bundle])
    os.symlink("/Applications", os.path.join(stage, "dmg", "Applications"))
    man_path = os.path.join(c, "Resources", "manifest.json")
    choice = choose(man_path) if choose is not None else None
    if choice is not None and choice.kind == "patch":
        dmg = patch_path(out, version, choice.base.version)
    else:
        dmg = bundle_path(out, version)
    if reuse is not None and reuse(man_path, dmg, choice):
        return dmg
    if choice is not None and choice.kind == "patch":
        src = patch_tree(os.path.join(stage, "patch-dmg"), bundle, choice, version)
        hdiutil(src, "%s %s update" % (lib.PRODUCT_NAME, version), dmg)
        return dmg
    hdiutil(os.path.join(stage, "dmg"), "%s %s" % (lib.PRODUCT_NAME, version), dmg)
    return dmg
