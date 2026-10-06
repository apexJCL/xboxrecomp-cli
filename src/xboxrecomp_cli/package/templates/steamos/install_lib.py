#!/usr/bin/env python3
"""@NAME@ installer for a SteamOS PC: a Steam Deck, or a gaming distribution
that ships umu-run (the steamos bundle).

Run through install.sh from the bundle directory:

  ./install.sh [install] [--root DIR] [--keep N] [--steam] [--import-saves DIR]
               [--fetch-umu] [--umu-dir DIR]
  ./install.sh rollback [VERSION] | status | list | uninstall   [--root DIR]

Layout under the root (default @DEFAULT_ROOT@):

  @APP@                 stable launcher (the Steam shortcut's target)
  current -> versions/<v>
  versions/<v>/          program files, hard-linked against the previous version
  game_files/            the dump, synced from the bundle on every install
  state/                 history, layout, umu-run (the umu-run install found)
  prefix/                Wine prefix, made by the first launch; disposable
  hdd/ config/ logs/     yours: saves, settings, logs

hdd/, config/ and logs/ are never written by any command here: install
only creates an absent config/, and --import-saves copies into an absent or
empty hdd/. Standard library only; Python 3.9 is enough.

The game runs under Proton through umu-run (umu-launcher). Some gaming
distributions ship it; a stock Steam Deck does not, so install finds it on PATH, in
~/.local/bin or in the copy it fetched itself, and otherwise offers to
download the one release the CLI pins, checked against its sha256.
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

PRODUCT = "@PRODUCT@"
NAME = "@NAME@"
APP = "@APP@"
EXE = "@EXE@"
# manifest.json "sources" names the game's tree by its pipeline name.
SOURCE = "@SOURCE_NAME@"
LAYOUT = 1
DEFAULT_ROOT = "@DEFAULT_ROOT@"
ROOT_ENV = "@ROOT_ENV@"
DEFAULT_KEEP = 3
USER_DIRS = ("hdd", "config", "logs")
NOTICE = (
    "PRIVATE: this install holds your own copy of %s and code generated "
    "from it. It is for your own machines only: never share, upload or publish it." % NAME
)

STABLE_LAUNCHER = (
    """#!/bin/sh
# %s: the Steam shortcut's target. Runs the current version's launcher.
# Written by install.sh; a real file (not a link) so that Steam, which
# resolves links when it adds a shortcut, keeps pointing here.
exec "$(dirname "$(readlink -f "$0")")/current/launch.sh" "$@"
"""
    % NAME
)


class InstallError(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── bundle ───────────────────────────────────────────────────────────────


def read_sums(path):
    sums = {}
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
            if not m:
                raise InstallError("%s:%d: not a sha256sum line" % (path, n))
            sums[m.group(2)] = m.group(1)
    return sums


class Bundle:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        mp = os.path.join(self.path, "manifest.json")
        if not os.path.isfile(mp):
            raise InstallError("%s is not a %s bundle (no manifest.json)" % (self.path, APP))
        with open(mp) as f:
            self.manifest = json.load(f)
        if self.manifest.get("product") != PRODUCT:
            raise InstallError("%s: manifest names another product" % mp)
        self.version = self.manifest["version"]
        self.sums = read_sums(os.path.join(self.path, "SHA256SUMS"))

    def verify(self):
        """sha256 of every payload file against SHA256SUMS."""
        for rel, want in sorted(self.sums.items()):
            p = os.path.join(self.path, rel)
            if not os.path.isfile(p):
                raise InstallError("bundle file missing: %s" % rel)
            if sha256_file(p) != want:
                raise InstallError("bundle file damaged (checksum mismatch): %s" % rel)

    def program_files(self):
        """Top-level files: the program, launch.sh, defaults, docs, the
        installer itself. game_files/ is installed once at the root."""
        return sorted(
            n
            for n in os.listdir(self.path)
            if os.path.isfile(os.path.join(self.path, n)) and n != ".DS_Store"
        )

    def game_files(self):
        base = os.path.join(self.path, "game_files")
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for f in sorted(filenames):
                if f == ".DS_Store":
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, f), base))
        return out

    def size(self, names, under=""):
        return sum(os.path.getsize(os.path.join(self.path, under, n)) for n in names)


# ── install root ─────────────────────────────────────────────────────────


class Root:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))

    def p(self, *parts):
        return os.path.join(self.path, *parts)

    def current(self):
        try:
            return os.path.basename(os.readlink(self.p("current")))
        except OSError:
            return None

    def history(self):
        try:
            with open(self.p("state", "history")) as f:
                lines = [line.split() for line in f if line.strip()]
        except OSError:
            return []
        # "<utc> <what> <old> -> <new>"
        return [
            {"time": parts[0], "what": parts[1], "old": parts[2], "new": parts[4]}
            for parts in lines
            if len(parts) == 5 and parts[3] == "->"
        ]

    def previous(self):
        cur = self.current()
        for h in reversed(self.history()):
            if h["new"] == cur and h["old"] not in ("-", cur):
                return h["old"]
        return None

    def versions(self):
        """Installed versions of this product, oldest first by last switch
        time (then by name, which starts with the build date)."""
        vdir = self.p("versions")
        if not os.path.isdir(vdir):
            return []
        mine = [v for v in os.listdir(vdir) if is_product_dir(os.path.join(vdir, v))]
        last = {}
        for i, h in enumerate(self.history()):
            last[h["new"]] = i
        return sorted(mine, key=lambda v: (last.get(v, -1), v))

    def manifest(self, version):
        with open(self.p("versions", version, "manifest.json")) as f:
            return json.load(f)


def is_product_dir(d):
    if d.endswith(".partial") or not os.path.isdir(d):
        return False
    try:
        with open(os.path.join(d, "manifest.json")) as f:
            return json.load(f).get("product") == PRODUCT
    except (OSError, ValueError):
        return False


# ── checks ───────────────────────────────────────────────────────────────


def bench_dir():
    """The dev/bench tree on this host: BENCH_DIR from the environment or
    from the bench's own bench.env, else ~/xbox-recomp."""
    d = os.environ.get("BENCH_DIR")
    env = os.path.expanduser("~/xbox-recomp/@SOURCE_NAME@/scripts/bench.env")
    if not d and os.path.isfile(env):
        with open(env) as f:
            for line in f:
                m = re.match(r"^\s*BENCH_DIR=(.*)$", line.strip())
                if m:
                    d = m.group(1).strip().strip("'\"")
    return os.path.realpath(os.path.expanduser(d or "~/xbox-recomp"))


def check_not_dev_tree(root, bench=None):
    real = os.path.realpath(root)
    bench = bench or bench_dir()
    if real == bench or real.startswith(bench.rstrip("/") + "/"):
        raise InstallError("refusing %s: it is inside the dev/bench tree %s" % (root, bench))
    for marker in (".git", "@SOURCE_NAME@", "xboxrecomp"):
        if os.path.exists(os.path.join(real, marker)):
            raise InstallError("refusing %s: it holds %s/ (a source checkout?)" % (root, marker))
    d = os.path.dirname(real)
    while d and d != "/":
        if os.path.exists(os.path.join(d, ".git")):
            raise InstallError("refusing %s: it is inside the git checkout %s" % (root, d))
        d = os.path.dirname(d)


def check_host():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise InstallError(
            "this bundle is for a Linux x86-64 PC (this is %s %s)"
            % (platform.system(), platform.machine())
        )


# ── umu-run ──────────────────────────────────────────────────────────────

# The one umu-launcher release --fetch-umu downloads: the CLI's pin (pins.py),
# written in when the bundle is packaged. Nothing else is ever fetched.
UMU_VERSION = "@UMU_VERSION@"
UMU_URL = "@UMU_URL@"
UMU_SHA256 = "@UMU_SHA256@"
UMU_MEMBER = "umu/umu-run"


def data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def default_umu_dir():
    """Where --fetch-umu puts its copy: one per user, shared by every game
    this CLI packages, outside any install root (uninstall keeps it)."""
    return os.path.join(data_home(), "xboxrecomp", "umu")


def is_exe(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def find_umu(umu_dir=None):
    """(path, where) of the umu-run to use, or (None, None). The launcher
    searches the same places, since Game Mode's PATH lacks ~/.local/bin."""
    p = shutil.which("umu-run")
    if p:
        return os.path.abspath(p), "PATH"
    p = os.path.expanduser("~/.local/bin/umu-run")
    if is_exe(p):
        return p, "~/.local/bin"
    for d in (umu_dir, default_umu_dir()):
        if d and is_exe(os.path.join(d, "umu-run")):
            return os.path.join(os.path.abspath(d), "umu-run"), "fetched copy"
    return None, None


def download(url, dst):
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "%s-install" % PRODUCT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)


def fetch_umu(umu_dir, fetch=None):
    """The pinned umu-launcher zipapp into <umu_dir>/umu-launcher-<v>/,
    checked against its sha256 before anything is unpacked, then
    <umu_dir>/umu-run linked to it. Returns the umu-run path."""
    if "@" in UMU_URL or not re.match(r"^[0-9a-f]{64}$", UMU_SHA256):
        raise InstallError("this bundle carries no umu-launcher pin; install umu-launcher yourself")
    fetch = fetch or download
    umu_dir = os.path.abspath(os.path.expanduser(umu_dir))
    vdir = os.path.join(umu_dir, "umu-launcher-%s" % UMU_VERSION)
    exe = os.path.join(vdir, "umu-run")
    link = os.path.join(umu_dir, "umu-run")
    if not is_exe(exe):
        os.makedirs(umu_dir, exist_ok=True)
        tar = os.path.join(umu_dir, ".umu-launcher-%s.tar.part" % UMU_VERSION)
        say("umu: downloading umu-launcher %s ..." % UMU_VERSION)
        try:
            try:
                fetch(UMU_URL, tar)
            except Exception as e:
                raise InstallError("umu: download failed: %s (%s)" % (e, UMU_URL)) from e
            got = sha256_file(tar)
            if got != UMU_SHA256:
                raise InstallError(
                    "umu: the download's sha256 is %s, not the pinned %s; nothing installed"
                    % (got, UMU_SHA256)
                )
            import tarfile

            # Only the one program file is taken (the tar also holds a
            # link to it): no path from the archive is ever written.
            with tarfile.open(tar) as t:
                try:
                    m = t.getmember(UMU_MEMBER)
                except KeyError:
                    raise InstallError("umu: %s is not in the release tar" % UMU_MEMBER) from None
                if not m.isfile():
                    raise InstallError("umu: %s in the release tar is not a file" % UMU_MEMBER)
                os.makedirs(vdir, exist_ok=True)
                src = t.extractfile(m)
                with open(exe + ".tmp-install", "wb") as f:
                    shutil.copyfileobj(src, f)
            os.chmod(exe + ".tmp-install", 0o755)
            os.replace(exe + ".tmp-install", exe)
        finally:
            if os.path.exists(tar):
                os.remove(tar)
    if os.path.realpath(link) != os.path.realpath(exe):
        tmp = link + ".new"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.relpath(exe, umu_dir), tmp)
        os.replace(tmp, link)
    say("umu: %s (umu-launcher %s, sha256 checked)" % (link, UMU_VERSION))
    return link


def ask_yes(question):
    if not sys.stdin.isatty():
        return False
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ensure_umu(want_fetch, umu_dir, fetch=None, ask=None):
    """The umu-run this install uses: found, or fetched when asked
    (--fetch-umu, or yes at the prompt)."""
    target = umu_dir or default_umu_dir()
    if want_fetch:
        return fetch_umu(target, fetch)
    p, where = find_umu(umu_dir)
    if p:
        say("umu: %s (%s)" % (p, where))
        return p
    say(
        "umu: umu-run is not installed. It runs the game under Proton; some gaming "
        "distributions ship it, a stock Steam Deck does not."
    )
    if (ask or ask_yes)(
        "Download umu-launcher %s (the pinned release, sha256 checked) into %s?"
        % (UMU_VERSION, target)
    ):
        return fetch_umu(target, fetch)
    raise InstallError(
        "umu-run is not installed; run './install.sh --fetch-umu' to download the "
        "pinned umu-launcher %s into %s, or install umu-launcher yourself" % (UMU_VERSION, target)
    )


def record_umu(root, path):
    """state/umu-run: the launcher's last place to look (a --umu-dir)."""
    p = root.p("state", "umu-run")
    with open(p + ".tmp-install", "w") as f:
        f.write(path + "\n")
    os.replace(p + ".tmp-install", p)


def free_bytes(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free


def check_ours(root, installing):
    """Only a root this installer made is ever changed: install refuses an
    existing, non-empty directory without state/layout (it would sync
    game_files/ and prune there), rollback and uninstall refuse any root
    without it (uninstall deletes trees)."""
    marker = root.p("state", "layout")
    if os.path.isfile(marker):
        return
    # What uninstall leaves (the user-data folders alone) takes a fresh
    # install again: reinstalling after an uninstall must find the saves.
    if installing and (
        not os.path.isdir(root.path)
        or all(n in USER_DIRS and os.path.isdir(root.p(n)) for n in os.listdir(root.path))
    ):
        return
    raise InstallError(
        "%s is not a %s install root (no state/layout); refusing to %s it"
        % (root.path, APP, "install into" if installing else "change")
    )


def check_layout(root):
    try:
        with open(root.p("state", "layout")) as f:
            have = int(f.read().strip() or 0)
    except OSError:
        return
    if have > LAYOUT:
        raise InstallError(
            "%s was installed by a newer installer (layout %d > %d); "
            "install a newer bundle" % (root.path, have, LAYOUT)
        )


# ── steps ────────────────────────────────────────────────────────────────


def same_file(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def copy_atomic(src, dst):
    tmp = dst + ".tmp-install"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def install_program(bundle, root):
    """versions/<v>.partial/, hard-linked against the current version where
    a file is unchanged, verified, then renamed into place."""
    v = bundle.version
    final = root.p("versions", v)
    if os.path.isdir(final) and same_file(
        os.path.join(final, "SHA256SUMS"), os.path.join(bundle.path, "SHA256SUMS")
    ):
        return False
    part = final + ".partial"
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(part)
    cur = root.current()
    prev_dir = root.p("versions", cur) if cur else None
    try:
        for name in bundle.program_files():
            src, dst = os.path.join(bundle.path, name), os.path.join(part, name)
            old = os.path.join(prev_dir, name) if prev_dir else None
            if old and same_file(src, old):
                os.link(old, dst)
            else:
                shutil.copy2(src, dst)
        for name in bundle.program_files():
            want = bundle.sums.get(name)
            if want and sha256_file(os.path.join(part, name)) != want:
                raise InstallError("copy damaged (checksum mismatch): %s" % name)
        for exe in ("launch.sh", "install.sh"):
            p = os.path.join(part, exe)
            if os.path.exists(p):
                os.chmod(p, 0o755)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    shutil.rmtree(final, ignore_errors=True)
    os.rename(part, final)
    return True


def sync_game_files(bundle, root):
    """root/game_files = the bundle's, by checksum; extra files there are
    removed (this directory only). Returns (copied, removed)."""
    src = os.path.join(bundle.path, "game_files")
    dst = root.p("game_files")
    want = set(bundle.game_files())
    copied = removed = 0
    for rel in sorted(want):
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        if os.path.isfile(d) and not os.path.islink(d):
            if bundle.sums.get("game_files/" + rel.replace(os.sep, "/")) == sha256_file(d):
                continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.isdir(d) and not os.path.islink(d):
            shutil.rmtree(d)
        copy_atomic(s, d)
        copied += 1
    for dirpath, _dirnames, filenames in os.walk(dst, topdown=False):
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), dst)
            if rel not in want:
                os.remove(os.path.join(dirpath, f))
                removed += 1
        if dirpath != dst and not os.listdir(dirpath):
            os.rmdir(dirpath)
    return copied, removed


def switch(root, version, what):
    """current -> versions/<version>, by one rename(2) of a relative link,
    so current is never missing; then the history line."""
    old = root.current() or "-"
    tmp = root.p("current.new")
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.join("versions", version), tmp)
    os.replace(tmp, root.p("current"))
    os.makedirs(root.p("state"), exist_ok=True)
    with open(root.p("state", "history"), "a") as f:
        f.write("%s %s %s -> %s\n" % (utcnow(), what, old, version))
    return old


def write_stable_launcher(root):
    p = root.p(APP)
    try:
        with open(p) as f:
            if f.read() == STABLE_LAUNCHER and os.access(p, os.X_OK):
                return
    except OSError:
        pass
    if os.path.islink(p):
        os.remove(p)
    with open(p + ".tmp-install", "w") as f:
        f.write(STABLE_LAUNCHER)
    os.chmod(p + ".tmp-install", 0o755)
    os.replace(p + ".tmp-install", p)


DESKTOP_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_arg(path):
    """One Exec= argument, quoted per the Desktop Entry spec: inside double
    quotes ", `, $ and \\ take a backslash; then the value itself escapes
    backslashes again."""
    if any(c in DESKTOP_RESERVED for c in path):
        path = '"%s"' % re.sub(r'(["`$\\])', r"\\\1", path)
    return path.replace("\\", "\\\\")


def desktop_name(root):
    """The Name= of the installed .desktop entry (the Steam shortcut's
    name), or None."""
    try:
        with open(root.p(APP + ".desktop")) as f:
            for line in f:
                if line.startswith("Name="):
                    return line[5:].strip()
    except OSError:
        pass
    return None


def write_desktop(root, name):
    """<root>/<app>.desktop: what add_to_steam hands to Steam, which takes
    the shortcut's name and icon from it (the icon: current/icon.png, the
    game's title image the bundle carries)."""
    p = root.p(APP + ".desktop")
    text = (
        "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nIcon=%s\nTerminal=false\n"
        "Categories=Game;\n"
        % (
            name,
            desktop_exec_arg(root.p(APP)),
            root.p("current", "icon.png").replace("\\", "\\\\"),
        )
    )
    with open(p + ".tmp-install", "w") as f:
        f.write(text)
    os.replace(p + ".tmp-install", p)
    return p


def prune(root, keep):
    """Keep the newest `keep` versions plus current and previous; remove
    only this product's version dirs, and .partial dirs older than a day."""
    vdir = root.p("versions")
    removed, foreign = [], []
    if not os.path.isdir(vdir):
        return removed, foreign
    mine = root.versions()
    protect = set(mine[-keep:]) if keep > 0 else set()
    protect |= {root.current(), root.previous()}
    for v in mine:
        if v not in protect:
            shutil.rmtree(os.path.join(vdir, v))
            removed.append(v)
    now = datetime.datetime.now().timestamp()
    for name in sorted(os.listdir(vdir)):
        d = os.path.join(vdir, name)
        if name.endswith(".partial"):
            if now - os.path.getmtime(d) > 86400:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(name)
        elif name not in mine and name not in removed:
            foreign.append(name)
    return removed, foreign


def add_to_steam(root):
    # The desktop entry gives the shortcut its name and icon; by hand,
    # Steam's dialog takes the launcher itself.
    target = root.p(APP + ".desktop")
    helper = shutil.which("steamos-add-to-steam")
    manual = (
        "  Steam (Desktop Mode) > Games > Add a Non-Steam Game to My Library >"
        " Browse > %s" % root.p(APP)
    )
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        say(
            "steam: no desktop session here; run './install.sh --steam' again from "
            "Desktop Mode with Steam open, or add it by hand:\n" + manual
        )
        return False
    if not helper:
        say("steam: steamos-add-to-steam is not installed; add the game by hand:\n" + manual)
        return False
    if subprocess.run(["pgrep", "-x", "steam"], stdout=subprocess.DEVNULL).returncode != 0:
        say(
            "steam: Steam is not running; open Steam and run './install.sh --steam' "
            "again, or add it by hand:\n" + manual
        )
        return False
    r = subprocess.run([helper, target])
    if r.returncode != 0:
        say(
            "steam: steamos-add-to-steam failed (exit %d); add it by hand:\n%s"
            % (r.returncode, manual)
        )
        return False
    say(
        "steam: added %s to Steam as a non-Steam game. Leave 'Force the use of a "
        "specific Steam Play compatibility tool' off for it." % target
    )
    return True


def import_saves(root, src):
    hdd = root.p("hdd")
    if os.path.isdir(hdd) and os.listdir(hdd):
        raise InstallError("--import-saves: %s is not empty; it never overwrites saves" % hdd)
    src = os.path.abspath(os.path.expanduser(src))
    parts = [
        n
        for n in ("UserData", "TitleData", "UDATA", "TDATA")
        if os.path.isdir(os.path.join(src, n))
    ]
    if not parts:
        raise InstallError(
            "--import-saves: %s has no UserData/, TitleData/, UDATA/ or TDATA/" % src
        )
    os.makedirs(hdd, exist_ok=True)
    for n in parts:
        shutil.copytree(os.path.join(src, n), os.path.join(hdd, n))
    return parts


def running_games(bundle_dir):
    rg = os.path.join(bundle_dir, "running_game.py")
    try:
        if os.path.isfile(rg):
            out = subprocess.run([sys.executable, rg], stdout=subprocess.PIPE).stdout.decode()
        else:
            out = subprocess.run(["pgrep", "-af", EXE], stdout=subprocess.PIPE).stdout.decode()
    except OSError:
        return []
    return [line for line in out.splitlines() if line.strip()]


def du(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_blocks * 512
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


# ── commands ─────────────────────────────────────────────────────────────


def cmd_install(
    bundle,
    root,
    keep,
    steam,
    saves_from,
    host_check=True,
    bench=None,
    fetch_umu_now=False,
    umu_dir=None,
    umu=None,
):
    """umu: ensure_umu's stand-in for the tests (None runs the real one)."""
    if host_check:
        check_host()
    check_not_dev_tree(root.path, bench)
    check_ours(root, installing=True)
    check_layout(root)
    umu_run = (umu or ensure_umu)(fetch_umu_now, umu_dir)
    prog = bundle.program_files()
    need = 2 * bundle.size(prog)
    if not os.path.isfile(root.p("game_files", "default.xbe")):
        need += bundle.size(bundle.game_files(), "game_files")
    if free_bytes(root.path) < need:
        raise InstallError("not enough space at %s: need %s" % (root.path, human(need)))
    say("verifying the bundle (%s) ..." % bundle.version)
    bundle.verify()
    os.makedirs(root.p("versions"), exist_ok=True)
    os.makedirs(root.p("state"), exist_ok=True)
    with open(root.p("state", "layout"), "w") as f:
        f.write("%d\n" % LAYOUT)
    if umu_run:
        record_umu(root, umu_run)
    # config/ exists from the start, so a setting can be added to
    # config/launch.env by hand before the first launch; what is in it is
    # never touched.
    if not os.path.isdir(root.p("config")):
        os.makedirs(root.p("config"))
        say("config: created %s" % root.p("config"))
    copied = install_program(bundle, root)
    say(
        "program: %s"
        % (
            "installed versions/%s" % bundle.version
            if copied
            else "versions/%s already installed" % bundle.version
        )
    )
    gc, gr = sync_game_files(bundle, root)
    say("game files: %d copied, %d removed, %d unchanged" % (gc, gr, len(bundle.game_files()) - gc))
    cur = root.current()
    if cur != bundle.version:
        old = switch(root, bundle.version, "install")
        say("current: %s -> %s" % (old, bundle.version))
    else:
        say("current: %s (unchanged)" % cur)
    write_stable_launcher(root)
    write_desktop(root, bundle.manifest["name"])
    removed, foreign = prune(root, keep)
    say("kept: %s" % ", ".join(root.versions()))
    if removed:
        say("removed: %s" % ", ".join(removed))
    for f in foreign:
        say("left alone: versions/%s (not a %s version)" % (f, APP))
    if saves_from:
        say("imported saves: %s" % ", ".join(import_saves(root, saves_from)))
    if steam:
        add_to_steam(root)
    if "-dirty" in bundle.version:
        say("WARNING: %s was built from uncommitted changes" % bundle.version)
    say("saves: %s (never touched by install, update, rollback or uninstall)" % root.p("hdd"))
    say("launch: %s" % root.p(APP))
    say()
    say(NOTICE)


def cmd_rollback(root, version=None):
    check_ours(root, installing=False)
    cur = root.current()
    target = version or root.previous()
    if not target:
        raise InstallError("no previous version to roll back to")
    if not is_product_dir(root.p("versions", target)):
        raise InstallError("version %s is not installed (see: ./install.sh list)" % target)
    if target == cur:
        say("current is already %s" % cur)
        return
    switch(root, target, "rollback")
    say("current: %s -> %s (saves untouched)" % (cur, target))


def cmd_list(root):
    cur, prev = root.current(), root.previous()
    for v in reversed(root.versions()):
        m = root.manifest(v)
        s = m.get("sources", {})
        mark = "*" if v == cur else ("<" if v == prev else " ")
        say(
            "%s %s  built %s  %s %s  toolkit %s%s"
            % (
                mark,
                v,
                m.get("built", "?"),
                SOURCE,
                s.get(SOURCE, {}).get("commit", "?")[:7],
                s.get("toolkit", {}).get("commit", "?")[:7],
                "  (dirty)" if "-dirty" in v else "",
            )
        )
    say("(* current, < previous: rollback target)")


def cmd_status(root, bundle_dir):
    if not os.path.isdir(root.path):
        say("not installed at %s" % root.path)
        return
    cur = root.current()
    say("root:     %s" % root.path)
    if cur and is_product_dir(root.p("versions", cur)):
        m = root.manifest(cur)
        s = m.get("sources", {})
        say(
            "current:  %s (%s %s, toolkit %s, built %s)"
            % (
                cur,
                SOURCE,
                s.get(SOURCE, {}).get("commit", "?")[:7],
                s.get("toolkit", {}).get("commit", "?")[:7],
                m.get("built", "?"),
            )
        )
    else:
        say("current:  none")
    say("previous: %s" % (root.previous() or "none"))
    say("versions: %s" % (", ".join(root.versions()) or "none"))
    xbe = root.p("game_files", "default.xbe")
    say(
        "game:     %s"
        % (
            "%s (%s)" % (xbe, human(os.path.getsize(xbe)))
            if os.path.isfile(xbe)
            else "MISSING " + xbe
        )
    )
    for d in USER_DIRS + ("prefix",):
        p = root.p(d)
        say(
            "%-9s %s"
            % (d + ":", "%s, %s" % (p, human(du(p))) if os.path.isdir(p) else "not created yet")
        )
    p, where = find_umu()
    try:
        with open(root.p("state", "umu-run")) as f:
            recorded = f.read().strip()
    except OSError:
        recorded = ""
    if not p and is_exe(recorded):
        p, where = recorded, "recorded by install"
    say("umu-run:  %s" % ("%s (%s)" % (p, where) if p else "not found (./install.sh --fetch-umu)"))
    say("launcher: %s" % (root.p(APP) if os.access(root.p(APP), os.X_OK) else "missing"))
    games = running_games(bundle_dir)
    say("running:  %s" % ("; ".join(games) if games else "no game process"))


def cmd_uninstall(root):
    if not os.path.isdir(root.path):
        say("not installed at %s" % root.path)
        return
    check_ours(root, installing=False)
    shortcut = desktop_name(root) or "game's"
    for name in ("current", "current.new", APP, APP + ".desktop"):
        p = root.p(name)
        if os.path.lexists(p):
            os.remove(p)
    for name in ("versions", "state", "game_files", "prefix"):
        shutil.rmtree(root.p(name), ignore_errors=True)
    kept = [d for d in USER_DIRS if os.path.isdir(root.p(d))]
    say("removed the program from %s" % root.path)
    say("kept: %s" % (", ".join(root.p(d) for d in kept) or "(no user data yet)"))
    say("Remove the %s shortcut in Steam yourself (right-click > Manage > Remove)." % shortcut)


def main(argv=None, host_check=True, bench=None, umu=None):
    ap = argparse.ArgumentParser(
        prog="install.sh", description="Install, update and manage %s on this PC." % NAME
    )
    ap.add_argument(
        "command",
        nargs="?",
        default="install",
        choices=("install", "rollback", "status", "list", "uninstall"),
    )
    ap.add_argument("version", nargs="?", help="rollback: the version to switch to")
    ap.add_argument("--root", default=os.environ.get(ROOT_ENV, DEFAULT_ROOT))
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument(
        "--steam", action="store_true", help="add the game to Steam (Desktop Mode, Steam open)"
    )
    ap.add_argument(
        "--import-saves", metavar="DIR", help="copy an old save root into an empty hdd/"
    )
    ap.add_argument(
        "--fetch-umu",
        action="store_true",
        help="download the pinned umu-launcher %s (sha256 checked) and use it" % UMU_VERSION,
    )
    ap.add_argument(
        "--umu-dir",
        metavar="DIR",
        help="where --fetch-umu puts umu-launcher (default %s)"
        % default_umu_dir().replace(os.path.expanduser("~"), "~", 1),
    )
    ap.add_argument(
        "--bundle", default=os.path.dirname(os.path.abspath(__file__)), help=argparse.SUPPRESS
    )
    a = ap.parse_args(argv)
    if a.version and a.command != "rollback":
        ap.error("a version is only taken by rollback")
    if a.keep < 1:
        ap.error("--keep must be at least 1")
    if (a.fetch_umu or a.umu_dir) and a.command != "install":
        ap.error("--fetch-umu and --umu-dir are taken by install")
    root = Root(a.root)
    try:
        if a.command == "install":
            cmd_install(
                Bundle(a.bundle),
                root,
                a.keep,
                a.steam,
                a.import_saves,
                host_check,
                bench,
                a.fetch_umu,
                a.umu_dir,
                umu,
            )
        elif a.command == "rollback":
            cmd_rollback(root, a.version)
        elif a.command == "status":
            cmd_status(root, a.bundle)
        elif a.command == "list":
            cmd_list(root)
        elif a.command == "uninstall":
            cmd_uninstall(root)
    except InstallError as e:
        print("install: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
