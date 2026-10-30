"""game.toml: what the CLI knows about a game, and every path it builds from
the game's root. The CLI holds no game value of its own; docs/manifest.md
describes every key.

load(root) reads and validates the manifest; Game carries the values (as a
nested dict, `g.m["pipeline"]["gen"]`) and the absolute paths the code used
to build from blinx2.py's ROOT. use(game) makes it the current game for the
modules that act on one (current()).
"""

import os
import re
import tomllib
import urllib.parse

from . import SCHEMA, gitpin

REQUIRED = object()
FILE = "game.toml"


class ManifestError(Exception):
    pass


# table -> key -> (type, default). A default that is a function of the
# manifest is filled after the plain ones (derived()).
STR, INT, BOOL, STRS = "string", "integer", "boolean", "list of strings"
INTS = "list of integers"
# An integer or a decimal: a ratio such as bench.db.regress.ms_rise.
FLOAT = "number"
# A table of string keys to string values, held as one key (not a table of
# the schema): golden.enhance_stock.
STRMAP = "table of strings"
# The same with a list of strings per key: enhance.hud.languages.
STRSMAP = "table of string lists"
MAPS = (STRMAP, STRSMAP)
# An array of tables ([[wizard.option]]), each checked by its own rules
# (_check_wizard): held as one key, a list of dicts.
TABLES = "array of tables"
DERIVED = object()
SPEC = {
    "": {"schema": (INT, REQUIRED)},
    "game": {
        "name": (STR, REQUIRED),
        "slug": (STR, REQUIRED),
        "env_header": (STR, ""),
        "env_doc": (STR, ""),
    },
    "xbe": {
        "path": (STR, DERIVED),
        "title_id": (INT, REQUIRED),
        "sha256": (STRS, []),
        # Section name -> sha256 of its raw bytes, for the XBEs sha256
        # lists (fingerprint.py): an unlisted XBE is judged by them.
        "sections": (STRMAP, {}),
    },
    # A pin is a release tag, a commit, or both (the commit then locks the
    # tag); _check_values requires one of them (gitpin.py).
    "cli": {
        "tag": (STR, ""),
        "commit": (STR, ""),
        "url": (STR, ""),
    },
    "toolkit": {
        "url": (STR, REQUIRED),
        "tag": (STR, ""),
        "branch": (STR, ""),
        "commit": (STR, ""),
    },
    "toolchain": {
        "llvm_mingw": (STR, ""),
        "nsis": (STR, "3.13"),
        # The SDL3 release the toolkit is tested against (e.g. "3.4.18"):
        # pinned by pins refresh, shown by doctor on macOS. "" for a game
        # whose toolkit is still on SDL2.
        "sdl3": (STR, ""),
    },
    "pipeline": {
        "game_name": (STR, REQUIRED),
        "gen": (STR, REQUIRED),
        "out": (STR, "analysis"),
        "analysis_json": (STR, ""),
        "seeds": (STRS, []),
        "icall_seeds": (BOOL, True),
        "spin_waits": (STR, ""),
        "exclude_manual": (STR, ""),
        "split": (INT, 250),
        "names_hooks": (STRS, []),
        "ghidra": (BOOL, True),
    },
    "pipeline.disasm": {
        "text_only": (BOOL, False),
        "extra_sections": (STRS, []),
    },
    "build": {
        "exe": (STR, REQUIRED),
        "exe_dir": (STR, ""),
        "targets": (STRS, ["windows"]),
        "windows_dir": (STR, "build-win"),
        "macos_dir": (STR, "build"),
        "toolchain_file": (STR, ""),
        "stock_cmake": (STRS, ["-DXBOXRECOMP_ENHANCE=ON"]),
        "nonstock_vars": (STRS, []),
        "icon_var": (STR, ""),
    },
    "build.resources": {
        # The peak of one gen/ compile job, in MiB (0: estimated from the
        # largest gen/ file); the largest single tool process and its step.
        "gen_job_mib": (INT, 0),
        "tool_mib": (INT, 0),
        "tool_step": (STR, ""),
        # Disk estimates for the generate stage and each build tree.
        "generate_mib": (INT, 1024),
        "tree_mib": (INT, 512),
        # What the imported game files take, for doctor before the first
        # import (0: unknown).
        "game_files_mib": (INT, 0),
        # The CMake targets whose compiles share the sized pool.
        "gen_targets": (STRS, DERIVED),
    },
    "data": {
        "game_files": (STR, "game_files"),
        "game_files_exclude": (STRS, ["default_analysis.json", "UDATA", "TDATA"]),
        # The reference dump's file count, without game_files_exclude
        # names: doctor reports a dump's against it (0: no line).
        "reference_files": (INT, 0),
        "dir_env": (STR, DERIVED),
        "windows": (STR, DERIVED),
        "steamos": (STR, DERIVED),
        "macos": (STR, DERIVED),
    },
    "input": {
        "script_env": (STR, "RECOMP_INPUT_SCRIPT"),
        "presets": (STRS, []),
        # Env keys a golden scenario may set to pin the person's input off
        # (a game's own host-pad or strict-input keys): golden play drops
        # them, since that run is a person's.
        "play_unset": (STRS, []),
    },
    "golden": {
        "json": (STR, ""),
        "frames": (STR, DERIVED),
        "audio": (STR, ""),
        "enhance_stock": (STRMAP, {}),
    },
    "package": {
        "app": (STR, REQUIRED),
        "product": (STR, DERIVED),
        "targets": (STRS, ["windows", "steamos"]),
        "brew": (STRS, []),
        "dylib_companions": (STRS, []),
        "content": (STR, "packaging"),
        "templates": (STR, ""),
        "icon": (STR, "xbe"),
        # The launch.env key the staged launch.env.default sets to the
        # XBE's sha256 and verdict, for the game to print at boot ("": none).
        "stamp_env": (STR, ""),
    },
    "bench": {
        "remote_name": (STR, ""),
        "main_branch": (STR, "main"),
        "toolkit_branch": (STR, ""),
        "toolkit_tests": (BOOL, True),
        "pacing_scenario": (STR, ""),
        "sync_excludes": (STRS, []),
        "crash_tag": (STR, "[CRASH]"),
    },
    # bench gc: the docs that name runs, past the ones every game keeps.
    "bench.gc": {
        "refs": (STRS, []),
        "refs_exclude": (STRS, []),
        "keep": (INT, 3),
        "days": (INT, 7),
    },
    # benchdb (docs/benchdb.md): what it keeps of this game's runs and
    # builds, and when it flags a regression. Workspace facts (roots,
    # hosts, the db path) are in the private benchdb.toml, never here.
    "bench.db": {
        # Published releases whose builds keep their large files.
        "keep_releases": (INT, 2),
        # Internal builds since the last release that keep theirs.
        "internal_keep": (INT, 3),
        # A log over this many bytes is a large file (frames always are).
        "heavy_log": (INT, 1 << 20),
        # Files up to this size get a sha256 in the artifacts table.
        "hash_max": (INT, 1 << 20),
    },
    # The CLAUDE.md perf rules and the build-time rule (benchdb regress).
    "bench.db.regress": {
        "flips_drop": (FLOAT, 0.25),
        "ms_rise": (FLOAT, 1.5),
        # The game's RAM or GPU memory peak at this many times the baseline's.
        "mem_rise": (FLOAT, 1.25),
        "build_rise": (FLOAT, 1.5),
        "build_min_secs": (FLOAT, 30),
        "build_window": (INT, 5),
        "build_min_baselines": (INT, 3),
        "build_min_edges": (INT, 20),
    },
    # Published versions (benchdb releases): which tags count, and the
    # paths whose changes make a different build.
    "release": {
        "tag_glob": (STR, "v*"),
        # Empty: src/, config/ and the [pipeline] and [build] paths;
        # game.toml's [toolkit] and [cli] tables always count.
        "inputs": (STRS, []),
    },
    # Owner-made enhancement assets (xbr enhance). A game without the
    # table has no HUD set to build.
    "enhance": {},
    "enhance.hud": {
        # The archives to scan, and the entry-name patterns (fnmatch, '/'
        # separators, lower case) of the UI set to upscale; exclude drops
        # entries textures matched.
        "archives": (STRS, []),
        "textures": (STRS, []),
        "exclude": (STRS, []),
        # The archive reader (enhance/sources.py): "ipk", IPK1 pairs, each
        # archive a basename under data.game_files; or "hook", a stdlib
        # script at the repo-relative path `hook` that prints an archive's
        # index, each archive a path under data.game_files.
        "source": (STR, "ipk"),
        "hook": (STR, ""),
        # Skip textures whose sides are not powers of two: the NV2A samples
        # DXT only at those sizes, so the engine resizes such a file before
        # upload and the guest copy never hashes like it.
        "pow2": (BOOL, False),
        "scale": (INT, 4),
        # Textures over this many guest texels are left out (0: no limit):
        # full-screen backgrounds are not HUD, and a 1024x512 one at 4x is
        # 32 MB of texels.
        "max_texels": (INT, 0),
        # The Latin font atlas: its entry name and, per band of ink rows in
        # it, the glyphs packed left to right in that band (spaces are
        # ignored; U+FFFD keeps the stock glyph, an icon the game draws
        # for that code).
        "font_atlas": (STR, ""),
        "font_rows": (STRS, []),
        # How far, in stock atlas texels [left, top, right, bottom] (guest
        # px only when the game draws text 1:1), the game's quads sample
        # past a glyph's stock ink box: the region a redrawn glyph
        # may fill. Empty: the glyph's whole slot (the gaps to its
        # neighbours), clipped to the slot either way.
        "font_margin": (INTS, []),
        # The most, in percent, a redrawn glyph is widened or narrowed
        # towards its stock ink width (0: never; the font's own widths).
        "font_stretch": (INT, 0),
        # Glyphs (brackets) drawn to span their stock ink box top to
        # bottom instead of sitting on the baseline at the font's height.
        "font_full_height": (STR, ""),
        # The font the atlas is redrawn from, by its full name (name ID 4),
        # looked up in the host's font folders when --font is not given.
        # The user installs it; the CLI never ships a font.
        "font": (STR, ""),
        # Per language, the archives (basenames, like archives) holding its
        # text; `xbr enhance hud --language` picks which are built (default
        # the first). An archive named here is read only when its language
        # is picked, whether or not archives lists it.
        "languages": (STRSMAP, {}),
        # Entry-name patterns (fnmatch over '<entry>' or '<entry>/<mix
        # name>', lower case) of baked text outside the language archives.
        # Text is built at 2x with BC3; everything else that is not the
        # font atlas is art.
        "text": (STRS, []),
    },
    # The player's build wizard (xbr wizard; docs/wizard.md): the repo it
    # clones at the release tag and asks about newer releases, its icon
    # (optional: the wizard's own gamepad otherwise),
    # the targets it offers, archives the build needs beyond the clone and
    # the options screen, and the game's own open-source credits (listed
    # before the CLI's own; credits.py). A game without the table has no
    # wizard.
    "wizard": {
        # Optional only with private = true (_check_wizard): a public game
        # cannot lose its repo by accident.
        "repo": (STR, ""),
        # A game with no public repo yet (SSX 3, Spartan): built through the
        # wizard's --source only, never published as a wizard release.
        "private": (BOOL, False),
        "icon": (STR, ""),
        "targets": (STRS, DERIVED),
        "fetch": (TABLES, []),
        "option": (TABLES, []),
        "credit": (TABLES, []),
    },
    # The public repo and how a round reaches it (public.py; docs/layout.md).
    # Published with the tree, so nothing private goes here: the patterns
    # and rewrite rules live in $XBR_PUBLIC_DIR.
    "public": {
        # The ssh form, git@github.com:<owner>/<repo>.git.
        "url": (STR, REQUIRED),
        "remote": (STR, "public"),
        "branch": (STR, "main"),
        # "export": one commit per round built from the private tree;
        # "history": the private branch itself is pushed.
        "mode": (STR, "export"),
        # "Name <email>": the export's author, committer and tagger, and
        # the one identity the audit accepts.
        "author": (STR, REQUIRED),
        # Globs on tracked paths, beyond the CLI's private set (layout.PRIVATE).
        "exclude": (STRS, []),
        # Images and binaries allowed besides docs/images/*.png.
        "allow": (STRS, []),
        # A fork's upstream ref (b3: origin/main): its commits are public
        # already.
        "upstream": (STR, ""),
        # Paths allowed only while they equal the upstream's blob.
        "upstream_files": (STRS, []),
        # The export commit's message; "" is "<name> static recompilation:
        # <date> round".
        "message": (STR, ""),
    },
    # Where the layout differs from the standard one (docs/layout.md).
    "layout": {
        "main": (STR, "src/main.c"),
        # Top-level tracked names beyond the standard ones (a fork's
        # upstream files): any other is a doctor note.
        "extra": (STRS, []),
    },
}
# Optional tables: a game without them still sets up, generates, builds and
# benches (package says the manifest has no [package]). A game without
# [public] is not published; one without [layout] has the standard one.
OPTIONAL = {"package", "enhance", "wizard", "public", "layout"}
PUBLIC_MODES = ("export", "history")
# "Name <email>", as git writes an identity.
IDENTITY_RE = re.compile(r"^([^<>\n]*[^<>\s])\s+<([^<>\s]+@[^<>\s]+)>$")
# [[wizard.fetch]] and [[wizard.option]] keys: (type, default).
WIZARD_FETCH = {
    "name": (STR, REQUIRED),
    "url": (STR, REQUIRED),
    "sha256": (STR, REQUIRED),
    "dest": (STR, REQUIRED),
    "unpack": (BOOL, True),
    "hosts": (STRS, ["windows", "macos", "linux"]),
}
WIZARD_OPTION = {
    "key": (STR, REQUIRED),
    "label": (STR, REQUIRED),
    "hint": (STR, ""),
    # A link after the hint ({ text, url }, https only): absent unless set.
    "hint_link": (STRMAP, None),
    # Words after the hint shown only while [enhance.hud] font is not
    # installed (or the wizard cannot tell); with it set, hint_link
    # follows the same rule (`xbr wizard font` reports the font).
    "font_hint": (STR, ""),
    "kind": (STR, REQUIRED),
    "values": (STRS, []),
    "default": (None, REQUIRED),  # a string for choice, a boolean for bool, an int for range
    "config": (STR, ""),
    "flag": (STR, ""),
    # kind range only: absent unless set (OPTIONAL_ENTRY).
    "min": (INT, None),
    "max": (INT, None),
    "step": (INT, None),
    "labels": (STRMAP, None),
}
# [[wizard.credit]]: a project the game stands on, shown in the wizard's
# credits with a link. own marks the game's own repo (or repos), listed
# before the CLI's; the rest go with the third-party projects (credits.py).
WIZARD_CREDIT = {
    "name": (STR, REQUIRED),
    "role": (STR, REQUIRED),
    "license": (STR, REQUIRED),
    "url": (STR, REQUIRED),
    "own": (BOOL, False),
}
# An SPDX licence expression, loosely: ids joined by AND, OR and WITH.
SPDX_RE = re.compile(r"^[A-Za-z0-9.+()-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+()-]+)*$")
# An entry key whose spec default is this stays absent when unset.
OPTIONAL_ENTRY = None
RANGE_KEYS = ("min", "max", "step", "labels")
RANGE_SPAN_MAX = 10000  # the binary's own cap (gamecfg.go)
WIZARD_HOSTS = ("windows", "macos", "linux")
TARGETS = ("windows", "steamos", "macos")
BUILD_TARGETS = ("windows", "macos")
# The windows target's toolchain file when build.toolchain_file is unset.
CLI_TOOLCHAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cmake", "llvm-mingw-x86_64.cmake"
)
# The toolkit's own enhancement keys, which the golden check holds built in
# (xboxrecomp_cli.golden.ENHANCE_STOCK); a game's table adds its own.
TOOLKIT_ENHANCE_KEYS = (
    "render.scale",
    "render.aniso",
    "display.aspect",
    "display.overscan",
    "present.pacing",
    "textures.pack",
    "present.shader",
    "menu.enabled",
    "overlay.stats",
)
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The bootstrap reads [cli] with a line parser, and the help without uv
# reads [game] name the same way: plain one-line strings there.
LINE_KEYS = {
    "cli": ("tag", "commit", "url"),
    "game": ("name", "slug"),
    "build": ("windows_dir", "macos_dir"),
}


def _type_ok(kind, v):
    if kind == STR:
        return isinstance(v, str)
    if kind == INT:
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == BOOL:
        return isinstance(v, bool)
    if kind == INTS:
        return isinstance(v, list) and all(_type_ok(INT, x) for x in v)
    if kind == FLOAT:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == STRMAP:
        return isinstance(v, dict) and all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items()
        )
    if kind == STRSMAP:
        return isinstance(v, dict) and all(
            isinstance(k, str) and _type_ok(STRS, x) for k, x in v.items()
        )
    if kind == TABLES:
        return isinstance(v, list) and all(isinstance(x, dict) for x in v)
    if kind is None:
        return True
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _table(doc, name):
    t = doc
    for part in name.split(".") if name else ():
        t = t.get(part, {})
    return t


def validate(doc):
    """The manifest with every default filled in, or ManifestError naming
    the key and the reason. Unknown keys are errors: a typo must not fall
    back to a default silently."""
    errors = []

    def walk(t, prefix):
        for k, v in t.items():
            name = prefix + k
            if isinstance(v, dict) and SPEC.get(prefix[:-1], {}).get(k, (None,))[0] in MAPS:
                continue
            if isinstance(v, dict):
                if name not in SPEC:
                    errors.append("%s: unknown table" % name)
                else:
                    walk(v, name + ".")
            elif k not in SPEC.get(prefix[:-1], {}):
                errors.append("%s: unknown key" % name)

    walk(doc, "")
    out = {}
    for tname, keys in SPEC.items():
        present = tname == "" or _has_table(doc, tname)
        if not present and tname.split(".")[0] in OPTIONAL:
            continue
        t = _table(doc, tname)
        dst = out
        for part in tname.split(".") if tname else ():
            dst = dst.setdefault(part, {})
        for k, (kind, default) in keys.items():
            name = (tname + "." if tname else "") + k
            if k in t and (kind in MAPS or not isinstance(t[k], dict)):
                if not _type_ok(kind, t[k]):
                    errors.append("%s: must be a %s" % (name, kind))
                    continue
                if kind in (STRS, INTS):
                    dst[k] = list(t[k])
                elif kind == STRSMAP:
                    dst[k] = {n: list(x) for n, x in t[k].items()}
                elif kind == STRMAP:
                    dst[k] = dict(t[k])
                elif kind == TABLES:
                    dst[k] = [dict(x) for x in t[k]]
                else:
                    dst[k] = t[k]
            elif default is REQUIRED:
                errors.append("%s: required" % name)
            elif default is not DERIVED:
                dst[k] = (
                    list(default)
                    if isinstance(default, list)
                    else dict(default)
                    if isinstance(default, dict)
                    else default
                )
    if errors:
        raise ManifestError("; ".join(errors))
    if out["schema"] != SCHEMA:
        raise ManifestError("schema: this CLI reads schema %d, not %d" % (SCHEMA, out["schema"]))
    _derive(out)
    _check_values(out)
    return out


def _has_table(doc, tname):
    t = doc
    for part in tname.split("."):
        if not isinstance(t, dict) or part not in t:
            return False
        t = t[part]
    return isinstance(t, dict)


def _derive(m):
    g, x, d = m["game"], m["xbe"], m["data"]
    m["build"]["resources"].setdefault("gen_targets", [m["build"]["exe"]])
    x.setdefault("path", d["game_files"] + "/default.xbe")
    app = m.get("package", {}).get("app") or g["slug"]
    d.setdefault("dir_env", re.sub(r"[^A-Z0-9_]", "_", app.upper()) + "_DATA_DIR")
    d.setdefault("windows", "%LOCALAPPDATA%\\" + app)
    d.setdefault("steamos", "~/Games/" + app)
    d.setdefault("macos", "~/Library/Application Support/" + app)
    gj = m["golden"]
    gj.setdefault("frames", (gj["json"].rsplit("/", 1)[0] + "/frames") if gj["json"] else "")
    if "package" in m:
        m["package"].setdefault("product", g["slug"] + "-recomp")
    if "wizard" in m:
        m["wizard"].setdefault("targets", list(m.get("package", {}).get("targets", [])))
        m["wizard"]["fetch"] = [_entry(e, WIZARD_FETCH) for e in m["wizard"]["fetch"]]
        m["wizard"]["option"] = [_entry(e, WIZARD_OPTION) for e in m["wizard"]["option"]]
        m["wizard"]["credit"] = [_entry(e, WIZARD_CREDIT) for e in m["wizard"]["credit"]]


def _entry(e, spec):
    """An array-of-tables entry with its defaults; unknown and missing keys
    are left for _check_wizard to name."""
    out = dict(e)
    for k, (_, default) in spec.items():
        if k not in out and default is not REQUIRED and default is not OPTIONAL_ENTRY:
            out[k] = list(default) if isinstance(default, list) else default
    return out


def repo_id(url):
    """<owner>/<repo> of a repo URL, lower case, whatever the form:
    git@host:o/r.git, ssh://git@host/o/r, https://host/o/r(.git)(/), so
    the ssh [public] url and the https [wizard] repo compare. "" when the
    URL names no owner and repo."""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?[^/]+/(.+)$", u)
    if not m:
        m = re.match(r"^(?:[^@/:]+@)?[^/:]+:(?!//)(.+)$", u)
    if not m:
        return ""
    parts = [p for p in m.group(1).split("/") if p]
    if len(parts) < 2:
        return ""
    return "/".join(parts[-2:]).lower()


def _check_public(m):
    p = m.get("public")
    if not p:
        return []
    errors = []
    if not re.match(r"^[^@\s:/]+@[^\s:/]+:[^\s]+$", p["url"]) or not repo_id(p["url"]):
        errors.append("public.url: the ssh form, git@<host>:<owner>/<repo>.git")
    if p["mode"] not in PUBLIC_MODES:
        errors.append("public.mode: %r is not one of %s" % (p["mode"], ", ".join(PUBLIC_MODES)))
    if not IDENTITY_RE.match(p["author"]):
        errors.append('public.author: "Name <email>"')
    for k in ("remote", "branch"):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", p[k]) or ".." in p[k]:
            errors.append("public.%s: %r is not a plain git name" % (k, p[k]))
    if p["upstream"].startswith("-"):
        errors.append("public.upstream: %r starts with '-'" % p["upstream"])
    # A [wizard] repo naming another repo is doctor's layout problem, not
    # a load error: the game still builds while it moves.
    return errors


def _check_wizard(m):
    """[wizard]'s own rules, the ones that need no file: the files (the
    icon, the config keys, the package flags) are wizard_problems()'s."""
    w = m.get("wizard")
    if not w:
        return []
    errors = []
    if not w["repo"]:
        if not w["private"]:
            errors.append("wizard.repo: required (or private = true: a game with no public repo)")
    elif not re.match(r"^https://[^\s/]+/\S+$", w["repo"]):
        errors.append("wizard.repo: an https:// URL")
    if "package" not in m:
        errors.append("wizard: needs a [package] table (the wizard runs package)")
    else:
        for t in w["targets"]:
            if t not in m["package"]["targets"]:
                errors.append("wizard.targets: %r is not in package.targets" % t)
    if not w["targets"]:
        errors.append("wizard.targets: names no target")
    for kind, entries, spec in (
        ("fetch", w["fetch"], WIZARD_FETCH),
        ("option", w["option"], WIZARD_OPTION),
        ("credit", w["credit"], WIZARD_CREDIT),
    ):
        for i, e in enumerate(entries):
            name = "wizard.%s[%d]" % (kind, i)
            for k in e:
                if k not in spec:
                    errors.append("%s.%s: unknown key" % (name, k))
            for k, (t, default) in spec.items():
                if k not in e:
                    if default is REQUIRED:
                        errors.append("%s.%s: required" % (name, k))
                elif t is not None and not _type_ok(t, e[k]):
                    errors.append("%s.%s: must be a %s" % (name, k, t))
    if errors:
        return errors
    names = set()
    for i, f in enumerate(w["fetch"]):
        name = "wizard.fetch[%d]" % i
        if f["name"] in names:
            errors.append("%s.name: %r twice" % (name, f["name"]))
        names.add(f["name"])
        if not f["url"].startswith("https://"):
            errors.append("%s.url: an https:// URL" % name)
        if not re.match(r"^[0-9a-f]{64}$", f["sha256"]):
            errors.append("%s.sha256: not a sha256" % name)
        d = f["dest"]
        # Each segment named: "" (a//b, a trailing /), "." and ".." would
        # point at the clone itself or out of it, and an unpack replaces
        # its dest wholesale.
        segs = d.split("/")
        if (
            os.path.isabs(d)
            or d.startswith("~")
            or "\\" in d
            or any(s in ("", ".", "..") for s in segs)
        ):
            errors.append("%s.dest: a path inside the clone, with /" % name)
        for h in f["hosts"]:
            if h not in WIZARD_HOSTS:
                errors.append("%s.hosts: %r is not one of %s" % (name, h, ", ".join(WIZARD_HOSTS)))
    keys = set()
    for i, o in enumerate(w["option"]):
        name = "wizard.option[%d]" % i
        if not re.match(r"^[a-z][a-z0-9_]*$", o["key"]):
            errors.append("%s.key: lower-case letters, digits and '_'" % name)
        if o["key"] in keys:
            errors.append("%s.key: %r twice" % (name, o["key"]))
        keys.add(o["key"])
        if bool(o["config"]) == bool(o["flag"]):
            errors.append("%s: exactly one of config or flag" % name)
        if o["kind"] != "range":
            for k in RANGE_KEYS:
                if k in o:
                    errors.append("%s.%s: only for kind range" % (name, k))
        if o["kind"] == "bool":
            if not isinstance(o["default"], bool):
                errors.append("%s.default: true or false (kind bool)" % name)
            if o["values"]:
                errors.append("%s.values: only for kind choice" % name)
        elif o["kind"] == "choice":
            if len(o["values"]) < 2:
                errors.append("%s.values: two or more (kind choice)" % name)
            if not isinstance(o["default"], str) or o["default"] not in o["values"]:
                errors.append("%s.default: one of values" % name)
            if o["flag"]:
                errors.append("%s: a flag option is kind bool (set or not)" % name)
        elif o["kind"] == "range":
            errors += _range_errors(name, o)
        else:
            errors.append("%s.kind: bool, choice or range" % name)
        if "hint_link" in o:
            errors += _hint_link_errors(name, o["hint_link"])
        if o["font_hint"] and not m.get("enhance", {}).get("hud", {}).get("font"):
            errors.append("%s.font_hint: needs [enhance.hud] font, the font it waits for" % name)
        if o["flag"] and not re.match(r"^--[a-z][a-z0-9-]*$", o["flag"]):
            errors.append("%s.flag: a long option, --name" % name)
        if o["config"] and not re.match(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$", o["config"]):
            errors.append("%s.config: a dotted key, table.key" % name)
    urls = set()
    for i, c in enumerate(w["credit"]):
        errors += credit_errors("wizard.credit[%d]" % i, c)
        # The wizard lists a url once (credits.merged): a second entry for
        # it would vanish without a word.
        key = c["url"].rstrip("/").lower()
        if key in urls:
            errors.append("wizard.credit[%d].url: another entry has this address" % i)
        urls.add(key)
    return errors


def credit_errors(name, c):
    """A credit as the wizard shows it: words for its name and role, an
    SPDX licence, an https link (the CLI's own entries pass the same)."""
    errors = []
    for k in ("name", "role"):
        if not c[k].strip() or c[k] != c[k].strip():
            errors.append("%s.%s: words, no spaces at the ends" % (name, k))
    if not SPDX_RE.match(c["license"]):
        errors.append("%s.license: an SPDX expression, such as MIT or MIT OR Apache-2.0" % name)
    if not https_ok(c["url"]):
        errors.append("%s.url: an https address" % name)
    return errors


def https_ok(url):
    """An https address the binary's url.Parse takes and the OS's opener
    gets as one argument: no whitespace or control characters."""
    try:
        u = urllib.parse.urlsplit(url)
        ok = u.scheme == "https" and bool(u.hostname)
    except ValueError:  # urlsplit refuses a broken [IPv6] host
        ok = False
    return ok and not re.search(r"[\s\x00-\x1f\x7f]", url)


def _hint_link_errors(name, link):
    """A hint link opens in the player's browser from a published binary:
    words to show and an https address, nothing else."""
    errors = []
    if set(link) != {"text", "url"}:
        errors.append("%s.hint_link: exactly text and url" % name)
    if not link.get("text", "").strip():
        errors.append("%s.hint_link.text: words to show" % name)
    if not https_ok(link.get("url", "")):
        errors.append("%s.hint_link.url: an https address" % name)
    return errors


HUD_SOURCES = ("ipk", "hook")


def rel_path_problem(path):
    """Why path is not a relative path with '/' and no '..', or ""."""
    if not path:
        return "is empty"
    if os.path.isabs(path) or path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
        return "must be relative"
    if "\\" in path:
        return "must use / separators"
    parts = path.split("/")
    if ".." in parts:
        return "must not hold .."
    if "." in parts or "" in parts:
        return "must not hold ., // or a trailing /"
    return ""


def builds_enhance(m):
    """Whether the stock build turns the toolkit's enhancements layer on,
    before any build exists: the toolkit's option defaults OFF, so a game
    whose build.stock_cmake does not set -DXBOXRECOMP_ENHANCE=ON (SSX 3's
    and Spartan's empty list) builds without it even on a toolkit that has
    src/enhance. CMake's truth, the last -D or -U winning, as
    package/lib.py enhance_on reads a cache."""
    on = False
    for d in m["build"]["stock_cmake"]:
        k, _, v = d.partition("=")
        if k.split(":")[0] == "-DXBOXRECOMP_ENHANCE":
            v = v.strip().upper()
            on = v not in ("OFF", "0", "FALSE", "NO", "N", "IGNORE", "NOTFOUND", "")
            on = on and not v.endswith("-NOTFOUND")
        elif k == "-UXBOXRECOMP_ENHANCE":
            on = False
    return on


def range_values(o):
    """The integers a kind range option takes, min to max by step."""
    return list(range(o["min"], o["max"] + 1, o.get("step", 1)))


def _range_errors(name, o):
    """A kind range option's rules: whole numbers, min below max, a step
    that lands on max, the default and every label on a step."""
    errors = []
    for k in ("min", "max"):
        if k not in o:
            errors.append("%s.%s: required (kind range)" % (name, k))
    if errors:
        return errors
    lo, hi, step = o["min"], o["max"], o.get("step", 1)
    if lo >= hi:
        errors.append("%s.max: above min (kind range)" % name)
        return errors
    if step < 1:
        errors.append("%s.step: 1 or more" % name)
        return errors
    if (hi - lo) % step:
        errors.append("%s.step: %d does not divide max - min (%d)" % (name, step, hi - lo))
        return errors
    # The binary refuses a wider range, and wizard config would write a
    # TOML literal for every value.
    if hi - lo > RANGE_SPAN_MAX:
        errors.append("%s.max: max - min above %d" % (name, RANGE_SPAN_MAX))
        return errors
    vals = range_values(o)
    d = o["default"]
    if isinstance(d, bool) or not isinstance(d, int) or d not in vals:
        errors.append("%s.default: a whole number from min to max, on a step" % name)
    if o["values"]:
        errors.append("%s.values: only for kind choice" % name)
    if o["flag"]:
        errors.append("%s: a flag option is kind bool (set or not)" % name)
    for k in o.get("labels", {}):
        # The binary compares keys as text with each value's decimal
        # form, so "01" would never match 1.
        if not re.match(r"^-?[0-9]+$", k) or str(int(k)) != k or int(k) not in vals:
            errors.append("%s.labels: %r is not a value of the range" % (name, k))
    return errors


def _check_values(m):
    errors = []
    if not SLUG.match(m["game"]["slug"]):
        errors.append("game.slug: lower-case letters, digits and '-' only")
    for k in ("cli", "toolkit"):
        p = m[k]
        if p["commit"] and not re.match(r"^[0-9a-f]{40}$", p["commit"]):
            errors.append("%s.commit: a full 40-character commit sha" % k)
        if p["tag"] and not gitpin.tag_ok(p["tag"]):
            errors.append("%s.tag: %r is not a plain tag name" % (k, p["tag"]))
        if p["url"].startswith("-"):
            # git would read it as an option (clone, ls-remote).
            errors.append("%s.url: %r starts with '-'" % (k, p["url"]))
    if not m["cli"]["tag"] and not m["cli"]["commit"]:
        errors.append("cli: tag or commit required")
    if not m["toolkit"]["tag"]:
        # Without a tag the clone follows the branch to the commit, as before.
        for k in ("branch", "commit"):
            if not m["toolkit"][k]:
                errors.append("toolkit.%s: required without a tag" % k)
    for t in m["build"]["targets"]:
        if t not in BUILD_TARGETS:
            errors.append("build.targets: %r is not one of %s" % (t, ", ".join(BUILD_TARGETS)))
    for k, v in m["build"]["resources"].items():
        if isinstance(v, int) and not isinstance(v, bool) and v < 0:
            errors.append("build.resources.%s: must not be negative" % k)
    if not m["build"]["resources"]["gen_targets"]:
        errors.append("build.resources.gen_targets: name at least one target")
    if "package" in m:
        for t in m["package"]["targets"]:
            if t not in TARGETS:
                errors.append("package.targets: %r is not one of %s" % (t, ", ".join(TARGETS)))
        if m["package"]["app"].lower() == m["build"]["exe"].lower():
            # <app>.exe (the launcher) and <exe>.exe sit in one folder, and
            # NTFS and APFS ignore case: the launcher would replace the game.
            errors.append(
                "package.app: %r names the same file as build.exe %r on a case-insensitive "
                "filesystem (Windows, macOS); pick another app name"
                % (m["package"]["app"], m["build"]["exe"])
            )
        if "macos" in m["package"]["targets"] and "macos" not in m["build"]["targets"]:
            errors.append("package.targets: macos needs build.targets to hold macos")
        for c in m["package"]["dylib_companions"]:
            if "=" not in c:
                errors.append("package.dylib_companions: %r is not NAME=brew:FORMULA" % c)
    for k in m["golden"]["enhance_stock"]:
        if k in TOOLKIT_ENHANCE_KEYS:
            errors.append(
                "golden.enhance_stock: %r is a toolkit key, which the golden check knows already"
                % k
            )
    hud = m.get("enhance", {}).get("hud")
    if hud:
        if hud["source"] not in HUD_SOURCES:
            errors.append("enhance.hud.source: one of %s" % ", ".join(HUD_SOURCES))
        if hud["source"] == "hook":
            if not hud["hook"]:
                errors.append('enhance.hud.hook: required with source = "hook"')
            else:
                why = rel_path_problem(hud["hook"])
                if why:
                    errors.append("enhance.hud.hook: %r %s" % (hud["hook"], why))
        elif hud["hook"]:
            errors.append('enhance.hud.hook: only with source = "hook"')
        for arc in hud["archives"] + [a for arcs in hud["languages"].values() for a in arcs]:
            why = rel_path_problem(arc)
            if why:
                errors.append("enhance.hud.archives: %r %s" % (arc, why))
        if hud["scale"] not in (2, 4):
            errors.append("enhance.hud.scale: 2 or 4")
        if hud["max_texels"] < 0:
            errors.append("enhance.hud.max_texels: 0 (no limit) or positive")
        if hud["font_atlas"] and not hud["font_rows"]:
            errors.append("enhance.hud.font_rows: required with font_atlas")
        fm = hud["font_margin"]
        if fm and (len(fm) != 4 or min(fm) < 0):
            errors.append(
                "enhance.hud.font_margin: four texel counts [left, top, right, bottom], none negative"
            )
        if not 0 <= hud["font_stretch"] < 100:
            errors.append("enhance.hud.font_stretch: a percent, 0 to 99")
        for i, row in enumerate(hud["font_rows"]):
            if not row.strip():
                errors.append("enhance.hud.font_rows: row %d names no glyph" % i)
        seen = {}
        for lang, arcs in hud["languages"].items():
            if not arcs:
                errors.append("enhance.hud.languages.%s: names no archive" % lang)
            for arc in arcs:
                if arc in seen:
                    errors.append(
                        "enhance.hud.languages.%s: %s is %s's already" % (lang, arc, seen[arc])
                    )
                seen[arc] = lang
    gc = m["bench"]["gc"]
    for k in ("keep", "days"):
        if gc[k] < 0:
            errors.append("bench.gc.%s: must not be negative" % k)
    db = m["bench"]["db"]
    for k in ("keep_releases", "internal_keep", "heavy_log", "hash_max"):
        if db[k] < 0:
            errors.append("bench.db.%s: must not be negative" % k)
    for k, v in db["regress"].items():
        if v <= 0:
            errors.append("bench.db.regress.%s: must be positive" % k)
    if not 0 < db["regress"]["flips_drop"] < 1:
        errors.append("bench.db.regress.flips_drop: must be between 0 and 1")
    for path in m["release"]["inputs"]:
        if os.path.isabs(path) or path.startswith("~") or "\\" in path or ".." in path.split("/"):
            errors.append("release.inputs: %r must be relative to the game root, with /" % path)
    for k in ("refs", "refs_exclude"):
        for path in gc[k]:
            # These may leave the game root on purpose (a workspace's notes
            # beside it), but stay relative so every worktree resolves them.
            if os.path.isabs(path) or path.startswith("~") or "\\" in path:
                errors.append(
                    "bench.gc.%s: %r must be relative to the game root, with /" % (k, path)
                )
    if m["pipeline"]["split"] <= 0:
        errors.append("pipeline.split: must be positive")
    errors += _check_wizard(m)
    errors += _check_public(m)
    for s in m["xbe"]["sha256"]:
        if not re.match(r"^[0-9a-f]{64}$", s):
            errors.append("xbe.sha256: %r is not a sha256" % s)
    for name, s in m["xbe"]["sections"].items():
        if not re.match(r"^[0-9a-f]{64}$", s):
            errors.append("xbe.sections.%s: %r is not a sha256" % (name, s))
    if m["data"]["reference_files"] < 0:
        errors.append("data.reference_files: must not be negative")
    stamp = m.get("package", {}).get("stamp_env", "")
    if stamp and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", stamp):
        errors.append("package.stamp_env: %r is not an environment variable name" % stamp)
    for name, path in _paths(m):
        if os.path.isabs(path) or path.startswith("~") or "\\" in path:
            errors.append("%s: a path relative to the game root, with /" % name)
        elif ".." in path.split("/"):
            errors.append("%s: must stay inside the game root" % name)
    if errors:
        raise ManifestError("; ".join(errors))


def _paths(m):
    """(key, value) of every key that is a path under the game root."""
    p = m["pipeline"]
    out = [
        ("xbe.path", m["xbe"]["path"]),
        ("pipeline.gen", p["gen"]),
        ("pipeline.out", p["out"]),
        ("data.game_files", m["data"]["game_files"]),
    ]
    out += [("pipeline.seeds", s) for s in p["seeds"]]
    out += [("pipeline.names_hooks", s) for s in p["names_hooks"]]
    for k in ("spin_waits", "exclude_manual", "analysis_json"):
        if p[k]:
            out.append(("pipeline." + k, p[k]))
    b = m["build"]
    out += [("build." + k, b[k]) for k in ("windows_dir", "macos_dir") if b[k]]
    for k in ("exe_dir", "toolchain_file"):
        if b[k]:
            out.append(("build." + k, b[k]))
    for k in ("json", "frames", "audio"):
        if m["golden"][k]:
            out.append(("golden." + k, m["golden"][k]))
    for k in ("env_header", "env_doc"):
        if m["game"][k]:
            out.append(("game." + k, m["game"][k]))
    if "package" in m:
        for k in ("content", "templates"):
            if m["package"][k]:
                out.append(("package." + k, m["package"][k]))
    if "wizard" in m and m["wizard"]["icon"]:
        out.append(("wizard.icon", m["wizard"]["icon"]))
    if "layout" in m:
        out.append(("layout.main", m["layout"]["main"]))
    return out


def parse_text(text, path=FILE):
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError("%s: %s" % (path, e)) from e
    try:
        m = validate(doc)
    except ManifestError as e:
        raise ManifestError("%s: %s" % (path, e)) from e
    bad = [
        "%s.%s" % (t, k)
        for t, keys in LINE_KEYS.items()
        for k in keys
        if k in m[t] and k in _table(tomllib.loads(text), t) and line_value(text, t, k) != m[t][k]
    ]
    if bad:
        raise ManifestError(
            '%s: %s: write as one plain line, key = "value" (the bootstrap reads it '
            "without a TOML parser)" % (path, ", ".join(bad))
        )
    return m


def line_value(text, table, key):
    """What the bootstrap's line parser reads for [table] key: the value of
    `key = "..."` inside that table, else None. Kept in step with
    wrapper/game.py's (test_wrapper checks both against tomllib)."""
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            cur = line.strip("[]").strip()
            continue
        if cur != table:
            continue
        m = re.match(r'^%s\s*=\s*"([^"\\]*)"\s*(#.*)?$' % re.escape(key), line)
        if m:
            return m.group(1)
    return None


class Game:
    """One game: its root, its manifest, and the paths built from them."""

    def __init__(self, root, m):
        self.root = os.path.abspath(root)
        self.m = m
        self.name = m["game"]["name"]
        self.slug = m["game"]["slug"]
        p, b = m["pipeline"], m["build"]
        j = self.path
        self.gen = j(p["gen"])
        gen_parent = os.path.dirname(self.gen)
        # recomp's marker and the generation key sit beside gen/.
        self.regen_marker = os.path.join(gen_parent, ".gen-regenerating")
        self.gen_key = os.path.join(gen_parent, "gen.key.json")
        self.venv = j(".venv")
        self.venv_ghidra = j(".venv-ghidra")
        self.third_party = j("third_party")
        self.logs = j("build-logs")
        self.game_files = j(m["data"]["game_files"])
        self.xbe = j(m["xbe"]["path"])
        stem = os.path.splitext(os.path.basename(self.xbe))[0]
        # xbe_parser's --json: pipeline.analysis_json, else beside the dump
        # (the toolkit's convention). A game whose dump is read-only (a
        # link to the player's own copy) names a path under its tree.
        self.analysis_json = (
            j(p["analysis_json"])
            if p["analysis_json"]
            else os.path.join(os.path.dirname(self.xbe), stem + "_analysis.json")
        )
        self.out = j(p["out"])
        self.seeds = [j(s) for s in p["seeds"]]
        self.icall_seeds = os.path.join(self.out, "icall_seeds.json")
        self.stage_extras = os.path.join(self.out, "stage-extras.json")
        self.ghidra_export = os.path.join(self.out, "ghidra", "export", "functions.json")
        self.names_hooks = [j(s) for s in p["names_hooks"]]
        self.recomp_manual = j(p["exclude_manual"]) if p["exclude_manual"] else ""
        self.spin_waits = j(p["spin_waits"]) if p["spin_waits"] else ""
        self.pins = j("config", "setup-pins.json")
        self.uv_lock = j("uv.lock")
        self.dist = j("dist")
        self.exe_name = b["exe"]
        # Unset: the CLI's own llvm-mingw toolchain file, the one every game
        # cross-compiles with (a game may still name its own).
        self.toolchain_file = j(b["toolchain_file"]) if b["toolchain_file"] else CLI_TOOLCHAIN
        pkg = m.get("package")
        self.content = j(pkg["content"]) if pkg else ""
        self.templates = j(pkg["templates"]) if pkg and pkg["templates"] else ""
        gj = m["golden"]
        self.golden_json = j(gj["json"]) if gj["json"] else ""
        self.golden_frames = j(gj["frames"]) if gj["frames"] else ""
        self.golden_audio = j(gj["audio"]) if gj["audio"] else ""

    def path(self, *rel):
        parts = []
        for r in rel:
            parts += r.split("/")
        return os.path.join(self.root, *parts)

    def rel(self, path):
        """A path under the root as the manifest writes it (with /)."""
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    @property
    def package(self):
        if "package" not in self.m:
            raise ManifestError("%s has no [package] table: this game is not packaged" % FILE)
        return self.m["package"]

    def build_dir(self, target):
        b = self.m["build"]
        return self.path(b["windows_dir"] if target == "windows" else b["macos_dir"])

    def exe(self, bdir, target):
        d = (
            os.path.join(bdir, *self.m["build"]["exe_dir"].split("/"))
            if self.m["build"]["exe_dir"]
            else bdir
        )
        return os.path.join(d, self.exe_name + (".exe" if target == "windows" else ""))


def load(root):
    path = os.path.join(root, FILE)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ManifestError(
            "no %s in %s: run this from a game that has one (or pass --game DIR), "
            "or start one with `xbr new DIR`" % (FILE, root)
        ) from e
    return Game(root, parse_text(text, path))


_current = None


def use(game):
    global _current
    _current = game
    return game


def current():
    if _current is None:
        raise ManifestError("no game loaded")
    return _current
