"""xbr wizard build and check: the wizard's three release assets from one
Mac (or the game's workflow), every input pinned in wizard/pins.json.

  <slug>-wizard-<tag>-macos.zip         the universal .app (fyne package)
  <slug>-wizard-<tag>-windows-x64.zip   one exe (cgo through llvm-mingw)
  <slug>-wizard-<tag>-linux-x64.tar.gz  one binary (the digest-pinned
                                        builder image, docker build --output)
  SHA256SUMS

Each archive also carries NOTICES.txt: the licences of the wizard and of
everything its binaries link or embed (the Go standard library, the Go
modules, Fyne's fonts, the Tabler glyphs), gathered from the module cache.

Go is the official tarball by sha256 under wizard/.tools/ (or $WIZARD_GO),
run with GOTOOLCHAIN=local. The module is staged into the output's work
folder with the generated config.json, so the CLI checkout is never
written. Archive entries carry the release commit's date
(SOURCE_DATE_EPOCH): two builds give the same bytes.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from . import cli_dir, fetch, host
from . import wizard as wizard_mod
from .host import CliError

ASSETS = ("macos", "windows", "linux")
# What the build never stages from the module: its own tool caches and a
# developer's outputs.
SKIP = {".tools", "dist", ".DS_Store"}
LDFLAGS = "-s -w -buildid="
# The epoch of an untagged developer build (SOURCE_DATE_EPOCH unset and no
# commit): any fixed date keeps the archives reproducible.
DEV_EPOCH = 1700000000


def parse_only(text):
    if not text:
        return list(ASSETS)
    out = []
    for t in text.split(","):
        t = t.strip()
        if t not in ASSETS:
            raise CliError("--only: %r is not one of %s" % (t, ", ".join(ASSETS)))
        if t not in out:
            out.append(t)
    return [t for t in ASSETS if t in out]


def module_dir():
    """wizard/ beside src/: the CLI must run from a checkout (the
    bootstrap's `uv run --project` form), not an installed wheel."""
    d = os.path.join(cli_dir.cli_dir(), "wizard")
    if not os.path.isfile(os.path.join(d, "go.mod")):
        raise CliError(
            "no wizard/ module beside this CLI (%s): run it from an xboxrecomp-cli checkout, "
            "as the game's bootstrap does ('<game> wizard build')" % cli_dir.cli_dir()
        )
    return d


def load_pins(mod=None):
    with open(os.path.join(mod or module_dir(), "pins.json")) as f:
        return json.load(f)


def asset_names(slug, tag):
    v = tag or "dev"
    return {
        "macos": "%s-wizard-%s-macos.zip" % (slug, v),
        "windows": "%s-wizard-%s-windows-x64.zip" % (slug, v),
        "linux": "%s-wizard-%s-linux-x64.tar.gz" % (slug, v),
    }


def go_key(os_name=None, arch=None):
    os_name = {"macos": "darwin"}.get(os_name or host.host_os(), os_name or host.host_os())
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch or host.host_arch(), arch)
    return "%s-%s" % (os_name, arch)


def tools_dir(mod):
    return os.path.join(mod, ".tools")


def ensure_go(pins, mod, environ=None):
    """GOROOT: $WIZARD_GO, else the pinned tarball under .tools/go-<v>/."""
    environ = os.environ if environ is None else environ
    if environ.get("WIZARD_GO"):
        root = os.path.abspath(environ["WIZARD_GO"])
    else:
        g = pins["go"]
        root = os.path.join(tools_dir(mod), "go-%s" % g["version"])
        if not os.path.isfile(os.path.join(root, "bin", "go" + host.exe_suffix())):
            pin = g["assets"].get(go_key())
            if not pin:
                raise CliError("wizard/pins.json has no Go for %s" % go_key())
            # fetch.extract drops the tarball's go/ folder.
            _fetch_unpack(pin, root)
    go = os.path.join(root, "bin", "go" + host.exe_suffix())
    ver = subprocess.run([go, "env", "GOVERSION"], capture_output=True, env=go_env(root, mod))
    want = "go" + pins["go"]["version"]
    if ver.returncode != 0 or ver.stdout.decode().strip() != want:
        raise CliError("%s is not %s (wizard/pins.json)" % (go, want))
    return root


def _fetch_unpack(pin, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    name = pin["url"].rsplit("/", 1)[1]
    arc = os.path.join(os.path.dirname(dest), name)
    fetch.download(pin["url"], arc, pin["sha256"], pin.get("size"))
    part = dest + ".partial"
    shutil.rmtree(part, ignore_errors=True)
    fetch.extract(arc, part)
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(part, dest)
    os.remove(arc)


def ensure_mingw(pins, mod, G, environ=None):
    """The llvm-mingw root for the Windows cgo build: $LLVM_MINGW, else the
    game's own third_party/ copy when it is the pinned tag, else the pinned
    archive under .tools/."""
    environ = os.environ if environ is None else environ
    if environ.get("LLVM_MINGW"):
        return os.path.abspath(environ["LLVM_MINGW"])
    lm = pins["llvm_mingw"]
    asset = fetch.mingw_asset_name(lm["tag"], "macos", "aarch64")
    own = os.path.join(G.third_party, asset[: -len(".tar.xz")])
    if os.path.isfile(os.path.join(own, "bin", "x86_64-w64-mingw32-clang")):
        return own
    dest = os.path.join(tools_dir(mod), asset[: -len(".tar.xz")])
    if not os.path.isfile(os.path.join(dest, "bin", "x86_64-w64-mingw32-clang")):
        pin = lm["assets"].get(asset)
        if not pin:
            raise CliError("wizard/pins.json has no %s" % asset)
        _fetch_unpack(pin, dest)
    return dest


def go_env(goroot, mod, base=None):
    """The environment of every go command: the pinned Go only, the module
    and build caches under .tools/, no GOOS/GOARCH from the caller."""
    e = dict(os.environ if base is None else base)
    for k in ("GOOS", "GOARCH", "CC", "CXX", "CGO_ENABLED", "GOFLAGS", "GOROOT", "GOPATH"):
        e.pop(k, None)
    t = tools_dir(mod)
    e.update(
        {
            "PATH": os.path.join(goroot, "bin") + os.pathsep + e.get("PATH", ""),
            "GOTOOLCHAIN": "local",
            "GOFLAGS": "-mod=readonly",
            "GOPATH": os.path.join(t, "gopath"),
            "GOMODCACHE": os.path.join(t, "gomod"),
            "GOCACHE": os.path.join(t, "gocache"),
        }
    )
    if host.host_os() == "macos" and os.path.isdir(host.CLT) and not e.get("DEVELOPER_DIR"):
        e["DEVELOPER_DIR"] = host.CLT
    return e


def stage_module(mod, dest, cfg_json, icon_png=None):
    """A copy of the module with the game's config.json, the build's
    sources (and Docker's context). icon_png, the game's own icon at 256
    pixels, replaces the generic window icon the module embeds."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(mod, dest, ignore=lambda d, names: [n for n in names if n in SKIP])
    with open(os.path.join(dest, "internal", "gamecfg", "config.json"), "w", newline="\n") as f:
        f.write(cfg_json)
    if icon_png is not None:
        with open(os.path.join(dest, "internal", "gamecfg", "icon.png"), "wb") as f:
            f.write(icon_png)


GENERIC_ICON = os.path.join("icon", "wizard-icon.svg")


def icongen_cmd(go, icon, out):
    """The module's icon renderer: pure Go, so it runs without cgo on any
    host the build runs on."""
    return [go, "run", "./cmd/icongen", "-in", icon, "-out", out]


def make_icons(go, env, mod, work, icon_bytes):
    """The icon set in <work>/icon (icon-<n>.png, wizard.ico, Icon.png)
    from the game's icon, or the module's generic one when it sets none."""
    icon = os.path.join(mod, GENERIC_ICON)
    if icon_bytes is not None:
        icon = os.path.join(work, "game-icon")
        with open(icon, "wb") as f:
            f.write(icon_bytes)
    out = os.path.join(work, "icon")
    _run(icongen_cmd(go, icon, out), env=dict(env, CGO_ENABLED="0"), cwd=mod)
    return out


def windres_cmd(mingw, rc, syso):
    """The Windows icon resource as a COFF object: the Go linker takes any
    .syso in the package, and the _windows_amd64 suffix keeps it out of the
    other builds. No preprocessor: the script has no includes."""
    return [
        os.path.join(mingw, "bin", "llvm-windres"),
        "--no-preprocess",
        "--target=pe-x86-64",
        "-O",
        "coff",
        "-i",
        rc,
        "-o",
        syso,
    ]


def desktop_entry(game, name):
    """The Linux menu entry beside the binary; it works once the three
    files are where XDG looks (docs/wizard.md, The icon)."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=%s build wizard\n"
        "Exec=%s\n"
        "Icon=%s\n"
        "Categories=Game;Utility;\n" % (game, name, name)
    )


NOTICES = "NOTICES.txt"
# What a module ships for its users: licence, copying and patent files.
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|patents)", re.I)
# A REUSE-style folder of licence texts, one file per SPDX id
# (LICENSES/MIT.txt): every file in it is a licence.
LICENSES_DIR = re.compile(r"^licen[cs]es$", re.I)
# The MinGW-w64 runtime's notices: llvm-mingw links its startup code and
# CRT helpers into the Windows exe statically, and parts of it ask for
# their notices with a binary. A copy from the pinned llvm-mingw, so the
# three archives' NOTICES.txt stays one file; the Windows build checks it
# still matches (mingw_notice). compiler-rt's builtins are Apache-2.0 WITH
# LLVM-exception, which asks for no notice in a compiled program, and
# nothing links libunwind, libc++ or winpthreads (no C++).
MINGW_NOTICE = os.path.join("notices", "mingw-w64-runtime.txt")
MINGW_RUNTIME = os.path.join(
    "x86_64-w64-mingw32", "share", "mingw32", "COPYING.MinGW-w64-runtime.txt"
)
# Folders under a package that nothing in the binary comes from.
NOT_LINKED = {"testdata", "cmd", "example", "examples", "doc", "docs", "test"}
# Every binary the build makes: a module any of them links is in NOTICES.txt,
# so the three archives carry the same file.
NOTICE_TARGETS = (
    ("darwin", "arm64"),
    ("darwin", "amd64"),
    ("windows", "amd64"),
    ("linux", "amd64"),
)


def linked_cmd(go):
    """Each package the wizard links, outside the module itself: its
    module@version, the module's folder and the package's folder."""
    f = "{{with .Module}}{{if not .Main}}{{.Path}}@{{.Version}}\t{{.Dir}}\t{{$.Dir}}{{end}}{{end}}"
    return [go, "list", "-deps", "-f", f, "./cmd/wizard"]


def linked_modules(go, env, mod):
    """{module@version: (module folder, {package folders})} over every
    target. go list only reads the sources, so no C compiler is needed."""
    out = {}
    for goos, arch in NOTICE_TARGETS:
        e = dict(env, CGO_ENABLED="1", GOOS=goos, GOARCH=arch)
        r = subprocess.run(linked_cmd(go), env=e, cwd=mod, capture_output=True)
        if r.returncode != 0:
            raise CliError("go list (%s/%s) failed: %s" % (goos, arch, r.stderr.decode()[-400:]))
        for line in r.stdout.decode().splitlines():
            name, mdir, pdir = line.split("\t")
            out.setdefault(name, (mdir, set()))[1].add(pdir)
    return out


def _texts(p):
    return [os.path.join(p, f) for f in sorted(os.listdir(p)) if os.path.isfile(os.path.join(p, f))]


def reuse_files(d):
    """The licence texts in d's REUSE-style LICENSES folder, if any."""
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if LICENSES_DIR.match(n) and os.path.isdir(os.path.join(d, n)):
            out += _texts(os.path.join(d, n))
    return out


def license_files(d, top=True):
    """The licence files in a package's folder and in its folders that are
    no package of their own: the C sources and fonts it builds in (GLFW's
    zlib licence, Fyne's font licences) keep theirs there."""
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    if not top and any(n.endswith(".go") for n in names):
        return []
    out = []
    for n in names:
        p = os.path.join(d, n)
        if os.path.isdir(p):
            if LICENSES_DIR.match(n):
                out += _texts(p)
            elif n not in NOT_LINKED:
                out += license_files(p, False)
        elif LICENSE_FILE.match(n):
            out.append(p)
    return out


def notice_sections(cli_root, goroot, modules):
    """(title, file) in NOTICES.txt's order: the wizard's own licence, the
    Go standard library, the Tabler glyphs the module embeds, then each
    linked module. A module without a licence file stops the build: its
    notice cannot be shipped."""
    with open(os.path.join(goroot, "VERSION")) as f:
        gov = f.readline().strip()
    secs = [
        ("The wizard (xboxrecomp-cli)", os.path.join(cli_root, "LICENSE")),
        ("Go standard library (%s): LICENSE" % gov, os.path.join(goroot, "LICENSE")),
        ("Go standard library (%s): PATENTS" % gov, os.path.join(goroot, "PATENTS")),
        ("Tabler Icons (the glyphs and the generic window icon)",
         os.path.join(cli_root, "LICENSES", "tabler-icons-MIT.txt")),
        ("MinGW-w64 runtime (in the Windows program)", os.path.join(cli_root, "wizard", MINGW_NOTICE)),
    ]  # fmt: skip
    for name in sorted(modules, key=str.lower):
        mdir, pdirs = modules[name]
        files = {
            os.path.join(mdir, n)
            for n in os.listdir(mdir)
            if LICENSE_FILE.match(n) and os.path.isfile(os.path.join(mdir, n))
        }
        files.update(reuse_files(mdir))
        for p in pdirs:
            files.update(license_files(p))
        if not files:
            raise CliError("%s has no licence file for %s" % (name, NOTICES))
        for f in sorted(files):
            secs.append(("%s: %s" % (name, os.path.relpath(f, mdir).replace(os.sep, "/")), f))
    return secs


def mingw_notice(mingw, mod):
    """Refuses a Windows build whose llvm-mingw runtime notices differ from
    the copy NOTICES.txt carries: a new llvm-mingw pin brings its own."""
    with open(os.path.join(mingw, MINGW_RUNTIME), "rb") as f:
        theirs = f.read()
    with open(os.path.join(mod, MINGW_NOTICE), "rb") as f:
        ours = f.read()
    if theirs != ours:
        raise CliError(
            "llvm-mingw's MinGW-w64 runtime notices changed: copy %s to wizard/%s"
            % (MINGW_RUNTIME, MINGW_NOTICE)
        )


def notices_text(secs):
    """One text file, LF endings, the same bytes for the same inputs."""
    parts = [
        "The licence of this program and of the code, fonts and icons it\n"
        "contains, as their authors publish them.\n"
    ]
    for title, path in secs:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            body = f.read().replace("\r\n", "\n").rstrip("\n")
        parts.append("\n%s\n%s\n%s\n\n%s\n" % ("=" * 72, title, "=" * 72, body))
    return "".join(parts)


def write_notices(go, env, mod, goroot, dest):
    secs = notice_sections(os.path.dirname(mod), goroot, linked_modules(go, env, mod))
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(notices_text(secs))
    return dest


def version_ldflags(tag):
    return "%s -X main.version=%s" % (LDFLAGS, tag or "dev")


def go_build_cmd(go, out, tag, windows=False):
    flags = version_ldflags(tag) + (" -H windowsgui" if windows else "")
    return [go, "build", "-trimpath", "-ldflags=" + flags, "-o", out, "./cmd/wizard"]


def app_id(repo, slug):
    """A bundle id from the release repo, io.github.<owner>.<slug>-wizard
    for a GitHub repo: the CLI names no organisation of its own."""
    m = re.match(r"^https://github\.com/([^/]+)/", repo)
    owner = re.sub(r"[^a-z0-9-]", "-", m.group(1).lower()) if m else "local"
    return "io.github.%s.%s-wizard" % (owner, slug)


def app_version(tag):
    """fyne package wants x.y.z: the tag's trailing dotted number."""
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?$", tag or "")
    if not m:
        return "0.0.0"
    return ".".join(x or "0" for x in m.groups())


def fyne_package_cmd(go, tool, name, appid, exe, icon, tag):
    return [
        go,
        "run",
        tool,
        "package",
        "-os",
        "darwin",
        "-name",
        name,
        "-app-id",
        appid,
        "-app-version",
        app_version(tag),
        "-app-build",
        "1",
        "-exe",
        exe,
        "-icon",
        icon,
        "-release",
    ]


def _entries(root):
    """(relative path with /, absolute path) of every file and folder under
    root, sorted: the order every archive writes."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for n in sorted(dirs) + sorted(files):
            p = os.path.join(d, n)
            out.append((os.path.relpath(p, os.path.dirname(root)).replace(os.sep, "/"), p))
    return sorted(out)


def write_zip(dest, items, epoch):
    """A zip with fixed dates and Unix modes. items: (name in the zip,
    path on disk) of files and folders."""
    import time

    dt = time.gmtime(max(epoch, 315532800))[:6]  # zip dates start in 1980
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, path in items:
            st = os.lstat(path)
            is_dir = os.path.isdir(path) and not os.path.islink(path)
            zi = zipfile.ZipInfo(name + ("/" if is_dir else ""), dt)
            zi.create_system = 3
            mode = 0o755 if is_dir or st.st_mode & 0o111 else 0o644
            zi.external_attr = ((0o040000 if is_dir else 0o100000) | mode) << 16
            if is_dir:
                zi.external_attr |= 0x10
                z.writestr(zi, b"")
                continue
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(zi, f.read(), compresslevel=9)


def sha256_file(path):
    return host.sha256_path(path)


def sums_text(paths):
    return "".join("%s  %s\n" % (sha256_file(p), os.path.basename(p)) for p in paths)


def set_mtimes(root, epoch):
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            os.utime(os.path.join(d, n), (epoch, epoch), follow_symlinks=False)
    os.utime(root, (epoch, epoch))


def commit_epoch(root, commit):
    if not commit:
        return DEV_EPOCH
    r = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%ct", commit], capture_output=True
    )
    try:
        return int(r.stdout.decode().strip())
    except ValueError:
        return DEV_EPOCH


def docker_cmds(pins, ctx, work, name, tag, epoch, dry=False, no_cache=False):
    """The two docker builds: the builder image (tagged by its inputs) and
    the asset build exported with --output; with dry, the --dry stage.
    no_cache rebuilds every asset layer (check's second build)."""
    b = pins["builder"]
    builder_df = os.path.join(ctx, "linux", "builder.Dockerfile")
    with open(builder_df, "rb") as f:
        key = hashlib.sha256(
            f.read() + b["base"].encode() + b["snapshot"].encode() + b["platform"].encode()
        ).hexdigest()[:16]
    image = "xbr-wizard-builder:%s" % key
    builder = [
        "docker",
        "build",
        "--platform",
        b["platform"],
        "-f",
        builder_df,
        "--build-arg",
        "BASE=" + b["base"],
        "--build-arg",
        "SNAPSHOT=" + b["snapshot"],
        "-t",
        image,
        os.path.join(ctx, "linux"),
    ]
    asset = [
        "docker",
        "build",
        "--platform",
        b["platform"],
        "-f",
        os.path.join(ctx, "linux", "Dockerfile"),
        "--build-arg",
        "BUILDER=" + image,
        "--build-arg",
        "NAME=" + name,
        "--build-arg",
        "VERSION=" + (tag or "dev"),
        "--build-arg",
        "SOURCE_DATE_EPOCH=%d" % epoch,
    ]
    if no_cache:
        asset += ["--no-cache", "--build-arg", "FRESH=1"]
    asset += ["--target", "dry"] if dry else ["--output", "type=local,dest=" + work]
    asset.append(ctx)
    return image, builder, asset


def _run(cmd, env=None, cwd=None, quiet=False):
    host.say("$ " + " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
    r = subprocess.run(
        cmd,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
    )
    if r.returncode != 0:
        if quiet and r.stdout:
            host.say(r.stdout.decode(errors="replace")[-4000:])
        raise CliError("%s failed (exit %d)" % (os.path.basename(cmd[0]), r.returncode))
    return r


def build_macos(go, env, src, work, name, tag, icon, appid, tool, epoch, out, notices):
    arm, amd = os.path.join(work, name + "-arm64"), os.path.join(work, name + "-amd64")
    for arch, dst in (("arm64", arm), ("amd64", amd)):
        e = dict(env, CGO_ENABLED="1", GOOS="darwin", GOARCH=arch)
        _run(go_build_cmd(go, dst, tag), env=e, cwd=src)
    uni = os.path.join(work, name)
    _run(["lipo", "-create", "-output", uni, arm, amd])
    app = os.path.join(work, name + ".app")
    shutil.rmtree(app, ignore_errors=True)
    # fyne package writes <name>.app into its working folder.
    _run(fyne_package_cmd(go, tool, name, appid, uni, icon, tag), env=env, cwd=work, quiet=True)
    # fyne package signs nothing, so a file added to Resources breaks no seal.
    res = os.path.join(app, "Contents", "Resources")
    os.makedirs(res, exist_ok=True)
    shutil.copyfile(notices, os.path.join(res, NOTICES))
    set_mtimes(app, epoch)
    write_zip(out, _entries(app), epoch)
    return {"macos-binary": uni, "macos-app": app}


def build_windows(go, env, src, work, name, tag, mingw, epoch, out, icons, notices):
    exe = os.path.join(work, name + ".exe")
    rc = os.path.join(icons, "wizard.rc")
    with open(rc, "w", newline="\n") as f:
        f.write('1 ICON "wizard.ico"\n')
    syso = os.path.join(src, "cmd", "wizard", "rsrc_windows_amd64.syso")
    # The script names the .ico relative to itself: run where both are.
    _run(windres_cmd(mingw, "wizard.rc", syso), cwd=icons)
    e = dict(
        env,
        CGO_ENABLED="1",
        GOOS="windows",
        GOARCH="amd64",
        CC=os.path.join(mingw, "bin", "x86_64-w64-mingw32-clang"),
        CXX=os.path.join(mingw, "bin", "x86_64-w64-mingw32-clang++"),
    )
    _run(go_build_cmd(go, exe, tag, windows=True), env=e, cwd=src)
    write_zip(out, [(name + ".exe", exe), (NOTICES, notices)], epoch)
    return {"windows-binary": exe}


def stage_linux_icon(src, icons, game, name):
    """icon/<name>.png (256) and icon/<name>.desktop in Docker's context,
    for the tar beside the binary."""
    d = os.path.join(src, "icon")
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(os.path.join(icons, "icon-256.png"), os.path.join(d, name + ".png"))
    with open(os.path.join(d, name + ".desktop"), "w", newline="\n") as f:
        f.write(desktop_entry(game, name))


def build_linux(pins, src, work, name, tag, epoch, out, no_cache=False):
    if not shutil.which("docker"):
        raise CliError("the Linux asset needs Docker (docker build --output); none on PATH")
    lw = os.path.join(work, "linux")
    shutil.rmtree(lw, ignore_errors=True)
    image, builder, asset = docker_cmds(pins, src, lw, name, tag, epoch, no_cache=no_cache)
    have = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if have.returncode != 0:
        _run(builder, quiet=True)
    _run(asset, quiet=True)
    shutil.copyfile(os.path.join(lw, name + ".tar.gz"), out)
    return {"linux-binary": os.path.join(lw, name)}, image


def dry_runs(built, pins, src, name, tag, epoch, image):
    """Each binary's --dry where this host can run it."""
    notes = []
    mac = built.get("macos-binary")
    if mac and host.host_os() == "macos":
        r = subprocess.run([mac, "--dry", "--no-update-check"], capture_output=True)
        notes.append(
            "macos --dry: %s"
            % ("ok" if r.returncode == 0 else "FAILED: " + r.stderr.decode(errors="replace")[-400:])
        )
        if r.returncode != 0:
            raise CliError(notes[-1])
    if built.get("windows-binary"):
        notes.append("windows --dry: skipped (not a Windows host)")
    if built.get("linux-binary") and image:
        _, _, dry = docker_cmds(pins, src, "", name, tag, epoch, dry=True)
        r = subprocess.run(dry, capture_output=True)
        notes.append(
            "linux --dry: %s"
            % (
                "ok (in the builder image)"
                if r.returncode == 0
                else "skipped (this Docker cannot run amd64 code)"
            )
        )
    return notes


def refuse_private(G):
    """A published wizard clones its game from [wizard] repo: a private
    game (private = true, with or without a repo) has no release to clone,
    so its wizard binaries are never built for a release."""
    if G.m.get("wizard", {}).get("private"):
        raise CliError(
            "game.toml's [wizard] private = true: this game's wizard is never built for "
            "publishing (developers build it with xbr-wizard --source; docs/wizard.md)"
        )


def build(G, tag="", dev=False, only=ASSETS, out="", keep_work=False, gocache="", private_ok=False):
    """The assets and SHA256SUMS into out (default dist/wizard/). Returns
    {name: sha256} of the archives and of the binaries inside them. With
    gocache, a fresh build: Go compiles into that empty cache and Docker
    rebuilds the asset layers, so nothing is reused from an earlier build.
    private_ok: wizard check's double build, which publishes nothing."""
    if not private_ok:
        refuse_private(G)
    mod = module_dir()
    pins = load_pins(mod)
    cfg, icon_bytes = wizard_mod.resolve(G, tag, dev)
    rel = cfg["release"]
    slug, tag_v = cfg["game"]["slug"], rel["tag"]
    name = "%s-wizard" % slug
    out = os.path.abspath(out or os.path.join(G.dist, "wizard"))
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, ".work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    src = os.path.join(work, "src")
    epoch = commit_epoch(G.root, rel["commit"])
    names = asset_names(slug, tag_v)
    host.step("wizard build %s %s (%s)" % (slug, tag_v or "dev", ", ".join(only)))
    host.say("release: %s @ %s" % (rel.get("repo", "private"), rel["commit"][:12] or "?"))
    built, image, done = {}, None, []
    # Go on the host for every asset: the icons come from the module's own
    # renderer before anything is built.
    goroot = ensure_go(pins, mod)
    env = go_env(goroot, mod)
    if gocache:
        env["GOCACHE"] = gocache
    go = os.path.join(goroot, "bin", "go" + host.exe_suffix())
    _run([go, "mod", "download"], env=env, cwd=mod)
    icons = make_icons(go, env, mod, work, icon_bytes)
    notices = write_notices(go, env, mod, goroot, os.path.join(work, NOTICES))
    game_png = None
    if icon_bytes is not None:
        with open(os.path.join(icons, "icon-256.png"), "rb") as f:
            game_png = f.read()
    stage_module(mod, src, wizard_mod.config_json(cfg), game_png)
    # Docker's context: the Linux tar takes it from there.
    shutil.copyfile(notices, os.path.join(src, NOTICES))
    if "macos" in only:
        if host.host_os() != "macos":
            raise CliError("the macOS asset needs a macOS host (Apple clang, lipo)")
        icon = os.path.join(icons, "Icon.png")
        built.update(
            build_macos(
                go,
                env,
                src,
                work,
                name,
                tag_v,
                icon,
                app_id(rel.get("repo", ""), slug),
                pins["fyne_tool"],
                epoch,
                os.path.join(out, names["macos"]),
                notices,
            )
        )
        done.append(names["macos"])
    if "windows" in only:
        mingw = ensure_mingw(pins, mod, G)
        mingw_notice(mingw, mod)
        built.update(
            build_windows(
                go,
                env,
                src,
                work,
                name,
                tag_v,
                mingw,
                epoch,
                os.path.join(out, names["windows"]),
                icons,
                notices,
            )
        )
        done.append(names["windows"])
    if "linux" in only:
        stage_linux_icon(src, icons, cfg["game"]["name"], name)
        b, image = build_linux(
            pins, src, work, name, tag_v, epoch, os.path.join(out, names["linux"]), bool(gocache)
        )
        built.update(b)
        done.append(names["linux"])
    paths = [os.path.join(out, n) for n in done]
    with open(os.path.join(out, "SHA256SUMS"), "w", newline="\n") as f:
        f.write(sums_text(paths))
    for note in dry_runs(built, pins, src, name, tag_v, epoch, image):
        host.say(note)
    hashes = {os.path.basename(p): sha256_file(p) for p in paths}
    for k in ("macos-binary", "windows-binary", "linux-binary"):
        if k in built and os.path.isfile(built[k]):
            hashes[k] = sha256_file(built[k])
    host.say()
    for p in paths:
        host.say("%s  %s  (%.1f MB)" % (hashes[os.path.basename(p)], p, os.path.getsize(p) / 1e6))
    host.say("SHA256SUMS: %s" % os.path.join(out, "SHA256SUMS"))
    if not keep_work:
        shutil.rmtree(os.path.join(work, "src"), ignore_errors=True)
    return hashes


def check_script(mod):
    return os.path.join(mod, "check.sh")


def check(G, tag="", dev=False, no_build=False):
    """wizard/check.sh (gofmt, vet, tests, staticcheck when present) on the
    pinned Go, then two builds of every asset with equal sha256s."""
    mod = module_dir()
    pins = load_pins(mod)
    goroot = ensure_go(pins, mod)
    env = go_env(goroot, mod)
    host.step("wizard check: gofmt, vet, test")
    _run(["bash", check_script(mod)], env=env, cwd=mod)
    if no_build:
        return 0
    host.step("wizard check: two builds, equal sha256s")
    runs = []
    with tempfile.TemporaryDirectory(prefix="wizard-check-") as tmp:
        runs.append(build(G, tag, dev, ASSETS, os.path.join(tmp, "1"), private_ok=True))
        # The second build shares no compiled output with the first (an
        # empty GOCACHE, docker --no-cache), so equal sha256s mean the
        # build reproduces, not that it was served from a cache.
        fresh = os.path.join(tmp, "gocache")
        runs.append(
            build(G, tag, dev, ASSETS, os.path.join(tmp, "2"), gocache=fresh, private_ok=True)
        )
    diff = sorted(k for k in set(runs[0]) | set(runs[1]) if runs[0].get(k) != runs[1].get(k))
    for k in sorted(runs[0]):
        host.say("%s %s %s" % ("same" if k not in diff else "DIFF", runs[0][k][:16], k))
    if diff:
        raise CliError("the two builds differ: %s" % ", ".join(diff))
    host.say("reproducible: %d files, equal sha256s" % len(runs[0]))
    return 0
