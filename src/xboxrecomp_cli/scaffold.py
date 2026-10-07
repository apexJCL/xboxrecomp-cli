"""new: a starter game in an empty directory, so that porting a third game
starts with one command instead of copying files out of another game.

  xbr new DIR [--slug S] [--name N] [--xbe PATH] [--offline] ...

It writes the manifest with every pin filled in, the bootstrap and its two
wrappers, the tools pyproject.toml, .gitignore, .gitattributes, README.md,
and the toolkit's templates/new-game/ with the game's constants patched
in; then, when the network, git and uv allow, it clones the toolkit at the
pin and writes config/setup-pins.json and uv.lock. Whatever cannot be done
is named as the command that finishes it. It never overwrites a file and
never copies, moves or links a dump: --xbe reads a header, and the dump
goes into game_files/ by the developer's own hand.

This module runs before a game is loaded (main.py dispatches `new` first);
once game.toml is written it loads the scaffold as the current game and
uses the same code setup and pins refresh use.
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from importlib import metadata

from . import cli_dir, host, manifest, pins, toolkit, wrapper, xbe
from .host import CliError

# The toolkit every game the CLI builds pins: the fork's integration branch.
TOOLKIT_URL = "https://github.com/apexJCL/xboxrecomp.git"
TOOLKIT_BRANCH = "blinx2/portability"
# The llvm-mingw release the games pin today; `pins refresh` fetches its
# hashes, and a game moves it in game.toml.
LLVM_MINGW = "20260922"
ZERO = "0" * 40
TEMPLATE_DIR = os.path.join("templates", "new-game")

# ── the files ────────────────────────────────────────────────────────────
#
# @KEY@ placeholders, as the packaging templates use. Every path a template
# names comes from the manifest written first, so the files agree with it.

GAME_TOML = """\
# game.toml: what xboxrecomp-cli needs to know about @NAME@. The CLI reads
# it on every command; every key and its default is in the CLI's
# docs/manifest.md. Written by `xbr new`; the CLI never writes it again.
schema = 1

[game]
name = "@NAME@"
slug = "@SLUG@"

[xbe]
path = "game_files/default.xbe"
# The certificate's title ID: `package` refuses a dump with another one.
title_id = @TITLE_ID@
# Known dumps (sha256 of default.xbe); empty accepts any dump.
sha256 = []

[cli]
# The xboxrecomp-cli commit ./@SLUG@ runs: the bootstrap clones it at this
# commit when it finds none. `@SLUG@ pins refresh` prints the newest head.
commit = "@CLI_COMMIT@"
url = "@CLI_URL@"

[toolkit]
# `@SLUG@ setup` clones it into external/xboxrecomp at this commit.
url = "@TOOLKIT_URL@"
branch = "@TOOLKIT_BRANCH@"
commit = "@TOOLKIT_COMMIT@"

[toolchain]
# The llvm-mingw release the windows target cross-compiles with, on every host.
llvm_mingw = "@LLVM_MINGW@"

[pipeline]
# Stamped into every generated file: fixed for the life of the game.
game_name = "@NAME@"
gen = "src/recomp/gen"
out = "analysis"
# Function starts the disassembler missed (ICALL failures at runtime).
seeds = ["config/seed_functions.json"]
# The toolkit's icall_feedback database belongs to another game: off.
icall_seeds = false
exclude_manual = "src/recomp_manual.c"

[pipeline.disasm]
# Only .text. A title with code in its XDK sections names them, for example
# extra_sections = ["D3D", "DSOUND", "XPP"].
text_only = true

[build]
# The CMake target and file name: build-win/@EXE@.exe
exe = "@EXE@"
# windows builds on every host (llvm-mingw). macos needs a main.c with the
# POSIX host code: the CLI's README, "macOS as a target".
targets = ["windows"]
# No enhancements layer: the stock build is the plain one.
stock_cmake = []

[data]
game_files = "game_files"

# Packaging (a private bundle with your own dump inside) is opt-in:
# [package]
# app = "@APP@"
# targets = ["windows", "steamos"]
# with packaging/launch.env.default.windows and packaging/windows/README.part.
# See docs/manifest.md, [package].
"""

PYPROJECT = """\
# The Python environment for the toolkit's tools and the build. ./@SLUG@
# (@SLUG@.py, standard library only) runs xboxrecomp-cli at the commit
# game.toml pins; `@SLUG@ setup` makes .venv/ from uv.lock (`uv sync
# --locked`) for cmake, ninja, capstone and pefile, and with --dev the
# tests and ruff. `@SLUG@ pins refresh` rewrites the lock.

[project]
name = "@SLUG@"
version = "0"
description = "@NAME@ static recompilation: the tools environment (the CLI is xboxrecomp-cli)"
requires-python = ">=3.9"
dependencies = [
  "cmake",
  "ninja",
  # The lifter decodes with capstone: a different version could change gen/.
  "capstone==5.0.9",
  "pefile==2024.8.26",
]

[dependency-groups]
dev = ["pytest", "ruff"]

[tool.uv]
package = false   # a plain environment: nothing here is installed as a project
# Wheels only: nothing builds from an sdist at install time.
no-build = true
"""

GITIGNORE = """\
# Written by `xbr new`. Game data, generated code, the toolchain and the
# bundles never enter git.

# Your dump, and the analysis the pipeline writes beside it.
@GAME_FILES@/

# Generated code and its key (`@SLUG@ package` regenerates when it differs).
@GEN@/
@GEN_PARENT@/gen.key.json
@GEN_PARENT@/.gen-regenerating

# The pipeline's intermediates.
@OUT@/

# Builds, and the packaging build trees.
@WINDOWS_DIR@/
@MACOS_DIR@/
build-*/
build-logs/

# `@SLUG@ setup`: the pinned venv, the toolchain, the toolkit and CLI clones.
.venv/
.venv-ghidra/
third_party/
external/
.xbr-pin

# `@SLUG@ package`: private bundles holding the game. Never commit them.
dist/
*.dmg
*.exe
*.pdb
*.nsi
*.tar

# The bench host.
bench-logs/
scripts/bench.env

# Game data, captures and game output, anywhere in the tree.
*.xbe
*.iso
*.xiso
*.xpr
*.xwb
*.adx
*.sfd
*.ipk
*.wav
*.bmp
*.png
# Screenshots of your own build for the README are the one exception.
!docs/images/*.png
*.ico
*.icns
*.ppm
*.raw
*.sav
*.xsv
UDATA/
TDATA/
saves/

*.log
__pycache__/
.ruff_cache/
.DS_Store
"""

GITATTRIBUTES = """\
# Line endings that must survive a Windows checkout (core.autocrlf=true):
# the shell wrapper and the files a bundle carries to Linux and macOS run
# there, and a CR breaks them. cmd.exe wants CRLF.
*.sh      text eol=lf
*.py      text eol=lf
*.in      text eol=lf
*.toml    text eol=lf
@SLUG@    text eol=lf
*.cmd     text eol=crlf
"""

SH_WRAPPER = """\
#!/bin/sh
# @SLUG@: the project CLI (@SLUG@.py). Python 3.9 or newer.
exec python3 "$(dirname "$0")/@SLUG@.py" "$@"
"""

# Written with CRLF (cmd.exe misreads an if block ending in a bare LF).
CMD_WRAPPER = """\
@echo off
rem @SLUG@: the project CLI (@SLUG@.py). Python 3.9 or newer.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0@SLUG@.py" %*
) else (
    python "%~dp0@SLUG@.py" %*
)
exit /b %errorlevel%
"""

README = """\
# @NAME@ static recompilation

A port of the original Xbox game *@NAME@* to native code with the
[xboxrecomp](https://github.com/apexJCL/xboxrecomp) toolkit, driven by
[xboxrecomp-cli](https://github.com/apexJCL/xboxrecomp-cli). Started with
`xbr new`.

**No game data here, and none ever will be.** Put your own dump of the disc
(`default.xbe` and the game's files) in `game_files/`; git ignores it.

```sh
./@SLUG@ setup      # once: .venv, llvm-mingw, the toolkit (each download sha256-pinned)
./@SLUG@ all        # analyze (parse, disasm, funcid, abi), recomp, build
./@SLUG@ doctor     # what this machine has, and what is next
```

Windows: `@SLUG@` (the `@SLUG@.cmd` wrapper) or `py -3 @SLUG@.py`, from a
short checkout path such as `C:\\g\\@SLUG@`, with
`git config --global core.longpaths true`.

The first build is `@WINDOWS_DIR@/@EXE@.exe`, cross-compiled with llvm-mingw on
every host. It will crash the first time: that is where the port starts.
`src/recomp_manual.c` takes hand-written overrides, `config/seed_functions.json`
the function starts the disassembler missed, and `./@SLUG@ analyze` then
`./@SLUG@ recomp` regenerate `src/recomp/gen/` (never edit it by hand).
The CLI's README has the loop; `game.toml` has every setting, documented in
the CLI's `docs/manifest.md`.
"""

# What `new` patches in the toolkit's template: (file, old, new, what to do
# by hand if the template changed, every occurrence?). A patch for one
# place must match exactly once; one marked for every occurrence (the
# game's name, which the template prints and comments) needs at least one.
ALL = True
TEMPLATE_PATCHES = (
    (
        "CMakeLists.txt",
        "project(your_game_recomp C)",
        "project(@EXE@ C)",
        "set project() to @EXE@ (the exe name game.toml builds)",
    ),
    ("CMakeLists.txt", "YOUR_GAME_NAME", "@NAME@", "", ALL),
    (os.path.join("src", "main.c"), "YOUR_GAME_NAME", "@NAME@", "", ALL),
    (
        os.path.join("src", "main.c"),
        "#define YOUR_GAME_ENTRY_POINT   0x00000000",
        "#define YOUR_GAME_ENTRY_POINT   0x@ENTRY@",
        "set YOUR_GAME_ENTRY_POINT to 0x@ENTRY@ (the XBE entry point)",
    ),
    (
        os.path.join("src", "main.c"),
        '#define YOUR_GAME_XBE_PATH      "game\\\\Your Game Title\\\\default.xbe"',
        '#define YOUR_GAME_XBE_PATH      "@XBE_PATH@"',
        "set YOUR_GAME_XBE_PATH to @XBE_PATH@",
    ),
    (
        os.path.join("src", "main.c"),
        '#define YOUR_GAME_DIR            "game\\\\Your Game Title"',
        '#define YOUR_GAME_DIR            "@GAME_FILES@"',
        "set YOUR_GAME_DIR to @GAME_FILES@",
    ),
    (
        os.path.join("src", "main.c"),
        " *   Title ID:    0x00000000",
        " *   Title ID:    0x@TITLE_ID_HEX@",
        "",
    ),
    (
        os.path.join("src", "main.c"),
        " *   Entry point: 0x00000000",
        " *   Entry point: 0x@ENTRY@",
        "",
    ),
)
# A declaration the template lacks and Clang (llvm-mingw) requires; MSVC
# only warned. Added after the entry point's declaration when the file has
# none. The toolkit's own fix makes this a no-op.
DISPATCH_DECL = (
    "extern void xbe_entry_point(void);\n",
    "extern void xbe_entry_point(void);\n\n"
    "/* Flat dispatch table builder (recomp_dispatch.c): declared here because\n"
    " * main.c does not include recomp_types.h, and Clang rejects the implicit\n"
    " * declaration MSVC only warned about. */\n"
    "extern int recomp_dispatch_init(void);\n",
)


def fill(text, values):
    for k, v in values.items():
        text = text.replace("@%s@" % k, str(v))
    return text


def write(path, text, crlf=False, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        raise CliError("%s exists; new never overwrites a file" % path)
    with open(path, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ── the pins ─────────────────────────────────────────────────────────────


def installed_commit():
    """The commit uv installed this package from (PEP 610 direct_url.json,
    what `uvx --from git+URL` records), or ''."""
    try:
        text = metadata.distribution("xboxrecomp-cli").read_text("direct_url.json")
        return (json.loads(text or "{}").get("vcs_info") or {}).get("commit_id", "") or ""
    except Exception:  # noqa: BLE001  (no distribution, no file, not JSON: the same answer)
        return ""


def resolve_cli_commit(override="", offline=False):
    """(commit, how): the checkout this runs from, else the installed
    distribution's, else the remote's main, else zeros."""
    if override:
        return override, "--cli-commit"
    head, dirty = cli_dir.tree_state()
    if head:
        return head, "this checkout" + (" (uncommitted changes)" if dirty else "")
    c = installed_commit()
    if c:
        return c, "the installed distribution"
    if not offline:
        c = pins.remote_head(pins.CLI_URL, pins.CLI_BRANCH)
        if c:
            return c, "%s %s" % (pins.CLI_URL, pins.CLI_BRANCH)
    return ZERO, "unresolved: fill in [cli] commit"


def git_branch(d):
    r = subprocess.run(
        ["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else ""


def toolkit_checkout(root, environ=None):
    """A toolkit checkout the game would use as it is: $XBOXRECOMP_DIR,
    else ../xboxrecomp beside the game (toolkit.toolkit_dir's order,
    without external/, which does not exist yet)."""
    environ = os.environ if environ is None else environ
    if environ.get("XBOXRECOMP_DIR"):
        return os.path.abspath(environ["XBOXRECOMP_DIR"])
    beside = os.path.abspath(os.path.join(root, "..", "xboxrecomp"))
    return beside if os.path.isdir(os.path.join(beside, "tools")) else ""


def resolve_toolkit_commit(root, url, branch, override="", offline=False):
    if override:
        return override, "--toolkit-commit"
    d = toolkit_checkout(root)
    if d and host.git_head(d):
        on = git_branch(d)
        return host.git_head(d), d + ("" if on in (branch, "") else " (not on %s)" % branch)
    if not offline:
        c = pins.remote_head(url, branch)
        if c:
            return c, "%s %s" % (url, branch)
    return ZERO, "unresolved: fill in [toolkit] commit"


# ── the scaffold ─────────────────────────────────────────────────────────


def clean_name(title, slug):
    """A certificate title as a TOML basic string the bootstrap's line
    parser reads: no backslash, quote or control character (LINE_KEYS
    forbids escapes), else the slug."""
    return re.sub(r'[\\\x00-\x1f\x7f"]', " ", title).strip() or slug


def slug_from(path):
    s = re.sub(r"[^a-z0-9-]+", "-", os.path.basename(os.path.abspath(path)).lower()).strip("-")
    return s or "game"


def xbe_values(path):
    """What the dump's header gives the manifest and main.c, or {} with the
    reason when there is no readable XBE."""
    if not path or not os.path.isfile(path):
        return (
            {},
            "no XBE read: title_id is 0 and the entry point 0x00000000 until you fill them in",
        )
    try:
        h = xbe.read_file(path)
    except (OSError, ValueError) as e:
        return {}, "%s: %s; title_id and the entry point are left to fill in" % (path, e)
    return h, "%s: title %r, title ID 0x%08X, entry point 0x%08X (%s)" % (
        path,
        h["title"],
        h["title_id"],
        h["entry"],
        h["kind"],
    )


def check_dir(root):
    """Absent, empty, or holding only game_files/ (the dump put there
    first): nothing of ours is overwritten either way."""
    if os.path.exists(root):
        if not os.path.isdir(root):
            raise CliError("%s exists and is not a directory" % root)
        if set(os.listdir(root)) - {"game_files", ".DS_Store"}:
            raise CliError(
                "%s is not empty; new writes into an empty or absent directory "
                "(game_files/ with your dump may already be there)" % root
            )


def patch_template(src, dst, values):
    """Copy the toolkit's templates/new-game into the game and patch the
    places it marks for editing. Returns the edits left to make by hand
    (a template that changed): the copy lands either way."""
    notes = []
    for rel in (
        "CMakeLists.txt",
        os.path.join("src", "main.c"),
        os.path.join("src", "recomp_manual.c"),
    ):
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        if not os.path.isfile(s):
            notes.append("the toolkit template has no %s: copy one from another game" % rel)
            continue
        with open(s, encoding="utf-8", newline="") as f:
            text = f.read()
        for file, old, new, hint, *every in TEMPLATE_PATCHES:
            if file != rel:
                continue
            n = text.count(old)
            if n == 1 or (every and n):
                text = text.replace(old, fill(new, values))
            elif hint:
                notes.append("%s: %s (the template changed)" % (rel, fill(hint, values)))
        if rel.endswith("main.c") and "recomp_dispatch_init(void)" not in text:
            if text.count(DISPATCH_DECL[0]) == 1:
                text = text.replace(*DISPATCH_DECL)
            else:
                notes.append("src/main.c: declare `extern int recomp_dispatch_init(void);`")
        write(d, text)
    return notes


def scaffold(root, a):
    """Write the files; return (values, notes) for the summary."""
    slug = a.slug or slug_from(root)
    if not manifest.SLUG.match(slug):
        raise CliError("slug %r: lower-case letters, digits and '-' only (--slug)" % slug)
    check_dir(root)
    xbe_path = a.xbe or os.path.join(root, "game_files", "default.xbe")
    h, xbe_note = xbe_values(xbe_path)
    name = clean_name(a.name or h.get("title") or "", slug)
    cli_commit, cli_how = resolve_cli_commit(a.cli_commit, a.offline)
    tk_commit, tk_how = resolve_toolkit_commit(
        root, a.toolkit_url, a.toolkit_branch, a.toolkit_commit, a.offline
    )
    exe = slug.replace("-", "_") + "_recomp"
    v = {
        "NAME": name,
        "SLUG": slug,
        "EXE": exe,
        "APP": re.sub(r"[^A-Za-z0-9]", "", name.title()) or slug,
        "TITLE_ID": "0x%08X" % h.get("title_id", 0),
        "TITLE_ID_HEX": "%08X" % h.get("title_id", 0),
        "ENTRY": "%08X" % h.get("entry", 0),
        "CLI_COMMIT": cli_commit,
        "CLI_URL": pins.CLI_URL,
        "TOOLKIT_URL": a.toolkit_url,
        "TOOLKIT_BRANCH": a.toolkit_branch,
        "TOOLKIT_COMMIT": tk_commit,
        "LLVM_MINGW": a.llvm_mingw,
        "GAME_FILES": "game_files",
        "XBE_PATH": "game_files/default.xbe",
        "GEN": "src/recomp/gen",
        "GEN_PARENT": "src/recomp",
        "OUT": "analysis",
        "WINDOWS_DIR": "build-win",
        "MACOS_DIR": "build",
    }
    notes = [xbe_note, "cli commit %s: %s" % (cli_commit[:12], cli_how)]
    notes.append("toolkit commit %s: %s" % (tk_commit[:12], tk_how))

    def j(*p):
        return os.path.join(root, *p)

    # Validated before the first write: a manifest the CLI would refuse
    # must not be left on disk for the retry to find "not empty".
    text = fill(GAME_TOML, v)
    manifest.parse_text(text, "game.toml")
    write(j("game.toml"), text)
    G = manifest.use(manifest.load(root))
    write(j(slug + ".py"), wrapper.template_text())
    write(j(slug), fill(SH_WRAPPER, v), executable=True)
    write(j(slug + ".cmd"), fill(CMD_WRAPPER, v), crlf=True)
    write(j("pyproject.toml"), fill(PYPROJECT, v))
    write(j(".gitignore"), fill(GITIGNORE, v))
    write(j(".gitattributes"), fill(GITATTRIBUTES, v))
    write(j("README.md"), fill(README, v))
    write(j("config", "seed_functions.json"), "[]\n")
    os.makedirs(G.game_files, exist_ok=True)
    os.makedirs(G.gen, exist_ok=True)
    return v, notes


def finish(root, v, a, notes):
    """The toolkit clone, the template copy, the pins and the lock: each
    one needs the network, git or uv, and each failure becomes the command
    that does it later."""
    slug = v["SLUG"]
    cmd = slug if host.host_os() == "windows" else "./" + slug
    later = []
    # A checkout the game would use ($XBOXRECOMP_DIR, ../xboxrecomp) needs
    # no network; clone_toolkit leaves it as it is too.
    tk = toolkit.toolkit_dir()
    if not os.path.isdir(os.path.join(tk, "tools")):
        tk = ""
        if a.offline:
            later.append("'%s setup' clones the toolkit (offline now)" % cmd)
        else:
            try:
                tk = toolkit.clone_toolkit()
            except CliError as e:
                later.append("'%s setup' clones the toolkit (%s)" % (cmd, e))
    if tk:
        notes += patch_template(os.path.join(tk, TEMPLATE_DIR), root, v)
    else:
        later.append(
            "then copy external/xboxrecomp/%s/ (CMakeLists.txt, src/) here and set "
            "project(%s C), YOUR_GAME_ENTRY_POINT 0x%s, YOUR_GAME_XBE_PATH %s, "
            "YOUR_GAME_DIR %s, and declare recomp_dispatch_init in src/main.c"
            % (TEMPLATE_DIR, v["EXE"], v["ENTRY"], v["XBE_PATH"], v["GAME_FILES"])
        )
    if a.offline:
        later.append(
            "'%s pins refresh' writes config/setup-pins.json and uv.lock (offline now)" % cmd
        )
    else:
        try:
            pins.pins_refresh()
        except (CliError, OSError, ValueError) as e:
            later.append(
                "'%s pins refresh' writes config/setup-pins.json and uv.lock (%s)" % (cmd, e)
            )
    return later


def summary(root, v, notes, later):
    slug = v["SLUG"]
    cmd = slug if host.host_os() == "windows" else "./" + slug
    host.say()
    host.say("== new: %s in %s ==" % (v["NAME"], root))
    for n in notes:
        host.say("  " + n)
    if later:
        host.say("left to do (no network, git or uv for it now):")
        for n in later:
            host.say("  - " + n)
    host.say("next:")
    host.say("  1. put your dump (default.xbe and the game's files) in %s/" % v["GAME_FILES"])
    host.say("  2. cd %s && %s setup" % (root, cmd))
    host.say("  3. %s all        -> %s/%s.exe" % (cmd, v["WINDOWS_DIR"], v["EXE"]))
    host.say("Commit the directory when you like: the dump, gen/ and the toolchain are ignored.")


def make_parser(prog):
    ap = argparse.ArgumentParser(
        prog="%s new" % prog,
        description="a starter game in DIR: manifest, bootstrap, tools environment, "
        "the toolkit's template with your game's constants, the toolkit clone and pins",
    )
    ap.add_argument("dir", help="the game's directory (absent, or empty)")
    ap.add_argument("--slug", help="the command name, [a-z0-9-] (default: DIR's name)")
    ap.add_argument("--name", help="the game's name (default: the XBE certificate's title)")
    ap.add_argument(
        "--xbe",
        help="read the title, title ID and entry point from this dump "
        "(default: DIR/game_files/default.xbe if it is there); never copied",
    )
    ap.add_argument("--toolkit-url", default=TOOLKIT_URL)
    ap.add_argument("--toolkit-branch", default=TOOLKIT_BRANCH)
    ap.add_argument(
        "--toolkit-commit", default="", help="default: a checkout beside DIR, else the branch head"
    )
    ap.add_argument("--cli-commit", default="", help="default: the commit this CLI runs from")
    ap.add_argument("--llvm-mingw", default=LLVM_MINGW, help="the llvm-mingw release tag")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="write the files only: no clone, no pins, no lock (each is named as a later step)",
    )
    return ap


def main(argv, prog="xbr"):
    a = make_parser(prog).parse_args(argv)
    root = os.path.abspath(a.dir)
    try:
        v, notes = scaffold(root, a)
        later = finish(root, v, a, notes)
    except (CliError, manifest.ManifestError) as e:
        print("%s new: %s" % (prog, e), file=sys.stderr)
        return 1
    summary(root, v, notes, later)
    return 0
