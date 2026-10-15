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

from . import SCHEMA, gitpin

REQUIRED = object()
FILE = "game.toml"


class ManifestError(Exception):
    pass


# table -> key -> (type, default). A default that is a function of the
# manifest is filled after the plain ones (derived()).
STR, INT, BOOL, STRS = "string", "integer", "boolean", "list of strings"
INTS = "list of integers"
# A table of string keys to string values, held as one key (not a table of
# the schema): golden.enhance_stock.
STRMAP = "table of strings"
# The same with a list of strings per key: enhance.hud.languages.
STRSMAP = "table of string lists"
MAPS = (STRMAP, STRSMAP)
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
    "data": {
        "game_files": (STR, "game_files"),
        "game_files_exclude": (STRS, ["default_analysis.json", "UDATA", "TDATA"]),
        "dir_env": (STR, DERIVED),
        "windows": (STR, DERIVED),
        "steamos": (STR, DERIVED),
        "macos": (STR, DERIVED),
    },
    "input": {
        "script_env": (STR, "RECOMP_INPUT_SCRIPT"),
        "presets": (STRS, []),
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
    # Owner-made enhancement assets (xbr enhance). A game without the
    # table has no HUD set to build.
    "enhance": {},
    "enhance.hud": {
        # IPK1 archives (basenames under data.game_files) to scan, and the
        # entry-name patterns (fnmatch, '/' separators, lower case) of the
        # UI set to upscale.
        "archives": (STRS, []),
        "textures": (STRS, []),
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
}
# Optional tables: a game without them still sets up, generates, builds and
# benches (package says the manifest has no [package]).
OPTIONAL = {"package", "enhance"}
TARGETS = ("windows", "steamos", "macos")
BUILD_TARGETS = ("windows", "macos")
# The windows target's toolchain file when build.toolchain_file is unset.
CLI_TOOLCHAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cmake", "llvm-mingw-x86_64.cmake"
)
# The toolkit's own enhancement keys, which the golden check holds built in
# (xboxrecomp_cli.golden.ENHANCE_STOCK); a game's table adds its own.
TOOLKIT_ENHANCE_KEYS = ("render.scale", "display.aspect", "present.pacing", "textures.pack")
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
    if kind == STRMAP:
        return isinstance(v, dict) and all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items()
        )
    if kind == STRSMAP:
        return isinstance(v, dict) and all(
            isinstance(k, str) and _type_ok(STRS, x) for k, x in v.items()
        )
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
    for s in m["xbe"]["sha256"]:
        if not re.match(r"^[0-9a-f]{64}$", s):
            errors.append("xbe.sha256: %r is not a sha256" % s)
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
