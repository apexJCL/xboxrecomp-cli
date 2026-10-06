"""The macos target: <app>.app with the game files and every non-system
dylib inside, relinked to @rpath and signed ad hoc, then a DMG."""

import os
import shutil
import subprocess

from .. import host
from ..host import CliError
from . import app, render, templates, write_manifest


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


def wrap_macos(stage, payload, version, exe, out, overrides, lib, icns, dump_unknown=False):
    a = app()
    exe_name = host.g().exe_name
    t = os.path.join(templates(), "macos")
    bundle = os.path.join(stage, "dmg", a + ".app")
    c = os.path.join(bundle, "Contents")
    for d in ("MacOS", "Frameworks", "Resources"):
        os.makedirs(os.path.join(c, d))
    for n in os.listdir(payload):
        shutil.move(os.path.join(payload, n), os.path.join(c, "Resources", n))
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
        dump_unknown=dump_unknown,
    )
    for dylib in plan["libs"]:
        host.run(
            ["codesign", "--force", "--sign", "-", os.path.join(c, "Frameworks", dylib["name"])]
        )
    host.run(["codesign", "--force", "--sign", "-", main_exe])
    host.run(["codesign", "--force", "--sign", "-", bundle])
    host.run(["codesign", "--verify", "--deep", "--strict", bundle])
    os.symlink("/Applications", os.path.join(stage, "dmg", "Applications"))
    dmg = os.path.join(out, "%s-%s.dmg" % (a, version))
    if os.path.exists(dmg):
        os.remove(dmg)
    host.run(
        ["hdiutil", "create"]
        + (["-puppetstrings"] if host.VIEW else ["-quiet"])
        + [
            "-srcfolder",
            os.path.join(stage, "dmg"),
            "-format",
            "UDZO",
            "-volname",
            "%s %s" % (lib.PRODUCT_NAME, version),
            dmg,
        ]
    )
    return dmg
