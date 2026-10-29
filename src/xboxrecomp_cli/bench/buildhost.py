"""BENCH_BUILD_HOST: a native Windows machine compiles the exe that
BENCH_BUILD=local would compile here, and this machine ships it as its own.

The build host keeps one tree per game under BENCH_BUILD_DIR,

  <BENCH_BUILD_DIR>/<game>/game, xboxrecomp, xboxrecomp-cli   git checkouts
  <BENCH_BUILD_DIR>/<game>/gen-digest.txt                     gen/ it holds

fed by git bundles of the commits this machine has checked out (a dirty
tree goes as `git stash create`'s commit: tracked changes, never untracked
files) and by gen/ as a tarball when its digest moved: gen/ is ignored by
git, and comes from the game's own files. There the game's `setup`
installs its pinned llvm-mingw, CMake and Ninja (sha256-checked, in user
space), and `<game> build windows` runs with the same CLI commit as this
one. The exe, its PDB, config.default.toml and DLLs come back by sftp,
checked against the sha256 the host printed, into

  <windows_dir>/build-host/   (an exe_dir below it as in windows_dir)

which local_exe() then names, so run, golden and integrate ship it to the
run host (a Deck under Proton, or the PC itself) as they ship a local one.

Every controller (cat, agent worktrees, build-all) shares the one tree per
game, so all that touches it runs under the PC's bench lock
(BENCH_BUILD_LOCK, the file its runs take): the checkouts, gen/'s unpack
and a digest of the gen/ the host then holds (a call whose state probe saw
another gen/ than the lock finds is refused, and sent again with gen/),
the command, and the copy of its outputs into the call's own .out/<call>,
which the pull reads after the lock is gone. Uploads go to the call's own
.in/<call>, so two controllers waiting for the lock never overwrite each
other's bundles.
"""

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import time

from ..cli_dir import cli_dir, is_checkout
from .remote import KEEPALIVE, BenchError, ssh_bin

# The ref a bundle carries, with the call's id below it: per worktree and
# per call, so two bench commands never move each other's.
REF = "refs/worktree/xbr-build-host"
REPOS = ("game", "xboxrecomp", "xboxrecomp-cli")
# Where the pulled build lands, under the game's windows_dir.
PULLED = "build-host"


def git(d, *args, check=True, env=None):
    r = subprocess.run(
        ["git", "-C", d] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, **env) if env else None,
    )
    if check and r.returncode:
        raise BenchError(
            "git -C %s %s: %s" % (d, " ".join(args), r.stderr.decode(errors="replace").strip())
        )
    return r.returncode, r.stdout.decode(errors="replace").strip()


def snapshot(d):
    """(commit, note): HEAD for a clean tree; for tracked changes, the
    commit `git stash create` makes of them (the tree and the stash list
    stay as they are). note names what does not travel: untracked files."""
    head = git(d, "rev-parse", "HEAD")[1]
    note = ""
    untracked = git(d, "ls-files", "--others", "--exclude-standard")[1]
    if untracked:
        n = len(untracked.splitlines())
        note = "%d untracked file(s) in %s are not sent" % (n, d)
    # Dated as HEAD, so the same edits make the same commit: the host then
    # has it, and an unchanged dirty tree sends nothing the next time.
    when = git(d, "log", "-1", "--format=%cI", "HEAD")[1]
    stamp = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    commit = git(d, "stash", "create", env=stamp)[1] or head
    return commit, note


def _lock_path(p):
    """One lock file however it is spelled: NTFS ignores case, and Windows
    takes either slash."""
    return p.replace("\\", "/").lower()


def bundle(d, commit, have, out, ref=REF):
    """A bundle of commit in out, without what the host has (have) when
    this repository knows that commit. None when the host has commit:
    have itself or a descendant of it (a toolkit tip below a feature
    branch built before, a CLI pin), for which git would refuse an empty
    bundle."""
    if commit == have:
        return None
    known = bool(have) and git(d, "cat-file", "-e", have + "^{commit}", check=False)[0] == 0
    if known and git(d, "merge-base", "--is-ancestor", commit, have, check=False)[0] == 0:
        return None
    if ref != REF:
        # A bare ref left by an older call blocks every ref under it (a
        # file where git needs a directory).
        git(d, "update-ref", "-d", REF, check=False)
    git(d, "update-ref", ref, commit)
    try:
        args = ["bundle", "create", "-q", out, ref]
        if known:
            args.append("^" + have)
        git(d, *args)
    finally:
        git(d, "update-ref", "-d", ref, check=False)
    return out


def gen_state(game):
    """sha256 over gen/'s digest and gen.key.json: what the host holds
    when its gen-digest.txt says the same."""
    from ..package import lib as package_lib

    gd, n = package_lib.gen_digest(game.gen)
    h = hashlib.sha256(gd.encode())
    try:
        with open(game.gen_key, "rb") as f:
            h.update(f.read())
    except OSError:
        pass
    return h.hexdigest(), n


def gen_tarball(game, out):
    with tarfile.open(out, "w:gz", compresslevel=6) as t:
        t.add(game.gen, arcname=game.rel(game.gen))
        if os.path.isfile(game.gen_key):
            t.add(game.gen_key, arcname=game.rel(game.gen_key))
    return out


def pulled_dir(cfg):
    return os.path.join(cfg.game.build_dir("windows"), PULLED)


def pulled_exe(cfg):
    return cfg.game.exe(pulled_dir(cfg), "windows")


# build.ps1's protocol lines: a prefix no CLI output starts with.
PROTO = "@xbr-"


def stream(argv):
    """Run argv, echoing its output as it comes, minus the protocol lines
    (@xbr-...), which are returned with the exit code."""
    sys.stdout.flush()
    p = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    proto = []
    for raw in p.stdout:
        line = raw.decode(errors="replace").rstrip("\r\n")
        if line.startswith(PROTO):
            proto.append(line[len(PROTO) :])
        else:
            print(line, flush=True)
    return p.wait(), proto


class BuildHost:
    """The BENCH_BUILD_HOST of a bench: its transport is WindowsHost's,
    pointed at the build host and BENCH_BUILD_DIR."""

    def __init__(self, b):
        from .hosts import WindowsHost

        self.b = b
        self.c = b.cfg
        self.target = self.c["BENCH_BUILD_HOST"]
        self.w = WindowsHost(b, target=self.target, base=self.c["BENCH_BUILD_DIR"])
        self.root = "%s/%s" % (self.c["BENCH_BUILD_DIR"], self.c.game.slug)
        self.call = "%d-%d" % (os.getpid(), time.time_ns())

    def trees(self):
        # BENCH_BUILD_CLI: the CLI the host builds with, when it is not the
        # one driving (build-all runs each game at its pinned CLI).
        cli = self.c.get("BENCH_BUILD_CLI") or cli_dir()
        if not is_checkout(cli):
            # Under uvx the CLI runs from uv's cache; an enclosing repository
            # ($HOME as dotfiles) would be bundled in its place.
            raise BenchError(
                "build host: the CLI at %s is not a git checkout; set BENCH_BUILD_CLI "
                "to one at the commit to build with" % cli
            )
        return (
            ("game", self.c.game_dir),
            ("xboxrecomp", self.c.toolkit),
            ("xboxrecomp-cli", cli),
        )

    def lock_held(self):
        """BENCH_LOCK_HELD=1 says the caller holds BENCH_LOCK, the run host's
        lock: the build host's only when the run host is this same PC and
        the two locks are one file. A Deck's flock leaves the PC's to take."""
        c = self.c
        return (
            c.get("BENCH_LOCK_HELD") == "1"
            and c.get("BENCH_HOST_OS") == "windows"
            and c.host == self.target
            and _lock_path(c.get("BENCH_LOCK") or "") == _lock_path(c["BENCH_BUILD_LOCK"])
        )

    def params(self, phase, **kw):
        p = {
            "phase": phase,
            "root": self.root,
            "slug": self.c.game.slug,
            "ref": REF,
            # What the host's `setup` fetches: game.toml's pin, not LLVM_MINGW_TAG
            # (the Proton host's symbolizer).
            "mingw": self.c.game.m["toolchain"]["llvm_mingw"],
            "lock": self.c["BENCH_BUILD_LOCK"],
            # BENCH_BUILD_GAME_FILES: the host's own copy, linked into the
            # tree for `package` (a build needs none, so none is sent).
            "call": self.call,
            "game_files": {"path": None, "rel": self.c.game.m["data"]["game_files"]},
            "lock_held": self.lock_held(),
            "owner": "bench build %s %s" % (self.c.game.slug, socket_name()),
        }
        p.update(kw)
        return p

    def state(self):
        """{repo: commit, 'gen': digest} the host holds now."""
        rc, out = self.w.ps(
            "build.ps1", self.params("state", repos=[[n, "", ""] for n in REPOS]), capture=True
        )
        if rc:
            raise BenchError("build host %s: state failed (exit %d)" % (self.target, rc))
        st = {}
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "head":
                st[parts[1]] = parts[2] if len(parts) > 2 else ""
            elif parts and parts[0] == "gen":
                st["gen"] = parts[1] if len(parts) > 1 else ""
        return st

    def run(self, cli_args, exe_rel=None, pull=()):
        """Send what the host lacks, run `<game> CLI_ARGS` there under the
        lock; returns (rc, status, [(name, sha256)] of the runtime files
        when exe_rel names the exe, [names] copied out for pull, commits
        sent). Both sets wait in out_dir() for pull() and pull_paths()."""
        st = self.state()
        rc, status, got, pulled, sent = self.run_once(cli_args, exe_rel, pull, st)
        if status == "gen-moved":
            # Another controller's call changed the host's gen/ between this
            # one's probe and its lock: once more, gen/ sent whatever the
            # host says.
            self.b.say("build host: gen/ there changed since the probe; sending it again")
            st = dict(self.state(), gen=None)
            rc, status, got, pulled, sent = self.run_once(cli_args, exe_rel, pull, st)
        return rc, status, got, pulled, sent

    def run_once(self, cli_args, exe_rel, pull, st):
        c, b = self.c, self.b
        sent = {}
        inbox = "%s/.in/%s" % (self.root, self.call)
        ref = "%s/%s" % (REF, self.call)
        with tempfile.TemporaryDirectory() as t:
            repos, files = [], []
            for name, d in self.trees():
                commit, note = snapshot(d)
                if note:
                    b.say("build host: warning: " + note)
                sent[name] = commit
                bun = bundle(d, commit, st.get(name, ""), os.path.join(t, name + ".bundle"), ref)
                if bun:
                    files.append(bun)
                repos.append([name, os.path.basename(bun) if bun else "", commit])
            gd, n = gen_state(c.game)
            g = c.game
            gen = {
                "tar": "",
                "sha256": "",
                "digest": gd,
                "rel": g.rel(g.gen),
                "key": g.rel(g.gen_key),
                "marker": g.rel(g.regen_marker),
                "build": g.rel(g.build_dir("windows")),
            }
            if st.get("gen") != gd:
                b.say("build host: gen/ (%d files) changed; sending it" % n)
                tar = gen_tarball(g, os.path.join(t, "gen.tar.gz"))
                if gen_state(g)[0] != gd:
                    raise BenchError("build host: gen/ changed while it was packed; send it again")
                from .ship import sha256_file

                files.append(tar)
                gen.update(tar="gen.tar.gz", sha256=sha256_file(tar))
            if files:
                size = sum(os.path.getsize(f) for f in files)
                b.step(
                    "build host: %d file(s), %.1f MB -> %s:%s"
                    % (len(files), size / 1e6, self.target, inbox)
                )
                rc, _ = self.w.powershell(
                    "New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % inbox
                )
                if rc or self.w.scp(files, inbox):
                    raise BenchError("build host: could not send the sources to %s" % self.target)
        b.step("build host: %s %s on %s" % (c.game.slug, " ".join(cli_args), self.target))
        params = self.params(
            "build",
            repos=repos,
            gen=gen,
            args=list(cli_args),
            exe_rel=exe_rel,
            pull=list(pull),
            ref=ref,
        )
        if not exe_rel and c.get("BENCH_BUILD_GAME_FILES"):
            # Linked for this call only (package); a build reads none.
            params["game_files"]["path"] = c["BENCH_BUILD_GAME_FILES"]
        rc, proto = self.ps_stream(params)
        status = next((x.split()[1] for x in proto if x.startswith("status ")), "")
        got = [tuple(x.split()[1:3]) for x in proto if x.startswith("file ")]
        pulled = [x.split(" ", 1)[1] for x in proto if x.startswith("pulled ")]
        return rc, status, got, pulled, sent

    def out_dir(self):
        return "%s/.out/%s" % (self.root, self.call)

    def drop_out(self):
        """The call's .out/<call> on the host, once pulled (or not wanted)."""
        self.w.powershell(
            "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath '%s'"
            % self.out_dir()
        )

    def ps_stream(self, params):
        """WindowsHost.ps with the output streamed (a build runs minutes)."""
        import json
        import time

        w = self.w
        d = w.scripts()
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "build-%d-%d.json" % (os.getpid(), time.time_ns()))
            with open(p, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=1)
            if w.scp([p], w.params_dir()):
                raise BenchError("could not send build.ps1's params to %s" % self.target)
            remote = "%s/%s" % (w.params_dir(), os.path.basename(p))
        return stream(
            [ssh_bin(), "-o", "BatchMode=yes"]
            + KEEPALIVE
            + [
                self.target,
                'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%s/build.ps1"'
                ' -Params "%s"' % (d, remote),
            ]
        )

    def pull(self, files):
        """The runtime files, from the call's .out/<call>, into
        pulled_dir()'s exe directory, each checked against the sha256 the
        host printed; the directory then holds exactly them."""
        c = self.c
        exe = pulled_exe(c)
        dest = os.path.dirname(exe)
        os.makedirs(dest, exist_ok=True)
        remote_dir = self.out_dir()
        with tempfile.TemporaryDirectory(dir=dest) as t:
            from .hosts import sftp_batch

            rc, err = self.w.sftp(sftp_batch([("%s/%s" % (remote_dir, n), t) for n, _ in files]))
            if rc:
                raise BenchError("build host: pulling the build failed: %s" % err.strip())
            from .ship import sha256_file

            for n, want in files:
                got = sha256_file(os.path.join(t, n))
                if got != want:
                    raise BenchError(
                        "build host: %s came back as sha256 %s, not the host's %s" % (n, got, want)
                    )
            names = {n for n, _ in files}
            for n in os.listdir(dest):
                p = os.path.join(dest, n)
                if n not in names and os.path.isfile(p):
                    os.remove(p)
            for n in names:
                os.replace(os.path.join(t, n), os.path.join(dest, n))
        return exe

    def pull_paths(self, names):
        """What build.ps1 copied into .out/<call> for --pull (files or
        directories: a patch bundle is a folder, so each get is recursive)
        into <windows_dir>/build-host/pulled/."""
        from .hosts import sftp_path

        dest = os.path.join(pulled_dir(self.c), "pulled")
        os.makedirs(dest, exist_ok=True)
        if not names:
            return dest
        src = sftp_path(self.out_dir())
        lines = "".join('get -R "%s/%s" "%s/"\n' % (src, n, dest) for n in names)
        rc, err = self.w.sftp(lines)
        if rc:
            raise BenchError("build host: pulling %s failed: %s" % (" ".join(names), err.strip()))
        return dest

    def build(self, cmake_args=(), stale_ok=False, jobs=0):
        """`<game> build windows` on the host, pulled here. Returns
        (local exe, {repo: commit sent})."""
        g = self.c.game
        argv = ["build", "windows"]
        if jobs:
            argv += ["--jobs", str(jobs)]
        if stale_ok:
            argv.append("--stale-gen-ok")
        if cmake_args:
            argv += ["--"] + list(cmake_args)
        exe_rel = g.rel(g.exe(g.build_dir("windows"), "windows"))
        try:
            rc, status, files, _, sent = self.run(argv, exe_rel=exe_rel)
            if status == "lock-timeout":
                raise BenchError(
                    "build: %s was held for an hour; nothing built" % self.c["BENCH_BUILD_LOCK"]
                )
            if status == "gen-moved":
                raise BenchError("build: gen/ on %s kept changing; nothing built" % self.target)
            if rc:
                raise BenchError("build: failed on %s (exit %d)" % (self.target, rc))
            if not files:
                raise BenchError("build: %s listed no files for %s" % (self.target, exe_rel))
            exe = self.pull(files)
        finally:
            self.drop_out()
        return exe, sent


def socket_name():
    import socket

    return socket.gethostname().split(".")[0]


def provenance_lines(cfg, sent):
    return [
        "build host: %s %s/%s (native llvm-mingw, BENCH_BUILD_HOST)"
        % (cfg["BENCH_BUILD_HOST"], cfg["BENCH_BUILD_DIR"], cfg.game.slug),
        "sent: " + " ".join("%s %s" % (k, sent[k]) for k in REPOS if k in sent),
    ]

