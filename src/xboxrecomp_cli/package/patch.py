"""Patches: an update that ships only what changed since the version a
machine has current (D5, D6).

The base is that version's manifest.json, read from wherever the
developer or the host has it: a manifest file, an installed version
folder, an installed app, a bundle tar (read in place), or a record this
folder keeps of every bundle it made (.git/xbr/bundles/<target>/). The
patch is the new staged tree's manifest against it:

  - changed: in both, another sha256;
  - new: only in the new tree;
  - deleted: only in the base;
  - kept: the rest, listed with size and sha256 but not shipped.

Whole files, never binary deltas: the program changes throughout on any
code change, and a delta decoder would be one more program per platform.
"""

import hashlib
import json
import os
import re
import tarfile

from .. import host
from ..host import CliError

SCHEMA = 1
PATCH_JSON = "patch.json"
# A patch is made by default only when it is under this share of the full
# bundle: above it the full bundle costs the player little more and needs
# no base.
DEFAULT_SHARE = 0.5
GAME_DIR = "game_files/"
# D10: game files are unversioned on steamos and windows (one game_files/
# at the root), so a rollback keeps the new ones.
GAME_DATA_WARNING = "this update also changes game data; rolling back will not undo that"


class Base:
    """A base manifest: its parsed fields, the sha256 of its bytes (what
    the applier compares the installed manifest.json against) and where it
    was read from."""

    def __init__(self, data, origin, versioned=None, path=""):
        try:
            self.manifest = json.loads(data)
        except ValueError as e:
            raise CliError("%s: not a manifest (%s)" % (origin, e)) from e
        if not isinstance(self.manifest, dict) or not isinstance(self.manifest.get("files"), list):
            raise CliError("%s: not a manifest (no files list)" % origin)
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.origin = origin
        self.version = str(self.manifest.get("version", ""))
        # windows: True when read from the versioned layout (current.txt),
        # None when the source cannot tell (a manifest file, a record).
        self.versioned = versioned
        # The folder --patch-from takes for this base ("" when unknown):
        # the plan names the host's install by it, so the wizard passes
        # the same base it showed.
        self.path = path


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def from_tar(path):
    """The manifest.json at the top of a bundle tar (or a patch tar, whose
    manifest is the full one), read without unpacking the tar."""
    try:
        with tarfile.open(path) as t:
            for m in t:
                parts = m.name.strip("/").split("/")
                if len(parts) == 2 and parts[1] == "manifest.json" and m.isfile():
                    return Base(t.extractfile(m).read(), path)
    except (tarfile.TarError, OSError) as e:
        raise CliError("%s: cannot read the tar (%s)" % (path, e)) from e
    raise CliError("%s: no manifest.json in the tar's top folder" % path)


def load(spec, records=None):
    """The base named by --patch-from: a manifest.json, a version folder
    (steamos versions/<v>, windows versions\\<v>, an unpacked bundle), an
    .app, a bundle tar, or the sha256 (12 hex or more) of a bundle record
    in records (the folder of .git/xbr/bundles/<target>/)."""
    if records and re.fullmatch(r"[0-9a-f]{12,64}", spec) and not os.path.exists(spec):
        hits = []
        if os.path.isdir(records):
            hits = [n for n in os.listdir(records) if n.endswith(".json") and n.startswith(spec)]
            hits = [n for n in hits if re.fullmatch(r"[0-9a-f]{64}\.json", n)]
        if len(hits) != 1:
            raise CliError(
                "--patch-from %s: %s bundle record" % (spec, "no" if not hits else "more than one")
            )
        p = os.path.join(records, hits[0])
        return Base(_read(p), "bundle record %s" % hits[0][:12])
    p = os.path.abspath(os.path.expanduser(spec))
    if os.path.isdir(p):
        app_man = os.path.join(p, "Contents", "Resources", "manifest.json")
        if os.path.isfile(app_man):
            return Base(_read(app_man), p)
        man = os.path.join(p, "manifest.json")
        if os.path.isfile(man):
            parent = os.path.dirname(p)
            versioned = None
            if os.path.basename(parent).lower() == "versions":
                versioned = os.path.isfile(os.path.join(os.path.dirname(parent), "current.txt"))
            return Base(_read(man), p, versioned)
        raise CliError("--patch-from %s: no manifest.json there" % spec)
    if os.path.isfile(p):
        if tarfile.is_tarfile(p):
            return from_tar(p)
        return Base(_read(p), p)
    raise CliError("--patch-from %s: not found" % spec)


def entries(manifest):
    """{path: (sha256, size)} of a manifest's files; None when an entry
    lacks either (such a base cannot be checked)."""
    out = {}
    for f in manifest.get("files", []):
        if not isinstance(f, dict):
            return None
        path, sha, size = f.get("path"), f.get("sha256"), f.get("size")
        if not isinstance(path, str) or not isinstance(sha, str) or not isinstance(size, int):
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        out[path] = (sha, size)
    return out


# The reasons' codes (the plan's why_codes, and Refusal's code): a front
# end tells the reasons apart by these, never by their text.
PRODUCT = "product"
TARGET = "target"
LAYOUT = "data_layout"
ENTRIES = "entries"
IN_PLACE = "in_place"
STUB = "stub_abi"
GAME_FILES = "game_files"
GAME_DELETES = "game_deletes"
SAME = "same_version"
SHARE = "share"
UNREADABLE = "unreadable"
HUD_GUARD = "hud_guard"
HUD_KEEP = "hud_keep"
NO_BASE = "no_base"
# Too little free disk for the run's stages (resources.check_disk).
DISK = "disk"
# The flag that lets a patch remove game files (D10: a rollback does not
# bring them back).
ALLOW_DELETES = "--allow-game-file-deletes"


def problem_codes(base, new, target, imported=False, early=False):
    """Why the base cannot take this patch, as [(code, reason)] ([] when it
    can): D5's rules. early: only what the two manifests' fixed fields
    decide (new holds just those), before anything is built; the version
    and the game files are judged on the built manifest. imported: this
    run imported the game files (--dump), so other game files are the
    point of the run, not a base built from another tree."""
    bm, nm = base.manifest, new
    why = []
    if bm.get("product") != nm.get("product"):
        why.append((PRODUCT, "another game (%s)" % bm.get("product")))
    if bm.get("target") != nm.get("target"):
        why.append((TARGET, "another target (%s)" % bm.get("target")))
    if bm.get("data_layout") != nm.get("data_layout"):
        why.append((LAYOUT, "game data layout changed"))
    if entries(bm) is None:
        why.append((ENTRIES, "the base lists a file without its sha256 and size"))
    if target == "windows":
        if base.versioned is False:
            why.append(
                (IN_PLACE, "an in-place install (no current.txt): the full installer migrates it")
            )
        b_abi = (bm.get("root") or {}).get("stub_abi")
        n_abi = (nm.get("root") or {}).get("stub_abi")
        if b_abi is None:
            why.append((IN_PLACE, "an in-place install (no versioned layout)"))
        elif b_abi != n_abi:
            why.append((STUB, "the launcher stub changed (stub_abi %s to %s)" % (b_abi, n_abi)))
    if early:
        return why
    if not imported and entries(bm) is not None:
        other = tree_diff(game_tree(bm), game_tree(nm))
        if other:
            why.append((GAME_FILES, "built from other game files (%s)" % other))
    if base.version == nm.get("version") and not why:
        why.append((SAME, "it is this version already"))
    return why


def problems(base, new, target, imported=False):
    return [m for _c, m in problem_codes(base, new, target, imported)]


def xbe_path():
    """The XBE's path in a manifest (game.toml's xbe.path under game_files/)."""
    G = host.g()
    rel = os.path.relpath(G.xbe, G.game_files).replace(os.sep, "/")
    return GAME_DIR + rel


def game_tree(manifest):
    """The game files' identity: the manifest's game_tree when it records
    one, else the same fields from its game_files/ entries (a manifest
    made before it). {"xbe_sha256", "files", "listing"}: the XBE's
    sha256, the file count and the sha256 of the sorted (path, sha256,
    size) lines."""
    rec = manifest.get("game_tree")
    if isinstance(rec, dict) and all(k in rec for k in ("xbe_sha256", "files", "listing")):
        return {k: rec[k] for k in ("xbe_sha256", "files", "listing")}
    return tree_of(manifest.get("files", []))


def tree_of(files):
    games = sorted(
        (f["path"], f["sha256"], f["size"])
        for f in files
        if isinstance(f, dict) and str(f.get("path", "")).startswith(GAME_DIR)
    )
    xbe = xbe_path()
    text = "".join("%s %s %d\n" % e for e in games)
    return {
        "xbe_sha256": next((sha for path, sha, _n in games if path == xbe), ""),
        "files": len(games),
        "listing": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def tree_diff(a, b):
    """'' when two game trees are the same, else what differs, for the
    reason (the XBE's sha256 and the count when they differ)."""
    if a["listing"] == b["listing"]:
        return ""
    out = []
    if a["xbe_sha256"] != b["xbe_sha256"]:
        out.append(
            "XBE %s, now %s" % (a["xbe_sha256"][:8] or "none", b["xbe_sha256"][:8] or "none")
        )
    if a["files"] != b["files"]:
        out.append("%d files, now %d" % (a["files"], b["files"]))
    return "; ".join(out) or "the same count, other contents"


def game_counts(d):
    """{changed, new, deleted}: the game files a patch changes, adds and
    removes."""
    return {
        k: sum(1 for e in d[k] if e["path"].startswith(GAME_DIR))
        for k in ("changed", "new", "deleted")
    }


def deletes_problem(base, d, allow):
    """(code, reason) when the patch removes game files and may not, else
    None. A base whose manifest records no game_tree cannot say what tree
    it was built from, so its deletions are refused even with the flag."""
    n = game_counts(d)["deleted"]
    if not n:
        return None
    if not isinstance(base.manifest.get("game_tree"), dict):
        return (
            GAME_DELETES,
            "the patch would remove %d game files, and the base records no game tree" % n,
        )
    if allow:
        return None
    return (
        GAME_DELETES,
        "the patch would remove %d game files (%s allows it)" % (n, ALLOW_DELETES),
    )


def diff(base_manifest, new_manifest):
    """changed, new, deleted and kept, each a sorted list of
    {path, sha256, size} (deleted: the base's entry)."""
    b, n = entries(base_manifest) or {}, entries(new_manifest)
    if n is None:
        raise CliError("the new manifest lists a file without its sha256 and size")
    out = {"changed": [], "new": [], "deleted": [], "kept": []}
    for path in sorted(set(b) | set(n)):
        if path not in b:
            kind, (sha, size) = "new", n[path]
        elif path not in n:
            kind, (sha, size) = "deleted", b[path]
        else:
            kind, (sha, size) = ("kept" if b[path] == n[path] else "changed"), n[path]
        out[kind].append({"path": path, "sha256": sha, "size": size})
    return out


def shipped(d):
    return d["changed"] + d["new"]


def payload_size(d):
    return sum(e["size"] for e in shipped(d))


def full_size(manifest):
    return sum(int(f.get("size", 0)) for f in manifest.get("files", []))


def changes_game_data(d):
    return any(e["path"].startswith(GAME_DIR) for k in ("changed", "new", "deleted") for e in d[k])


def patch_doc(base, new_manifest, d):
    return {
        "schema": SCHEMA,
        "base_version": base.version,
        "base_manifest_sha256": base.sha256,
        "version": new_manifest["version"],
        "changed": d["changed"],
        "new": d["new"],
        "deleted": d["deleted"],
        "kept": d["kept"],
        "game_data": changes_game_data(d),
    }


def write_doc(dst, doc):
    with open(dst, "w", newline="\n") as f:
        f.write(json.dumps(doc, indent=1) + "\n")


def short(version):
    """The c<game>-g<gen> part of a version: the patch's file name says
    what it applies to without the base's whole version."""
    c = re.search(r"(?:^|-)(c[0-9a-f]+)(?:-|$)", version)
    gm = re.search(r"(?:^|-)(g[0-9a-f]+)(?:-|$)", version)
    if c and gm:
        return "%s-%s" % (c.group(1), gm.group(1))
    return hashlib.sha256(version.encode()).hexdigest()[:8]


# ── the base on this host (D5.2) ─────────────────────────────────────────


def steamos_root(m):
    env = m["game"]["slug"].upper().replace("-", "_") + "_ROOT"
    return os.path.expanduser(os.environ.get(env) or m["data"]["steamos"])


def host_base(target, app, m):
    """The installed version that is current on this host, when the target
    is the host's own platform; None otherwise. Only the current version is
    a base: a patch against a merely kept one would also move the program
    past what the player runs."""
    hos = host.host_os()
    if target == "steamos" and hos == "linux":
        root = steamos_root(m)
        try:
            cur = os.path.basename(os.readlink(os.path.join(root, "current")))
        except OSError:
            return None
        man = os.path.join(root, "versions", cur, "manifest.json")
        if not os.path.isfile(man):
            return None
        return Base(_read(man), "installed %s" % cur, path=os.path.dirname(man))
    if target == "macos" and hos == "macos":
        for d in ("/Applications", os.path.expanduser("~/Applications")):
            man = os.path.join(d, app + ".app", "Contents", "Resources", "manifest.json")
            if os.path.isfile(man):
                at = os.path.join(d, app + ".app")
                return Base(_read(man), at, path=at)
        return None
    if target == "windows" and hos == "windows":
        loc = windows_install_location(app)
        if not loc:
            return None
        cur_txt = os.path.join(loc, "current.txt")
        if not os.path.isfile(cur_txt):
            # The in-place layout: the full installer migrates it (D8).
            man = os.path.join(loc, "manifest.json")
            return Base(_read(man), loc, versioned=False, path=loc) if os.path.isfile(man) else None
        with open(cur_txt, encoding="utf-8") as f:
            cur = f.read().strip()
        man = os.path.join(loc, "versions", cur, "manifest.json")
        if not os.path.isfile(man):
            return None
        return Base(_read(man), "installed %s" % cur, versioned=True, path=os.path.dirname(man))
    return None


def windows_install_location(app):
    try:
        import winreg
    except ImportError:
        return None
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + app
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return None


# ── the choice ───────────────────────────────────────────────────────────


class Choice:
    """What the wrap makes: kind "full" or "patch", the base, the diff and
    why (a line for the log, and the wizard's reason for a full bundle)."""

    def __init__(self, kind, why, base=None, d=None):
        self.kind, self.why, self.base, self.diff = kind, why, base, d


def choose(a, new_manifest, target, app, m, records):
    """D5: --full makes the full bundle; --patch-from a patch against that
    base (refused when it cannot take one); otherwise a patch against the
    host's own install when it can take one and the patch is under half
    the full bundle, else the full bundle."""
    if getattr(a, "full", False):
        return Choice("full", "--full")
    imported = bool(getattr(a, "dump", None))
    allow = bool(getattr(a, "allow_game_file_deletes", False))
    spec = getattr(a, "patch_from", None)
    if spec:
        # Strict (D7): a named base gets a patch or a refusal, never a
        # full bundle the caller did not ask for.
        base = load(spec, records)
        why, d = judge(base, new_manifest, target, imported, allow)
        if why:
            raise host.Refusal(
                "--patch-from %s (%s): %s; make the full bundle with --full"
                % (spec, base.version or "?", "; ".join(m for _c, m in why)),
                why[0][0],
            )
        return Choice("patch", "from %s (--patch-from)" % base.version, base, d)
    base = host_base(target, app, m)
    if base is None:
        return Choice("full", "no install found")
    why, d = judge(base, new_manifest, target, imported, allow)
    if why and why[0][0] == SHARE:
        return Choice("full", why[0][1])
    if why:
        return Choice(
            "full",
            "the installed %s: %s" % (base.version or "?", "; ".join(m for _c, m in why)),
        )
    return Choice("patch", "from the installed %s" % base.version, base, d)


def judge(base, new_manifest, target, imported, allow):
    """([(code, reason)], the diff or None): every rule, the ones on the
    diff (game files removed, the half-size rule) once the rest pass."""
    why = problem_codes(base, new_manifest, target, imported)
    if why:
        return why, None
    d = diff(base.manifest, new_manifest)
    dp = deletes_problem(base, d, allow)
    if dp:
        return [dp], d
    share = share_problem(d, new_manifest)
    return ([(SHARE, share)] if share else []), d


def share_problem(d, new_manifest):
    """The half-size rule: "" when the patch is under DEFAULT_SHARE of the
    full bundle, else the reason."""
    size, full = payload_size(d), full_size(new_manifest)
    if full and size >= DEFAULT_SHARE * full:
        return "a patch would be %d%% of the full bundle" % (100 * size // full)
    return ""
