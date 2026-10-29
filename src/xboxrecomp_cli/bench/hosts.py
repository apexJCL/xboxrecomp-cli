"""A native Windows bench host (BENCH_HOST_OS=windows): Windows 11 with the
OpenSSH server, whose login shell is cmd.exe. No bash, rsync, flock or
Proton there, so the host side is PowerShell (host/windows/*.ps1): copied
once per digest to <BENCH_DIR>/.xbr/host/<digest>/ by scp, each run given
a JSON params file instead of shell assignments, and its files pulled with
sftp batches. The POSIX host's paths in bench/__init__.py, golden.py and
ship.py are untouched; they call here only when cfg.windows.

Paths here are drive paths with / (config.windows_paths): scp takes
D:/x, sftp names the same file /D:/x, and the game is handed D:\\x."""

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from . import ship as ship_mod
from .remote import KEEPALIVE, BenchError, ssh_bin

WIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host", "windows")
SCRIPTS = ("build.ps1", "launch.ps1", "prune_frames.ps1", "run_game.ps1", "ship.ps1")

# RECOMP_TRACE and RECOMP_DEBUG are lists: a scenario's pins and BENCH_ENV
# each add their keys (docs/env.md) instead of the last one winning, as
# run_game.sh joins them.
LIST_KEYS = ("RECOMP_TRACE", "RECOMP_DEBUG")

# What a Windows host cannot do, and why: the verbs left are run, golden,
# logs, build and sync (both local), all, shell and integrate.
REFUSED = {
    "setup": "nothing to set up: the host only runs what this machine built (no box, no Proton)",
    "tests": "the toolkit's Proton ctests need a POSIX host",
    "pacing": "pacing reads Proton's timings; not ported to a Windows host",
    "gc": "gc's host scripts are bash; clear BENCH_DIR's run directories by hand",
    "doctor": "doctor's checks are bash; `bench shell` opens the host",
    "symbolize": "symbolize runs llvm-symbolizer on the host; not installed on a Windows host",
}

# check_run_end's word for a run that reached its limit (timeout's code on
# the POSIX host): the game exits 0 itself at RECOMP_WINDOW_QUIT_AFTER, and
# a backstop kill past the limit reads the same, as a Deck timeout does.
TIMED_OUT_RC = 124


def refusal(cmd):
    why = REFUSED.get(cmd)
    return "%s: not on a Windows host (BENCH_HOST_OS=windows): %s" % (cmd, why) if why else None


def win_path(p):
    """The game's spelling of a host path."""
    return p.replace("/", "\\")


def sftp_path(p):
    """sftp names a Windows drive path /D:/..., scp takes D:/..."""
    return p if p.startswith("/") else "/" + p


def ps_quote(s):
    """A PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def script_digest():
    h = hashlib.sha256()
    for name in SCRIPTS:
        with open(os.path.join(WIN_DIR, name), "rb") as f:
            h.update(name.encode() + b"\0" + f.read() + b"\0")
    return h.hexdigest()[:16]


def game_env(env_text, log_dir, game_files, frames, timeout, cache_dir=None):
    """[(key, value)] the game starts with: the raw GAME_ENV text split as
    run_game.sh's bash splits it, the list keys joined, @run resolved to
    this run's log dir and the bench's own keys added, every path in the
    game's spelling. RECOMP_WINDOW_QUIT_AFTER makes the game end itself at
    the limit (no SIGINT on Windows); the controller's kill is only the
    backstop. cache_dir: the base's shader cache (RECOMP_DXBC_CACHE)."""
    try:
        words = shlex.split(env_text)
    except ValueError:
        words = env_text.split()
    out, lists = {}, {k: [] for k in LIST_KEYS}
    for w in words:
        if "=" not in w:
            continue
        k, v = w.split("=", 1)
        if k in lists:
            lists[k].append(v)
        else:
            out[k] = v
    log = win_path(log_dir)
    if out.get("RECOMP_SAVE_DIR") == "@run":
        out["RECOMP_SAVE_DIR"] = log + "\\save"
    if out.get("RECOMP_FB_DUMP") == "@run":
        del out["RECOMP_FB_DUMP"]
        lists["RECOMP_DEBUG"].append("fb_dump=%s\\frames\\f" % log)
    if frames == "1":
        lists["RECOMP_DEBUG"].append("d3d11_dump=%s\\frames\\" % log)
    for k in LIST_KEYS:
        if lists[k]:
            out[k] = ",".join(lists[k])
    out["RECOMP_STDIO_LOG"] = log + "\\game-stdio.log"
    # A fresh emulated disk per run: the default is the player's own
    # (%LOCALAPPDATA%\xboxrecomp), which no Wine prefix stands in for here.
    out.setdefault("RECOMP_HDD_DIR", log + "\\hdd")
    # The shader cache defaults to the HDD dir, which would make every run
    # compile cold; a Deck run keeps its prefix's cache warm. Compile
    # stalls shift wall time against flips, and wall-time content (a
    # title's rotation) with it, so the PC keeps one cache per base.
    if cache_dir:
        out.setdefault("RECOMP_DXBC_CACHE", win_path(cache_dir))
    out.setdefault("RECOMP_GAME_FILES", win_path(game_files))
    if timeout:
        out["RECOMP_WINDOW_QUIT_AFTER"] = str(timeout)
    return sorted(out.items())


def sftp_batch(pairs):
    """An sftp -b batch getting each (remote, local dir); a leading - lets
    the batch go on past a file the run did not write. A trailing /* stays
    outside the quotes: sftp escapes a glob inside them, which then matches
    only a file named *."""

    def arg(r):
        r = sftp_path(r)
        return '"%s/"*' % r[:-2] if r.endswith("/*") else '"%s"' % r

    return "".join('-get %s "%s/"\n' % (arg(r), d) for r, d in pairs)


class WindowsHost:
    """target and base: the ssh target and the directory its scripts live
    under, BENCH_HOST and BENCH_DIR unless given (a build host's are
    BENCH_BUILD_HOST and BENCH_BUILD_DIR: bench/buildhost.py)."""

    def __init__(self, b, target=None, base=None):
        self.b = b
        self.c = b.cfg
        self.target = target or self.c.host
        self.base = base or self.c["BENCH_DIR"]
        self._scripts = None

    # transport
    def _run(self, argv, capture=False):
        sys.stdout.flush()
        # No stdin: the host scripts read their params file, and an ssh
        # left on this terminal's stdin would swallow what is typed next.
        r = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None
        )
        return r.returncode, (r.stdout.decode(errors="replace") if capture else "")

    def ssh(self, cmd, capture=False):
        """ssh HOST CMD; cmd.exe reads CMD."""
        return self._run(
            [ssh_bin()] + ["-o", "BatchMode=yes"] + KEEPALIVE + [self.target, cmd], capture=capture
        )

    def powershell(self, command, capture=False):
        return self.ssh(
            'powershell -NoProfile -NonInteractive -Command "%s"' % command, capture=capture
        )

    def scp(self, files, dest_dir):
        s = shutil.which("scp")
        if not s:
            raise BenchError("no scp on PATH; install the OpenSSH client")
        argv = (
            [s, "-q", "-o", "BatchMode=yes"]
            + KEEPALIVE
            + list(files)
            + ["%s:%s/" % (self.target, dest_dir)]
        )
        return self._run(argv)[0]

    def sftp(self, batch):
        s = shutil.which("sftp")
        if not s:
            raise BenchError("no sftp on PATH; install the OpenSSH client")
        with tempfile.NamedTemporaryFile("w", suffix=".sftp", delete=False) as f:
            f.write(batch)
        try:
            argv = [s, "-q", "-o", "BatchMode=yes"] + KEEPALIVE + ["-b", f.name, self.target]
            r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        finally:
            os.remove(f.name)
        return r.returncode, r.stderr.decode(errors="replace")

    # host scripts
    def scripts(self):
        """The host's copy of host/windows, sent once per digest (a changed
        script gets a new directory, so a run never mixes two versions)."""
        if self._scripts:
            return self._scripts
        d = "%s/.xbr/host/%s" % (self.base, script_digest())
        # The params dir every time: removed by hand, it would fail each
        # later ps() while digest.txt still says the copy is whole.
        rc, _ = self.powershell(
            "New-Item -ItemType Directory -Force -Path %s | Out-Null; "
            "if (Test-Path -LiteralPath %s) { exit 0 }; "
            "New-Item -ItemType Directory -Force -Path %s | Out-Null; exit 3"
            % (ps_quote(self.params_dir()), ps_quote(d + "/digest.txt"), ps_quote(d))
        )
        if rc == 3:
            with tempfile.TemporaryDirectory() as t:
                digest = os.path.join(t, "digest.txt")
                with open(digest, "w") as f:
                    f.write(script_digest() + "\n")
                # digest.txt last: its presence says the copy is whole.
                if self.scp([os.path.join(WIN_DIR, n) for n in SCRIPTS], d) or self.scp(
                    [digest], d
                ):
                    raise BenchError("could not copy the host scripts to %s:%s" % (self.target, d))
        elif rc:
            raise BenchError(
                "ssh %s: PowerShell failed (exit %d); is it a Windows host?" % (self.target, rc)
            )
        self._scripts = d
        return d

    def params_dir(self):
        return "%s/.xbr/params" % self.base

    def ps(self, name, params, capture=False):
        """Run host/windows/NAME with PARAMS (a dict, sent as a JSON file
        the script reads and removes)."""
        d = self.scripts()
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "%s-%d-%d.json" % (name[:-4], os.getpid(), time.time_ns()))
            with open(p, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=1)
            if self.scp([p], self.params_dir()):
                raise BenchError("could not send %s's params to %s" % (name, self.target))
            remote = "%s/%s" % (self.params_dir(), os.path.basename(p))
        return self.ssh(
            'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%s/%s" -Params "%s"'
            % (d, name, remote),
            capture=capture,
        )

    # ship
    def ship(self, exe=None, exe_sha=None):
        """ship.ship for this host: the staged set made exactly the exe's
        directory's files (ship.ps1 lists what differs by sha256), sent by
        scp. No game files link: the game is pointed at BENCH_GAME_FILES."""
        b, c = self.b, self.c
        with ship_mod.staged(b, exe, exe_sha) as (t, out, exe, sha):
            if getattr(b, "_shipped", None) == (c.remote_game, sha):
                return sha
            dest = ship_mod.exe_rel(c)
            windows_dir = c.game.m["build"]["windows_dir"]
            prov = ship_mod.provenance_file(c, exe, sha, out, t)
            b.step("ship: %s -> %s:%s" % (os.path.relpath(exe, c.game_dir), c.host, c.remote_game))
            files = [
                [dest, n, ship_mod.sha256_file(os.path.join(out, n))]
                for n in sorted(os.listdir(out))
            ]
            if os.path.dirname(prov) != out:
                files.append([windows_dir, "provenance.txt", ship_mod.sha256_file(prov)])
            rc, need = self.ps(
                "ship.ps1",
                {
                    "run_dir": c.remote_game,
                    "exact": dest,
                    "exe": os.path.basename(exe),
                    "files": files,
                },
                capture=True,
            )
            sys.stdout.write("".join(x for x in need.splitlines(True) if not x.startswith("need ")))
            if rc:
                raise BenchError("ship: the host refused (exit %d)" % rc)
            want = [x[5:].strip() for x in need.splitlines() if x.startswith("need ")]
            by_dir = {}
            for d, n, _ in files:
                if "%s/%s" % (d, n) in want:
                    src = (
                        prov
                        if (d, n) == (windows_dir, "provenance.txt")
                        and os.path.dirname(prov) != out
                        else os.path.join(out, n)
                    )
                    by_dir.setdefault(d, []).append(src)
            for d, srcs in by_dir.items():
                b.say("ship: %d file(s) -> %s/" % (len(srcs), d))
                if self.scp(srcs, "%s/%s" % (c.remote_game, d)):
                    raise BenchError("ship: scp to %s:%s/%s failed" % (c.host, c.remote_game, d))
            if not by_dir:
                b.say("ship: the host has this build already")
        b._shipped = (c.remote_game, sha)
        return sha

    # run
    def run_game(self, stamp, game_args, env_text, timeout, frames, lock_held, kill_game, expect):
        c = self.c
        log = "%s/bench-logs/%s" % (c.remote_game, stamp)
        exe = c.game.m["build"]["exe"] + ".exe"
        params = {
            "stamp": stamp,
            "run_dir": c.remote_game,
            "log": log,
            "exe_rel": "%s/%s" % (ship_mod.exe_rel(c), exe),
            "provenance": "%s/provenance.txt" % c.game.m["build"]["windows_dir"],
            "game_files": c["BENCH_GAME_FILES"],
            "env": [
                list(kv)
                for kv in game_env(
                    env_text,
                    log,
                    c["BENCH_GAME_FILES"],
                    frames,
                    timeout,
                    cache_dir=c.base + "/shader-cache",
                )
            ],
            "args": list(game_args),
            "timeout": int(timeout) if timeout else 0,
            "lock": c["BENCH_LOCK"],
            "lock_held": lock_held == "1",
            "kill_game": kill_game == "1",
            "expect_sha": expect,
            "owner": "bench %s %s %s" % (c.game.slug, stamp, socket.gethostname()),
            "launch": "%s/launch.ps1" % self.scripts(),
        }
        rc = self.ps("run_game.ps1", params)[0]
        if rc == ship_mod.EXE_CHANGED_RC:
            raise BenchError(
                "run: the exe in %s is no longer the one this command shipped "
                "(another ship to the same run directory?); nothing ran" % c.remote_game
            )
        if rc == 75:
            raise BenchError("run: %s was held for an hour; nothing ran" % c["BENCH_LOCK"])
        if rc:
            raise BenchError("run: the host did not start the game (exit %d, see above)" % rc)

    # pulls
    def logs(self, stamps):
        """The runs' top-level files (logs, run-info, exit-code), not their
        frames/ or save/: sftp's get without -r skips directories."""
        dst = os.path.join(self.c.game_dir, "bench-logs")
        pairs = []
        for s in stamps:
            os.makedirs(os.path.join(dst, s), exist_ok=True)
            pairs.append(("%s/bench-logs/%s/*" % (self.c.remote_game, s), os.path.join(dst, s)))
        rc, err = self.sftp(sftp_batch(pairs))
        if rc:
            sys.stdout.write(err)
        return rc

    def pull_frames(self, stamp, names, dest):
        """frames/NAMES of a run into dest; returns the names not pulled."""
        src = "%s/bench-logs/%s/frames" % (self.c.remote_game, stamp)
        if names:
            self.sftp(sftp_batch([("%s/%s" % (src, n), dest) for n in names]))
        return [n for n in names if not os.path.isfile(os.path.join(dest, n))]

    def prune(self, stamps):
        return self.ps("prune_frames.ps1", {"run_dir": self.c.remote_game, "stamps": stamps})[0]

    def exe_sha(self):
        """(rc, "<sha256>  <path>") of the run directory's exe, as
        integrate_show.sh prints it."""
        c = self.c
        p = "%s/%s/%s.exe" % (c.remote_game, ship_mod.exe_rel(c), c.game.m["build"]["exe"])
        rc, out = self.powershell(
            "(Get-FileHash -LiteralPath %s).Hash.ToLower()" % ps_quote(p), capture=True
        )
        return rc, "%s  %s\n" % (out.strip(), p) if rc == 0 else out

    def shell(self):
        c = self.c
        sys.stdout.flush()
        return subprocess.run(
            [ssh_bin(), "-t"]
            + KEEPALIVE
            + [
                c.host,
                'powershell -NoExit -Command "Set-Location -LiteralPath %s"'
                % ps_quote(c.remote_game),
            ]
        ).returncode
