"""The macos target: <app>.app with the game files and every non-system
dylib inside, relinked to @rpath and signed ad hoc, then a DMG."""

import json
import os
import re
import shutil
import subprocess

from .. import host
from ..host import CliError
from . import DEFAULTS, app, copy_lf, render, templates, write_manifest

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


def brew_keg(src):
    """(formula, version, keg dir) of a dylib's resolved Cellar path, or
    None when it is not a Homebrew file."""
    parts = src.split("/")
    if "Cellar" in parts and len(parts) > parts.index("Cellar") + 3:
        i = parts.index("Cellar")
        return parts[i + 1], parts[i + 2], "/".join(parts[: i + 3])
    return None


# The keg-root names Homebrew keeps a formula's licence under (libepoxy
# has COPYING, sdl3 and openssl LICENSE.txt). NOTICE too: Apache-2.0 4(d)
# asks for a NOTICE file to ship with the binary.
LICENCE_STEMS = ("LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE")

# Homebrew formula (without @N) -> its credits.THIRD_PARTY name, so the
# notice gives the licence and the project the wizard's Credits give.
CREDIT_NAMES = {"sdl3": "SDL", "sdl2": "SDL", "openssl": "OpenSSL", "libepoxy": "libepoxy"}

# Notices a keg does not carry, for code vendored into the formula's
# library whose licence asks for its notice beside binaries: formula
# (without @N) -> files in templates/macos/licences/, staged as they are.
EXTRA_NOTICES = {
    # SDL's YUV converters (yuv420_rgb24_std and friends in libSDL3) are
    # Adrien Descamps' yuv2rgb, BSD-3-Clause. The text is SDL's
    # src/video/yuv2rgb/LICENSE at tag release-3.4.18 (829a65d), verbatim.
    # SDL's other vendored code asks for no notice in binaries: stb_image
    # is taken under its public-domain alternative, HIDAPI's original
    # licence keeps only the source notices, and libm is the system's on
    # macOS (SDL_scalbn and the rest call it).
    "sdl3": ("sdl3-yuv2rgb-BSD-3-Clause.txt",),
}

# Bundled libraries from outside Homebrew, each with the licence file the
# toolkit's LICENSES/ (staged before the wrap) must hold for it.
# librashader.dylib is incr.SHADER_LIBS["macos"], from present.shader.
COVERED = {"librashader.dylib": "MPL-2.0-librashader.txt"}


def keg_licences(keg):
    return sorted(
        n
        for n in os.listdir(keg)
        if n.upper().startswith(LICENCE_STEMS) and os.path.isfile(os.path.join(keg, n))
    )


def stage_dylib_licences(resources, libs, covered=None):
    """Each bundled Homebrew library's licence into Resources/LICENSES/ as
    <formula>-<file>, with its EXTRA_NOTICES, and BUNDLED-LIBRARIES.md
    naming each library, its version and its files. The app ships copies
    of these libraries, and libepoxy (MIT), OpenSSL (Apache-2.0, 4(a)) and
    SDL's yuv2rgb (BSD-3-Clause) require their licence beside every copy.
    covered: dylib name -> the file Resources/LICENSES/ must already hold
    for it (COVERED by default). Anything that cannot be licensed stops
    the build before a file is written, so a new dependency cannot ship
    without one and no licence is overwritten."""
    from .. import credits

    covered = COVERED if covered is None else covered
    dst = os.path.join(resources, "LICENSES")
    by_name = {c["name"]: c for c in credits.THIRD_PARTY}
    kegs, problems = {}, []
    for dylib in libs:
        if dylib["name"] in covered:
            need = covered[dylib["name"]]
            if not os.path.isfile(os.path.join(dst, need)):
                problems.append(
                    "%s: its licence LICENSES/%s is not staged (an older toolkit, "
                    "or the file was renamed)" % (dylib["name"], need)
                )
            continue
        k = brew_keg(dylib["src"])
        files = keg_licences(k[2]) if k and os.path.isdir(k[2]) else []
        if not files:
            problems.append(
                "%s (%s): no licence file. A Homebrew keg should hold a LICENSE "
                "or COPYING file; a library from outside Homebrew needs its "
                "licence in the toolkit's LICENSES/ and a row in "
                "package.macos.COVERED" % (dylib["name"], dylib["src"])
            )
            continue
        kegs.setdefault(k, (files, []))[1].append(dylib["name"])
    formulas = {}
    for formula, version, _ in kegs:
        formulas.setdefault(formula, []).append(version)
    for formula, versions in sorted(formulas.items()):
        if len(versions) > 1:
            problems.append(
                "two %s kegs are bundled (%s): their licences would share a name"
                % (formula, ", ".join(sorted(versions)))
            )
    copies = []  # (src, staged name)
    for (formula, _, keg), (files, _) in sorted(kegs.items()):
        copies += [(os.path.join(keg, n), "%s-%s" % (formula, n)) for n in files]
        copies += [
            (os.path.join(DEFAULTS_MACOS, "licences", n), n)
            for n in EXTRA_NOTICES.get(formula.split("@")[0], ())
        ]
    for _, n in copies:
        if os.path.exists(os.path.join(dst, n)):
            problems.append("LICENSES/%s is already staged (by the toolkit?)" % n)
    if problems:
        raise CliError("cannot license the bundled libraries: " + "; ".join(problems))
    if not kegs:
        return
    os.makedirs(dst, exist_ok=True)
    for src, n in copies:
        copy_lf(src, os.path.join(dst, n))
    lines = [
        "# Bundled macOS libraries\n",
        "\n",
        "`Contents/Frameworks` holds copies of these Homebrew libraries, changed only\n",
        "in their install names (@rpath) and ad hoc signatures. Each one's licence\n",
        "is here, as Homebrew installed it, with the notices of code vendored into it.\n",
        "\n",
    ]
    for (formula, version, _), (files, names) in sorted(kegs.items()):
        c = by_name.get(CREDIT_NAMES.get(formula.split("@")[0]))
        head = "%s %s" % (c["name"] if c else formula, version)
        if c:
            head += " (%s, %s)" % (c["license"], c["url"])
        lines.append("- %s: `%s`\n" % (head, "`, `".join(sorted(names))))
        lines += ["  - `%s-%s`\n" % (formula, n) for n in files]
        lines += ["  - `%s`\n" % n for n in EXTRA_NOTICES.get(formula.split("@")[0], ())]
    _text(os.path.join(dst, "BUNDLED-LIBRARIES.md"), "".join(lines))


def brew_prefix_lib(formula, name):
    if not shutil.which("brew"):
        return None
    r = subprocess.run(
        ["brew", "--prefix", formula], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    p = os.path.join(r.stdout.decode().strip(), "lib", name)
    return p if r.returncode == 0 and os.path.isfile(p) else None


def companions(exe=None):
    """package.dylib_companions (NAME=brew:FORMULA): libraries the game
    loads with dlopen, so otool cannot see them, as NAME=PATH for the plan.
    One whose formula is not installed is left out, as before. With exe,
    also librashader.dylib when the build put it beside the exe
    (present.shader); the runtime finds it in Frameworks/."""
    from . import incr

    out = []
    shader_lib, _ = incr.present_shader_files("macos", exe)
    if shader_lib:
        out.append("%s=%s" % (os.path.basename(shader_lib), shader_lib))
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
    plan = lib.dylib_plan(exe, companions(exe))
    for dylib in plan["libs"]:
        dst = os.path.join(c, "Frameworks", dylib["name"])
        shutil.copy2(dylib["src"], dst)
        os.chmod(dst, 0o755)
        host.run(["install_name_tool", "-id", "@rpath/" + dylib["name"], dst])
    for ch in plan["changes"]:
        f = main_exe if ch["file"] == "@exe" else os.path.join(c, "Frameworks", ch["file"])
        host.run(["install_name_tool", "-change", ch["old"], ch["new"], f])
    host.run(["install_name_tool", "-add_rpath", "@executable_path/../Frameworks", main_exe])
    stage_dylib_licences(os.path.join(c, "Resources"), plan["libs"])
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
    # Ad hoc, without the hardened runtime: that is what lets the APU's
    # DSP JIT (dsp56300, on by default) map executable memory. Signing
    # with --options runtime (notarisation) needs the
    # com.apple.security.cs.allow-jit entitlement, or the JIT cannot run.
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
