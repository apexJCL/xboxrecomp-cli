"""BENCH_BUILD=local: this machine builds the Windows exe (`<game> build
windows`'s own build, llvm-mingw on a Mac or Linux, native on Windows), and
the host only runs it. A ship sends the exe and the runtime files beside it
to a run directory of its own on the host,

  $BENCH_DIR/<game>/runs/<id>/   <windows_dir>/ (exe, pdb, config, dlls,
                                 provenance.txt), game_files -> the host's
                                 single copy, bench-logs/

where <id> is BENCH_RUN or the exe's sha256 (12 hex digits). That directory
is the REMOTE_GAME every host script runs in, so run, golden, logs and
symbolize work as they did in a synced tree. A new run directory starts as
a host-side copy of the newest other one, so rsync sends only the blocks
that changed; rsync --delete then leaves exactly the shipped files there.

A host where nothing may compile carries ~/.recomp-no-build:
build.sh and tests.sh exit NO_BUILD_RC there, whatever the controller asks.
"""

import contextlib
import glob
import os
import shutil
import socket
import tempfile
import time

from ..cli_dir import cli_dir
from .checks import tree_state
from .remote import BenchError, host_path, shell_quote
from .sync import rsync

# build.sh and tests.sh on a host with ~/.recomp-no-build (EX_CONFIG).
NO_BUILD_RC = 78
NO_BUILD_MARKER = "~/.recomp-no-build"
# run_game.sh under the run lock: the run directory's exe is not the one
# this command shipped (EXPECT_SHA).
EXE_CHANGED_RC = 76
RUN_ID_LEN = 12
# The provenance the local build leaves beside the exe; a ship sends it to
# the host as provenance.txt, the name run_game.sh quotes into run-info.txt.
LOCAL_PROVENANCE = "bench-provenance.txt"


def no_build_error(c, what):
    return BenchError(
        "%s: %s is a no-build host (%s); build here and ship the exe "
        "(BENCH_BUILD=local, the default)" % (what, c.host, NO_BUILD_MARKER)
    )


@contextlib.contextmanager
def driving(cfg):
    """The tree and toolkit the bench drives (BENCH_GAME_DIR,
    XBOXRECOMP_DIR) as the CLI's current ones, which they need not be."""
    from .. import manifest

    saved, saved_tk = manifest.current(), os.environ.get("XBOXRECOMP_DIR")
    manifest.use(cfg.game)
    os.environ["XBOXRECOMP_DIR"] = cfg.toolkit
    try:
        yield
    finally:
        manifest.use(saved)
        if saved_tk is None:
            os.environ.pop("XBOXRECOMP_DIR", None)
        else:
            os.environ["XBOXRECOMP_DIR"] = saved_tk


def local_exe(cfg):
    g = cfg.game
    return g.exe(g.build_dir("windows"), "windows")


def exe_rel(cfg):
    """The exe's directory under the run directory: windows_dir[/exe_dir]."""
    b = cfg.game.m["build"]
    return "/".join(x for x in (b["windows_dir"], b["exe_dir"]) if x)


def sha256_file(path):
    from .. import host

    return host.sha256_path(path)


def run_dir(cfg, exe_sha):
    return "%s/runs/%s" % (cfg.base, cfg["BENCH_RUN"] or exe_sha[:RUN_ID_LEN])


def resolve(cfg):
    """Point REMOTE_GAME at this tree's run directory without shipping
    (logs, symbolize, shell, doctor, gc): BENCH_RUN's, else the one the
    local exe ships to. With neither, it stays the game's base directory."""
    if not cfg.local_build:
        return
    if cfg["BENCH_RUN"]:
        cfg.remote_game = run_dir(cfg, "")
        return
    exe = local_exe(cfg)
    if os.path.isfile(exe):
        cfg.remote_game = run_dir(cfg, sha256_file(exe))


def runtime_files(cfg, exe):
    """What the exe needs beside it on the host: itself, its PDB (symbolize
    names addresses with it), config.default.toml (the game writes the
    player's config from it) and any DLL the build put there."""
    d = os.path.dirname(exe)
    out = [exe]
    for p in (os.path.splitext(exe)[0] + ".pdb", os.path.join(d, "config.default.toml")):
        if os.path.isfile(p):
            out.append(p)
    out += sorted(glob.glob(os.path.join(glob.escape(d), "*.dll")))
    return out


def provenance(cfg, exe, exe_sha, gen, first):
    """provenance.txt for the host: who built the exe from what, the gen/
    digest it was built from, and its sha256."""
    lines = [
        "%s: %s on %s from %s (BENCH_BUILD=local)"
        % (
            first,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            socket.gethostname().split(".")[0],
            cfg.game_dir,
        ),
        tree_state(cfg.game_name, cfg.game_dir),
        tree_state("toolkit", cfg.toolkit),
        tree_state("cli", cli_dir()),
    ]
    if gen:
        lines.append("gen: sha256 %s (%d files)" % gen)
    from ..benchdb import live

    rec = live.record_for_exe(exe, exe_sha)
    if rec:
        # The run-info quotes it as `built:  build: <uuid>`: the db links
        # the run to this very build, not to any with the same exe sha.
        lines.append("build: %s" % rec["uuid"])
    lines.append("exe: %s %s" % (exe_sha, os.path.basename(exe)))
    return "\n".join(lines) + "\n"


def recorded_sha(path):
    """The exe sha256 a local provenance file names, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("exe: "):
                    return line.split()[1]
    except (OSError, IndexError):
        pass
    return None


def gen_of(cfg):
    gen = cfg.game.gen
    try:
        names = [n for n in os.listdir(gen) if not n.startswith(".")]
    except OSError:
        return None
    if not names:
        return None
    from ..package import lib as package_lib

    return package_lib.gen_digest(gen)


def build_local(b, cmake_args=(), stale_ok=False, command="bench build", lineage=None):
    """`<game> build windows` for the bench's tree, then its provenance
    beside the exe. Returns (exe, sha256, gen (digest, files)). command and
    lineage go into the build's record."""
    from .. import build as build_mod
    from ..host import CliError

    c = b.cfg
    if os.path.exists(c.regen_marker):
        # As host-mode sync refuses: recomp is rewriting gen/, and a build
        # now would mix two generations of the code.
        raise BenchError(
            "build: gen/ is mid-regeneration (%s exists); wait for %s recomp"
            % (c.regen_marker, c.game.slug)
        )
    try:
        jobs = int(c.get("BENCH_JOBS") or 0)
    except ValueError:
        raise BenchError("BENCH_JOBS=%s: not a number" % c["BENCH_JOBS"]) from None
    b.step("build: %s here (BENCH_BUILD=local)" % os.path.relpath(local_exe(c), c.game_dir))
    gen_before = gen_of(c)
    with driving(c):
        try:
            exe = build_mod.build(
                "windows",
                list(cmake_args),
                stale_ok=stale_ok,
                jobs=jobs,
                command=command,
                lineage=lineage,
            )
        except CliError as e:
            raise BenchError("build: %s" % e) from None
    gen = gen_of(c)
    if gen != gen_before:
        # recomp rewrote gen/ while the compiler read it: the exe may mix
        # two generations of the code.
        raise BenchError(
            "build: %s/ changed during the build (%s before, %s after); build again"
            % (c.game.m["pipeline"]["gen"], gen_before and gen_before[0], gen and gen[0])
        )
    sha = sha256_file(exe)
    from ..benchdb import live

    rec = live.record_for_exe(exe, sha)
    if rec:
        # ship's side of the same record: what the bench built it for.
        live.build(
            os.path.dirname(live.record_path_of(exe, rec)),
            {
                "uuid": rec["uuid"],
                "gen_digest": gen[0] if gen else None,
                "ship": {"host": c.host, "run_dir": c.remote_game},
            },
        )
    with open(os.path.join(os.path.dirname(exe), LOCAL_PROVENANCE), "w", encoding="utf-8") as f:
        f.write(provenance(c, exe, sha, gen, "built"))
    b.say("build: %s sha256 %s" % (os.path.relpath(exe, c.game_dir), sha))
    return exe, sha, gen


def stage(files, into):
    """The ship's file set in one directory, so rsync --delete makes the
    host's match it exactly. Hard links where the filesystem allows; the
    exe is always a copy (a snapshot: a build that rewrites it during the
    transfer must not change what was hashed)."""
    os.makedirs(into, exist_ok=True)
    for i, f in enumerate(files):
        dst = os.path.join(into, os.path.basename(f))
        if i:
            try:
                os.link(f, dst)
                continue
            except OSError:
                pass
        shutil.copy2(f, dst)


def check_dest(c):
    """The exe's directory under the run directory, refused when it is the
    run directory itself."""
    dest = exe_rel(c)
    # rsync --delete empties the exe's directory into the shipped set: never
    # the run directory itself (bench-logs/, config/, the game_files link).
    if not dest or any(p in ("", ".", "..") for p in dest.split("/")):
        raise BenchError(
            "ship: the exe's directory %r (build.windows_dir, build.exe_dir) must be a "
            "subdirectory of the run directory: rsync --delete would empty the run "
            "directory itself" % dest
        )
    return dest


@contextlib.contextmanager
def staged(b, exe=None, exe_sha=None):
    """The ship's file set staged in a temporary directory, its exe hashed
    and pinned for the command, and REMOTE_GAME pointed at its run
    directory. Yields (tmp, staged dir, exe, sha256)."""
    b.need_host()
    c = b.cfg
    check_dest(c)
    exe = exe or local_exe(c)
    if not os.path.isfile(exe):
        raise BenchError(
            "no %s here: build it first (%s bench build)"
            % (os.path.relpath(exe, c.game_dir), c.game.slug)
        )
    with tempfile.TemporaryDirectory() as t:
        files = runtime_files(c, exe)
        out = os.path.join(t, "exe")
        stage(files, out)
        sha = sha256_file(os.path.join(out, os.path.basename(exe)))
        if exe_sha and exe_sha != sha:
            raise BenchError(
                "ship: %s changed since it was built (sha256 %s, now %s); build again"
                % (os.path.relpath(exe, c.game_dir), exe_sha, sha)
            )
        pinned = getattr(b, "_pinned", None)
        if pinned and pinned != sha:
            raise BenchError(
                "ship: %s changed during this command (sha256 %s, this command ran %s); "
                "nothing ran with it" % (os.path.relpath(exe, c.game_dir), sha, pinned)
            )
        b._pinned = sha
        c.remote_game = run_dir(c, sha)
        yield t, out, exe, sha


def provenance_file(c, exe, sha, out, t):
    """provenance.txt for the ship, written into the staged set when the
    exe's directory is windows_dir, else beside it. Returns its path."""
    local_prov = os.path.join(os.path.dirname(exe), LOCAL_PROVENANCE)
    prov = os.path.join(
        out if exe_rel(c) == c.game.m["build"]["windows_dir"] else t, "provenance.txt"
    )
    if recorded_sha(local_prov) == sha:
        # copy2: its time too, so a second ship of the same exe resends
        # no provenance bytes (rsync may still touch the times).
        shutil.copy2(local_prov, prov)
    else:
        # An exe `<game> build windows` made, not bench build: the trees
        # as they are now, which need not be what it was built from.
        with open(prov, "w", encoding="utf-8") as f:
            f.write(provenance(c, exe, sha, None, "shipped (not built by bench)"))
    return prov


def ship(b, exe=None, exe_sha=None):
    """Send the local exe and the files beside it to its run directory on
    the host (once per command), and make that the REMOTE_GAME. The exe's
    directory there then holds exactly that set. Returns the sha256 of the
    exe shipped, which run_game.sh checks again under the run lock.

    The first ship of a command pins its exe: a later one (the next golden
    scenario) refuses another sha, so one command never runs two builds.
    integrate pins the exe it built and checked on the host."""
    c = b.cfg
    if c.windows:
        return b.host.ship(exe, exe_sha)
    with staged(b, exe, exe_sha) as (t, out, exe, sha):
        dest = exe_rel(c)
        if getattr(b, "_shipped", None) == (c.remote_game, sha):
            return sha
        b.step("ship: %s -> %s:%s" % (os.path.relpath(exe, c.game_dir), c.host, c.remote_game))
        assigns = "DEST=%s\nSRC=%s\n" % (shell_quote(dest), host_path(c["BENCH_GAME_FILES"]))
        b.ok(b.r.remote(b.r.ship("ship_prep.sh", assigns))[0])
        windows_dir = c.game.m["build"]["windows_dir"]
        prov = provenance_file(c, exe, sha, out, t)
        # --checksum: content decides, not times. -i lists every file that
        # differed (and *deleting for what the seed had but this build
        # lacks); the stats line says how many bytes went as literal data.
        opts = ["-az", "--checksum", "-i", "--info=stats1"]
        b.ok(rsync(*opts, "--delete", out + "/", "%s:%s/%s/" % (c.host, c.remote_game, dest)))
        if dest != windows_dir:
            b.ok(rsync(*opts, prov, "%s:%s/%s/" % (c.host, c.remote_game, windows_dir)))
    # run_game.sh's check for a game started outside the bench.
    b.ok(
        rsync(
            *opts,
            os.path.join(cli_dir(), "src", "xboxrecomp_cli", "running_game.py"),
            "%s:%s/xboxrecomp-cli/src/xboxrecomp_cli/" % (c.host, c["BENCH_DIR"]),
        )
    )
    b._shipped = (c.remote_game, sha)
    return sha
