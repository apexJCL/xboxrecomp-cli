"""package: a private bundle of the game for windows, steamos or macos.

The plan (setup, generate, build, package) is judged before anything runs;
the payload is staged, checked and wrapped per target (windows.py,
steamos.py, macos.py). Names, folders and the title ID come from game.toml;
the content files from the game's package.content. The mechanism
templates (installer, launchers, README) are this CLI's own (templates/);
a game that needs its own copy of one puts it in package.templates, and
that copy wins (template())."""

import json
import os
import re
import shutil
import tomllib

from .. import build as build_mod
from .. import (
    buildstate,
    cli_dir,
    fingerprint,
    gitpin,
    host,
    manifest,
    pipeline,
    setup,
    toolkit,
)
from ..host import CliError
from . import incr, patch, tweaks
from . import lib as lib_mod

NOTICE_TEXT = (
    "PRIVATE: this bundle contains your own copy of %s and code generated from it.\n"
    "It is for your own machines only. The game is not yours to redistribute:\n"
    "never share, upload or publish this bundle."
)
# The bundle README's paragraph and the terminal note for a game whose
# defaults file ships the sleeping frame wait: the console spins there, so
# the player is told how to get the original timing back.
PACING_README = (
    'Frame pacing defaults to "sleep": the game\'s frame wait sleeps until the\n'
    'next vblank, which saves CPU and battery. "spin" is the console-original\n'
    "timing, where that wait holds a CPU core as it did on the Xbox. To switch,\n"
    "put\n"
    "\n"
    "  [present]\n"
    '  pacing = "spin"\n'
    "\n"
    "in config/config.toml, or RECOMP_PRESENT_PACING=spin in config/launch.env.\n"
    "\n"
)
PACING_NOTE = (
    'note: frame pacing defaults to "sleep" (the game\'s frame wait sleeps until the\n'
    'next vblank: less CPU and battery). "spin" is the console-original timing, the\n'
    'wait holding a CPU core as on the Xbox: pacing = "spin" under [present] in\n'
    "config/config.toml, or RECOMP_PRESENT_PACING=spin in config/launch.env."
)
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TARGET_NAMES = {
    "windows": "windows-x86_64",
    "steamos": "steamos-x86_64-proton",
    "macos": "macos-arm64",
}


def g():
    return host.g()


def fixed_fields(target, lib):
    """The new manifest's fields known before anything is built: what
    patch.problem_codes(early=True) judges a base by."""
    out = {"product": lib.PRODUCT, "target": TARGET_NAMES[target], "data_layout": lib.DATA_LAYOUT}
    if target == "windows":
        from . import windows

        out["root"] = {"stub_abi": windows.STUB_ABI}
    return out


def early_base_check(a, base, lib):
    """--patch-from's rules on the manifests' fixed fields, before anything
    is built (D7): another game, target, data layout or launcher stub is
    refused now; the version, the game files and the half-size rule wait
    for the built manifest."""
    spec = getattr(a, "patch_from", None)
    if not spec or base is None:
        return
    why = patch.problem_codes(base, fixed_fields(a.target, lib), a.target, early=True)
    if why:
        raise host.Refusal(
            "--patch-from %s (%s): %s; make the full bundle with --full"
            % (spec, base.version or "?", "; ".join(m for _c, m in why)),
            why[0][0],
        )


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
        G.m["build"]["stock_cmake"],
    )
    return lib_mod


def templates():
    """The game's own templates dir (package.templates): the macos templates
    are still only there."""
    if not g().templates:
        raise CliError("game.toml sets no package.templates (the macos target needs them)")
    return g().templates


def template(target, name, content=False):
    """<target>/<name> ("" for the top level): the game's copy in
    package.templates (or, with content, package.content) wins over this
    CLI's."""
    G = g()
    roots = [G.templates] if G.templates else []
    if content and G.content:
        roots.append(G.content)
    for r in roots:
        p = os.path.join(r, target, name)
        if os.path.isfile(p):
            return p
    return os.path.join(DEFAULTS, target, name)


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


def pacing_sleeps(defaults):
    """Whether the staged defaults file sets [present] pacing = "sleep".
    That file is what the bundle's player gets, so the note needs no
    toolkit knowledge: a game that ships "spin", or omits the key, gets
    none. A file tomllib cannot read gets one warning and no note; the
    runtime reports its own defaults file."""
    try:
        with open(defaults, "rb") as f:
            doc = tomllib.load(f)
    except FileNotFoundError:
        return False
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        host.say("WARNING: %s: %s; no frame pacing note" % (os.path.basename(defaults), e))
        return False
    present = doc.get("present")
    return isinstance(present, dict) and present.get("pacing") == "sleep"


def reads_config(cache=None):
    """Whether the bundle's game reads config.default.toml and config.toml:
    the toolkit it builds on has the enhancements layer, the reader
    (upstream xboxrecomp has none). A developer's own checkout (no pin
    mark: theirs, or an XBOXRECOMP_DIR) is what gets built, so its working
    tree answers, offline. A clone setup made at the pin, or none yet (the
    plan's setup makes one), is the pin's: the wizard's own check, which
    reads the clone's history and fetches only a commit it lacks. A game
    whose stock build leaves the layer off (manifest.builds_enhance) reads
    no config file whatever the toolkit has: cache, the packaging build's
    CMakeCache values once it is built (lib.enhance_on, which falls back to
    stock_cmake), else stock_cmake itself (the configure passes it)."""
    on = plib().enhance_on(cache) if cache is not None else manifest.builds_enhance(g().m)
    if not on:
        return False
    tk = toolkit.toolkit_dir()
    marked = any(
        os.path.isfile(os.path.join(tk, m)) for m in (toolkit.PIN_MARK, toolkit.legacy_pin_mark())
    )
    if os.path.isdir(tk) and not marked:
        return toolkit.features(tk)["enhance"]
    return toolkit.pin_has_enhance(g().m["toolkit"])


# The capability blocks of the README frame and parts: a line holding
# only one marker, each block closed by @endif@, no nesting.
CAP_IF = re.compile(r"@if (!?)([a-z_]+)@")
CAP_END = "@endif@"
CAPS = ("enhance",)


def cap_blocks(text, caps, where):
    """text with each `@if CAP@` block kept when CAP is in caps (an
    `@if !CAP@` block when it is not) and dropped otherwise, the marker
    lines always dropped. Text without markers comes back as it was."""
    out, keep, opened = [], True, None
    for line in text.splitlines(keepends=True):
        mark = line.strip()
        m = CAP_IF.fullmatch(mark)
        if not m and mark != CAP_END and mark.startswith(("@if", "@end")):
            # A mistyped marker would otherwise reach the player's README.
            raise CliError(
                "%s: %r is not a marker (@if CAP@, @if !CAP@ or %s, alone on the line)"
                % (where, mark, CAP_END)
            )
        if m:
            if opened is not None:
                raise CliError("%s: %s inside the %s block" % (where, mark, opened))
            if m.group(2) not in CAPS:
                raise CliError(
                    "%s: %s names no capability (known: %s)" % (where, mark, ", ".join(CAPS))
                )
            opened = mark
            keep = (m.group(2) in caps) != bool(m.group(1))
        elif mark == CAP_END:
            if opened is None:
                raise CliError("%s: %s with no @if CAP@ open" % (where, CAP_END))
            opened, keep = None, True
        elif keep:
            out.append(line)
    if opened is not None:
        raise CliError("%s: %s is not closed by %s" % (where, opened, CAP_END))
    return "".join(out)


def read_template(path, caps):
    with open(path) as f:
        return cap_blocks(f.read(), caps, path)


def readme(target, version, lib, dst, enhance=True):
    """README.txt at `dst`, beside the staged config.default.toml. Returns
    whether the frame pacing paragraph went in (package() then prints the
    terminal note). enhance: reads_config()'s answer; without it the
    `@if enhance@` blocks of the frame, the intro and the part go."""
    G = g()
    caps = {"enhance"} if enhance else set()
    # The paragraph names config.toml: only for a game that reads it.
    pacing = enhance and pacing_sleeps(os.path.join(os.path.dirname(dst), "config.default.toml"))
    cat = lib.tree_state(G.root)
    tk = lib.tree_state(toolkit.toolkit_dir())
    values = {
        "NAME": lib.PRODUCT_NAME,
        "PRODUCT": lib.PRODUCT,
        "VERSION": version,
        "TARGET": TARGET_NAMES[target],
        "SOURCES": "%s %s, toolkit %s"
        % (G.m["pipeline"]["game_name"], cat["commit"][:7], tk["commit"][:7]),
        "DATA_PATHS": data_paths(target),
        "LOG_KEEP": "10",
        "OPTION_NOTES": PACING_README if pacing else "",
    }
    # The PRIVATE and "built by" paragraphs: the game's README.intro (in
    # package.content) when it words them its own way, else the CLI's.
    intro = read_template(template("", "README.intro", content=True), caps)
    for k, v in values.items():
        intro = intro.replace("@%s@" % k, v)
    values["INTRO"] = intro
    text = read_template(template("", "README.txt.in"), caps)
    # A game's own README.txt.in from before the placeholder gets no
    # paragraph, so no note either: the note promises the README says it.
    pacing = pacing and "@OPTION_NOTES@" in text
    for k, v in values.items():
        text = text.replace("@%s@" % k, v)
    with open(dst, "w", newline="\n") as f:
        f.write(text)
    part_values = {"VERSION": version}
    if target == "steamos":
        # The CLI's own install section, unless the game has its own.
        from . import steamos

        part_path = template("steamos", "README.part", content=True)
        part_values.update(steamos.steamos_values(lib))
    else:
        part_path = os.path.join(G.content, target, "README.part")
    part = read_template(part_path, caps)
    for k, v in part_values.items():
        part = part.replace("@%s@" % k, v)
    with open(dst, "a", newline="\n") as f:
        f.write(part.replace("\r\n", "\n"))
    return pacing


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


def exe_icon_args(btarget, icon_dir):
    """The taskbar shows the icon of the window's process: the game's exe."""
    icon_var = g().m["build"]["icon_var"]
    if btarget == "windows" and icon_var:
        return ["-D%s=%s" % (icon_var, os.path.join(icon_dir, app() + ".ico"))]
    return []


def config_layer(enhance, lib, target):
    """reads_config()'s answer, corrected by the build's own cache once one
    exists: a layer the configure left off reads no config file. Returns
    (enhance, cache)."""
    cache_path = os.path.join(build_mod.pkg_build_dir(build_target(target)), "CMakeCache.txt")
    cache = lib.read_cache(cache_path) if os.path.isfile(cache_path) else None
    if enhance and cache is not None and not lib.enhance_on(cache):
        return False, cache
    return enhance, cache


def stage_payload(target, a, lib, seed=None, hud_pack=None):
    """Build, check, and stage the payload common to every target. seed
    and hud_pack, when given, put the -o options digest in the version.
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
    fingerprint.warn(fingerprint.current(), "; packaging it anyway")
    btarget = build_target(target)
    bdir = build_mod.pkg_build_dir(btarget)
    st = a.state
    a.icon_dir = make_icon(bdir)
    icon_key = incr.record_icon(st, a.icon_dir)
    icon_args = exe_icon_args(btarget, a.icon_dir)
    cstage = incr.compile_stage(btarget)
    if a.no_build:
        host.refuse_while_regenerating()
        exe = G.exe(bdir, btarget)
        if not os.path.isfile(exe):
            raise CliError(
                "--no-build: no %s yet; run '%s package %s' without it first"
                % (os.path.relpath(exe, G.root), host.cli_name(), target)
            )
        # The exe as it is is the compile's output: the stages below chain
        # on its bytes.
        inputs = {"no_build": st.output_record(exe)["sha256"]}
        st.record(cstage, buildstate.stage_key(inputs), inputs, [exe])
    else:
        compile_stage(btarget, bdir, icon_args, icon_key, a, lib)
        exe = G.exe(bdir, btarget)
    if not os.listdir(G.gen):
        raise CliError("%s/ is empty: run '%s recomp'" % (G.rel(G.gen), host.prog()))
    cache = os.path.join(bdir, "CMakeCache.txt")
    vals = lib.read_cache(cache)
    debug, nonstock = lib.cache_problems(vals)
    bad = ([] if a.allow_debug else debug) + ([] if a.allow_nonstock else nonstock)
    if bad:
        raise CliError(stock_refusal(target, bdir, bad))
    overrides = (debug if a.allow_debug else []) + (nonstock if a.allow_nonstock else [])
    hud_sha = host.sha256_path(hud_pack["path"]) if hud_pack else None
    options = lib.options_digest(vals, seed, hud_sha)
    try:
        version = lib.compute_version(
            G.root, toolkit.toolkit_dir(), G.gen, exe, cli_dir.cli_dir(), options
        )
    except ValueError as e:
        raise CliError(str(e)) from e
    host.mark("package")
    host.step("stage the files")
    host.say("version: %s" % version)
    # One staging tree per target, kept between runs and updated in place
    # (D11): its game files are clones or hard links, and a run that
    # changed nothing reuses it whole.
    stage = os.path.join(a.out, ".stage-%s" % target)
    payload = os.path.join(stage, incr.PAYLOAD)
    os.makedirs(payload, exist_ok=True)
    return stage, payload, version, overrides, exe


def compile_stage(btarget, bdir, icon_args, icon_key, a, lib):
    """The build, skipped when its key (the game and toolkit trees, the
    toolchain, the build env and -D list, gen/ and the icon) is the one the
    exe was built with and the exe is unchanged. A toolchain that moved
    starts the CMake cache afresh: a cache keeps the old compiler's paths."""
    G = g()
    st = a.state
    cstage = incr.compile_stage(btarget)
    inputs = incr.compile_inputs(btarget, a, lib, icon_args, bdir)
    up = {"generate": st.key_of("generate"), "icon": icon_key}
    key = buildstate.stage_key(inputs, up)
    if incr.toolchain_moved(st, cstage, inputs):
        host.say("build: the toolchain changed, configuring afresh")
        build_mod.start_afresh(bdir)
    why = ["--reconfigure"] if a.reconfigure else st.check(cstage, key, inputs, up)
    if not why and os.path.isfile(G.exe(bdir, btarget)):
        host.say("build: %s is current" % G.rel(G.exe(bdir, btarget)))
        return
    host.step("build %s (%s)" % (btarget, os.path.basename(bdir)))
    host.say("build: %s" % ", ".join(why))
    st.begin(cstage)
    build_mod.build(
        btarget,
        icon_args,
        a.system_tools,
        bdir=bdir,
        stock=True,
        reconfigure=a.reconfigure,
        # Its plan has regenerated a stale gen/ already, or refused.
        stale_ok=True,
    )
    st.record(cstage, key, inputs, incr.compile_outputs(btarget, bdir), upstream=up)


def hud_pack_path(arg):
    """The pack `--hud-pack [DIR]` names (the CLI's default output when DIR
    is omitted), checked: this runs before anything is built."""
    from ..enhance import pack as pack_mod

    d = arg or os.path.join(g().dist, "enhance", "hud")
    path = os.path.join(os.path.abspath(d), "hud.pack")
    if not os.path.isfile(path):
        raise CliError(
            "--hud-pack: no %s; make it with '%s enhance hud' first" % (path, host.cli_name())
        )
    try:
        count, scale = pack_mod.read_header(path)
    except (OSError, ValueError) as e:
        raise CliError("--hud-pack: %s: %s" % (path, e)) from e
    return {"path": path, "entries": count, "scale": scale}


# The pack of this package run (hud_pack_path's result plus its sha256),
# recorded in the manifest; None without --hud-pack.
HUD_PACK = None
# write_manifest's sha256 source for game_files/ (buildstate.HashCache),
# set by package() for its run.
HASHER = None


def has_textures_table(path):
    """Whether the TOML file at `path` already opens a [textures] table
    outside a comment. --hud-pack appends one, and a second header makes
    the layer reject the whole defaults file."""
    with open(path, encoding="utf-8") as f:
        return any(re.match(r"\s*\[\s*textures\s*\]", line) for line in f)


def check_content(hud_pack=None, enhance=True):
    """Refuse, before anything is built, a game whose packaging content
    still has the old defaults file: the runtime reads config.default.toml
    beside the executable, and a bundle without it would ship no
    defaults. With --hud-pack, also refuse defaults that already have a
    [textures] table, which the appended key would duplicate. Without the
    enhancements layer (enhance false) nothing reads the defaults, so
    neither applies."""
    if not enhance:
        return
    G = g()
    old = os.path.join(G.content, "enhance.toml.default")
    new = os.path.join(G.content, "config.default.toml")
    if os.path.isfile(old) and not os.path.isfile(new):
        raise CliError(
            "%s: the defaults file is config.default.toml now; rename it "
            "(git mv %s %s)" % (G.rel(old), G.rel(old), G.rel(new))
        )
    if not os.path.isfile(new):
        raise CliError(
            "%s is missing: the game's build has the enhancements layer, which reads "
            "its defaults from it (or leave -DXBOXRECOMP_ENHANCE=ON out of "
            "build.stock_cmake for a game without the layer)" % G.rel(new)
        )
    if hud_pack and os.path.isfile(new) and has_textures_table(new):
        raise CliError(
            "--hud-pack: %s already has a [textures] table; comment it out "
            "(--hud-pack writes the pack key there)" % G.rel(new)
        )


def stage_hud_pack(payload, target, info):
    """The pack into the payload's enhance/ and the key into the staged
    config.default.toml. On macOS the executable sits in Contents/MacOS
    and the payload in Contents/Resources, so the path is relative to
    that; elsewhere the payload is the executable's directory. The game
    reads that file under the player's config.toml, so the key reaches
    an upgrade as well as a first install."""
    d = os.path.join(payload, "enhance")
    os.makedirs(d, exist_ok=True)
    shutil.copy2(info["path"], os.path.join(d, "hud.pack"))
    rel = "../Resources/enhance/hud.pack" if target == "macos" else "enhance/hud.pack"
    sha = host.sha256_path(info["path"])
    defaults = os.path.join(payload, "config.default.toml")
    if os.path.isfile(defaults) and has_textures_table(defaults):
        raise CliError("--hud-pack: the staged config.default.toml already has a [textures] table")
    with open(defaults, "a", newline="\n") as f:
        f.write(
            "\n[textures]\n"
            "# Your HUD texture pack (%d entries at %dx), made by enhance hud from\n"
            "# your own game files. It applies to an upgrade too; for the stock HUD\n"
            '# put pack = "none" under [textures] in config/config.toml.\n'
            'pack = "%s"\n' % (info["entries"], info["scale"], rel)
        )
    if info.get("base"):
        # A kept pack: the base's record, so the manifest says the same.
        return dict(info["base"], sha256=sha, path=rel)
    rec = {"sha256": sha, "scale": info["scale"], "entries": info["entries"], "path": rel}
    made = tweaks.made_with(info["path"], sha)
    if made:
        rec["made_with"] = made
    return rec


SEED_NAME = "config.seed.toml"
NO_CONFIG_NOTE = (
    "config: the toolkit at %s has no enhancements layer, so the game reads no "
    "config file: no config.default.toml staged, and README.txt says nothing of "
    "config.toml"
)
NO_LAYER_NOTE = (
    "config: game.toml's build.stock_cmake does not turn the enhancements layer on "
    "(-DXBOXRECOMP_ENHANCE=ON), so the game reads no config file: no "
    "config.default.toml staged, and README.txt says nothing of config.toml"
)


def no_config_note(cache=None):
    """Why the game reads no config file: the game's build leaves the layer
    off, or the toolkit has none."""
    on = plib().enhance_on(cache) if cache is not None else manifest.builds_enhance(g().m)
    if not on:
        return NO_LAYER_NOTE
    return NO_CONFIG_NOTE % gitpin.describe(g().m["toolkit"])


def refuse_no_config(hud_pack, seed_path, layer_off=False):
    """--hud-pack and --seed-config, refused before anything is built when
    the game reads no config file: both only reach the game through one
    (the pack's key in config.default.toml, the seed as config.toml)."""
    flags = [f for f, on in (("--hud-pack", hud_pack), ("--seed-config", seed_path)) if on]
    if flags and (layer_off or not manifest.builds_enhance(g().m)):
        raise CliError(
            "%s: game.toml's build.stock_cmake does not turn the enhancements layer on "
            "(-DXBOXRECOMP_ENHANCE=ON), which reads config.toml; the game would never read %s"
            % (" and ".join(flags), "them" if len(flags) > 1 else "it")
        )
    if flags:
        raise CliError(
            "%s: the toolkit at %s has no enhancements layer (src/enhance/), which reads "
            "config.toml; the game would never read %s"
            % (
                " and ".join(flags),
                gitpin.describe(g().m["toolkit"]),
                "them" if len(flags) > 1 else "it",
            )
        )


def _flat_keys(doc, prefix=""):
    """Every dotted key of a TOML document's values (a table's own keys,
    not the table)."""
    out = []
    for k, v in doc.items():
        name = prefix + k
        if isinstance(v, dict):
            out += _flat_keys(v, name + ".")
        else:
            out.append(name)
    return out


def check_seed(path):
    """--seed-config FILE, checked before anything is built: TOML that
    tomllib reads, every key one the game's config.default.toml has (the
    wizard writes only the keys its options name, which `wizard config`
    checked against that file). Returns the text, LF only."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        seed = tomllib.loads(raw.decode("utf-8"))
    except OSError as e:
        raise CliError("--seed-config: %s: %s" % (path, e.strerror)) from e
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CliError("--seed-config: %s: not TOML (%s)" % (path, e)) from e
    with open(os.path.join(g().content, "config.default.toml"), "rb") as f:
        defaults = tomllib.load(f)
    known = set(_flat_keys(defaults))
    bad = [k for k in _flat_keys(seed) if k not in known]
    if bad:
        raise CliError(
            "--seed-config: %s names keys config.default.toml does not have: %s"
            % (path, ", ".join(bad))
        )
    return raw.decode("utf-8").replace("\r\n", "\n")


def write_common(
    payload, target, version, lib, hud_pack=None, seed=None, enhance=True, game_files=True
):
    """The files every target shares. Returns readme()'s answer: whether
    the README has the frame pacing paragraph. seed: --seed-config's text,
    staged as config.seed.toml beside config.default.toml; each installer
    copies it to the data folder's config/config.toml when that file does
    not exist. enhance: reads_config()'s answer; without it the game
    reads no config file, so config.default.toml is not staged (package()
    has refused a seed and a hud pack already)."""
    G = g()
    fam = "macos" if target == "macos" else "windows"
    copy_lf(
        os.path.join(G.content, "launch.env.default." + fam),
        os.path.join(payload, "launch.env.default"),
    )
    stamp_launch_env(os.path.join(payload, "launch.env.default"))
    # Read by the game beside its executable (Contents/Resources on macOS,
    # where the payload goes), never copied to the player's config/.
    if enhance:
        copy_lf(
            os.path.join(G.content, "config.default.toml"),
            os.path.join(payload, "config.default.toml"),
        )
    if seed is not None:
        with open(os.path.join(payload, SEED_NAME), "w", newline="\n") as f:
            f.write(seed)
    global HUD_PACK
    HUD_PACK = stage_hud_pack(payload, target, hud_pack) if hud_pack else None
    # LICENSE always; NOTICE when the game has one (third-party notices).
    for n in ("LICENSE", "NOTICE"):
        if n == "LICENSE" or os.path.isfile(os.path.join(G.root, n)):
            copy_lf(os.path.join(G.root, n), os.path.join(payload, n))
    pacing = readme(target, version, lib, os.path.join(payload, "README.txt"), enhance)
    if game_files:
        n = lib.stage_game_files(G.game_files, os.path.join(payload, "game_files"))
        host.say("game files: %d entries staged" % n)
    return pacing


def stamp_launch_env(path):
    """End the staged launch.env.default with [package] stamp_env set to
    the dump's verdict: the game prints it at boot, so a log names the XBE
    its build was made from. Nothing when stamp_env is empty."""
    line = fingerprint.stamp_line()
    if not line:
        return
    # A template without a final newline would join our comment to its
    # last line.
    with open(path, "rb") as f:
        ends = True
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            ends = f.read(1) == b"\n"
    with open(path, "a", newline="\n") as f:
        if not ends:
            f.write("\n")
        f.write("# The XBE this build was made from (xbr fingerprint).\n")
        f.write(line + "\n")


def write_manifest(root, target, version, overrides, lib, extra=None, hasher=None):
    G = g()
    cat, tk = lib.tree_state(G.root), lib.tree_state(toolkit.toolkit_dir())
    cli = lib.tree_state(cli_dir.cli_dir())
    gsha, gn = lib.gen_digest(G.gen)
    btarget = build_target(target)
    cache = lib.read_cache(os.path.join(build_mod.pkg_build_dir(btarget), "CMakeCache.txt"))
    ex = {"host": {"os": host.host_os(), "arch": host.host_arch()}, "ghidra_names": ghidra_named()}
    v = fingerprint.current()
    if v is not None:
        # The dump this build was made from and its verdict: a modded or
        # unknown one builds and plays, but no golden was recorded on it.
        ex["xbe"] = fingerprint.record(v)
    if HUD_PACK:
        ex["enhance"] = {"hud_pack": dict(HUD_PACK)}
    if os.path.isfile(os.path.join(root, SEED_NAME)):
        ex["seed_config"] = True
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
        source=lib.source_record(cat, lib.read_mark(G.root), G.m.get("wizard", {}).get("repo", "")),
        built=lib.build_time((G.root, toolkit.toolkit_dir())),
        hasher=hasher if hasher is not None else HASHER,
    )
    # What game files the build was made from: a patch to a device built
    # from another tree is refused (patch.problem_codes).
    man["game_tree"] = patch.tree_of(man["files"])
    text = json.dumps(man, indent=2) + "\n"
    leaks = lib.private_leaks(text)
    if leaks:
        raise CliError("the manifest would carry private data: " + ", ".join(leaks))
    with open(os.path.join(root, "manifest.json"), "w", newline="\n") as f:
        f.write(text)
    with open(os.path.join(root, "SHA256SUMS"), "w", newline="\n") as f:
        f.write(lib.sums_text(man))
    return man


def package_plan(a, imp=None):
    """[(step, why)] for this package run, before anything runs. Generation
    is judged again after setup (the key needs the toolkit) and after an
    import (the key hashes the XBE). imp: --dump's prepared import."""
    importing = imp is not None and imp.writes
    plan = [("import", imp.plan_why())] if importing else []
    needs = [] if a.no_setup else setup.setup_needs(a.target, a.system_tools)
    if needs:
        plan.append(("setup", ", ".join(needs)))
    if not a.no_build:
        if "no toolkit" in needs:
            plan.append(("generate", "checked after setup"))
        elif importing:
            plan.append(("generate", "checked after import"))
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


def stage_files(a, stage, payload, version, exe, lib, hud_pack, seed, enhance):
    """The payload, in place: skipped whole when its key (the exe, the
    icon, the HUD pack, the game files, the content and the CLI's
    templates) is the one it was staged with and nothing in it moved;
    otherwise the program files are made in stage/.next and synced in
    (equal bytes left as they are), and game_files/ synced by size and
    mtime. Returns write_common's answer (the pacing paragraph)."""
    from . import steamos, windows

    global HUD_PACK
    G = g()
    st = a.state
    target = a.target
    sname = incr.stage_name(target)
    cstage = incr.compile_stage(build_target(target))
    exe_sha = incr.output_sha(st, cstage, exe) or buildstate.sha256_file(exe)
    hud_sha = host.sha256_path(hud_pack["path"]) if hud_pack else ""
    inputs = incr.stage_inputs(target, version, enhance, seed, hud_sha, lib, exe_sha)
    up = {"compile": st.key_of(cstage), "icon": st.key_of("icon"), "import": st.key_of("import")}
    key = buildstate.stage_key(inputs, up)
    why = st.check(sname, key, inputs, up)
    meta = (st.get(sname) or {}).get("meta")
    if not why and isinstance(meta, dict):
        host.say("stage: current, kept")
        HUD_PACK = meta.get("hud_pack")
        return meta.get("pacing")
    host.say("stage: %s" % ", ".join(why))
    st.begin(sname)
    # Anything else in the stage is an older layout's (a versioned payload
    # folder) or the last run's work files.
    for n in os.listdir(stage):
        if n != incr.PAYLOAD:
            p = os.path.join(stage, n)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    nxt = os.path.join(stage, ".next")
    os.makedirs(nxt)
    pacing = write_common(nxt, target, version, lib, hud_pack, seed, enhance, game_files=False)
    if target != "macos":
        shutil.copy2(exe, os.path.join(nxt, G.exe_name + ".exe"))
        pdb = os.path.join(os.path.dirname(exe), G.exe_name + ".pdb")
        if os.path.isfile(pdb):
            shutil.copy2(pdb, os.path.join(nxt, G.exe_name + ".pdb"))
        if target == "steamos":
            steamos.stage_steamos(nxt, lib, a.icon_dir)
        else:
            windows.build_launcher_exe(
                os.path.join(nxt, app() + ".exe"),
                os.path.join(a.icon_dir, app() + ".ico"),
                stage,
                lib.PRODUCT_NAME,
            )
            windows.build_root_tools(
                stage, os.path.join(a.icon_dir, app() + ".ico"), lib.PRODUCT_NAME
            )
    incr.sync_tree(nxt, payload)
    shutil.rmtree(nxt)
    n = incr.sync_game_files(G.game_files, os.path.join(payload, "game_files"), lib)
    host.say("game files: %d entries staged" % n)
    check_staged(payload, lib)
    st.record(
        sname,
        key,
        inputs,
        [payload],
        meta={"pacing": bool(pacing), "hud_pack": HUD_PACK},
        skip=incr.MANIFEST_FILES,
        upstream=up,
    )
    return pacing


def wrap(a, stage, payload, version, exe, overrides, lib):
    """The bundle beside the stage. The manifest is always written again
    (a rebase that keeps the files still names new commits); the wrap is
    skipped when its key (that manifest's sha256 and the wrapper's tools)
    is the bundle's on disk. Returns the bundle's path."""
    from . import macos, steamos, windows

    st = a.state
    target = a.target
    wname = incr.wrap_name(target)
    archive = bool(getattr(a, "archive", False)) and target == "windows"

    basis = {}

    def choose(man_path):
        from ..clean import human

        with open(man_path, encoding="utf-8") as f:
            man = json.load(f)
        c = patch.choose(a, man, target, app(), g().m, incr.bundles_dir(st, target))
        if c.kind == "patch":
            host.say(
                "patch: %s (%s of %s; --full for the full bundle)"
                % (
                    c.why,
                    human(patch.payload_size(c.diff)),
                    human(patch.full_size(man)),
                )
            )
            if patch.changes_game_data(c.diff) and target != "macos":
                host.say("patch: " + patch.GAME_DATA_WARNING)
        else:
            host.say("bundle: full (%s)" % c.why)
        return c

    def current(man_path, result, choice=None):
        patching = choice is not None and choice.kind == "patch"
        inputs = incr.wrap_inputs(
            target,
            incr._sha(man_path),
            lib,
            kind="patch" if patching else "full",
            base=choice.base.sha256 if patching else "",
            exe=exe,
            archive=archive,
        )
        basis.update(incr.wrap_basis(st.key_of(incr.stage_name(target)), version, lib, inputs))
        # No upstream key: the manifest lists every staged file with its
        # sha256, so a stage that reran into the same bytes (a touched game
        # file) keeps the bundle.
        key = buildstate.stage_key(inputs)
        why = st.check(wname, key, inputs)
        if not why and os.path.exists(result):
            host.say("bundle: current, kept")
            # The plan's view of it follows this run's stage key.
            st.data["stages"][wname].setdefault("meta", {})["basis"] = dict(basis)
            st.save()
            return True, key, inputs
        host.say("bundle: %s" % ", ".join(why or ["%s is gone" % os.path.basename(result)]))
        st.begin(wname)
        return False, key, inputs

    def done(ok, key, inputs, outs, man_path):
        if not ok:
            st.record(wname, key, inputs, outs, meta={"basis": dict(basis)})
        incr.record_bundle(st, target, man_path, outs[0], made=not ok)
        prune_bundles(a, target, outs[0])
        incr.prune_packs(st, (HUD_PACK or {}).get("sha256"))

    if target == "macos":
        host.step("app and dmg")
        seen = {}

        def reuse(man_path, dmg, choice=None):
            ok, seen["key"], seen["inputs"] = current(man_path, dmg, choice)
            seen["ok"], seen["man"] = ok, man_path
            return ok

        result = macos.wrap_macos(
            stage,
            payload,
            version,
            exe,
            a.out,
            overrides,
            lib,
            os.path.join(a.icon_dir, app() + ".icns"),
            reuse=reuse,
            choose=choose,
        )
        done(seen["ok"], seen["key"], seen["inputs"], [result], seen["man"])
        return result
    extra = None
    if target == "windows":
        if not windows.root_tools_ready(stage):
            windows.build_root_tools(
                stage, os.path.join(a.icon_dir, app() + ".ico"), lib.PRODUCT_NAME
            )
        extra = {"root": windows.root_block(stage)}
    write_manifest(payload, target, version, overrides, lib, extra=extra)
    man_path = os.path.join(payload, "manifest.json")
    if target == "steamos":
        choice = choose(man_path)
        if choice.kind == "patch":
            result = steamos.patch_path(a.out, version, choice.base.version)
        else:
            result = steamos.bundle_path(a.out, version)
        ok, key, inputs = current(man_path, result, choice)
        if not ok:
            host.step("tar")
            top = "%s-%s-%s" % (app(), version, target)
            result = steamos.wrap_steamos(payload, version, exe, a.out, top, choice)
        done(ok, key, inputs, [result], man_path)
        return result
    choice = choose(man_path)
    patching = choice.kind == "patch"
    if patching:
        result = windows.patch_path(a.out, version, choice.base.version)
    else:
        result = windows.bundle_path(a.out, version)
    ok, key, inputs = current(man_path, result, choice)
    outs = [result]
    if not ok:
        if patching:
            host.step("patch")
            result = windows.wrap_windows_patch(stage, payload, version, a.out, choice)
        else:
            host.step("installer")
            result = windows.wrap_windows(stage, payload, version, a.out, lib, a.icon_dir)
        outs = [result]
        if archive:
            result_zip = windows.archive(result)
            host.say("archive: %s" % result_zip)
            outs.append(result_zip)
    done(ok, key, inputs, outs, man_path)
    return result


def prune_bundles(a, target, current):
    """The newest --keep-bundles (2) bundles of the target, and as many of
    its patches, stay in the output folder, the one just made always among
    them; the older ones go
    (their manifest records stay, so a patch against them can still be
    made). Only in a folder inside the game folder: an --out elsewhere is
    the player's own, and nothing there is deleted."""
    from .. import clean

    keep = getattr(a, "keep_bundles", None) or incr.KEEP_BUNDLES
    G = g()
    if not clean._inside(os.path.realpath(a.out), os.path.realpath(G.root)):
        return
    cur = os.path.abspath(current)
    c = clean.collect(())
    # Full bundles and patches each keep their newest.
    for patches in (False, True):
        found = incr.bundles_on_disk(a.out, target, app(), patches)
        found.sort(key=lambda x: os.path.abspath(x[0]) != cur)
        for p, extra in found[keep:]:
            for q in [p] + extra:
                c.add("bundles", q)
    for _cat, p, n in c.items:
        host.say(
            "bundles: removed %s (%s; keeping the newest %d)" % (G.rel(p), clean.human(n), keep)
        )
    c.delete()


def cmd_package(a):
    if getattr(a, "plan", False):
        package(a)
        return
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
    from .. import dump

    G = g()
    if a.target not in G.package["targets"]:
        raise CliError("game.toml's package.targets has no %s" % a.target)
    lib = plib()
    a.out = os.path.abspath(a.out)
    dump_path = getattr(a, "dump", None)
    blockers = setup.host_blockers(a.target, importing=bool(dump_path))
    if blockers:
        raise CliError("cannot package %s on this host:\n  %s" % (a.target, "\n  ".join(blockers)))
    # --dump is judged whole (title, names, space, what game_files/ holds)
    # before the plan, so a wrong disc or a refusal costs nothing.
    imp = None
    if dump_path:
        imp = dump.prepare(dump_path, a.replace)
    else:
        note = dump.leftover_note()
        if note:
            host.say(note)
    toolkit.exclude_pin_mark(toolkit.toolkit_dir())
    # The pack is checked before the plan runs: a missing one must not
    # cost a build. --plan only names it: the wizard makes it after.
    planning = getattr(a, "plan", False)
    choice = tweaks.hud_choice(a)
    want_hud = choice in ("make", "keep")
    hud_pack = hud_pack_path(a.hud_pack) if choice == "make" and not planning else None
    seed_path = getattr(a, "seed_config", None)
    base, remake = None, False
    try:
        if not planning:
            # The base is read before anything is built (manifests only):
            # its pack decides the HUD flags (D3), and a bad --patch-from
            # costs nothing.
            st0 = buildstate.State(G.root)
            base = tweaks.run_base(a, a.target, st0)
            early_base_check(a, base, lib)
            tweaks.guard(a, base)
            if choice == "keep":
                if base is None:
                    raise host.Refusal(
                        "--keep-hud-pack keeps the base's pack, and this run has no base: "
                        "name it with --patch-from",
                        patch.NO_BASE,
                    )
                hud_pack, remake = tweaks.keep_early(st0, base)
                want_hud = hud_pack is not None
        enhance = reads_config()
        if not enhance:
            refuse_no_config(hud_pack or want_hud, seed_path)
        seed = check_seed(seed_path) if seed_path else None
        check_content(hud_pack or want_hud, enhance)
        if planning:
            from . import plan as plan_mod

            plan_mod.show(plan_mod.make_plan(a, lib, imp, enhance, seed), getattr(a, "json", False))
            if imp is not None:
                imp.src.close()
            return
        if getattr(a, "clean", None):
            from .. import clean

            clean.package_clean(a.clean, a.out)
        plan = package_plan(a, imp)
    except BaseException:
        if imp is not None:
            imp.src.close()
        raise
    host.VIEW.plan = [n for n, _ in plan]
    host.say(plan_line(plan))
    if imp is not None:
        host.mark("import")
        # stage_payload warns about an unknown dump; once is enough.
        imp.run(warn_unknown=False)
    run_plan_prefix(a, plan)
    # Loaded after the import and the generate, which write their own
    # entries (they also run alone, from the wizard).
    st = a.state = buildstate.State(G.root)
    st.verify = getattr(a, "verify", False)
    incr.record_import(st, lib)
    incr.record_generate(st)
    if remake:
        hud_pack = tweaks.remake(st, base)
    os.makedirs(a.out, exist_ok=True)
    stage, payload, version, overrides, exe = stage_payload(a.target, a, lib, seed, hud_pack)
    # The build's own cache has the last word: a layer the configure left
    # off (stock_cmake without -DXBOXRECOMP_ENHANCE=ON) reads no config
    # file, whatever the toolkit tree has.
    layer, cache = config_layer(enhance, lib, a.target)
    if enhance and not layer:
        if hud_pack or seed is not None:
            refuse_no_config(hud_pack, seed_path, layer_off=True)
        enhance = False
    if not enhance:
        host.say(no_config_note(cache))
    pacing_note = stage_files(a, stage, payload, version, exe, lib, hud_pack, seed, enhance)
    if seed is not None:
        host.say("seed config: %s (copied to config/config.toml on a first install)" % SEED_NAME)
    if hud_pack:
        host.say("hud pack: %d entries at %dx" % (hud_pack["entries"], hud_pack["scale"]))
        if HUD_PACK:
            # A later --keep-hud-pack finds these bytes here (D4).
            incr.store_pack(st, hud_pack["path"], HUD_PACK["sha256"])
    elif choice == "clear":
        host.say("hud pack: none (--clear-hud-pack)")
    global HASHER
    hc = buildstate.HashCache(st.dir)
    hc.verify = st.verify
    # Only game_files/: the CLI's own small files are hashed afresh.
    HASHER = lambda rel, p: hc.sha256(rel, p) if rel.startswith("game_files/") else None  # noqa: E731
    try:
        result = wrap(a, stage, payload, version, exe, overrides, lib)
    finally:
        HASHER = None
        hc.save()
    host.VIEW.end(True)
    if "-dirty" in version:
        host.say("WARNING: built from uncommitted changes (%s)" % version)
    host.say()
    host.say("bundle: %s" % result)
    host.say()
    if pacing_note:
        host.say(PACING_NOTE)
        host.say()
    host.say(NOTICE_TEXT % lib.PRODUCT_NAME)
