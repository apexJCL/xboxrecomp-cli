"""Pinned downloads: llvm-mingw and NSIS, each checked against the sha256 in
the game's config/setup-pins.json before anything is unpacked."""

import json
import os
import shutil
import tarfile
import urllib.request
import zipfile

from . import host
from .host import CliError

# The llvm-mingw asset for each host, by OS and arch.
MINGW_ASSETS = {
    ("windows", "x86_64"): "llvm-mingw-{tag}-ucrt-x86_64.zip",
    ("windows", "aarch64"): "llvm-mingw-{tag}-ucrt-aarch64.zip",
    ("linux", "x86_64"): "llvm-mingw-{tag}-ucrt-ubuntu-22.04-x86_64.tar.xz",
    ("linux", "aarch64"): "llvm-mingw-{tag}-ucrt-ubuntu-22.04-aarch64.tar.xz",
    ("macos", "x86_64"): "llvm-mingw-{tag}-ucrt-macos-universal.tar.xz",
    ("macos", "aarch64"): "llvm-mingw-{tag}-ucrt-macos-universal.tar.xz",
}


def load_pins(path=None):
    with open(path or host.g().pins) as f:
        return json.load(f)


def mingw_tag(env=None):
    env = os.environ if env is None else env
    tag = env.get("LLVM_MINGW_TAG") or host.g().m["toolchain"]["llvm_mingw"]
    if not tag:
        raise CliError("game.toml sets no toolchain.llvm_mingw (the windows target needs it)")
    return tag


def mingw_root(env=None):
    """LLVM_MINGW_ROOT, else the pinned tag under third_party/, else ''
    (the toolchain file then searches PATH)."""
    env = os.environ if env is None else env
    if env.get("LLVM_MINGW_ROOT"):
        return os.path.abspath(env["LLVM_MINGW_ROOT"])
    third_party = host.g().third_party
    prefix = "llvm-mingw-%s-" % mingw_tag(env)
    if os.path.isdir(third_party):
        cands = sorted(
            d
            for d in os.listdir(third_party)
            if d.startswith(prefix)
            and not d.endswith(".partial")
            and os.path.isdir(os.path.join(third_party, d))
        )
        if cands:
            return os.path.join(third_party, cands[-1])
    if shutil.which("x86_64-w64-mingw32-clang"):
        return ""
    raise CliError("no llvm-mingw: run '%s setup' (or set LLVM_MINGW_ROOT)" % host.prog())


def mingw_tool(name):
    root = mingw_root()
    return (
        os.path.join(root, "bin", "x86_64-w64-mingw32-" + name + host.exe_suffix())
        if root
        else shutil.which("x86_64-w64-mingw32-" + name)
    )


def mingw_asset_name(tag, os_name=None, arch=None):
    key = (os_name or host.host_os(), arch or host.host_arch())
    if key not in MINGW_ASSETS:
        raise CliError("no llvm-mingw build for %s %s" % key)
    return MINGW_ASSETS[key].format(tag=tag)


def mingw_dir_for(asset):
    """third_party/<asset without its archive extension>."""
    for ext in (".tar.xz", ".zip"):
        if asset.endswith(ext):
            return os.path.join(host.g().third_party, asset[: -len(ext)])
    raise CliError("unknown archive type: %s" % asset)


def download(url, dest, want_sha256, want_size=None):
    """Fetch to dest.part, check the pinned sha256, then rename. A mismatch
    deletes the download: nothing unpinned is ever unpacked or run."""
    part = dest + ".part"
    host.say("fetching %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "%s-setup" % host.prog()})
    with urllib.request.urlopen(req) as r, open(part, "wb") as f:
        hdr = getattr(r, "headers", None)
        total = (int(hdr.get("Content-Length") or 0) if hdr else 0) or want_size or 0
        done = 0
        for chunk in iter(lambda: r.read(1 << 16), b""):
            f.write(chunk)
            done += len(chunk)
            if host.VIEW and total:
                host.VIEW.progress(done >> 20, max(1, total >> 20))  # MiB
    got = host.sha256_path(part)
    if got != want_sha256 or (want_size and os.path.getsize(part) != want_size):
        os.remove(part)
        raise CliError(
            "%s: checksum mismatch (got %s, pinned %s); nothing was unpacked"
            % (os.path.basename(dest), got, want_sha256)
        )
    os.replace(part, dest)
    host.say("sha256 verified: %s" % got)


def _member_ok(name, linkname=None):
    """No absolute paths, no '..', no link leaving the tree (on top of
    tarfile's own data filter)."""
    for n in (name,) + ((linkname,) if linkname else ()):
        n = n.replace("\\", "/")
        if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
            return False
        if ".." in n.split("/"):
            return False
    return True


def check_archive_members(members):
    """members: (name, linkname or None, is_link_relative_to_member_dir)."""
    bad = [m[0] for m in members if not _member_ok(m[0], None)]
    for name, link, rel in members:
        if link is None:
            continue
        target = os.path.normpath(os.path.join(os.path.dirname(name), link)) if rel else link
        if not _member_ok(target.replace(os.sep, "/")):
            bad.append("%s -> %s" % (name, link))
    if bad:
        raise CliError("archive has unsafe members: %s" % ", ".join(bad[:5]))


def extract(archive, dest, strip=1):
    """Unpack into dest, dropping the archive's top folder (strip=1)."""
    os.makedirs(dest, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            check_archive_members([(i.filename, None, False) for i in z.infolist()])
            for i in z.infolist():
                parts = i.filename.replace("\\", "/").split("/")[strip:]
                if not parts or parts == [""]:
                    continue
                out = os.path.join(dest, *parts)
                if i.is_dir():
                    os.makedirs(out, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(i) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f, 1 << 20)
                mode = (i.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(out, mode)
        return
    with tarfile.open(archive) as t:
        members = t.getmembers()
        check_archive_members(
            [(m.name, m.linkname if (m.issym() or m.islnk()) else None, m.issym()) for m in members]
        )
        for m in members:
            parts = m.name.split("/")[strip:]
            if not parts or parts == [""]:
                continue
            m.name = "/".join(parts)
            if m.islnk():
                m.linkname = "/".join(m.linkname.split("/")[strip:])
            # The stdlib's own check on top of ours (no device files, no
            # setuid bits).
            t.extract(m, dest, filter="data")


def fetch_mingw(pins, force=False):
    tag = mingw_tag()
    asset = mingw_asset_name(tag)
    pin = pins["llvm_mingw"]["assets"].get(asset)
    if pins["llvm_mingw"]["tag"] != tag or not pin:
        raise CliError(
            "config/setup-pins.json has no pin for %s (run '%s pins refresh')"
            % (asset, host.prog())
        )
    root = host.g().root
    third_party = host.g().third_party
    dest = mingw_dir_for(asset)
    cc = os.path.join(dest, "bin", "x86_64-w64-mingw32-clang" + host.exe_suffix())
    if not force and os.path.isfile(cc) and host.read_text(os.path.join(dest, ".tag")) == tag:
        host.say("llvm-mingw %s: already installed (%s)" % (tag, os.path.relpath(dest, root)))
        return dest
    os.makedirs(third_party, exist_ok=True)
    arc = os.path.join(third_party, asset)
    download(pin["url"], arc, pin["sha256"], pin.get("size"))
    part = dest + ".partial"
    shutil.rmtree(part, ignore_errors=True)
    extract(arc, part)
    with open(os.path.join(part, ".tag"), "w") as f:
        f.write(tag + "\n")
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(part, dest)
    os.remove(arc)
    host.say("llvm-mingw %s -> %s" % (tag, os.path.relpath(dest, root)))
    return dest


def fetch_nsis(pins, force=False):
    """Windows hosts only: the portable NSIS zip (makensis.exe needs no
    install). Linux and macOS take makensis from the package manager."""
    pin = pins["nsis"]
    third_party = host.g().third_party
    dest = os.path.join(third_party, "nsis-%s" % pin["version"])
    if not force and os.path.isfile(os.path.join(dest, "makensis.exe")):
        host.say("NSIS %s: already installed" % pin["version"])
        return dest
    os.makedirs(third_party, exist_ok=True)
    arc = os.path.join(third_party, "nsis-%s.zip" % pin["version"])
    download(pin["url"], arc, pin["sha256"], pin.get("size"))
    part = dest + ".partial"
    shutil.rmtree(part, ignore_errors=True)
    extract(arc, part)
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(part, dest)
    os.remove(arc)
    return dest
