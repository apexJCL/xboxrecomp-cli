"""The bench's configuration: the BENCH_* table of `bench --help`, read the
way the game's scripts/bench.sh read it, so both drive the host the same.

Order (bench.sh's): the toolkit is resolved first; then the game's
scripts/bench.env is applied over the environment (it was sourced, so its
lines win); then game.toml's toolchain.llvm_mingw if LLVM_MINGW_TAG is
still unset; then the defaults, for anything unset or empty. Host paths
keep a literal ~ so the host's shell expands it.
"""

import os
import re

from .. import manifest

KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

        default("BENCH_DIR", "~/xbox-recomp")
        default("BENCH_BOX", "xbr-build")
        default("BENCH_IMAGE", "fedora:42")
        default("LLVM_MINGW_ROOT", env["BENCH_DIR"] + "/llvm-mingw")
        default("PROTONPATH", "GE-Proton")
        default("BENCH_PREFIX", env["BENCH_DIR"] + "/prefix")
        default("BENCH_ENV", "")
        default("BENCH_GAME_FILES", "~/xbox-recomp/%s/game_files" % self.game_name)
        self.env = env
        self.remote_game = env["BENCH_DIR"] + "/" + self.game_name
        self.regen_marker = self.game.regen_marker

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
    def host(self):
        return self.env.get("BENCH_HOST", "")
