"""The controller's checks on a pulled run, and the provenance of a sync:
the old scripts/bench.sh's check_run_end, check_present_mismatch,
tree_state and provenance, with the same messages and return codes."""

import os
import re
import socket
import subprocess
import sys
import time

from ..cli_dir import cli_dir
from ..package import lib as package_lib  # the one gen digest, shared with packaging


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _lines(text):
    return text.splitlines() if text else []


def awk_num(s):
    """awk's string-to-number: the leading numeric prefix, else 0."""
    m = re.match(r"\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s or "")
    return float(m.group(0)) if m else 0.0


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


PRESENT_RE = re.compile(r"^\[D3D11\] flip .* present 0x[0-9A-Fa-f]+ walker 0x[0-9A-Fa-f]+")


def check_present_mismatch(log, out=print):
    """Fail a run whose D3D11 flips presented a surface other than the one
    the walker drew (`present X walker Y`, X != Y). A log without those
    lines (another backend, or no flip trace) passes with a note."""
    text = _read(log)
    if text is None:
        print("present: no log %s" % log, file=sys.stderr)
        return 1
    hits = [x for x in _lines(text) if PRESENT_RE.match(x)]
    if not hits:
        out("present: no [D3D11] flip lines in %s (not checked)" % log)
        return 0
    n, first = 0, ""
    for line in hits:
        f = line.split()
        p = w = ""
        # awk's `for (i = 1; i < NF; i++)`: the last field is never a key.
        for i in range(len(f) - 1):
            if f[i] == "present":
                p = f[i + 1]
            if f[i] == "walker":
                w = f[i + 1]
        if p != w:
            n += 1
            if n <= 5:
                first += " %s(%s/%s)" % (f[2] if len(f) > 2 else "", p, w)
    if n == 0:
        out("present: 0/%d flips mismatched" % len(hits))
        return 0
    out(
        "present: FAIL %d/%d flips present a surface the walker did not draw; first: %s"
        % (n, len(hits), first[1:])
    )
    return 1


def check_run_end(log, min_flips="", out=print, crash_tag="[CRASH]"):
    """Fail a run that crashed (a game-stdio.log line starting with
    CRASH_TAG, game.toml's bench.crash_tag) or ended before its limit; with
    MIN_FLIPS, a run under the floor is INCONCLUSIVE (3) on a busy host and
    a FAIL on an idle one. Returns 0, 1 or 3."""
    stdio = _read(os.path.join(log, "game-stdio.log"))
    crash = next((x for x in _lines(stdio) if x.startswith(crash_tag)), "")
    if crash:
        out("end: FAIL crashed: %s" % crash)
        return 1
    rc = (_read(os.path.join(log, "exit-code")) or "").rstrip("\n")
    if not rc:
        out("end: FAIL no exit-code in %s (host script died?)" % log)
        return 1
    info = _read(os.path.join(log, "run-info.txt"))
    limit = "\n".join(
        f[1]
        for f in (x.split() for x in _lines(info) if x.startswith("limit: "))
        if len(f) > 1 and f[1] != "none"
    )
    survived = _read(os.path.join(log, "survived"))
    if survived is not None:
        if any(x.startswith("still alive") for x in _lines(survived)):
            out(
                "end: FAIL the game outlived umu-run and survived wineserver -k and kill -9; "
                "see %s/survived" % log
            )
            return 1
        if not any(x.startswith("killed:") for x in _lines(survived)):
            out(
                "end: FAIL the survivor sweep did not finish (host script died in it?); "
                "see %s/survived" % log
            )
            return 1
        out("end: warning: the game outlived umu-run and was killed; see %s/survived" % log)
    if not limit:
        out("end: exit %s (no limit set; not checked)" % rc)
        return 0
    if rc == "124":
        out("end: ran to the %s s limit" % limit)
    elif rc == "137":
        out("end: FAIL ignored SIGINT at the %s s limit; SIGKILLed" % limit)
        return 1
    else:
        out("end: FAIL exit %s before the %s s limit" % (rc, limit))
        return 1
    if not min_flips:
        return 0
    flips = "\n".join(
        (f[1] if len(f) > 1 else "")
        for f in (x.split() for x in _lines(info) if x.startswith("flips: "))
    )
    busy = "\n".join(x[len("busy:") :].lstrip(" ") for x in _lines(info) if x.startswith("busy:"))
    if flips == "?":
        out("end: FAIL no game-stdio.log: the game wrote no log, so no flip count")
        return 1
    if not flips:
        out("end: FAIL no flips: line in run-info (host script died after the run?)")
        return 1
    try:
        rate = "%.1f" % (awk_num(flips) / awk_num(limit))
    except ZeroDivisionError:
        rate = ""
    fi, mi = _int(flips), _int(min_flips)
    if fi is not None and mi is not None and fi >= mi:
        out("end: %s flips (%s/s; floor %s)" % (flips, rate, min_flips))
        return 0
    load = "\n".join(x[len("load:") :].lstrip(" ") for x in _lines(info) if x.startswith("load:"))
    if busy.startswith("yes"):
        out(
            "end: INCONCLUSIVE %s flips under the floor %s (%s/s): host busy, %s; load %s"
            % (flips, min_flips, rate, busy, load)
        )
        return 3
    out(
        "end: FAIL %s flips under the floor %s (%s/s) on an idle host: perf regression? load %s"
        % (flips, min_flips, rate, load)
    )
    return 1


def _git(d, *args):
    r = subprocess.run(
        ["git", "-C", d] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return r.returncode, r.stdout.decode(errors="replace")


def tree_state(name, d):
    """One line: commit, branch, and whether the working tree differs from
    it (tracked changes or untracked, non-ignored files: a sync sends both)."""
    rc, sha = _git(d, "rev-parse", "HEAD")
    if rc != 0:
        return "%s: (not a git checkout: %s)" % (name, d)
    rc, branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip() if rc == 0 else "?"
    n = _git(d, "status", "--porcelain")[1].count("\n")
    state = "clean" if n == 0 else "dirty (%d paths)" % n
    return "%s: %s %s %s" % (name, sha.strip(), branch, state)


def provenance(cfg):
    """What a sync sent: bench-provenance.txt on the host; a build copies
    it to build-win/provenance.txt and run-info.txt quotes that copy."""
    return "synced: %s from %s:%s\n%s\n%s\n%s\n" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        socket.gethostname().split(".")[0],
        cfg.game_dir,
        tree_state(cfg.game_name, cfg.game_dir),
        tree_state("toolkit", cfg.toolkit),
        # Which CLI synced and built: its own line, after the two trees
        # bench.sh named, so readers of the first lines see what they did.
        tree_state("cli", cli_dir()),
    )


def gen_digest_local(gen_dir):
    """sha256 over gen/'s `sha256  name` listing, sorted by name: the same
    digest package_lib records in a bundle's manifest (one implementation)."""
    return package_lib.gen_digest(gen_dir)[0]
