"""bench sync: the toolkit and this project to the host with rsync, and the
host's single game_files copy (sync --game-files)."""

import os
import shutil
import subprocess
import sys

from .checks import provenance
from .remote import KEEPALIVE, BenchError

# Source trees: -a without -t, plus --checksum. A file whose content changed
# is rewritten and gets the host's current time; an unchanged one is left
# alone. Keeping this machine's mtimes (-t) could give a changed source a
# time older than the host's build objects, and ninja then skipped the
# rebuild (stale build-win/tests objects).
SRC_OPTS = ["-az", "--no-times", "--checksum", "--delete", "--info=stats1"]
TOOLKIT_EXCLUDES = [".git", "build/", "build-*/", ".venv/", "__pycache__/", ".DS_Store"]
# --delete leaves excluded paths alone, so the host's build-win/, bench-logs/
# and game_files/ survive a sync. game_excludes() puts the game's own
# directories (game.toml's pipeline.out, data.game_files, the marker beside
# pipeline.gen) in the places marked None.
GAME_EXCLUDES = [
    ".git",
    "build/",
    "build-*/",
    "/bench-logs",
    None,  # "/" + pipeline.out
    "/.venv-ghidra",
    None,  # "/" + data.game_files
    "/third_party",
    ".claude/",
    "/bench-provenance.txt",
    None,  # the .gen-regenerating marker beside pipeline.gen
    "scripts/bench.env",
    ".DS_Store",
    "*.log",
    # Gitignored local output the host never builds from: package bundles
    # (dist/ holds game_files, gigabytes), the Mac venv and toolkit clone,
    # worktrees, timeline renders, caches, saves.
    "/dist/",
    "/wt/",
    "/.venv/",
    "/external/",
    "/timeline/",
    "/.ruff_cache/",
    "__pycache__/",
    "UDATA/",
    "TDATA/",
    "saves/",
    # Game archives and disc images, and package files, wherever they sit.
    "*.7z",
    "*.iso",
    "*.xiso",
    "*.zip",
    "*.dmg",
    "*.tar",
]


def game_excludes(game):
    """GAME_EXCLUDES for this game, in the order the host has always seen."""
    fill = iter(
        [
            "/" + game.m["pipeline"]["out"],
            "/" + game.m["data"]["game_files"],
            game.rel(game.regen_marker),
        ]
    )
    return [x if x is not None else next(fill) for x in GAME_EXCLUDES]


def synced_ignored(game):
    """Gitignored but synced on purpose: gen/ is what the host builds from.
    (game_files/ is excluded; sync --game-files links the host's copy.)"""
    return (game.m["pipeline"]["gen"],)


def rsync(*args):
    r = shutil.which("rsync")
    if not r:
        raise BenchError("no rsync on PATH (the rsync-free transfer is a deferred follow-up)")
    sys.stdout.flush()
    # The same ssh options as every other host call: a sync or a frame pull
    # over a stalled connection fails instead of hanging.
    rsh = " ".join(["ssh", "-o", "BatchMode=yes"] + KEEPALIVE)
    return subprocess.run([r, "-e", rsh] + list(args)).returncode


def excludes(pats):
    out = []
    for p in pats:
        out += ["--exclude", p]
    return out


def _git_word(d, *args, fallback):
    r = subprocess.run(
        ["git", "-C", d] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return r.stdout.decode().strip() if r.returncode == 0 else fallback


def cmd_sync(b, args):
    b.need_host()
    game_files = None
    for a in args:
        if a == "--game-files":
            game_files = "link"
        elif a == "--game-files=reflink":
            game_files = "reflink"
        else:
            raise BenchError("sync: unknown option %s" % a)
    c = b.cfg
    if os.path.exists(c.regen_marker):
        raise BenchError(
            "gen/ is mid-regeneration (%s exists); wait for %s recomp"
            % (c.regen_marker, c.game.slug)
        )
    b.ok(b.r.remote("mkdir -p %s %s/xboxrecomp\n" % (c.remote_game, c["BENCH_DIR"]))[0])

    b.step(
        "sync: xboxrecomp @ %s %s"
        % (
            _git_word(c.toolkit, "rev-parse", "--abbrev-ref", "HEAD", fallback="?"),
            _git_word(c.toolkit, "rev-parse", "--short", "HEAD", fallback=""),
        )
    )
    b.ok(
        rsync(
            *SRC_OPTS,
            *excludes(TOOLKIT_EXCLUDES),
            c.toolkit + "/",
            "%s:%s/xboxrecomp/" % (c.host, c["BENCH_DIR"]),
        )
    )
    b.step("sync: %s" % c.game_name)
    b.ok(
        rsync(
            *SRC_OPTS,
            *excludes(game_excludes(c.game)),
            c.game_dir + "/",
            "%s:%s/" % (c.host, c.remote_game),
        )
    )
    b.ok(b.r.command("cat > %s/bench-provenance.txt" % c.remote_game, stdin=provenance(c))[0])
    b.say(provenance(c), end="")
    if game_files is None:
        return 0
    with b.no_errexit():
        return sync_game_files(b, game_files)


def sync_game_files(b, mode):
    """game_files/ on the host: one copy, BENCH_GAME_FILES, shared by every
    tree. The tree that owns it gets this machine's game_files/ rsynced in
    (made writable for the copy if it was read-only, then put back); any
    other tree gets a symlink to it, or with MODE reflink a btrfs reflink
    copy. A real directory already in a tree's place is left alone."""
    c = b.cfg
    gf = c["BENCH_GAME_FILES"]
    parent = gf.rsplit("/", 1)[0] if "/" in gf else gf
    _, out = b.r.command("realpath -m %s; realpath -m %s" % (parent, c.remote_game), capture=True)
    lines = out.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    uniq = [x for i, x in enumerate(lines) if i == 0 or x != lines[i - 1]]
    if len(uniq) == 1:
        b.step("sync: game_files (to %s only) -> %s, the host's single copy" % (c.host, gf))
        _, ro = b.r.command(
            "mkdir -p %s; [ -w %s ] && echo 0 || { chmod -R u+w %s; echo 1; }" % (gf, gf, gf),
            capture=True,
        )
        ro = ro.rstrip("\n")
        # Keeps -a's times on purpose: game_files/ is game data, not build
        # input, and the size+mtime quick check avoids re-reading every file.
        rc = rsync(
            "-az",
            "--info=stats1",
            "--exclude",
            ".DS_Store",
            c.game_dir + "/" + c.game.m["data"]["game_files"] + "/",
            "%s:%s/" % (c.host, gf),
        )
        if ro != "0":
            b.r.command("chmod -R a-w %s" % gf)
        return rc
    b.step("sync: game_files -> %s of %s" % (mode, gf))
    assigns = "src=%s\ndst=%s/game_files\nmode=%s\n" % (gf, c.remote_game, mode)
    return b.r.remote(b.r.ship("game_files.sh", assigns))[0]
