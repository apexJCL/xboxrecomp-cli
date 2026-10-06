"""package: a private bundle of the game for windows, steamos or macos.

The plan (setup, generate, build, package) is judged before anything runs;
the payload is staged, checked and wrapped per target (windows.py,
steamos.py, macos.py). Names, folders and the title ID come from game.toml;
the templates and content files from the game's package.templates and
package.content."""

import json
import os
import shutil

from .. import build as build_mod
from .. import cli_dir, doctor, host, pipeline, setup, toolkit
from ..host import CliError
from . import lib as lib_mod

NOTICE_TEXT = (
    "PRIVATE: this bundle contains your own copy of %s and code generated from it.\n"
    "It is for your own machines only. The game is not yours to redistribute:\n"
    "never share, upload or publish this bundle."
)
TARGET_NAMES = {
    "windows": "windows-x86_64",
    "steamos": "steamos-x86_64-proton",
    "macos": "macos-arm64",
}


def g():
    return host.g()


def app():
    return g().package["app"]


def plib():
    """package/lib.py, configured with the game's values."""
    G = g()
    pkg = G.package
    lib_mod.configure(
        pkg["product"],
        G.name,
        G.m["xbe"]["title_id"],
        G.m["pipeline"]["game_name"],
        pkg["app"],
        G.m["data"]["game_files_exclude"],
        G.m["build"]["nonstock_vars"],
    )
    return lib_mod


def templates():
    """The dir the mechanism files are read from (package.templates)."""
    if not g().templates:
        raise CliError(
            "game.toml sets no package.templates (this CLI has no templates of its own yet)"
        )
    return g().templates


def data_paths(target):
    d = g().m["data"]
    return {
        "windows": "  %s\\   (hdd, config, logs)" % d["windows"],
        "steamos": "  %s/   (hdd, config, logs; the program is in versions/)" % d["steamos"],
        "macos": "  %s/   (hdd, config, logs)" % d["macos"],
    }[target]


def make_icon(bdir):
    """The icon set from the XBE's title image (or the generic one) in
    <packaging build dir>/icon: game data, never in the source tree."""
    from . import icon

    out = os.path.join(bdir, "icon")
    xbe = g().xbe if g().package["icon"] == "xbe" else ""
    desc, rebuilt = icon.make_icons(xbe, out, app(), g().root)
    host.say("icon: %s%s" % (desc, "" if rebuilt else " (cached)"))
    return out


def render(template, values, dst, mode=None):
    with open(template) as f:
        text = f.read()
    for k, v in values.items():
        text = text.replace("@%s@" % k, v)
    with open(dst, "w", newline="\n") as f:
        f.write(text)
    if mode:
        os.chmod(dst, mode)


def copy_lf(src, dst, mode=0o644):
    """A text file into a bundle with LF line endings, whatever the checkout
    did (a Windows clone without .gitattributes would have CRLF)."""
    with open(src, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    with open(dst, "wb") as f:
        f.write(data)
    os.chmod(dst, mode)


def ghidra_named():
    return os.path.isfile(g().ghidra_export)


def readme(target, version, lib, dst):
    G = g()
    cat = lib.tree_state(G.root)
    tk = lib.tree_state(toolkit.toolkit_dir())
    values = {
        "NAME": lib.PRODUCT_NAME,
        "VERSION": version,
        "TARGET": TARGET_NAMES[target],
        "SOURCES": "%s %s, toolkit %s"
        % (G.m["pipeline"]["game_name"], cat["commit"][:7], tk["commit"][:7]),
        "DATA_PATHS": data_paths(target),
        "LOG_KEEP": "10",
    }
    render(os.path.join(templates(), "README.txt.in"), values, dst)
    part_values = {"VERSION": version}
    if target == "steamos":
        # The CLI's own install section, unless the game has its own.
        from . import steamos

        part_path = steamos.template("README.part")
        part_values.update(steamos.steamos_values(lib))
    else:
        part_path = os.path.join(G.content, target, "README.part")
    with open(part_path) as f:
        part = f.read()
    for k, v in part_values.items():
        part = part.replace("@%s@" % k, v)
    with open(dst, "a", newline="\n") as f:
        f.write(part.replace("\r\n", "\n"))


def check_staged(root, lib):
    problems = lib.staged_problems(root)
    if problems:
        raise CliError("refusing to package:\n  " + "\n  ".join(problems))


def stock_refusal(target, bdir, bad):
    """The refusal for a non-stock packaging cache, with the exact fix."""
    rel = os.path.relpath(bdir, g().root)
    return (
        "%s is not a stock build: %s\n"
        "  fix: %s package %s --reconfigure    (or delete %s/)\n"
        "  or package it as it is, recorded in the manifest: --allow-debug / --allow-nonstock"
        % (rel, "; ".join(bad), host.cli_name(), target, rel)
    )


def build_target(target):
    return "macos" if target == "macos" else "windows"


def stage_payload(target, a, lib):
    """Build, check, and stage the payload common to every target.
    Returns (stage dir, payload dir, version, overrides, exe)."""
    G = g()
    host.mark("build")
    if target == "macos" and host.host_os() != "macos":
        raise CliError(
            "package macos needs a macOS host (Apple clang, codesign, hdiutil); "
            "this host can package: windows, steamos"
        )
    host.require(G.xbe, pipeline.dump_hint())
    with open(G.xbe, "rb") as f:
        try:
            tid = lib.xbe_title_id(f.read(0x10000))
        except ValueError as e:
            raise CliError("%s: %s" % (G.xbe, e)) from e
    if tid != lib.TITLE_ID:
        raise CliError(
            "%s: title ID 0x%08X is not %s (0x%08X)" % (G.xbe, tid, lib.PRODUCT_NAME, lib.TITLE_ID)
        )
    sha, known = doctor.dump_state()
    a.dump_unknown = not known
    if not known:
        host.say("WARNING: %s; packaging it anyway" % doctor.unknown_dump_note(sha))
    btarget = build_target(target)
    bdir = build_mod.pkg_build_dir(btarget)
    a.icon_dir = make_icon(bdir)
    # The taskbar shows the icon of the window's process: the game's exe.
    icon_var = G.m["build"]["icon_var"]
    icon_args = (
        ["-D%s=%s" % (icon_var, os.path.join(a.icon_dir, app() + ".ico"))]
        if btarget == "windows" and icon_var
        else []
    )
    if a.no_build:
        host.refuse_while_regenerating()
        exe = G.exe(bdir, btarget)
        if not os.path.isfile(exe):
            raise CliError(
                "--no-build: no %s yet; run '%s package %s' without it first"
                % (os.path.relpath(exe, G.root), host.cli_name(), target)
            )
    else:
        host.step("build %s (%s)" % (btarget, os.path.basename(bdir)))
        exe = build_mod.build(
            btarget, icon_args, a.system_tools, bdir=bdir, stock=True, reconfigure=a.reconfigure
        )
    if not os.listdir(G.gen):
        raise CliError("%s/ is empty: run '%s recomp'" % (G.rel(G.gen), host.prog()))
    cache = os.path.join(bdir, "CMakeCache.txt")
    debug, nonstock = lib.cache_problems(lib.read_cache(cache))
    bad = ([] if a.allow_debug else debug) + ([] if a.allow_nonstock else nonstock)
    if bad:
        raise CliError(stock_refusal(target, bdir, bad))
    overrides = (debug if a.allow_debug else []) + (nonstock if a.allow_nonstock else [])
    version = lib.compute_version(G.root, toolkit.toolkit_dir(), G.gen, exe, cli_dir.cli_dir())
    host.mark("package")
    host.step("stage the files")
    host.say("version: %s" % version)
    stage = os.path.join(a.out, ".stage-%s" % target)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    payload = os.path.join(stage, "%s-%s-%s" % (app(), version, target))
    os.makedirs(payload)
    return stage, payload, version, overrides, exe


def write_common(payload, target, version, lib):
    G = g()
    fam = "macos" if target == "macos" else "windows"
    copy_lf(
        os.path.join(G.content, "launch.env.default." + fam),
        os.path.join(payload, "launch.env.default"),
    )
    copy_lf(
        os.path.join(G.content, "enhance.toml.default"),
        os.path.join(payload, "enhance.toml.default"),
    )
    for n in ("LICENSE", "NOTICE"):
        copy_lf(os.path.join(G.root, n), os.path.join(payload, n))
    readme(target, version, lib, os.path.join(payload, "README.txt"))
    n = lib.stage_game_files(G.game_files, os.path.join(payload, "game_files"))
    host.say("game files: %d entries staged" % n)


def write_manifest(root, target, version, overrides, lib, extra=None, dump_unknown=False):
    G = g()
    cat, tk = lib.tree_state(G.root), lib.tree_state(toolkit.toolkit_dir())
    cli = lib.tree_state(cli_dir.cli_dir())
    gsha, gn = lib.gen_digest(G.gen)
    btarget = build_target(target)
    cache = lib.read_cache(os.path.join(build_mod.pkg_build_dir(btarget), "CMakeCache.txt"))
    ex = {"host": {"os": host.host_os(), "arch": host.host_arch()}, "ghidra_names": ghidra_named()}
    if dump_unknown:
        # A dump game.toml does not list (xbe.sha256): it builds and plays,
        # but no golden reference was recorded on it.
        ex["xbe"] = "unknown"
    ex.update(extra or {})
    man = lib.build_manifest(
        root,
        TARGET_NAMES[target],
        version,
        cat,
        tk,
        gsha,
        gn,
        cache,
        lib.compiler_line(cache, build_mod.pkg_build_dir(btarget)),
        overrides,
        ex,
        cli=cli,
    )
    text = json.dumps(man, indent=2) + "\n"
    leaks = lib.private_leaks(text)
    if leaks:
        raise CliError("the manifest would carry private data: " + ", ".join(leaks))
    with open(os.path.join(root, "manifest.json"), "w", newline="\n") as f:
        f.write(text)
    with open(os.path.join(root, "SHA256SUMS"), "w", newline="\n") as f:
        f.write(lib.sums_text(man))
    return man


def package_plan(a):
    """[(step, why)] for this package run, before anything runs. Generation
    is judged again after setup (the key needs the toolkit)."""
    plan = []
    needs = [] if a.no_setup else setup.setup_needs(a.target, a.system_tools)
    if needs:
        plan.append(("setup", ", ".join(needs)))
    if not a.no_build:
        if "no toolkit" in needs:
            plan.append(("generate", "checked after setup"))
        else:
            why = pipeline.gen_stale_reasons()
            if why:
                plan.append(("generate", ", ".join(why)))
        plan.append(("build", os.path.basename(build_mod.pkg_build_dir(build_target(a.target)))))
    plan.append(("package", a.target))
    return plan


def plan_line(plan):
    return "plan: " + ", ".join("%s (%s)" % (n, w) if w else n for n, w in plan)


def run_plan_prefix(a, plan):
    """The setup and generate steps of the plan; build and package follow."""
    names = [n for n, _ in plan]
    if "setup" in names:
        host.mark("setup")
        setup.run_setup(need_uv=False)
        still = setup.setup_needs(a.target, a.system_tools)
        if still:
            raise CliError(
                "setup finished but still: %s (run '%s doctor')"
                % (", ".join(still), host.cli_name())
            )
    if "generate" in names:
        host.mark("generate")
        why = pipeline.gen_stale_reasons()
        if why:
            host.say("generate: %s" % ", ".join(why))
            pipeline.reset_stage_extras()
            pipeline.stage_parse()
            pipeline.stage_disasm()
            pipeline.stage_funcid()
            pipeline.stage_abi()
            pipeline.maybe_names()
            pipeline.stage_recomp()
        else:
            host.say("gen/ is current")


def cmd_package(a):
    prog = host.progress_lib()
    host.VIEW = prog.View(prog.choose_mode(a.plain or a.verbose), logs=g().logs)
    host.VIEW.verbose = a.verbose
    host.VIEW.plan = []
    try:
        package(a)
    finally:
        host.VIEW.close()
        prog.prune_logs(g().logs)
        host.VIEW = None


def package(a):
    from . import macos, steamos, windows

    G = g()
    if a.target not in G.package["targets"]:
        raise CliError("game.toml's package.targets has no %s" % a.target)
    lib = plib()
    a.out = os.path.abspath(a.out)
    blockers = setup.host_blockers(a.target)
    if blockers:
        raise CliError("cannot package %s on this host:\n  %s" % (a.target, "\n  ".join(blockers)))
    toolkit.exclude_pin_mark(toolkit.toolkit_dir())
    plan = package_plan(a)
    host.VIEW.plan = [n for n, _ in plan]
    host.say(plan_line(plan))
    run_plan_prefix(a, plan)
    os.makedirs(a.out, exist_ok=True)
    stage, payload, version, overrides, exe = stage_payload(a.target, a, lib)
    write_common(payload, a.target, version, lib)
    unknown = getattr(a, "dump_unknown", False)
    if a.target == "macos":
        check_staged(payload, lib)
        host.step("app and dmg")
        result = macos.wrap_macos(
            stage,
            payload,
            version,
            exe,
            a.out,
            overrides,
            lib,
            os.path.join(a.icon_dir, app() + ".icns"),
            dump_unknown=unknown,
        )
    else:
        exe_name = G.exe_name + ".exe"
        shutil.copy2(exe, os.path.join(payload, exe_name))
        pdb = os.path.join(os.path.dirname(exe), G.exe_name + ".pdb")
        if os.path.isfile(pdb):
            shutil.copy2(pdb, os.path.join(payload, G.exe_name + ".pdb"))
        if a.target == "steamos":
            steamos.stage_steamos(payload, lib, a.icon_dir)
        else:
            windows.build_launcher_exe(
                os.path.join(payload, app() + ".exe"),
                os.path.join(a.icon_dir, app() + ".ico"),
                stage,
                lib.PRODUCT_NAME,
            )
        check_staged(payload, lib)
        write_manifest(payload, a.target, version, overrides, lib, dump_unknown=unknown)
        if a.target == "steamos":
            host.step("tar")
            result = steamos.wrap_steamos(payload, version, exe, a.out)
        else:
            host.step("installer")
            result = windows.wrap_windows(stage, payload, version, a.out, lib, a.icon_dir)
            if a.archive:
                result_zip = windows.archive(result)
                host.say("archive: %s" % result_zip)
    if not a.keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    host.VIEW.end(True)
    if "-dirty" in version:
        host.say("WARNING: built from uncommitted changes (%s)" % version)
    host.say()
    host.say("bundle: %s" % result)
    host.say()
    host.say(NOTICE_TEXT % lib.PRODUCT_NAME)
