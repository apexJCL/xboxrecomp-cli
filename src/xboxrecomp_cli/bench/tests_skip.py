"""bench tests, skipped when nothing they test changed: the key of what the
toolkit's Proton tests depend on (the host's toolkit tree, the CLI, the
Proton and prefix it resolves, the toolchain), recorded per host tree in
the local bench-logs when they pass. `bench golden` and `integrate
--golden` skip the tests when the key is the recorded one; `--tests` runs
them anyway, and `bench tests` always runs.

Stdlib only."""

import hashlib
import os
import subprocess
import time

# Host parts tests_key.sh prints, one "name: value" line each. A part that
# is missing or reads "unknown" makes the whole key unknown: never skipped.
HOST_PARTS = ("toolkit", "toolchain", "cmake", "proton", "prefix")


def parse_host_parts(text):
    parts = {}
    for line in text.splitlines():
        name, sep, value = line.partition(": ")
        if sep and name in HOST_PARTS:
            parts[name] = value.strip()
    return parts


def cli_part(d):
    """The CLI checkout's commit, plus a hash of its uncommitted diff when
    it has one; 'unknown' outside git."""
    from ..cli_dir import is_checkout

    if not is_checkout(d):
        return "unknown"

    def git(*a):
        r = subprocess.run(
            ["git", "-C", d] + list(a), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        return r.returncode, r.stdout

    rc, head = git("rev-parse", "HEAD")
    if rc or not head.strip():
        return "unknown"
    rc, diff = git("diff", "HEAD")
    if rc:
        return "unknown"
    head = head.decode().strip()
    return head + ("+" + hashlib.sha256(diff).hexdigest()[:16] if diff else "")


def key_of(parts):
    """sha256 over the parts, or None when one is missing or unknown."""
    want = HOST_PARTS + ("cli", "script", "llvm_mingw")
    if any(not parts.get(p) or parts[p] == "unknown" for p in want):
        return None
    text = "".join("%s: %s\n" % (p, parts[p]) for p in sorted(want))
    return hashlib.sha256(text.encode()).hexdigest()


def record_path(game_dir, host, remote_game):
    """One file per host tree: the local store is shared by every tree."""
    tid = hashlib.sha256(("%s\0%s" % (host, remote_game)).encode()).hexdigest()[:16]
    return os.path.join(game_dir, "bench-logs", "tests-pass", tid + ".txt")


def read_record(path):
    """(key, passed time) from a record, (None, None) without one."""
    key = when = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                name, _, value = line.rstrip("\n").partition(": ")
                if name == "key":
                    key = value
                elif name == "passed":
                    when = value
    except OSError:
        pass
    return key, when


def write_record(path, key, parts, host, remote_game, now=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("key: %s\n" % key)
        f.write("passed: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
        f.write("host: %s\n" % host)
        f.write("dir: %s\n" % remote_game)
        for p in sorted(parts):
            f.write("%s: %s\n" % (p, parts[p]))
    os.replace(tmp, path)


def skip_reason(key, recorded):
    """None when the tests must run, else why they need not."""
    rkey, when = recorded
    if key is None or rkey != key:
        return None
    return "toolkit, CLI and Proton unchanged since the pass at %s" % (when or "?")
