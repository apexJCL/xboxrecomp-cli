"""game.toml: what the CLI knows about a game, and every path it builds from
the game's root. The CLI holds no game value of its own; docs/manifest.md
describes every key.

load(root) reads and validates the manifest; Game carries the values (as a
nested dict, `g.m["pipeline"]["gen"]`) and the absolute paths the code used
to build from blinx2.py's ROOT. use(game) makes it the current game for the
modules that act on one (current()).
"""

import math
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
# A table of names to tables of string keys to string values:
# mods.signatures, one row per name.
STRMAPS = "table of string tables"
MAPS = (STRMAP, STRSMAP, STRMAPS)
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
        # The reference release, from its certificate (fingerprint.py): a
        # dump whose region or version differs is another release, and a
        # dump with another title ID is one too when it names this title
        # by its alternate title IDs or its name. None: not recorded.
        "region": (INT, None),
        "version": (INT, None),
        "title": (STR, ""),
        "alt_title_ids": (INTS, []),
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
        "symbols": (STR, ""),
        # The port's names and their per-release maps (docs/names.md).
        "names": (STR, ""),
        "releases": (STR, "config/releases"),
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
        "stock_cmake": (STRS, ["-DXBOXRECOMP_ENHANCE=ON", "-DXBOXRECOMP_APU_DSP=ON"]),
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
    "enhance.textures": {
        # The readers, in order (enhance/sources/): archive, hook, loose,
        # dump. Empty: the [enhance.hud] reader when it names archives, and
        # the dump.
        "sources": (STRS, []),
        # Archive basenames (or hook paths) under data.game_files; empty:
        # every archive of [enhance.hud], its languages' too.
        "archives": (STRS, []),
        # fnmatch patterns ('/', lower case) of the loose files under
        # data.game_files; the hook's script, as enhance.hud.hook.
        "loose": (STRS, []),
        "hook": (STR, ""),
        # Names never packed, whatever their class.
        "exclude": (STRS, []),
        # Class to patterns: font, text, ui, world, and grid for a sprite
        # sheet's fact. Named world patterns keep other named textures out.
        "classes": (STRSMAP, {}),
        # A game's presets: name to classes and name patterns. hud and traced
        # are not redefined.
        "presets": (STRSMAP, {}),
        # Entries the preview shows first (D10).
        "preview": (STRS, []),
        # The game's load re-encodes the levels under 4x4 of a DDS chain, so
        # such a file's bytes are not what the runtime hashes (measure it).
        "reencodes_small_levels": (BOOL, False),
    },
    "enhance.hud": {
        # The archives to scan, and the entry-name patterns (fnmatch, '/'
        # separators, lower case) of the UI set to upscale; exclude drops
        # entries textures matched.
        "archives": (STRS, []),
        "textures": (STRS, []),
        "exclude": (STRS, []),
        # The archive reader (enhance/sources/): "ipk", IPK1 pairs, each
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
    # Mod hook slots (mod-connectivity-layer D3): the functions recomp
    # wraps with a pre and post dispatch, by VA ("0x0003B180") or by a
    # signature ("sig:<name>", a [mods.signatures] row). analyze resolves
    # them into config/hook_functions.json (hooks.py).
    "mods": {
        "hook_functions": (STRS, []),
        # name -> {pattern, section}: hex bytes with ?? wildcards, matched
        # exactly once in that XBE section.
        "signatures": (STRMAPS, {}),
        # hook_functions entries whose float return analyze's walk cannot
        # settle: st0 returns one in st0, no_st0 does not.
        "st0": (STRS, []),
        "no_st0": (STRS, []),
    },
    # Stages from the owner's dump to a local Godot project (xbr extract;
    # extract/). Every per-game value of the Ninja reader lives here.
    "extract": {
        # Under the game root; gitignored (dist/).
        "out": (STR, "dist/extract"),
        # Bytes of a vertex chunk's header: 8 in Ninja, 16 in ports that
        # widened it (BLiNX 1).
        "vertex_header": (INT, 8),
        # Float normals packed into NORMPACKED3 in the VN_* vertex chunks.
        "packed_normals": (BOOL, False),
        # NJS_OBJECT angles: "bams" (int32, 0x10000 a turn) or "radians" (f32).
        "angles": (STR, "bams"),
        # The world's up axis: "y", or "-y" for a world whose floors face -Y
        # in the data (turned a half turn about X into glTF's Y-up). Not for
        # a flip the game's view matrix applies at draw time.
        "up": (STR, "y"),
        # A textured strip's colour: "diffuse" (stock Ninja, the material
        # modulates the texture) or "white" (an engine that draws UV and
        # environment-mapped strips with its own constant colour).
        "textured_colour": (STR, "diffuse"),
        # Bytes of one texlist name.
        "texname_size": (INT, 32),
        # Collision surface classes (0xRRGGBB diffuse colours) the game never
        # tests: extract --collision draws them but gives them no shape.
        "collision_skip": (INTS, []),
        # Names for collision surface classes (EXTRACT_COLLISION_CLASS below).
        "collision_class": (TABLES, []),
        # Where a texlist name's DDS is: a pattern under data.game_files
        # ("media/{name}.dds", any case) or "ipk:<archive>:<entry pattern>".
        "textures": (STR, REQUIRED),
        "stage": (TABLES, []),
        # The game world placements live in, against the stage trees' data
        # (extract --placements): "y" the same axes, "-y" the data turned a
        # half turn about X before the game draws it.
        "placement_world": (STR, "y"),
        # Per-stage placement tables and the models of their classes, which
        # only extract --placements reads (EXTRACT_PLACEMENT_SET below).
        "placement_set": (TABLES, []),
        "placement_model": (TABLES, []),
    },
    # Where the layout differs from the standard one (docs/layout.md).
    "layout": {
        "main": (STR, "src/main.c"),
        # Top-level tracked names beyond the standard ones (a fork's
        # upstream files): any other is a doctor note.
        "extra": (STRS, []),
    },
    # What the title's console settings may be: the languages its disc
    # carries, as XC_LANGUAGE codes (LANGUAGES). build passes them to the
    # toolkit (XBOXRECOMP_LANGUAGES), whose menu offers only these.
    "system": {
        "languages": (STRS, []),
    },
    # hud-map (docs/commands.md): the game's HUD table for the toolkit's
    # HUD layer, the scenarios check runs by default, and the mod id that
    # switches the layer (capture --level 1 and check run it on and off).
    "hud": {
        "table": (STR, "config/hud.toml"),
        "scenarios": (STRS, []),
        "mod": (STR, ""),
    },  # saves import (docs/manifest.md, xbr saves): which title IDs are the
    # game's, how its saves are signed, and whether its TDATA travels.
    "saves": {
        "title_ids": (INTS, DERIVED),
        # "resign": [[saves.signed]] says how; "none": unsigned; "" (unset):
        # not known, imported as they are with a warning.
        "signing": (STR, ""),
        "tdata": (STR, "optional"),
        "signed": (TABLES, []),
    },
}
# The dashboard's language codes, XC_LANGUAGE 1..9 in order (the toolkit's
# kernel_language.c holds the same table).
LANGUAGES = ("en", "ja", "de", "fr", "es", "it", "ko", "zh", "pt")
# Optional tables: a game without them still sets up, generates, builds and
# benches (package says the manifest has no [package]). A game without
# [public] is not published; one without [layout] has the standard one;
# one without [hud] has no HUD table.
TEXTURE_SOURCES = ("archive", "hook", "loose", "dump")
TEXTURE_CLASS_KEYS = ("font", "text", "ui", "world", "grid")
OPTIONAL = {"package", "enhance", "wizard", "public", "layout", "extract", "hud", "saves"}
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
# [[extract.stage]]: a stage's name (its file and scene names), the XBE
# section its data lives in (for --list and --scan), the NJS_OBJECT
# tree(s) to draw (a VA or a list of VAs) and its texlist's VA (0: none),
# or one texlist per root when the parts of a stage each have their own,
# and optionally one transform per root: the scale and translation the
# game's draw code applies around a tree (a sky drawn again mirrored in
# Y is the same root twice, the second with scale [1, -1, 1]; a mirrored
# tree drawn into a render target may name the target's clear colour as a
# backdrop), and optionally a texture_alpha table: texture names whose
# surfaces the game blends with an opacity of its own, such as a floor a
# planar reflection shows through. collision names the stage's collision
# trees (a VA or a list), which only extract --collision reads;
# placements is the stage's index into the [[extract.placement_set]] tables,
# which only extract --placements reads; placement_layers replaces a set's
# layers on this stage alone (a game that loads an area's lists only at its
# first stage, or another stage's list at a boss stage): offsets may be
# negative while the index stays 0 or more, and [] loads nothing.
EXTRACT_STAGE = {
    "name": (STR, REQUIRED),
    "section": (STR, ""),
    "root": (None, REQUIRED),
    "texlist": (None, 0),
    "transform": (None, None),  # absent unless set (OPTIONAL_ENTRY)
    "texture_alpha": (None, None),  # likewise
    "collision": (None, None),  # likewise
    "placements": (INT, None),  # likewise
    "placement_layers": (None, None),  # likewise
}
# [[extract.collision_class]]: a name for the collision surface classes
# whose 0xRRGGBB class, under mask, equals value; the first row that
# matches wins. colour, when given, replaces the class colour in
# extract --collision's debug mesh.
EXTRACT_COLLISION_CLASS = {
    "value": (INT, REQUIRED),
    "mask": (INT, 0xFFFFFF),
    "name": (STR, REQUIRED),
    "colour": (INT, None),
}
COLLISION_CLASS_NAME = re.compile(r"[a-z0-9_]+")
# Godot's import hints (resource_importer_scene.cpp): a node name ending
# in one after '-' or '_' becomes a body, a navmesh or nothing.
GODOT_HINT_WORDS = frozenset(
    "col colonly convcol convcolonly rigid noimp navmesh vehicle wheel occ occonly"
    " loop cycle vcol".split()
)
# [[extract.placement_set]]: one set of what a stage spawns at load
# (enemies, items, props). table: a VA of u32 pointers indexed by the
# stage's placements index plus each of layers; each points to a u32 count
# and a u32 pointer to stride-byte records, with the class dword, position
# and rotation (f32 radians) at the given offsets. The class dword is the
# one record shape the CLI reads: byte 0 the class, byte 1 the variant,
# byte 3 flags. pos_at and rot_at have no default: they are the game's.
# size and colour: the marker box drawn where a class has no model (half
# its edge; 0xRRGGBB, default by set order).
EXTRACT_PLACEMENT_SET = {
    "name": (STR, REQUIRED),
    "table": (INT, REQUIRED),
    "stride": (INT, REQUIRED),
    "layers": (INTS, [0]),
    "class_at": (INT, 0),
    "pos_at": (INT, REQUIRED),
    "rot_at": (INT, REQUIRED),
    "size": (FLOAT, 4.0),
    "colour": (INT, None),
}
# [[extract.placement_model]]: the model a set's class (the class dword's
# low byte) is drawn with. Either a variant table (table, one stride-byte
# record per variant; table_flagged for records whose flags byte is set),
# whose records hold the NJS_OBJECT and texlist pointers at object_at and
# texlist_at, or a fixed object and texlist. variants: the table's record
# count; a variant past it is drawn as a marker.
EXTRACT_PLACEMENT_MODEL = {
    "set": (STR, REQUIRED),
    "class": (INT, REQUIRED),
    "table": (INT, None),
    "table_flagged": (INT, None),
    "stride": (INT, 0),
    "object_at": (INT, 0),
    "texlist_at": (INT, 4),
    "variants": (INT, None),
    "object": (INT, None),
    "texlist": (INT, None),
}
EXTRACT_TRANSFORM_KEYS = ("scale", "translation", "backdrop")
EXTRACT_ANGLES = ("bams", "radians")
EXTRACT_UP = ("y", "-y")
EXTRACT_TEXTURED_COLOURS = ("diffuse", "white")
STAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# A node name ending in one of Godot's import hints ("-col", "_rigid",
# ...) becomes a body or is dropped on import: a placement set's top node
# must not read as one.
GODOT_HINT = re.compile(
    r"[-_$](noimp|col|colonly|convcol|convcolonly|navmesh|rigid|vcol|loop|cycle|occ|occonly)$",
    re.I,
)
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
    "textures.font",
    "textures.text",
    "textures.world",
    "present.shader",
    "menu.enabled",
    "overlay.stats",
    "mods",
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
    if kind == STRMAPS:
        return isinstance(v, dict) and all(
            isinstance(k, str) and _type_ok(STRMAP, x) for k, x in v.items()
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
                elif kind == STRMAPS:
                    dst[k] = {n: dict(x) for n, x in t[k].items()}
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
    if "saves" in m:
        m["saves"].setdefault("title_ids", [x["title_id"]])
    gj = m["golden"]
    gj.setdefault("frames", (gj["json"].rsplit("/", 1)[0] + "/frames") if gj["json"] else "")
    if "package" in m:
        m["package"].setdefault("product", g["slug"] + "-recomp")
    if "wizard" in m:
        m["wizard"].setdefault("targets", list(m.get("package", {}).get("targets", [])))
        m["wizard"]["fetch"] = [_entry(e, WIZARD_FETCH) for e in m["wizard"]["fetch"]]
        m["wizard"]["option"] = [_entry(e, WIZARD_OPTION) for e in m["wizard"]["option"]]
        m["wizard"]["credit"] = [_entry(e, WIZARD_CREDIT) for e in m["wizard"]["credit"]]
    if "extract" in m:
        stages = []
        for e in m["extract"]["stage"]:
            e = _entry(e, EXTRACT_STAGE)
            for k in ("root", "collision"):
                if isinstance(e.get(k), int) and not isinstance(e[k], bool):
                    e[k] = [e[k]]
            t = e.get("texlist")
            if isinstance(e.get("root"), list) and isinstance(t, int) and not isinstance(t, bool):
                e["texlist"] = [t] * len(e["root"])
            stages.append(e)
        m["extract"]["stage"] = stages
        m["extract"]["collision_class"] = [
            _entry(e, EXTRACT_COLLISION_CLASS) for e in m["extract"]["collision_class"]
        ]
        m["extract"]["placement_set"] = [
            _entry(e, EXTRACT_PLACEMENT_SET) for e in m["extract"]["placement_set"]
        ]
        m["extract"]["placement_model"] = [
            _entry(e, EXTRACT_PLACEMENT_MODEL) for e in m["extract"]["placement_model"]
        ]


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


def _check_extract(m):
    x = m.get("extract")
    if not x:
        return []
    errors = []
    if x["vertex_header"] < 8 or x["vertex_header"] % 4:
        errors.append("extract.vertex_header: 8 or more, a multiple of 4")
    if x["angles"] not in EXTRACT_ANGLES:
        errors.append("extract.angles: one of %s" % ", ".join(EXTRACT_ANGLES))
    if x["up"] not in EXTRACT_UP:
        errors.append("extract.up: one of %s" % ", ".join(EXTRACT_UP))
    if x["placement_world"] not in EXTRACT_UP:
        errors.append("extract.placement_world: one of %s" % ", ".join(EXTRACT_UP))
    if x["textured_colour"] not in EXTRACT_TEXTURED_COLOURS:
        errors.append("extract.textured_colour: one of %s" % ", ".join(EXTRACT_TEXTURED_COLOURS))
    if x["texname_size"] <= 0:
        errors.append("extract.texname_size: must be positive")
    if not all(
        isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 0xFFFFFF
        for c in x["collision_skip"]
    ):
        errors.append("extract.collision_skip: 0xRRGGBB colours")
    errors += _check_collision_classes(x["collision_class"])
    why = rel_path_problem(x["out"])
    if why:
        errors.append("extract.out: %r %s" % (x["out"], why))
    from .extract.textures import parse_source

    try:
        parse_source(x["textures"])
    except ValueError as e:
        errors.append("extract.textures: %s" % e)
    seen = set()
    for i, e in enumerate(x["stage"]):
        name = "extract.stage[%d]" % i
        errors += _entry_errors(name, e, EXTRACT_STAGE)
        n = e.get("name")
        if isinstance(n, str):
            if not STAGE_NAME.match(n):
                errors.append("%s.name: letters, digits, '_' and '-' only" % name)
            elif n in seen:
                errors.append("%s.name: %r twice" % (name, n))
            seen.add(n)
        r = e.get("root")
        if "root" in e and (
            not isinstance(r, list)
            or not r
            or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in r)
        ):
            errors.append("%s.root: a VA or a list of VAs" % name)
        t = e.get("texlist")
        if isinstance(r, list) and (
            not isinstance(t, list)
            or len(t) != len(r)
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in t)
        ):
            errors.append("%s.texlist: a VA, or one VA per root (0: none)" % name)
        if "transform" in e:
            errors += _check_transform(name, e["transform"], r, t)
        elif isinstance(r, list):
            errors += _check_repeats(name, r, t, None)
        if "texture_alpha" in e:
            errors += _check_texture_alpha(name, e["texture_alpha"])
        c = e.get("collision")
        if "collision" in e and (
            not isinstance(c, list)
            or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in c)
        ):
            errors.append("%s.collision: a VA or a list of VAs" % name)
        if "placements" in e:
            if not x["placement_set"]:
                errors.append("%s.placements: no [[extract.placement_set]] to index" % name)
            if _type_ok(INT, e["placements"]) and e["placements"] < 0:
                errors.append("%s.placements: an index of 0 or more" % name)
        if "placement_layers" in e:
            errors += _check_stage_layers(name, e, x["placement_set"])
    return errors + _check_placements(x)


def _check_stage_layers(name, e, sets):
    """A stage's placement_layers: a table of set name -> offsets, each
    offset once, the stage index plus each offset 0 or more."""
    v, name = e["placement_layers"], name + ".placement_layers"
    if "placements" not in e:
        return ["%s: needs placements on the same stage" % name]
    if not isinstance(v, dict) or not all(_type_ok(INTS, o) for o in v.values()):
        return ["%s: a table of set name = [offsets]" % name]
    errors = []
    known = {s.get("name") for s in sets}
    index = e["placements"] if _type_ok(INT, e["placements"]) else 0
    for k, offsets in v.items():
        if k not in known:
            errors.append("%s.%s: no [[extract.placement_set]] named %r" % (name, k, k))
        if len(set(offsets)) != len(offsets):
            errors.append("%s.%s: an offset twice" % (name, k))
        if any(index + o < 0 for o in offsets):
            errors.append("%s.%s: placements plus each offset must be 0 or more" % (name, k))
    return errors


def _entry_errors(name, e, spec):
    errors = []
    for k in e:
        if k not in spec:
            errors.append("%s.%s: unknown key" % (name, k))
    for k, (kind, default) in spec.items():
        if k not in e:
            if default is REQUIRED:
                errors.append("%s.%s: required" % (name, k))
        elif kind is not None and not _type_ok(kind, e[k]):
            errors.append("%s.%s: must be a %s" % (name, k, kind))
    return errors


def _check_collision_classes(rows):
    """[[extract.collision_class]]: 0xRRGGBB values, masks and colours, a
    value with no bits outside its mask, a name of [a-z0-9_] that is not
    and does not end in a Godot import hint (the node name ends in the
    colour token, which keeps it from reading as one; this is a second
    guard), and no row an earlier row covers (it could never match)."""
    errors, ok = [], []
    for i, e in enumerate(rows):
        name = "extract.collision_class[%d]" % i
        bad = _entry_errors(name, e, EXTRACT_COLLISION_CLASS)
        errors += bad
        if bad:
            continue
        for k in ("value", "mask", "colour"):
            if k in e and not 0 <= e[k] <= 0xFFFFFF:
                errors.append("%s.%s: a 0xRRGGBB value" % (name, k))
        if e["value"] & ~e["mask"]:
            errors.append("%s.value: bits outside mask 0x%06X" % (name, e["mask"] & 0xFFFFFF))
        if not COLLISION_CLASS_NAME.fullmatch(e["name"]):
            errors.append("%s.name: lower-case letters, digits and '_' only" % name)
        elif e["name"].rsplit("_", 1)[-1] in GODOT_HINT_WORDS:
            errors.append("%s.name: %r ends in a Godot import hint" % (name, e["name"]))
        for j, f in ok:
            # Row j matches every class this row does: its mask is no wider
            # and the value agrees on its bits.
            if not f["mask"] & ~e["mask"] and e["value"] & f["mask"] == f["value"]:
                errors.append(
                    "%s: never matches, extract.collision_class[%d] covers it" % (name, j)
                )
                break
        ok.append((i, e))
    return errors


def _check_placements(x):
    """[[extract.placement_set]] and [[extract.placement_model]]: unique
    set names, sizes that fit a record, and a model entry that names a set,
    with either a variant table or a fixed object."""
    errors, sets = [], {}
    for i, e in enumerate(x["placement_set"]):
        name = "extract.placement_set[%d]" % i
        bad = _entry_errors(name, e, EXTRACT_PLACEMENT_SET)
        errors += bad
        if bad:
            continue
        if not STAGE_NAME.match(e["name"]):
            errors.append("%s.name: letters, digits, '_' and '-' only" % name)
        elif GODOT_HINT.search(e["name"]):
            errors.append("%s.name: %r ends in a Godot import hint" % (name, e["name"]))
        elif e["name"] in sets:
            errors.append("%s.name: %r twice" % (name, e["name"]))
        sets[e["name"]] = e
        if e["table"] <= 0 or e["stride"] <= 0:
            errors.append("%s: table and stride must be positive" % name)
        for k, n in (("class_at", 4), ("pos_at", 12), ("rot_at", 12)):
            if not 0 <= e[k] <= e["stride"] - n:
                errors.append("%s.%s: %d bytes inside the stride" % (name, k, n))
        if any(v < 0 for v in e["layers"]) or not e["layers"]:
            errors.append("%s.layers: offsets of 0 or more" % name)
        elif len(set(e["layers"])) != len(e["layers"]):
            errors.append("%s.layers: an offset twice" % name)
        if e["size"] <= 0:
            errors.append("%s.size: must be positive" % name)
        if "colour" in e and not 0 <= e["colour"] <= 0xFFFFFF:
            errors.append("%s.colour: a 0xRRGGBB colour" % name)
    seen = set()
    for i, e in enumerate(x["placement_model"]):
        name = "extract.placement_model[%d]" % i
        bad = _entry_errors(name, e, EXTRACT_PLACEMENT_MODEL)
        errors += bad
        if bad:
            continue
        if e["set"] not in sets:
            errors.append("%s.set: no [[extract.placement_set]] named %r" % (name, e["set"]))
        if not 0 <= e["class"] <= 0xFF:
            errors.append("%s.class: the class byte, 0 to 0xFF" % name)
        if (e["set"], e["class"]) in seen:
            errors.append("%s: class 0x%02X of %s twice" % (name, e["class"], e["set"]))
        seen.add((e["set"], e["class"]))
        for k in ("table", "table_flagged", "object", "texlist"):
            if k in e and e[k] < 0:
                errors.append("%s.%s: a VA, 0 or more" % (name, k))
        fixed = "object" in e
        if fixed == ("table" in e):
            errors.append("%s: a table or an object, one of the two" % name)
        elif fixed:
            if "texlist" not in e:
                errors.append("%s.texlist: required with object (0: none)" % name)
            for k in ("table_flagged", "variants"):
                if k in e:
                    errors.append("%s.%s: only with table, not object" % (name, k))
        elif e["stride"] <= 0:
            errors.append("%s.stride: required with table" % name)
        else:
            for k in ("object_at", "texlist_at"):
                if not 0 <= e[k] <= e["stride"] - 4:
                    errors.append("%s.%s: 4 bytes inside the stride" % (name, k))
            if "variants" in e and e["variants"] <= 0:
                errors.append("%s.variants: must be positive" % name)
    return errors


def _check_texture_alpha(name, ta):
    """[[extract.stage]] texture_alpha: texture names to an opacity from 0
    to 1. Names match texlist names in any case, so two spellings of one
    name would give it two opacities."""
    at = "%s.texture_alpha" % name
    if not isinstance(ta, dict):
        return ["%s: a table of texture names to an opacity from 0 to 1" % at]
    errors, seen = [], {}
    for k, v in ta.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
            errors.append("%s.%s: an opacity from 0 to 1" % (at, k))
        if k.lower() in seen:
            errors.append("%s.%s: the same texture as %s" % (at, k, seen[k.lower()]))
        seen.setdefault(k.lower(), k)
    return errors


def _vec3(v, nonzero=False):
    return (
        isinstance(v, list)
        and len(v) == 3
        and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v
        )
        and (not nonzero or all(x != 0 for x in v))
    )


def _check_transform(name, t, roots, texlists):
    """[[extract.stage]] transform: one table per root, each with an
    optional scale (no zero axis) and translation, three numbers each."""
    n = len(roots) if isinstance(roots, list) else None
    if not isinstance(t, list) or (n is not None and len(t) != n):
        return ["%s.transform: one table per root ({} for none)" % name]
    errors = []
    for i, x in enumerate(t):
        at = "%s.transform[%d]" % (name, i)
        if not isinstance(x, dict):
            errors.append("%s: a table with scale and/or translation" % at)
            continue
        for k in x:
            if k not in EXTRACT_TRANSFORM_KEYS:
                errors.append("%s.%s: unknown key" % (at, k))
        if "scale" in x and not _vec3(x["scale"], nonzero=True):
            errors.append("%s.scale: three finite non-zero numbers" % at)
        if "translation" in x and not _vec3(x["translation"]):
            errors.append("%s.translation: three finite numbers" % at)
        if "backdrop" in x:
            b = x["backdrop"]
            if not _vec3(b) or not all(0 <= c <= 1 for c in b):
                errors.append("%s.backdrop: an RGB colour, three numbers from 0 to 1" % at)
            # The box closes the mirrored side and leaves the plane open,
            # so it needs exactly one mirrored axis to know which is which.
            # A scale that failed above has its own error already.
            sc = x.get("scale")
            bad_scale = "scale" in x and not _vec3(sc, nonzero=True)
            if not bad_scale and (sc is None or sum(c < 0 for c in sc) != 1):
                errors.append("%s.backdrop: needs a scale that mirrors one axis" % at)
    if errors or n is None:
        return errors
    return _check_repeats(name, roots, texlists, t)


def _xf_key(x):
    """A transform table as the matrix it gives: absent keys are the
    identity, so {} and {scale = [1, 1, 1]} are the same draw."""
    return (
        tuple(float(v) for v in x.get("scale", (1, 1, 1))),
        tuple(float(v) for v in x.get("translation", (0, 0, 0))),
    )


def _check_repeats(name, roots, texlists, t):
    """A root listed again must be drawn differently: the same root,
    texlist and transform twice writes the same geometry twice, one copy
    z-fighting the other."""
    if not isinstance(texlists, list) or len(texlists) != len(roots):
        return []  # reported as a texlist error
    if not all(isinstance(v, int) for v in roots):
        return []  # reported as a root error
    errors, seen = [], {}
    for i, key in enumerate(zip(roots, texlists, (_xf_key(x) for x in t or [{}] * len(roots)))):
        if key in seen:
            errors.append(
                "%s.root[%d]: 0x%08X drawn again with the same texlist and transform as root[%d]"
                % (name, i, key[0], seen[key])
            )
        else:
            seen[key] = i
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
    whose build.stock_cmake does not set -DXBOXRECOMP_ENHANCE=ON (an empty
    list, as older game.toml files had) builds without it even on a toolkit
    that has src/enhance. CMake's truth, the last -D or -U winning, as
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
    langs = m["system"]["languages"]
    for code in langs:
        if code not in LANGUAGES:
            errors.append("system.languages: %r is not one of %s" % (code, " ".join(LANGUAGES)))
    if len(set(langs)) != len(langs):
        errors.append("system.languages: a code is listed twice")
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
    tex = m.get("enhance", {}).get("textures")
    if tex:
        for src in tex["sources"]:
            if src not in TEXTURE_SOURCES:
                errors.append(
                    "enhance.textures.sources: %r is not one of %s"
                    % (src, ", ".join(TEXTURE_SOURCES))
                )
        for arc in tex["archives"]:
            why = rel_path_problem(arc)
            if why:
                errors.append("enhance.textures.archives: %r %s" % (arc, why))
        if tex["hook"]:
            why = rel_path_problem(tex["hook"])
            if why:
                errors.append("enhance.textures.hook: %r %s" % (tex["hook"], why))
        elif "hook" in tex["sources"] and not m["enhance"].get("hud", {}).get("hook"):
            errors.append('enhance.textures.hook: required with "hook" in sources')
        for c in tex["classes"]:
            if c not in TEXTURE_CLASS_KEYS:
                errors.append(
                    "enhance.textures.classes.%s: one of %s" % (c, ", ".join(TEXTURE_CLASS_KEYS))
                )
        for p in tex["presets"]:
            if p in ("hud", "traced"):
                errors.append("enhance.textures.presets.%s: fixed, not redefined" % p)
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
        if hud["scale"] not in (2, 3, 4):
            errors.append("enhance.hud.scale: 2, 3 or 4")
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
    errors += _check_mods(m)
    errors += _check_extract(m)
    errors += _check_saves(m)
    for s in m["xbe"]["sha256"]:
        if not re.match(r"^[0-9a-f]{64}$", s):
            errors.append("xbe.sha256: %r is not a sha256" % s)
    for name, s in m["xbe"]["sections"].items():
        if not re.match(r"^[0-9a-f]{64}$", s):
            errors.append("xbe.sections.%s: %r is not a sha256" % (name, s))
    for k in ("region", "version"):
        v = m["xbe"][k]
        if v is not None and not 0 <= v <= 0xFFFFFFFF:
            errors.append("xbe.%s: %r is not a 32-bit certificate field" % (k, v))
    for t in m["xbe"]["alt_title_ids"]:
        if not 0 < t <= 0xFFFFFFFF:
            errors.append("xbe.alt_title_ids: %r is not a title ID" % t)
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


SAVES_SIGNED_KEYS = ("glob", "size", "data", "digest", "key")
SAVES_SIGN_KEYS = ("roamable",)
SAVES_DIGEST = 20  # HMAC-SHA1


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_saves(m):
    """[saves] (openspec save-import D3): every recipe must describe one
    file layout the importer can re-sign without touching its own data."""
    s = m.get("saves")
    if not s:
        return []
    errors = []
    if not s["title_ids"]:
        errors.append("saves.title_ids: name at least one title ID")
    for t in s["title_ids"]:
        if not 0 < t <= 0xFFFFFFFF:
            errors.append("saves.title_ids: %r is not a 32-bit title ID" % t)
    if s["signing"] not in ("", "resign", "none"):
        errors.append('saves.signing: "resign" or "none" (or unset: not known)')
    if s["tdata"] not in ("ignore", "optional", "required"):
        errors.append('saves.tdata: one of "ignore", "optional", "required"')
    if s["signing"] == "resign" and not s["signed"]:
        errors.append('saves.signed: signing = "resign" needs at least one [[saves.signed]]')
    if s["signed"] and s["signing"] != "resign":
        errors.append('saves.signed: only with signing = "resign"')
    by_glob = {}
    for i, e in enumerate(s["signed"]):
        name = "saves.signed[%d]" % i
        bad = sorted(set(e) - set(SAVES_SIGNED_KEYS))
        if bad:
            errors.append("%s: unknown key %s" % (name, ", ".join(bad)))
        missing = [k for k in SAVES_SIGNED_KEYS if k not in e]
        if missing:
            errors.append("%s: %s required" % (name, ", ".join(missing)))
            continue
        g, size, data, dg = e["glob"], e["size"], e["data"], e["digest"]
        if (
            not isinstance(g, str)
            or not g
            or g.startswith("/")
            or "\\" in g
            or ".." in g.split("/")
        ):
            errors.append("%s.glob: a pattern below UDATA/<tid>/, with /, no '..'" % name)
            continue
        if not _int(size) or size <= 0:
            errors.append("%s.size: a positive byte count" % name)
            continue
        if not (isinstance(data, list) and len(data) == 2 and all(_int(x) for x in data)) or not (
            0 <= data[0] < data[1] <= size
        ):
            errors.append("%s.data: [start, end] inside the file, start < end" % name)
            continue
        if not _int(dg) or dg < 0 or dg + SAVES_DIGEST > size:
            errors.append("%s.digest: an offset with 20 bytes inside the file" % name)
            continue
        if e["key"] not in SAVES_SIGN_KEYS:
            errors.append('%s.key: "roamable"' % name)
        by_glob.setdefault(g.lower(), []).append((name, size, data, dg))
    for rs in by_glob.values():
        if len({r[1] for r in rs}) > 1:
            errors.append("%s: entries for one glob must share size" % rs[0][0])
        for name, _, _, dg in rs:
            for other, _, data, _ in rs:
                if dg < data[1] and data[0] < dg + SAVES_DIGEST:
                    errors.append("%s.digest: inside %s's data range" % (name, other))
            for other, _, _, dg2 in rs:
                if other != name and abs(dg - dg2) < SAVES_DIGEST:
                    errors.append("%s.digest: overlaps %s's" % (name, other))
    return errors


# A hook_functions entry: a VA, or a [mods.signatures] row by name.
HOOK_VA = re.compile(r"^0x[0-9A-Fa-f]{1,8}$")
HOOK_SIG = re.compile(r"^sig:([A-Za-z0-9_.-]+)$")
SIG_KEYS = ("pattern", "section")


def _check_mods(m):
    from . import hooks

    errors = []
    mods = m["mods"]
    seen = set()
    for e in mods["hook_functions"]:
        if HOOK_VA.match(e):
            key = int(e, 16)
        elif HOOK_SIG.match(e):
            key = e
            if e[4:] not in mods["signatures"]:
                errors.append("mods.hook_functions: %r has no [mods.signatures.%s]" % (e, e[4:]))
        else:
            errors.append("mods.hook_functions: %r is neither a VA (0x...) nor sig:<name>" % e)
            continue
        if key in seen:
            errors.append("mods.hook_functions: %r is listed twice" % e)
        seen.add(key)
    for key in ("st0", "no_st0"):
        for e in mods[key]:
            if e not in mods["hook_functions"]:
                errors.append("mods.%s: %r is not in hook_functions" % (key, e))
    for e in sorted(set(mods["st0"]) & set(mods["no_st0"])):
        errors.append("mods: %r is in both st0 and no_st0" % e)
    for name, row in mods["signatures"].items():
        where = "mods.signatures.%s" % name
        if not HOOK_SIG.match("sig:" + name):
            errors.append("%s: a name of letters, digits, '_', '.' and '-'" % where)
        for k in sorted(set(row) - set(SIG_KEYS)):
            errors.append("%s.%s: unknown key" % (where, k))
        for k in SIG_KEYS:
            if not row.get(k):
                errors.append("%s.%s: required" % (where, k))
        if row.get("pattern"):
            try:
                hooks.pattern_regex(row["pattern"])
            except ValueError as e:
                errors.append("%s.pattern: %s" % (where, e))
    return errors


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
    for k in ("spin_waits", "exclude_manual", "analysis_json", "symbols", "names", "releases"):
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
        # The game's symbol map (docs/symbols.md): names and notes for gen/.
        self.symbols = j(p["symbols"]) if p["symbols"] else ""
        # The port's names (docs/names.md): one per address or value the
        # hand-written side uses, resolved per release through releases/.
        self.names = j(p["names"]) if p["names"] else ""
        self.releases = j(p["releases"])
        # analyze's resolved hook slots, beside the seeds; recomp reads it.
        self.hook_functions = j("config", "hook_functions.json")
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
