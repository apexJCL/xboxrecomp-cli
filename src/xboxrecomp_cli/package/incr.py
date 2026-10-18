"""The inputs of each package stage (the keys buildstate compares) and the
in-place sync of the staged payload.

The edges are as narrow as the inputs allow: generate keeps its own key
(pipeline.gen_key, which holds the XBE's sha256, so a new dump reaches it
without an edge from import that would rerun the 13-minute analysis on a
touched media file); compile follows the build folder (build-pkg-win/
serves windows and steamos); the stage of a target holds the exe, the
icon, the HUD pack and the game files it copies; the wrap holds the
staged manifest's sha256, so a rebased tree with equal files still gets a
bundle that names its own commits.
"""

import filecmp
import os
import platform
import shutil
import subprocess
import sys

from .. import build as build_mod
from .. import buildstate, cli_dir, fetch, host, pipeline, toolkit

PAYLOAD = "payload"
# The files write_manifest adds after the stage: the stage's own record
# leaves them out, or the next run would find its payload changed.
MANIFEST_FILES = ("manifest.json", "SHA256SUMS")
# What host.build_env passes on to CMake and the compilers.
ENV_KEYS = (
    "CC",
    "CXX",
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "DEVELOPER_DIR",
    "MACOSX_DEPLOYMENT_TARGET",
    "SDKROOT",
    "SOURCE_DATE_EPOCH",
)


def g():
    return host.g()


def _out(cmd, env=None):
    """A tool's version text, '' when it is missing or fails."""
    try:
        r = subprocess.run(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else ""


def _first(text):
    return text.splitlines()[0] if text else ""


def _sha(path):
    return buildstate.sha256_file(path) if path and os.path.isfile(path) else "absent"


def tree_input(tree, lib):
    """A checkout as a build input: its HEAD tree (a rebase that keeps the
    files keeps the key) and a digest of its uncommitted and untracked
    changes."""
    try:
        t = lib.git(tree, "rev-parse", "HEAD^{tree}").decode().strip()
        dirt = lib.tree_state(tree).get("_dirt", "")
    except (OSError, subprocess.CalledProcessError):
        # Outside git: no tree to name, so the stage reruns on its other
        # inputs only (a plain folder is a developer's, never the wizard's).
        return {"tree": "none", "dirt": ""}
    return {"tree": t, "dirt": dirt}


def content_digest(root, skip=None):
    """Names and bytes of a small tree (packaging/): a touch alone keeps it."""
    if not root or not os.path.isdir(root):
        return "absent"
    rows = []
    for rel, _size, _mt in buildstate.listing(root, skip):
        rows.append([rel, buildstate.sha256_file(os.path.join(root, *rel.split("/")))])
    return buildstate.digest(rows)


# ── import and generate ─────────────────────────────────────────────────


def import_inputs(lib):
    """The dump marker and the game_files/ listing (path, size, mtime_ns):
    no 3 GB rehash per run."""
    G = g()
    from ..dump import MARKER

    gf = G.game_files
    return {
        "marker": _sha(os.path.join(gf, MARKER)),
        "listing": buildstate.listing_digest(gf, lib.game_file_excluded)
        if os.path.isdir(gf)
        else "absent",
    }


def record_import(st, lib):
    """The import key, recorded when it moved (import, a new dump, or a
    player's edit in game_files/). Returns it."""
    inputs = import_inputs(lib)
    key = buildstate.stage_key(inputs)
    if st.key_of("import") != key:
        st.record("import", key, inputs)
    return key


def generate_inputs():
    """The gen key (what generate ran with) and gen/'s listing: a recomp run
    by hand, or an edit, moves the listing, and the compile below reruns
    (ninja would rebuild those files too)."""
    G = g()
    rec = buildstate.load_json(G.gen_key, None)
    return {
        "gen_key": buildstate.digest(rec) if isinstance(rec, dict) else "absent",
        "gen": buildstate.listing_digest(G.gen) if os.path.isdir(G.gen) else "absent",
    }


def record_generate(st):
    """generate's entry, recorded after the plan's generate step (which
    gen_stale_reasons decides, as before) when gen/ moved."""
    inputs = generate_inputs()
    key = buildstate.stage_key(inputs)
    if st.key_of("generate") != key:
        st.record("generate", key, inputs)
    return key


# ── icon and HUD ────────────────────────────────────────────────────────


def record_icon(st, icon_dir):
    """make_icons keeps its own cache (icon.key: the XBE's sha256 and the
    icon code's version); its record here is what compile and the stages
    chain on."""
    rec = buildstate.load_json(os.path.join(icon_dir, "icon.key"), None)
    inputs = {
        "icon_key": buildstate.digest(rec.get("key")) if isinstance(rec, dict) else "absent",
        "source": g().package.get("icon", "xbe"),
    }
    # No edge from import: the XBE's sha256 is in icon.key, and a touched
    # media file must not rerun the compile that chains on this key.
    key = buildstate.stage_key(inputs)
    if st.check("icon", key, inputs):
        st.record("icon", key, inputs, [icon_dir])
    return key


# ── compile ──────────────────────────────────────────────────────────────


def compile_stage(btarget):
    return "compile:" + btarget


def cmake_version(system_tools):
    try:
        cmake, ninja = build_mod.build_tools(system_tools)
    except host.CliError:
        return {"cmake": "", "ninja": ""}
    return {
        "cmake": _first(_out([cmake, "--version"])),
        "ninja": _out([ninja, "--version"]) if ninja else "",
    }


def toolchain(btarget, system_tools):
    """The compilers and SDK the build used, taken directly (not through
    setup): a change here also resets the CMake cache."""
    env = host.build_env()
    tc = dict(cmake_version(system_tools))
    if btarget == "windows":
        tag = fetch.mingw_tag()
        root = fetch.mingw_root()
        tc.update(
            {
                "llvm_mingw": tag,
                "llvm_mingw_root": os.path.basename(root) if root else "PATH",
                "clang": _first(_out([fetch.mingw_tool("clang"), "--version"], env)),
                "toolchain_file": _sha(build_mod.toolchain_file()),
            }
        )
    else:
        tc.update(
            {
                "clang": _first(_out(["clang", "--version"], env)),
                "sdk": _out(["xcrun", "--show-sdk-version"], env),
            }
        )
    return tc


def dylib_shas(exe, lib):
    """The brew dylibs the app copies, by name: their bytes. The wrap's
    input, not the compile's (the compile links them by path, and the
    plan reads the exe the compile makes)."""
    from . import macos

    try:
        plan = lib.dylib_plan(exe, macos.companions())
    except (OSError, ValueError, subprocess.CalledProcessError, host.CliError):
        return "unknown"
    return {d["name"]: _sha(d["src"]) for d in plan["libs"]}


def compile_inputs(btarget, a, lib, icon_args, bdir):
    G = g()
    env = host.build_env()
    cache_path = os.path.join(bdir, "CMakeCache.txt")
    cache = lib.read_cache(cache_path) if os.path.isfile(cache_path) else {}
    dlist = (
        list(build_mod.stock_cmake_args())
        + ["-DXBOXRECOMP_DIR=" + toolkit.toolkit_dir()]
        + list(icon_args)
    )
    inputs = {
        "game": tree_input(G.root, lib),
        # The whole toolkit: the runtime the exe links is in src/, include/,
        # cmake/, not only the tools/ that generate reads.
        "toolkit": tree_input(toolkit.toolkit_dir(), lib),
        "toolchain": buildstate.digest(toolchain(btarget, a.system_tools)),
        "env": {k: pipeline._placeholders(env.get(k, "")) for k in ENV_KEYS},
        "cmake": [pipeline._placeholders(x) for x in dlist],
        "allow": [n for n in ("debug", "nonstock") if getattr(a, "allow_" + n, False)],
    }
    if getattr(a, "allow_nonstock", False):
        # A cache value no stock configure resets (the generated code's
        # options): it shapes the exe, so it is an input.
        inputs["nonstock"] = {v: cache.get(v, "") for v in lib.NONSTOCK_VARS}
    return inputs


def toolchain_moved(st, cstage, inputs):
    old = (st.get(cstage) or {}).get("inputs") or {}
    return bool(old.get("toolchain")) and old.get("toolchain") != inputs["toolchain"]


def compile_outputs(btarget, bdir):
    G = g()
    exe = G.exe(bdir, btarget)
    out = [exe]
    pdb = os.path.join(os.path.dirname(exe), G.exe_name + ".pdb")
    if btarget == "windows" and os.path.isfile(pdb):
        out.append(pdb)
    return out


def output_sha(st, stage, path):
    rec = st.get(stage) or {}
    return (rec.get("outputs") or {}).get(st._store_path(path), {}).get("sha256", "")


# ── stage ────────────────────────────────────────────────────────────────


def stage_name(target):
    return "stage:" + target


def stage_inputs(target, version, enhance, seed, hud_sha, lib, exe_sha):
    G = g()
    return {
        "target": target,
        # README.txt and the launcher carry it.
        "version": version,
        "enhance": bool(enhance),
        "seed": buildstate.digest(seed) if seed is not None else "",
        "hud": hud_sha or "",
        "exe": exe_sha,
        "packaging": content_digest(G.content),
        "templates": content_digest(G.templates),
        "license": {n: _sha(os.path.join(G.root, n)) for n in ("LICENSE", "NOTICE")},
        # The templates and wrappers are the CLI's.
        "cli": tree_input(cli_dir.cli_dir(), lib),
    }


def skip_manifest(rel):
    return rel in MANIFEST_FILES


def sync_tree(src, dst, keep=("game_files",)):
    """Make dst hold src's files: a file with the same bytes is left as it
    is (its inode and mtime too, so the next stage check and the cached
    sha256s stay valid), others are moved in from src, and files only in
    dst go, except under the top-level names in keep and the manifest
    files write_manifest adds."""
    os.makedirs(dst, exist_ok=True)
    want = set()
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, src)
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            want.add(rel)
            d = os.path.join(dst, rel)
            if os.path.isfile(d) and not os.path.islink(d):
                same = os.path.getsize(s) == os.path.getsize(d) and filecmp.cmp(s, d, shallow=False)
                if same and (os.stat(s).st_mode & 0o777) == (os.stat(d).st_mode & 0o777):
                    continue
            elif os.path.isdir(d):
                shutil.rmtree(d)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.replace(s, d)
    for dirpath, dirnames, filenames in os.walk(dst, topdown=True):
        rel_dir = os.path.relpath(dirpath, dst)
        if rel_dir == ".":
            dirnames[:] = [n for n in dirnames if n not in keep]
        for fn in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            if rel not in want and not (rel_dir == "." and (fn in keep or skip_manifest(fn))):
                os.remove(os.path.join(dirpath, fn))
    for dirpath, _dirnames, _filenames in os.walk(dst, topdown=False):
        rel_dir = os.path.relpath(dirpath, dst)
        if rel_dir != "." and rel_dir.split(os.sep)[0] not in keep and not os.listdir(dirpath):
            os.rmdir(dirpath)


def _linker(hard=True):
    """clonefile where the filesystem has it (APFS: no space), else a hard
    link (the payload is read only), else a copy that keeps the mtime."""
    from . import lib as plib_mod

    clone = plib_mod._clonefile()

    def put(s, d):
        if clone is not None and clone(os.fsencode(s), os.fsencode(d), 0) == 0:
            return
        if hard:
            try:
                os.link(s, d)
                return
            except OSError:
                pass
        shutil.copy2(s, d)

    return put


def sync_game_files(src, dst, lib):
    """game_files/ into the payload in place: a file whose size and mtime_ns
    match is kept, others are cloned or hard-linked, and files the dump no
    longer has go. Returns the number of top-level entries."""
    src = os.path.realpath(src)
    put = _linker()
    os.makedirs(dst, exist_ok=True)
    want = set()
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        if rel_dir != "." and lib.game_file_excluded(rel_dir):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            n
            for n in dirnames
            if not lib.game_file_excluded(os.path.normpath(os.path.join(rel_dir, n)))
        )
        for n in dirnames:
            os.makedirs(os.path.join(dst, rel_dir, n), exist_ok=True)
        for fn in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            if lib.game_file_excluded(rel):
                continue
            want.add(rel)
            s, d = os.path.join(src, rel), os.path.join(dst, rel)
            try:
                ss, ds = os.stat(s), os.stat(d)
                if ss.st_size == ds.st_size and ss.st_mtime_ns == ds.st_mtime_ns:
                    continue
                os.remove(d)
            except FileNotFoundError:
                pass
            if os.path.isdir(d):
                shutil.rmtree(d)
            put(s, d)
    for dirpath, _dirnames, filenames in os.walk(dst, topdown=False):
        rel_dir = os.path.relpath(dirpath, dst)
        for fn in filenames:
            if os.path.normpath(os.path.join(rel_dir, fn)) not in want:
                os.remove(os.path.join(dirpath, fn))
        if rel_dir != "." and not os.listdir(dirpath):
            os.rmdir(dirpath)
        else:
            # A disc copy's r-x directories: the stage must stay removable.
            mode = os.stat(dirpath).st_mode & 0o7777
            if mode & 0o700 != 0o700:
                os.chmod(dirpath, mode | 0o700)
    return len(os.listdir(dst))


def link_tree(src, dst, hard=True):
    """A copy of src at dst by clones or hard links (the windows folder's
    game_files/, the app's Resources): seconds and no space, and src stays
    in place for the next run. hard=False: clones or plain copies only,
    for a copy that is written to afterwards."""
    put = _linker(hard)
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for fn in filenames:
            put(os.path.join(dirpath, fn), os.path.join(dst, rel, fn))


# ── wrap ─────────────────────────────────────────────────────────────────


def wrap_name(target):
    return "wrap:" + target


def wrap_tools(target):
    from .. import doctor

    if target == "windows":
        mk = doctor.makensis_path()
        return {"makensis": _out([mk, "-VERSION"]) if mk else ""}
    if target == "macos":
        # hdiutil has no version of its own: it moves with the OS.
        return {"macos": platform.mac_ver()[0]}
    return {"python": "%d.%d" % sys.version_info[:2]}


def wrap_inputs(target, manifest_sha, lib, kind="full", base="", exe=None, archive=False):
    inputs = {
        "manifest": manifest_sha,
        "kind": kind,
        "base": base,
        "tools": wrap_tools(target),
        "cli": tree_input(cli_dir.cli_dir(), lib),
    }
    if target == "macos":
        # The app's Frameworks/ and its exe are outside the manifest, which
        # lists the Resources only.
        inputs["exe"] = _sha(exe)
        inputs["dylibs"] = dylib_shas(exe, lib)
    if archive:
        inputs["archive"] = True
    return inputs


def wrap_basis(stage_key, version, lib, inputs):
    """What the manifest is made of besides the staged files (the commits
    and branches it names, its built time, the wizard's mark), with the
    wrap's other inputs and the stage key. Stored beside the wrap's key,
    so the plan can tell whether a wrap would be reused before the
    manifest exists; the run itself decides by the manifest's sha256."""
    G = g()
    trees = {}
    for name, tree in (
        ("game", G.root),
        ("toolkit", toolkit.toolkit_dir()),
        ("cli", cli_dir.cli_dir()),
    ):
        try:
            ts = lib.tree_state(tree)
            trees[name] = [ts.get("commit"), ts.get("branch"), ts.get("_dirt")]
        except (OSError, subprocess.CalledProcessError):
            trees[name] = "none"
    try:
        built = lib.build_time((G.root, toolkit.toolkit_dir())).isoformat()
    except ValueError as e:
        built = str(e)
    rest = {k: v for k, v in inputs.items() if k != "manifest"}
    return {
        "stage": stage_key,
        "version": version,
        "wrap": buildstate.digest(rest),
        "trees": buildstate.digest(trees),
        "built": built,
        "mark": buildstate.digest(lib.read_mark(G.root)),
    }


# ── bundles: the records patches are made against, and the kept two ──────

BUNDLES = "bundles"
INDEX = "index.json"
KEEP_BUNDLES = 2


def bundles_dir(st, target):
    return os.path.join(st.dir, BUNDLES, target)


def _now():
    import datetime

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def tree_size(path):
    """Bytes under path (a file or a folder), each inode once: the windows
    folder's game files are hard links of the stage's."""
    seen = set()
    total = 0
    if os.path.isfile(path) or os.path.islink(path):
        return os.lstat(path).st_size
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                s = os.lstat(os.path.join(dirpath, fn))
            except OSError:
                continue
            if (s.st_dev, s.st_ino) in seen:
                continue
            seen.add((s.st_dev, s.st_ino))
            total += s.st_size
    return total


def record_bundle(st, target, man_path, bundle, made):
    """A copy of the bundle's manifest as bundles/<target>/<sha256>.json
    (patches are made against it, D5.3), and its row in index.json: the
    version, when this folder made it, where and how big. made: the wrap
    ran (a reused bundle keeps its first time)."""
    if not os.path.isfile(man_path):
        return ""
    sha = _sha(man_path)
    d = bundles_dir(st, target)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, sha + ".json")
    if not os.path.isfile(dst):
        shutil.copyfile(man_path, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    man = buildstate.load_json(man_path, {})
    idx_path = os.path.join(d, INDEX)
    idx = buildstate.load_json(idx_path, {})
    if not isinstance(idx, dict):
        idx = {}
    e = idx.get(sha) if isinstance(idx.get(sha), dict) else {}
    if made or "made" not in e:
        e["made"] = _now()
    e.update(
        {
            "version": man.get("version", "") if isinstance(man, dict) else "",
            "bundle": st._store_path(bundle),
            "size": tree_size(bundle) if os.path.exists(bundle) else 0,
        }
    )
    idx[sha] = e
    buildstate.write_json(idx_path, idx)
    return sha


def bases(st, target):
    """The bundle records of this target, newest first: what a patch for a
    machine that has one of them is made against (the wizard's "The Deck
    has" list)."""
    d = bundles_dir(st, target)
    idx = buildstate.load_json(os.path.join(d, INDEX), {})
    out = []
    for sha, e in idx.items() if isinstance(idx, dict) else ():
        if not isinstance(e, dict) or not os.path.isfile(os.path.join(d, sha + ".json")):
            continue
        out.append(
            {
                "version": e.get("version", ""),
                "made": e.get("made", ""),
                "sha256": sha,
                "size": e.get("size", 0),
                "on_disk": os.path.exists(st._real_path(e.get("bundle", ""))),
                # The patch against it: known once phase 4's patches exist.
                "patch_size": None,
            }
        )
    out.sort(key=lambda b: (b["made"], b["version"]), reverse=True)
    return out


# ── the pack store: the HUD packs bundles were made with (D4) ────────────

PACKS = "packs"
# The packs of the newest records of each target stay: the 3 versions a
# SteamOS install keeps by default.
KEEP_PACKS = 3


def packs_dir(st):
    return os.path.join(st.dir, PACKS)


def pack_in_store(st, sha):
    return os.path.join(packs_dir(st), sha + ".pack")


def store_pack(st, src, sha):
    """The pack at src into the store as <sha256>.pack: a hard link when on
    the same volume (pack.write replaces hud.pack rather than rewriting
    it, so the link keeps these bytes), else a clone or a copy. Returns
    its path."""
    from . import lib as plib_mod

    dst = pack_in_store(st, sha)
    if os.path.isfile(dst):
        return dst
    os.makedirs(packs_dir(st), exist_ok=True)
    tmp = dst + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    try:
        os.link(src, tmp)
    except OSError:
        plib_mod.copy_file(src, tmp, plib_mod._clonefile())
    os.replace(tmp, dst)
    return dst


def record_packs(st, keep=KEEP_PACKS):
    """{sha256} of the packs the newest `keep` bundle records of each
    target name."""
    out = set()
    root = os.path.join(st.dir, BUNDLES)
    if not os.path.isdir(root):
        return out
    for target in sorted(os.listdir(root)):
        d = os.path.join(root, target)
        if not os.path.isdir(d):
            continue
        for b in bases(st, target)[:keep]:
            man = buildstate.load_json(os.path.join(d, b["sha256"] + ".json"), None)
            hp = (
                ((man or {}).get("enhance") or {}).get("hud_pack")
                if isinstance(man, dict)
                else None
            )
            if isinstance(hp, dict) and isinstance(hp.get("sha256"), str):
                out.add(hp["sha256"])
    return out


def prune_packs(st, current=None):
    """Drop the stored packs no recent record names and this run did not
    stage. Returns the removed paths."""
    d = packs_dir(st)
    if not os.path.isdir(d):
        return []
    want = record_packs(st)
    if current:
        want.add(current)
    gone = []
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if n.endswith(".pack") and n[: -len(".pack")] in want:
            continue
        if os.path.isfile(p) or os.path.islink(p):
            os.remove(p)
            gone.append(p)
    return gone


def bundle_patterns(target, app):
    import glob

    a = glob.escape(app)
    return {
        "steamos": [a + "-*-steamos.tar"],
        "windows": [a + "-*-windows"],
        "macos": [a + "-*.dmg"],
    }[target]


def patch_patterns(target, app):
    import glob

    a = glob.escape(app)
    return {
        "steamos": [a + "-*-steamos-patch-from-*.tar"],
        "windows": [a + "-*-windows-patch-from-*"],
        "macos": [a + "-*-patch-from-*.dmg"],
    }[target]


PATCH_MARK = "-patch-from-"


def bundles_on_disk(out, target, app, patches=False):
    """[(path, [paths that go with it])] of the target's full bundles in
    out (its patches with patches=True), newest first (a windows folder
    with its --archive zip). The two are kept apart: a patch is no use
    without the version it updates, so it never stands for a bundle."""
    import glob

    found = []
    for pat in (patch_patterns if patches else bundle_patterns)(target, app):
        for p in glob.glob(os.path.join(glob.escape(out), pat)):
            if not patches and PATCH_MARK in os.path.basename(p):
                continue
            extra = [p + ".zip"] if target == "windows" and os.path.isfile(p + ".zip") else []
            found.append((p, extra))
    found.sort(key=lambda x: os.lstat(x[0]).st_mtime_ns, reverse=True)
    return found
