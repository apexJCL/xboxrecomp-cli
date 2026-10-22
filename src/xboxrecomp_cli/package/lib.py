#!/usr/bin/env python3
"""Packaging helpers for `package` (package/__init__.py imports this
module): version, manifest, sums, XBE title check, the staged-tree checks,
the macOS dylib plan and the NSIS file lists. Standard library only. The
game's values (product, name, title ID, ...) come from its game.toml
through configure(). The subcommands below expose the same functions for
debugging.

  python -m xboxrecomp_cli.package.lib version --game DIR --toolkit DIR --gen DIR --exe FILE
  ... check-cache CMakeCache.txt [--allow-debug] [--allow-nonstock] [--nonstock VAR]...
  ... xbe-title FILE --expect 0xTITLEID
  ... stage-game-files SRC DST         (clone or copy, minus exclusions)
  ... manifest  --root DIR --target T --version V --game DIR
                --toolkit DIR --gen DIR --cache FILE
                [--override NAME]... [--extra JSON] [--files-under SUB]
  ... dylibs    EXE [--companion NAME=PATH]...   (prints a JSON plan)
  ... nsis-files DIR                   (prints !define-able blocks)

Nothing here writes outside the paths it is given, and the manifest never
holds a host name or an absolute path: bundles live on the user's machines
only, but their metadata should not say whose machines those are.
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys

from .. import xbe

# The game's values, from game.toml (configure()): manifest.json "product",
# the name the player sees (installer, app, shortcuts, README, messages),
# the title ID package checks the dump against, the source tree's name in
# manifest.json "sources", the app name (file and folder names), and the
# CMake cache variables a stock build leaves empty.
PRODUCT = ""
PRODUCT_NAME = ""
TITLE_ID = None
SOURCE_NAME = "game"
APP = ""
NONSTOCK_VARS = ()
# build.stock_cmake: the -DVAR=VALUE options a stock build is configured
# with (manifest.py's default until configure() gives the game's).
STOCK_CMAKE = ("-DXBOXRECOMP_ENHANCE=ON",)
# 2: "source" (where the game tree came from) and "volatile". The readers
# (install_lib, the patch bases) take 1 and 2: 2 only adds fields.
SCHEMA = 2
DATA_LAYOUT = 1
# The manifest fields two builds of the same inputs may still differ in:
# the build time and the branch names (a tag clone is detached, a
# developer's is on a branch). Every other field is equal between them.
VOLATILE = ("built", "sources.*.branch")
# The wizard's mark on its clone (D4): $(git rev-parse --git-dir)/xbr/.
MARK_NAME = "wizard.json"

# game_files/ entries that are not the dump: the pipeline's XBE analysis and
# any save tree a dev run left there (data.game_files_exclude), Finder's, and
# the marker `import` writes (it names a path on the player's machine).
GAME_FILES_EXCLUDE = (".DS_Store", ".xbr-dump.json")

TARGETS = ("windows-x86_64", "steamos-x86_64-proton", "macos-arm64")


def configure(
    product,
    name,
    title_id,
    source_name,
    app,
    game_files_exclude=(),
    nonstock_vars=(),
    stock_cmake=STOCK_CMAKE,
):
    """The game's values (package/__init__.py passes game.toml's)."""
    global PRODUCT, PRODUCT_NAME, TITLE_ID, SOURCE_NAME, APP, GAME_FILES_EXCLUDE
    global NONSTOCK_VARS, STOCK_CMAKE, LF_ONLY, ICON_FILES
    PRODUCT, PRODUCT_NAME, TITLE_ID, SOURCE_NAME, APP = product, name, title_id, source_name, app
    GAME_FILES_EXCLUDE = tuple(game_files_exclude) + (".DS_Store", ".xbr-dump.json")
    NONSTOCK_VARS = tuple(nonstock_vars)
    STOCK_CMAKE = tuple(stock_cmake)
    LF_ONLY = lf_only(app)
    ICON_FILES = icon_files(app)


def die(msg):
    print("package_lib: " + msg, file=sys.stderr)
    sys.exit(1)


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class GitError(subprocess.CalledProcessError, ValueError):
    """A git command that failed, with git's stderr in its message: the
    callers that fall back catch CalledProcessError as before, and package
    turns the ValueError into a CliError."""

    def __str__(self):
        err = (self.stderr or b"").decode(errors="replace").strip()
        cmd = " ".join(self.cmd[3:]) if len(self.cmd) > 3 else " ".join(self.cmd)
        return "git %s in %s failed: %s" % (cmd, self.cmd[2], err or "exit %d" % self.returncode)


def git(tree, *args):
    # git's stderr stays with the error: a caller that falls back (no
    # repo, no commits yet) must not leave "fatal: ..." on the terminal,
    # and one that does not shows it in the message.
    cmd = ["git", "-C", tree] + list(args)
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise GitError(r.returncode, cmd, r.stdout, r.stderr)
    return r.stdout


# ── version ──────────────────────────────────────────────────────────────

# A tree with no commit to name: outside git ("nogit"), or a repo before
# its first commit ("nocommit"). Its version names a digest of its files
# instead, after this word: c<word><6 hex>.
NOGIT, NOCOMMIT = "nogit", "nocommit"
MODE_NOTES = {NOGIT: "no git repository", NOCOMMIT: "no commits yet"}
# realpath -> skip(rel, is_dir) for the walk of a tree outside git: the
# game's, from layout.py (package registers it). Any other tree skips .git
# and __pycache__ anywhere, and at the top setup's clones, the venvs,
# .xbr/, dist/, node_modules and the build trees.
CONTENT_SKIP = {}
# (realpath, mode) -> content_state: one walk and hash per tree per run
# (package asks for each tree's state several times).
CONTENT_MEMO = {}
_TOP_SKIP = {"external", "third_party", "dist", "node_modules", ".xbr"}


def git_mode(tree):
    """NOGIT, NOCOMMIT, or "" for the root of a repo with a HEAD commit.
    A tree inside another repo is NOGIT: that repo's commit does not name
    it. A git that fails for another reason (dubious ownership, a corrupt
    repo) raises GitError with its message."""
    cmd = ["git", "-C", tree, "rev-parse", "--show-toplevel"]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except OSError:
        return NOGIT  # no git on this machine
    if r.returncode != 0:
        if b"not a git repository" in r.stderr.lower():
            return NOGIT
        raise GitError(r.returncode, cmd, r.stdout, r.stderr)
    top = r.stdout.decode(errors="replace").strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(tree)):
        return NOGIT
    cmd = ["git", "-C", tree, "rev-parse", "--verify", "HEAD^{commit}"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0:
        return ""
    # No commit: HEAD names a branch that does not exist yet. A branch
    # whose commit is missing, or a detached HEAD that names none, is a
    # broken repo, and show-ref says so.
    ref = subprocess.run(["git", "-C", tree, "symbolic-ref", "-q", "HEAD"], capture_output=True)
    name = ref.stdout.decode(errors="replace").strip()
    if ref.returncode == 0 and name:
        show = ["git", "-C", tree, "show-ref", "--verify", "-q", name]
        s = subprocess.run(show, capture_output=True)
        if s.returncode == 1 and not s.stderr.strip():
            return NOCOMMIT
        if s.stderr.strip():
            raise GitError(s.returncode, show, s.stdout, s.stderr)
    raise GitError(r.returncode, cmd, r.stdout, r.stderr)


def _default_skip(rel, is_dir):
    name = rel.rsplit("/", 1)[-1]
    if name in (".git", "__pycache__"):
        return True
    if "/" in rel:
        return False
    if name in _TOP_SKIP or name.startswith(".venv"):
        return True
    return is_dir and name.startswith("build")


def _skip_for(tree):
    return CONTENT_SKIP.get(os.path.realpath(tree)) or _default_skip


def _kept(rel, skip):
    """rel and none of its folders skipped."""
    segs = rel.split("/")
    for i in range(1, len(segs)):
        if skip("/".join(segs[:i]), True):
            return False
    return not skip(rel, False)


def walk_files(tree, skip=None):
    """The files of a tree outside git, as sorted rel paths: the walk
    leaves out what skip names (folders whole) and, when the tree has a
    .gitignore and git runs, what that .gitignore ignores, so the digest
    holds what a first commit would. A symlink to a folder is an entry of
    its own, as git records it, and the walk does not follow it."""
    from .. import layout

    skip = skip or _skip_for(tree)
    rels = []
    for dirpath, dirnames, filenames in os.walk(tree):
        base = os.path.relpath(dirpath, tree).replace(os.sep, "/")
        base = "" if base == "." else base + "/"
        keep = []
        for d in sorted(dirnames):
            if skip(base + d, True):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                rels.append(base + d)
            else:
                keep.append(d)
        dirnames[:] = keep
        rels += [base + f for f in filenames if not skip(base + f, False)]
    try:
        with open(os.path.join(tree, ".gitignore"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = None
    if text is not None and rels:
        try:
            gone = layout.ignored(text, rels)
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            gone = set()
        rels = [r for r in rels if r not in gone]
    return sorted(rels, key=lambda r: r.encode())


def _hash_file(path):
    try:
        return sha256_file(path)
    except OSError as e:
        raise ValueError("cannot read %s: %s" % (path, e.strerror or e)) from e


def content_state(tree, mode):
    """tree_state for a tree with no commit: commit and branch "none",
    dirty, and a digest of its non-ignored files (names, sha256s and
    symlink targets) as both the content and the dirt. A file it cannot
    read raises ValueError naming it."""
    key = (os.path.realpath(tree), mode)
    if key in CONTENT_MEMO:
        return dict(CONTENT_MEMO[key])
    if mode == NOCOMMIT:
        out = git(tree, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        skip = _skip_for(tree)
        names = {n for n in out.decode().split("\0") if n and _kept(n, skip)}
        names = sorted(names, key=lambda n: n.encode())
    else:
        names = walk_files(tree)
    h = hashlib.sha256()
    for name in names:
        h.update(b"\0" + name.encode() + b"\0")
        p = os.path.join(tree, name)
        if os.path.islink(p):
            h.update(b"link:" + os.fsencode(os.readlink(p)))
        elif os.path.isfile(p):
            h.update(_hash_file(p).encode())
    digest = h.hexdigest()
    state = {
        "commit": "none",
        "branch": "none",
        "dirty": True,
        "dirty_paths": len(names),
        "_dirt": digest,
        "content": digest,
        "mode": mode,
    }
    CONTENT_MEMO[key] = dict(state)
    return state


def commit_label(state, n=7):
    """The commit's first n hex, or for a tree with no commit the mode
    and 6 hex of its content (nogit3fa2c1, nocommit3fa2c1)."""
    if state.get("mode"):
        return state["mode"] + state["content"][:6]
    return state["commit"][:n]


def source_label(state):
    """The README's name for a tree: the version's label, and for a tree
    with no commit what it is."""
    label = commit_label(state)
    if state.get("mode"):
        label += " (%s)" % MODE_NOTES[state["mode"]]
    return label


def tree_state(tree):
    """commit, branch, the dirty-path count and a digest of the dirt
    (tracked diff plus untracked, non-ignored files with their contents).
    A tree outside git or before its first commit gets content_state."""
    mode = git_mode(tree)
    if mode:
        return content_state(tree, mode)
    commit = git(tree, "rev-parse", "HEAD").decode().strip()
    branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    porcelain = [line for line in git(tree, "status", "--porcelain").decode().splitlines() if line]
    h = hashlib.sha256()
    if porcelain:
        h.update(git(tree, "diff", "HEAD", "--binary"))
        others = git(tree, "ls-files", "--others", "--exclude-standard", "-z")
        for name in sorted(n for n in others.decode().split("\0") if n):
            h.update(b"\0" + name.encode() + b"\0")
            p = os.path.join(tree, name)
            if os.path.isfile(p) and not os.path.islink(p):
                h.update(_hash_file(p).encode())
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(porcelain),
        "dirty_paths": len(porcelain),
        "_dirt": h.hexdigest() if porcelain else "",
    }


def read_mark(tree):
    """The wizard's mark on the game clone, or None: no git, no mark, or
    one this CLI cannot read (the provenance is then "dev")."""
    try:
        gd = git(tree, "rev-parse", "--absolute-git-dir").decode().strip()
        with open(os.path.join(gd, "xbr", MARK_NAME), encoding="utf-8") as f:
            mark = json.load(f)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    return mark if isinstance(mark, dict) else None


def source_record(state, mark, repo):
    """The manifest's "source": how the game tree was obtained (D14).

    - tag: the wizard's release clone, at the commit it marked;
    - local: the wizard's clone of a developer's checkout or bundle;
    - dev: anything else (a developer's own checkout, a marked clone
      someone moved off its commit or edited).

    Never a path or the clone's file:// origin: a local source records
    only how it came (via) and the ref named."""
    commit = state["commit"]
    ok = mark is not None and mark.get("commit") == commit and not state["dirty"]
    if ok and mark.get("origin") == "local":
        via = mark.get("via") if mark.get("via") in ("checkout", "bundle") else "checkout"
        return {
            "kind": "local",
            "via": via,
            "commit": commit,
            "ref": str(mark.get("ref") or "HEAD"),
        }
    if ok and mark.get("origin") == "repo" and repo and mark.get("ref"):
        return {"kind": "tag", "repo": repo, "tag": str(mark["ref"]), "commit": commit}
    return {"kind": "dev", "commit": commit}


def gen_digest(gen_dir):
    """The digest blinx2 bench compares with the host's (gen_digest_local): sha256 of the
    `shasum -a 256 -- *` listing, sorted by name. Dotfiles are not in *."""
    names = sorted(
        (
            n
            for n in os.listdir(gen_dir)
            if not n.startswith(".") and os.path.isfile(os.path.join(gen_dir, n))
        ),
        key=lambda n: n.encode(),
    )
    listing = "".join("%s  %s\n" % (sha256_file(os.path.join(gen_dir, n)), n) for n in names)
    return hashlib.sha256(listing.encode()).hexdigest(), len(names)


def format_version(built_utc, cat, toolkit, gen_sha, cli=None, options=""):
    """<time>-c<game sha7>-t<toolkit sha7>-g<gen 8>, then -o<options 6>
    for a build that is not stock (options_digest), then -dirty<hash> when
    the game, the toolkit or the CLI tree has uncommitted changes. The
    CLI's sha is not in it (a third sha would change every version for no
    reader); its dirt is, since its code shapes the bundle. A tree with
    no commit puts commit_label's c<mode><6 hex> in place of its sha
    (cnogit3fa2c1) and is always -dirty."""
    v = "%s-c%s-t%s-g%s" % (
        built_utc.strftime("%Y%m%d.%H%M"),
        commit_label(cat),
        commit_label(toolkit),
        gen_sha[:8],
    )
    if options:
        v += "-o" + options
    cli_dirty = bool(cli and cli["dirty"])
    if cat["dirty"] or toolkit["dirty"] or cli_dirty:
        text = cat["_dirt"] + ":" + toolkit["_dirt"]
        if cli_dirty:
            # Only a dirty CLI adds to the hash, so a clean one leaves the
            # version what the game and toolkit alone made it.
            text += ":" + cli["_dirt"]
        dirt = hashlib.sha256(text.encode()).hexdigest()
        v += "-dirty" + dirt[:8]
    return v


def exe_time(exe):
    return datetime.datetime.fromtimestamp(int(os.stat(exe).st_mtime), datetime.UTC)


def commit_time(tree):
    """HEAD's committer time, or None outside git."""
    try:
        t = git(tree, "log", "-1", "--format=%ct", "HEAD").decode().strip()
        return int(t) if t else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def build_time(trees, exe=None, env=None):
    """The time a bundle says it was built, reproducibly (D12):
    SOURCE_DATE_EPOCH when set, else the newest commit time of the trees
    (the game and the toolkit). Never now(), and never the exe's mtime
    (a rebuild would move it): two builds of the same inputs agree. The
    exe's mtime only for a tree outside git."""
    env = os.environ if env is None else env
    sde = env.get("SOURCE_DATE_EPOCH", "").strip()
    if sde:
        try:
            return datetime.datetime.fromtimestamp(int(sde), datetime.UTC)
        except (ValueError, OverflowError, OSError) as e:
            raise ValueError("SOURCE_DATE_EPOCH=%s: not a time in seconds (%s)" % (sde, e)) from e
    times = [t for t in (commit_time(x) for x in trees) if t is not None]
    if times:
        return datetime.datetime.fromtimestamp(max(times), datetime.UTC)
    if exe:
        return exe_time(exe)
    return datetime.datetime.fromtimestamp(0, datetime.UTC)


def compute_version(cat_dir, tk_dir, gen_dir, exe, cli_dir=None, options=""):
    cat, tk = tree_state(cat_dir), tree_state(tk_dir)
    cli = tree_state(cli_dir) if cli_dir else None
    gsha, _ = gen_digest(gen_dir)
    return format_version(build_time((cat_dir, tk_dir), exe), cat, tk, gsha, cli, options)


def options_digest(cache_vals, seed=None, hud_sha=None):
    """The version's -o part: 6 hex of the option set (the --seed-config
    text: render scale and the like), the HUD pack's sha256 and the CMake
    -D values a package checks (the build type, build.nonstock_vars, the
    build.stock_cmake defines), or "" for a stock build. The build time and
    the commits no longer tell two builds of one commit apart, so without
    it a 2x and a 1x build would share a version, and a bundle record and
    an install's versions/<v>."""
    debug, nonstock = cache_problems(cache_vals)
    if not (debug or nonstock or seed is not None or hud_sha):
        return ""
    cmake = {"CMAKE_BUILD_TYPE": cache_vals.get("CMAKE_BUILD_TYPE", "")}
    for var in NONSTOCK_VARS:
        cmake[var] = cache_vals.get(var, "")
    for var, want in stock_defines():
        cmake[var] = cache_vals.get(var, want)
    doc = {
        "cmake": cmake,
        "seed": hashlib.sha256(seed.encode()).hexdigest() if seed is not None else None,
        "hud_pack": hud_sha or None,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:6]


# ── CMake cache: a packaged build is the stock Release configuration ─────


def read_cache(path):
    vals = {}
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):[A-Z]+=(.*)$", line.rstrip("\n"))
            if m:
                vals[m.group(1)] = m.group(2)
    return vals


def cache_problems(vals):
    """(debug, nonstock) lists of human-readable reasons."""
    debug, nonstock = [], []
    if vals.get("CMAKE_BUILD_TYPE", "") != "Release":
        debug.append("CMAKE_BUILD_TYPE=%s (want Release)" % vals.get("CMAKE_BUILD_TYPE", ""))
    for var in NONSTOCK_VARS:
        if vals.get(var, ""):
            nonstock.append("%s=%s (want empty)" % (var, vals[var]))
    for var, want in stock_defines():
        if var in vals and not cmake_same(vals[var], want):
            nonstock.append("%s=%s (want %s)" % (var, vals[var], want))
    return debug, nonstock


CMAKE_FALSE = ("OFF", "0", "FALSE", "NO", "N", "IGNORE", "NOTFOUND", "")
CMAKE_TRUE = ("ON", "1", "TRUE", "YES", "Y")


def stock_defines():
    """(VAR, VALUE) for each -DVAR[:TYPE]=VALUE of build.stock_cmake. The
    -U entries are build.nonstock_vars' business."""
    out = []
    for opt in STOCK_CMAKE:
        m = re.match(r"^-D([A-Za-z_][A-Za-z0-9_]*)(?::[A-Z]+)?=(.*)$", opt)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def cmake_same(actual, want):
    """A cache value against the stock one: booleans by CMake's truth (OFF,
    0, NO... all false), anything else as text."""
    a, w = actual.upper(), want.upper()
    if w in CMAKE_TRUE or (w and w in CMAKE_FALSE):
        return (a in CMAKE_TRUE) == (w in CMAKE_TRUE)
    return actual == want


def enhance_on(cache):
    """Whether the build has the toolkit's enhancements layer: the cache's
    XBOXRECOMP_ENHANCE by CMake's truth, else build.stock_cmake's value (the
    next configure sets it). A game with neither has no such layer: false."""
    val = cache.get("XBOXRECOMP_ENHANCE")
    if val is None:
        val = dict(stock_defines()).get("XBOXRECOMP_ENHANCE", "OFF")
    v = val.upper()
    return v not in CMAKE_FALSE and not v.endswith("-NOTFOUND")


def nonstock_help(nonstock_vars, stock_cmake):
    """--allow-nonstock's list: each nonstock var, then each stock define
    as the value that would make the build non-stock."""
    out = list(nonstock_vars)
    for opt in stock_cmake:
        m = re.match(r"^-D([A-Za-z_][A-Za-z0-9_]*)(?::[A-Z]+)?=(.*)$", opt)
        if not m:
            continue
        var, want = m.group(1), m.group(2)
        if want.upper() in CMAKE_TRUE:
            out.append("%s=OFF" % var)
        elif want.upper() in CMAKE_FALSE and want:
            out.append("%s=ON" % var)
        else:
            out.append("%s!=%s" % (var, want))
    return out


def compiler_line(vals, build_dir=None):
    """The compiler's first --version line. A toolchain file's compiler is
    not in the cache, only in CMakeFiles/<cmake version>/CMakeCCompiler.cmake."""
    cc = vals.get("CMAKE_C_COMPILER", "")
    if not cc and build_dir:
        import glob

        for f in sorted(
            glob.glob(os.path.join(build_dir, "CMakeFiles", "*", "CMakeCCompiler.cmake"))
        ):
            with open(f, errors="replace") as fh:
                m = re.search(r'^set\(CMAKE_C_COMPILER "([^"]+)"\)', fh.read(), re.M)
            if m:
                cc = m.group(1)
    try:
        out = subprocess.run(
            [cc, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True
        ).stdout.decode()
        return out.splitlines()[0].strip()
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "unknown"


# ── XBE ──────────────────────────────────────────────────────────────────


def xbe_title_id(data):
    """The certificate's title ID (xbe.py reads the header; XbeError is a
    ValueError, so the callers' handlers stand)."""
    return xbe.title_id(data)


# ── game files ───────────────────────────────────────────────────────────


def game_file_excluded(rel):
    first = rel.replace("\\", "/").split("/")[0]
    return first in GAME_FILES_EXCLUDE or os.path.basename(rel) == ".DS_Store"


def _clonefile():
    """macOS clonefile(2) through ctypes, or None: on APFS a clone takes no
    space, which matters for a dump of several GB."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        f = libc.clonefile
        f.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
        return f
    except (OSError, AttributeError):
        return None


def copy_file(src, dst, clone=None):
    """Clone where the filesystem can, else a plain copy (other volumes,
    other hosts)."""
    if clone is not None and clone(os.fsencode(src), os.fsencode(dst), 0) == 0:
        return
    shutil.copy2(src, dst)


def stage_game_files(src, dst):
    """Copy the dump without the pipeline's and dev runs' leftovers."""
    src = os.path.realpath(src)
    clone = _clonefile()
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        if game_file_excluded(name):
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(
                s,
                d,
                copy_function=lambda a, b: copy_file(a, b, clone),
                ignore=shutil.ignore_patterns(".DS_Store"),
            )
            # copytree gives each directory the dump's mode, often r-x (a
            # disc copy): the stage could not then be removed with rm -rf.
            for dirpath, _dirnames, _filenames in os.walk(d):
                mode = os.stat(dirpath).st_mode & 0o7777
                if mode & 0o700 != 0o700:
                    os.chmod(dirpath, mode | 0o700)
        else:
            copy_file(s, d, clone)
        n += 1
    return n


# ── staged-tree checks ───────────────────────────────────────────────────

# Names Windows cannot create (any extension), and characters it refuses.
# The steamos tar is unpacked on Linux, but the same payload is built on
# Windows hosts too, so every target gets the same rules.
RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I)
BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


# Files a POSIX shell or Python reads: a CR breaks `#!/bin/sh\r` and KEY=value.
def lf_only(app):
    """(The macOS launcher is the app name with no extension.)"""
    extra = "|^%s$" % re.escape(app) if app else ""
    return re.compile(r"(\.(sh|py|default|in|toml|env)$|^launch\.env\.default$%s)" % extra)


LF_ONLY = lf_only("")


def name_problem(name):
    """Why Windows cannot hold a file or directory called name, or None."""
    if RESERVED.match(name):
        return "reserved name on Windows"
    if name.endswith((".", " ")):
        return "trailing dot or space"
    if BAD_CHARS.search(name):
        return "character Windows refuses"
    return None


# The icon files game_icon.py writes (the game's title image). The .ico is
# compiled into the exes, never shipped loose; the others belong next to
# the launcher or in the app, never among the game files.
def icon_files(app):
    return ("icon.png", app + ".icns", app + ".ico") if app else ("icon.png",)


ICON_FILES = icon_files("")


def staged_problems(root):
    """Reasons a staged payload must not be packaged."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if os.path.islink(p):
                out.append("%s: symlink" % rel)
            if (
                n in filenames
                and n in ICON_FILES
                and (n.endswith(".ico") or rel.startswith("game_files/"))
            ):
                out.append("%s: icon file out of place" % rel)
            why = name_problem(n)
            if why:
                out.append("%s: %s" % (rel, why))
            if n in filenames and not rel.startswith("game_files/") and LF_ONLY.search(n):
                with open(p, "rb") as f:
                    if b"\r\n" in f.read():
                        out.append("%s: CRLF line endings" % rel)
    return out


# ── manifest and sums ────────────────────────────────────────────────────


def list_files(root, under=None):
    """Every regular file under root (or root/under), as sorted relative
    POSIX paths."""
    base = os.path.join(root, under) if under else root
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.isfile(p):
                out.append(os.path.relpath(p, root).replace(os.sep, "/"))
    return sorted(out)


def build_manifest(
    root,
    target,
    version,
    cat,
    toolkit,
    gen_sha,
    gen_files,
    cache,
    compiler,
    overrides=(),
    extra=None,
    under=None,
    built=None,
    cli=None,
    source=None,
    hasher=None,
):
    """hasher(rel, path): the sha256 of a file, from a cache that knows it
    (buildstate.HashCache for game_files/); sha256_file otherwise."""
    files = []
    for rel in list_files(root, under):
        if rel in ("manifest.json", "SHA256SUMS"):
            continue
        p = os.path.join(root, rel)
        sha = hasher(rel, p) if hasher else None
        files.append({"path": rel, "sha256": sha or sha256_file(p), "size": os.path.getsize(p)})
    m = {
        "schema": SCHEMA,
        "product": PRODUCT,
        "name": PRODUCT_NAME,
        "version": version,
        "target": target,
        "built": (built or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "volatile": list(VOLATILE),
        "sources": {
            # "content" only for a tree with no commit (content_state).
            k: {x: v[x] for x in ("commit", "branch", "dirty", "dirty_paths", "content") if x in v}
            for k, v in ((SOURCE_NAME, cat), ("toolkit", toolkit), ("cli", cli))
            if v is not None
        },
        "source": source or {"kind": "dev", "commit": (cat or {}).get("commit", "")},
        "gen": {"sha256": gen_sha, "files": gen_files},
        "build": {
            "type": cache.get("CMAKE_BUILD_TYPE", ""),
            "enhance": enhance_on(cache),
            # The first of build.nonstock_vars: the generated code's options.
            "gen_opt": cache.get(NONSTOCK_VARS[0], "") if NONSTOCK_VARS else "",
            "compiler": compiler,
            "overrides": list(overrides),
        },
        "data_layout": DATA_LAYOUT,
        "files": files,
        "notice": "personal use only: contains your own copy of the game and code generated from it; never share it",
    }
    if extra:
        m.update(extra)
    return m


def sums_text(manifest):
    return "".join("%s  %s\n" % (f["sha256"], f["path"]) for f in manifest["files"])


# A path start anywhere in the text: after a quote, a space, a colon, a
# bracket or at the start, never inside a relative path (a folder named
# home under game_files) and never a bare drive letter without a
# separator (C:X). Bracketed
# letters: the same paths, without this line matching the public-push
# audit's own search for them.
PATH_RE = re.compile(r"(?<![A-Za-z0-9_./\\-])(?:/[U]sers/|/[h]ome/|/var/[h]ome/|[A-Za-z]:[\\/])")


def home_pattern(home):
    """$HOME as a regex that also matches it JSON-escaped (C:\\\\Users) and
    with forward slashes (c:/users), without case: a Windows home in a
    manifest reaches the text in any of those forms."""
    parts = [re.escape(p) for p in re.split(r"[\\/]+", home.rstrip("\\/"))]
    return re.compile(r"(?:\\\\|\\|/)+".join(parts), re.I)


def private_leaks(text, home=None, host=None):
    """Strings a manifest must never carry: absolute paths, $HOME, host name."""
    leaks = []
    home = home if home is not None else os.path.expanduser("~")
    # platform.node(): Windows has no os.uname.
    host = host if host is not None else platform.node().split(".")[0]
    if home and home != "/" and len(home) > 3 and home_pattern(home).search(text):
        leaks.append("home directory")
    if host and len(host) > 2 and re.search(r"\b%s\b" % re.escape(host), text, re.I):
        leaks.append("host name")
    if PATH_RE.search(text):
        leaks.append("absolute path")
    # A --source clone's origin (D14): it names the developer's folder.
    if re.search(r"\bfile:/", text, re.I):
        leaks.append("file:// URL")
    return leaks


# ── macOS dylibs ─────────────────────────────────────────────────────────

SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def parse_otool(text):
    """otool -L output -> list of install names (the first line names the
    file itself and is skipped)."""
    names = []
    for line in text.splitlines()[1:]:
        m = re.match(r"^\s+(\S.*?) \(compatibility version", line)
        if m:
            names.append(m.group(1))
    return names


def otool_runner(path):
    return subprocess.run(["otool", "-L", path], check=True, stdout=subprocess.PIPE).stdout.decode()


def dylib_plan(exe, companions=(), run=otool_runner, realpath=os.path.realpath):
    """The libraries to bundle: every non-system install name reachable
    from exe, plus companions (libraries loaded with dlopen, NAME=PATH, which
    otool cannot see), with their own dependencies. Returns
    {"libs": [{"name", "src"}], "changes": [{"file", "old", "new"}]}, file
    being "@exe" or a bundled lib name."""
    libs = {}  # name -> src realpath
    changes = []
    queue = [("@exe", exe)]
    for spec in companions:
        name, _, path = spec.partition("=")
        if name not in libs:
            libs[name] = realpath(path)
            queue.append((name, path))
    seen = set()
    while queue:
        who, path = queue.pop(0)
        if who in seen:
            continue
        seen.add(who)
        deps = parse_otool(run(path))
        if who != "@exe" and deps:
            deps = deps[1:]  # a dylib lists its own id first
        for dep in deps:
            if dep.startswith(SYSTEM_PREFIXES) or dep.startswith("@"):
                continue
            name = os.path.basename(dep)
            changes.append({"file": who, "old": dep, "new": "@rpath/" + name})
            if name not in libs:
                libs[name] = realpath(dep)
                queue.append((name, dep))
    return {"libs": [{"name": n, "src": s} for n, s in sorted(libs.items())], "changes": changes}


# ── NSIS ─────────────────────────────────────────────────────────────────


def nsis_lists(root, under=""):
    """The program files (everything but game_files/) as NSIS File and
    Delete lines, so the uninstaller removes exactly what was installed.
    under: the folder below $INSTDIR the File lines extract into (the
    versioned layout's versions/<v>.partial); the Delete lines stay
    $INSTDIR's, an in-place install's files."""
    names = [r for r in list_files(root) if not r.startswith("game_files/")]
    # File puts a file in the current SetOutPath, so each subdirectory
    # (enhance/ when --hud-pack stages a pack) gets its own; the template
    # set $INSTDIR before this block and expects it again after.
    install, cur = [], ""
    if under:
        install.append('  SetOutPath "%s"\n' % win_dir(under))
    for n in sorted(names, key=lambda n: (os.path.dirname(n), n)):
        d = os.path.dirname(n)
        if d != cur:
            install.append('  SetOutPath "%s"\n' % win_dir("/".join(x for x in (under, d) if x)))
            cur = d
        # Native separators: Windows makensis finds no "enhance/hud.pack".
        src = os.path.join(root, *n.split("/"))
        install.append('  File "%s"\n' % src.replace("$", "$$"))
    if cur or under:
        install.append('  SetOutPath "$INSTDIR"\n')
    uninstall = ['  Delete "$INSTDIR\\%s"\n' % n.replace("/", "\\") for n in names]
    # Plain RMDir, deepest first: a directory the player put files in stays.
    dirs = {os.path.dirname(n) for n in names} - {""}
    for d in list(dirs):
        while "/" in d:
            d = os.path.dirname(d)
            dirs.add(d)
    uninstall += ['  RMDir "%s"\n' % win_dir(d) for d in sorted(dirs, reverse=True)]
    return names, "".join(install), "".join(uninstall)


def win_dir(rel):
    return "$INSTDIR" + ("\\" + rel.replace("/", "\\") if rel else "")


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("version")
    for k in ("game", "toolkit", "gen", "exe"):
        v.add_argument("--" + k, required=True)

    c = sub.add_parser("check-cache")
    c.add_argument("cache")
    c.add_argument("--allow-debug", action="store_true")
    c.add_argument("--allow-nonstock", action="store_true")
    c.add_argument(
        "--nonstock", action="append", default=[], help="a variable a stock build leaves empty"
    )
    c.add_argument(
        "--stock",
        action="append",
        default=None,
        help="-DVAR=VALUE a stock build is configured with (default: -DXBOXRECOMP_ENHANCE=ON)",
    )

    x = sub.add_parser("xbe-title")
    x.add_argument("xbe")
    x.add_argument("--expect", required=True)

    g = sub.add_parser("stage-game-files")
    g.add_argument("src")
    g.add_argument("dst")

    m = sub.add_parser("manifest")
    for k in ("root", "target", "version", "game", "toolkit", "gen", "cache"):
        m.add_argument("--" + k, required=True)
    m.add_argument("--override", action="append", default=[])
    m.add_argument("--extra", default=None, help="JSON object merged into the manifest")
    m.add_argument("--files-under", default=None)

    d = sub.add_parser("dylibs")
    d.add_argument("exe")
    d.add_argument("--companion", action="append", default=[])

    n = sub.add_parser("nsis-files")
    n.add_argument("root")
    n.add_argument("--out-install", required=True)
    n.add_argument("--out-uninstall", required=True)

    a = ap.parse_args(argv)

    if a.cmd == "version":
        print(compute_version(a.game, a.toolkit, a.gen, a.exe))
    elif a.cmd == "check-cache":
        global NONSTOCK_VARS, STOCK_CMAKE
        NONSTOCK_VARS = tuple(a.nonstock)
        if a.stock is not None:
            STOCK_CMAKE = tuple(a.stock)
        debug, nonstock = cache_problems(read_cache(a.cache))
        bad = ([] if a.allow_debug else debug) + ([] if a.allow_nonstock else nonstock)
        if bad:
            die(
                "not a stock Release build: "
                + "; ".join(bad)
                + " (--allow-debug / --allow-nonstock to package anyway)"
            )
        for r in (debug if a.allow_debug else []) + (nonstock if a.allow_nonstock else []):
            print(r)
    elif a.cmd == "xbe-title":
        try:
            with open(a.xbe, "rb") as f:
                tid = xbe_title_id(f.read(0x10000))
        except (OSError, ValueError) as e:
            die("%s: %s" % (a.xbe, e))
        print("0x%08X" % tid)
        if tid != int(a.expect, 16):
            die("%s: title ID 0x%08X is not %s" % (a.xbe, tid, a.expect))
    elif a.cmd == "stage-game-files":
        print(stage_game_files(a.src, a.dst))
    elif a.cmd == "manifest":
        if a.target not in TARGETS:
            die("unknown target %s" % a.target)
        cat, tk = tree_state(a.game), tree_state(a.toolkit)
        gsha, gn = gen_digest(a.gen)
        cache = read_cache(a.cache)
        extra = json.loads(a.extra) if a.extra else None
        man = build_manifest(
            a.root,
            a.target,
            a.version,
            cat,
            tk,
            gsha,
            gn,
            cache,
            compiler_line(cache),
            a.override,
            extra,
            a.files_under,
        )
        text = json.dumps(man, indent=2) + "\n"
        leaks = private_leaks(text)
        if leaks:
            die("manifest would carry private data: " + ", ".join(leaks))
        with open(os.path.join(a.root, "manifest.json"), "w") as f:
            f.write(text)
        with open(os.path.join(a.root, "SHA256SUMS"), "w") as f:
            f.write(sums_text(man))
        print("%d files" % len(man["files"]))
    elif a.cmd == "dylibs":
        print(json.dumps(dylib_plan(a.exe, a.companion), indent=1))
    elif a.cmd == "nsis-files":
        names, inst, uninst = nsis_lists(a.root)
        with open(a.out_install, "w") as f:
            f.write(inst)
        with open(a.out_uninstall, "w") as f:
            f.write(uninst)
        print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
