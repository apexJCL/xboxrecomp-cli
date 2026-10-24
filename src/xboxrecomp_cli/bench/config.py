"""The bench's configuration: the BENCH_* table of `bench --help`, read the
way the game's scripts/bench.sh read it, so both drive the host the same.

Order (bench.sh's): the toolkit is resolved first; then the game's
scripts/bench.env is applied over the environment (it was sourced, so its
lines win); then game.toml's toolchain.llvm_mingw if LLVM_MINGW_TAG is
still unset; then the defaults, for anything unset or empty. Host paths
keep a literal ~ so the host's shell expands it.
"""

import os
import posixpath
import re

from .. import manifest

KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# BENCH_BUILD: local (this machine builds, the host only runs: bench/ship.py)
# or host (the host compiles a synced tree).
BUILD_MODES = ("local", "host")
# BENCH_HOST_OS: posix (ssh bash, rsync, flock, Proton) or windows (native
# Windows 11 over OpenSSH: PowerShell scripts, scp/sftp, a lock file).
# Configured, not probed: a probe costs a round trip per command and guesses
# wrong through a jump host.
HOST_OSES = ("posix", "windows")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ConfigError(Exception):
    pass


def tilde(value, home):
    """What bash does to an unquoted assignment value: ~ at the start and
    after each ':' becomes $HOME (~user forms are not supported)."""
    parts = value.split(":")
    out = []
    for p in parts:
        if p == "~" or p.startswith("~/"):
            p = home + p[1:]
        out.append(p)
    return ":".join(out)


def read_env_file(path, home=None):
    """scripts/bench.env: `KEY=value` lines, # comments and blank lines. The
    value may be unquoted, '...' or "..."; anything the shell would expand
    ($, backquotes) or any other line is an error naming the line, since
    bench.sh sourced the file and this cannot reproduce arbitrary shell."""
    home = home if home is not None else os.path.expanduser("~")
    out = {}
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            k, eq, v = line.partition("=")
            if not eq or not KEY.match(k):
                raise ConfigError("%s:%d: not a KEY=value line: %s" % (path, n, line))
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                q, v = v[0], v[1:-1]
                if q in v or (q == '"' and re.search(r"[$`\\]", v)):
                    raise ConfigError("%s:%d: quoting the bench cannot read: %s" % (path, n, line))
            else:
                if re.search(r"[\s$`\\'\";&|<>()]", v):
                    raise ConfigError(
                        "%s:%d: quote the value, or drop $, ` and spaces: %s" % (path, n, line)
                    )
                v = tilde(v, home)
            out[k] = v
    return out


def toolkit_dir(game_dir, env):
    """XBOXRECOMP_DIR, else external/xboxrecomp in the project, else
    ../xboxrecomp, as an absolute path without resolving symlinks (bash's
    `cd DIR && pwd`)."""
    d = env.get("XBOXRECOMP_DIR") or ""
    if not d:
        ext = os.path.join(game_dir, "external", "xboxrecomp")
        d = ext if os.path.isdir(ext) else os.path.join(game_dir, "..", "xboxrecomp")
    if not os.path.isdir(d):
        raise ConfigError("no toolkit at %s (set XBOXRECOMP_DIR)" % d)
    return os.path.normpath(os.path.join(os.getcwd(), d))


def load_game(root, game_dir):
    """The manifest of the tree the bench drives: BENCH_GAME_DIR's own
    game.toml when it has one, else the game the CLI was started for."""
    if (
        root is not None
        and isinstance(root, manifest.Game)
        and (
            os.path.realpath(root.root) == os.path.realpath(game_dir)
            or not os.path.isfile(os.path.join(game_dir, manifest.FILE))
        )
    ):
        return root
    try:
        return manifest.load(game_dir)
    except manifest.ManifestError as e:
        raise ConfigError(str(e)) from e


WINDOWS_PATHS = ("BENCH_DIR", "BENCH_GAME_FILES", "BENCH_LOCK")


def windows_paths(env):
    """A Windows host's paths: drive paths, given (BENCH_DIR and
    BENCH_GAME_FILES have POSIX defaults, ~/xbox-recomp, which mean nothing
    there), with / for \\ (PowerShell, scp and the game take either) and no
    ~ (no shell expands it) or double quote (cmd.exe, the OpenSSH server's
    shell, reads the command line)."""
    for k in ("BENCH_DIR", "BENCH_GAME_FILES"):
        if not env.get(k):
            raise ConfigError(
                "%s is not set: a Windows host (BENCH_HOST_OS=windows) needs it as a "
                "drive path, e.g. %s=%s"
                % (k, k, "D:/bench" if k == "BENCH_DIR" else "D:/bench/game_files")
            )
    for k in WINDOWS_PATHS:
        v = env.get(k)
        if not v:
            continue
        v = v.replace("\\", "/").rstrip("/")
        if "~" in v or '"' in v or not re.match(r"^[A-Za-z]:/", v):
            raise ConfigError(
                '%s=%s: a Windows host takes a drive path (D:/...), without ~ or "' % (k, env[k])
            )
        env[k] = v


class Config:
    """Every BENCH_* value as bench.sh had it, plus the trees."""

    def __init__(self, game, environ=None):
        env = dict(os.environ if environ is None else environ)
        root = game.root if isinstance(game, manifest.Game) else game
        self.game_dir = env.get("BENCH_GAME_DIR") or root
        self.game = load_game(game, self.game_dir)
        m = self.game.m
        self.toolkit = toolkit_dir(self.game_dir, env)
        self.game_name = m["bench"]["remote_name"] or (
            os.path.basename(self.game_dir.rstrip("/")) or self.game_dir
        )
        bench_env = os.path.join(self.game_dir, "scripts", "bench.env")
        if os.path.isfile(bench_env):
            env.update(read_env_file(bench_env, env.get("HOME")))
        if not env.get("LLVM_MINGW_TAG"):
            env["LLVM_MINGW_TAG"] = m["toolchain"]["llvm_mingw"]

        def default(k, v):
            if not env.get(k):
                env[k] = v

        default("BENCH_HOST_OS", "posix")
        if env["BENCH_HOST_OS"] not in HOST_OSES:
            raise ConfigError(
                "BENCH_HOST_OS=%s: use one of %s" % (env["BENCH_HOST_OS"], ", ".join(HOST_OSES))
            )
        if env["BENCH_HOST_OS"] == "windows":
            windows_paths(env)
        default("BENCH_DIR", "~/xbox-recomp")
        default("BENCH_BOX", "xbr-build")
        default("BENCH_IMAGE", "fedora:42")
        default("LLVM_MINGW_ROOT", env["BENCH_DIR"] + "/llvm-mingw")
        default("PROTONPATH", "GE-Proton")
        default("BENCH_PREFIX", env["BENCH_DIR"] + "/prefix")
        default("BENCH_ENV", "")
        default("BENCH_BUILD", "local")
        if env["BENCH_BUILD"] not in BUILD_MODES:
            raise ConfigError(
                "BENCH_BUILD=%s: use one of %s" % (env["BENCH_BUILD"], ", ".join(BUILD_MODES))
            )
        if env["BENCH_HOST_OS"] == "windows" and env["BENCH_BUILD"] != "local":
            raise ConfigError(
                "BENCH_BUILD=%s: a Windows host only runs what this machine built "
                "(BENCH_BUILD=local)" % env["BENCH_BUILD"]
            )
        # Beside the bench directory, not in it: every bench directory under
        # one root (and the hand-run legs) shares the one PC lock.
        default("BENCH_LOCK", posixpath.dirname(env["BENCH_DIR"]) + "/.bench-lock")
        # The game's directory on the host. With BENCH_BUILD=local the host
        # scripts run in one of its runs/<id>/ (ship.resolve, ship.ship),
        # and it is named after the game, not the checkout, so every
        # worktree of a game shares one base there.
        base_name = self.game_name
        if env["BENCH_BUILD"] == "local":
            base_name = m["bench"]["remote_name"] or m["game"]["slug"]
        self.base = env["BENCH_DIR"] + "/" + base_name
        # The host's single game files copy: in local mode the base owns it,
        # so `sync --game-files` fills it on a fresh host.
        default(
            "BENCH_GAME_FILES",
            "%s/%s"
            % (
                self.base if env["BENCH_BUILD"] == "local" else "~/xbox-recomp/" + self.game_name,
                m["data"]["game_files"],
            ),
        )
        run = env.get("BENCH_RUN", "")
        if run and (not RUN_ID.match(run) or run in (".", "..")):
            raise ConfigError("BENCH_RUN=%s: letters, digits, '.', '_' and '-' only" % run)
        self.env = env
        self.remote_game = self.base
        self.regen_marker = self.game.regen_marker

    @property
    def windows(self):
        return self.env.get("BENCH_HOST_OS") == "windows"

    HOST_PATHS = ("BENCH_DIR", "BENCH_PREFIX", "LLVM_MINGW_ROOT", "BENCH_GAME_FILES")

    def warnings(self):
        """Host paths under this machine's home: an unquoted ~ in bench.env
        (or the shell) expanded here, so the host gets a path it lacks."""
        home = os.path.expanduser("~").rstrip("/")
        out = []
        for k in self.HOST_PATHS:
            v = self.env.get(k, "")
            if home and (v == home or v.startswith(home + "/")):
                out.append(
                    "%s=%s is a path on this machine; quote the ~ (%s='~/...') so the "
                    "host expands it" % (k, v, k)
                )
        return out

    def __getitem__(self, k):
        return self.env.get(k, "")

    def get(self, k, default=""):
        return self.env.get(k, default)

    @property
    def local_build(self):
        return self.env.get("BENCH_BUILD") == "local"

    @property
    def host(self):
        return self.env.get("BENCH_HOST", "")
