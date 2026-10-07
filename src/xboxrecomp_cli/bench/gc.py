"""bench gc: list, and with --apply remove, bench runs that nothing names
any more, in this machine's store and (--host) the bench host's.

A dry run by default. Only directories named by a run stamp
(YYYYMMDD-HHMMSS, with an optional -suffix) are ever removed; loose files
and named directories are listed by total and left to a person. A run is
removed only when no rule protects it, first match shown:

  1. not ours: its run-info.txt exe line names another game's exe (never
     removed), or there is no exe line (unowned; --include-unowned lets
     it go on to the next rules). The store is shared between games.
  2. referenced: its stamp, or its HHMMSS part as a bare 6-digit token, is
     in the reference text: golden.json, audio.json, TASKS.md,
     OPEN_QUESTIONS.md, RESUME*.md, openspec/ and game.toml's
     [bench.gc] refs, in every worktree of the game's repository.
  3. incomplete: no exit-code file (a run being written, or a broken pull).
  4. young: started less than [bench.gc] days ago (a live agent may name it
     in notes it has not written yet).
  5. among the newest [bench.gc] keep runs of its (kind, scenario): kind
     golden, pacing or plain; scenario the input script on its env line.
  6. host only: the local store lacks a whole copy (same run-info.txt,
     exit-code, and every file outside frames/ and the Proton logs).

Stdlib only. The removal checks every name against the stamp pattern and
every path against the store, and never follows a link.
"""

import argparse
import datetime
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time

from ..benchlog_retention import SIX_RE, STAMP_RE, protection
from .remote import BenchError, quote_words

RUN_RE = re.compile(r"^(\d{8}-\d{6})(-[\w.-]+)?$")
EXE_LINE_RE = re.compile(r"^[0-9a-f]{64}  (\S+\.exe)\s*$", re.M)
BUILT_RE = re.compile(r"^built:\s+([\w.-]+): [0-9a-f]{7,40}\b", re.M)
REF_SUFFIXES = (".md", ".json", ".txt")
SKIP_DIRS = {".git", "bench-logs", "third_party", ".venv", "node_modules"}
MAX_REF_FILE = 8 << 20
# The pipeline's stage outputs under pipeline.out (pipeline.py).
STAGE_DIRS = ("disasm", "func_id", "abi", "recomp", "ghidra")
# The trees the bench scripts and the CLI's own state name (not a game's).
PROVENANCE_TREES = {"synced", "toolkit", "cli"}


def fmt(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return "%d%s" % (n, u) if u == "B" else "%.1f%s" % (n, u)
        n /= 1024.0


def allocated(path):
    """Allocated bytes under path (st_blocks): sparse disk images in a run
    hold gigabytes of holes, which st_size counts. No link is followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512
    total = st.st_blocks * 512
    for d, dirs, files in os.walk(path, followlinks=False):
        for n in dirs + files:
            try:
                total += os.lstat(os.path.join(d, n)).st_blocks * 512
            except OSError:
                pass
    return total


def stamp_time(name):
    m = RUN_RE.match(name)
    return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()


def parse_run_info(text, script_env):
    """{exe, tree, scenario, golden} from a run-info.txt's text (None: no
    run-info.txt)."""
    if text is None:
        return {"exe": None, "tree": None, "scenario": "(none)", "golden": False}
    m = EXE_LINE_RE.search(text)
    trees = [t for t in BUILT_RE.findall(text) if t not in PROVENANCE_TREES]
    env = ""
    for line in text.splitlines():
        if line.startswith("env:"):
            env = line
            break
    scen = re.findall(r"(?<!\S)" + re.escape(script_env) + r"=(\S+)", env)
    return {
        "exe": m.group(1) if m else None,
        "tree": trees[0] if trees else None,
        "scenario": scen[-1] if scen else "(none)",
        "golden": "fb_dump_at=" in env,
    }


def our_exe(game):
    b = game.m["build"]
    return "/".join(x for x in (b["windows_dir"], b["exe_dir"], b["exe"] + ".exe") if x)


def owner(info, exe):
    """'ours', 'other game (X.exe)' or 'unowned', from the exe line alone:
    the built: tree line is missing from older runs and names the Mac
    folder (an agent's worktree), not the game."""
    if not info["exe"]:
        return "unowned"
    if info["exe"] == exe:
        return "ours"
    return "other game (%s)" % os.path.basename(info["exe"])


def kind(name, info):
    if info["golden"]:
        return "golden"
    if (RUN_RE.match(name).group(2) or "") == "-pacing":
        return "pacing"
    return "plain"


# ---- the reference text --------------------------------------------------------


def worktrees(root):
    """The game repository's worktrees, the main checkout first; refuses
    when git cannot list them (a partial reference text deletes evidence)."""
    r = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"], capture_output=True)
    if r.returncode != 0:
        raise BenchError(
            "gc: cannot list the worktrees of %s (%s); refusing: the reference text would be "
            "partial" % (root, r.stderr.decode(errors="replace").strip())
        )
    out = []
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("worktree "):
            out.append(line[len("worktree ") :])
    if not out:
        raise BenchError("gc: no worktrees listed for %s; refusing" % root)
    return out


class Refs:
    """The files read, their stamp sets, and what each one alone protects."""

    def __init__(self):
        self.files = {}  # path -> text
        self.skipped_big = []
        self.missing = []
        self.worktrees = 0

    def add(self, path):
        real = os.path.realpath(path)
        if real in self.files:
            return
        try:
            if os.path.getsize(real) > MAX_REF_FILE:
                self.skipped_big.append(os.path.normpath(path))
                return
            with open(real, errors="replace") as f:
                self.files[real] = f.read()
        except OSError:
            pass


def _excluded(path, patterns):
    p = os.path.normpath(path)
    return any(fnmatch.fnmatch(p, pat) for pat in patterns)


def _walk(top, suffixes, skip, excl, refs):
    for d, dirs, files in os.walk(top, followlinks=False):
        dirs[:] = sorted(
            x
            for x in dirs
            if x not in SKIP_DIRS
            and not x.startswith("build")
            and os.path.normpath(os.path.join(d, x)) not in skip
        )
        for n in sorted(files):
            p = os.path.join(d, n)
            if os.path.islink(p) or (suffixes and not n.endswith(suffixes)):
                continue
            if not _excluded(p, excl):
                refs.add(p)


def read_refs(game, main_root, others, extra=()):
    """Refs over the main checkout (every source required) and the other
    worktrees (missing paths skipped)."""
    m = game.m
    gc = m["bench"]["gc"]
    refs = Refs()
    for i, root in enumerate([main_root] + list(others)):
        main = i == 0
        refs.worktrees += 1
        excl = [os.path.normpath(os.path.join(root, pat)) for pat in gc["refs_exclude"]]
        # Generated output, never docs: gen/ and the pipeline's stage dirs,
        # whose hex and counts would protect runs by their HHMMSS at random.
        out = m["pipeline"]["out"]
        skip = {os.path.normpath(os.path.join(root, m["pipeline"]["gen"]))}
        skip |= {os.path.normpath(os.path.join(root, out, d)) for d in STAGE_DIRS}
        required = [m["golden"]["json"]] if m["golden"]["json"] else []
        required += [m["golden"]["audio"]] if m["golden"]["audio"] else []
        required += ["TASKS.md", "openspec"] + list(gc["refs"])
        for rel in required:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                if main:
                    refs.missing.append(p)
                continue
            if os.path.isdir(p):
                _walk(p, REF_SUFFIXES, skip, excl, refs)
            elif not _excluded(p, excl):
                refs.add(p)
        for n in sorted(os.listdir(root)) if os.path.isdir(root) else ():
            if n == "OPEN_QUESTIONS.md" or (n.startswith("RESUME") and n.endswith(".md")):
                refs.add(os.path.join(root, n))
    for e in extra:
        if os.path.isdir(e):
            _walk(e, None, set(), [], refs)
        elif os.path.isfile(e):
            refs.add(e)
        else:
            refs.missing.append(e)
    return refs


def protectors(refs, names):
    """{run name: [files naming it]}, by benchlog_retention's matching
    (stamps, suffixed names, and bare HHMMSS tokens)."""
    out = {}
    for path, text in refs.files.items():
        for n in protection(text, names):
            out.setdefault(n, []).append(path)
    return out


# ---- the plan -------------------------------------------------------------------


def plan(runs, prot, exe, keep, days, now, include_unowned=False, pulled=None):
    """runs: [{name, info, exit, size}] -> the same dicts with owner, group,
    reason and remove set. pulled: {name: missing-count} for a host plan."""
    for r in runs:
        r["owner"] = owner(r["info"], exe)
        r["group"] = (kind(r["name"], r["info"]), r["info"]["scenario"])
    newest = set()
    groups = {}
    for r in runs:
        if r["owner"] == "ours" or (r["owner"] == "unowned" and include_unowned):
            groups.setdefault(r["group"], []).append(r)
    for rs in groups.values():
        rs.sort(key=lambda r: (stamp_time(r["name"]), r["name"]))
        newest.update(r["name"] for r in rs[-keep:] if keep > 0)
    cutoff = now - days * 86400
    for r in runs:
        n = r["name"]
        r["remove"] = False
        if r["owner"].startswith("other game"):
            r["reason"] = r["owner"]
        elif r["owner"] == "unowned" and not include_unowned:
            r["reason"] = "unowned (no exe line in run-info.txt)"
        elif n in prot:
            r["reason"] = "referenced (%d file%s)" % (
                len(prot[n]),
                "" if len(prot[n]) == 1 else "s",
            )
        elif not r["exit"]:
            r["reason"] = "incomplete (no exit-code)"
        elif stamp_time(n) >= cutoff:
            r["reason"] = "younger than %gd" % days
        elif n in newest:
            r["reason"] = "newest %d of %s %s" % (keep, *r["group"])
        elif pulled is not None and pulled.get(n) != 0:
            miss = pulled.get(n)
            r["reason"] = "not in the local store" + (" (%d files missing)" % miss if miss else "")
        else:
            r["reason"], r["remove"] = "removable", True
    return runs


# ---- local store ----------------------------------------------------------------


def scan_local(store, script_env):
    """(runs, unmanaged): stamp-named run dirs and the rest of the store."""
    runs, unmanaged = [], []
    for n in sorted(os.listdir(store)):
        p = os.path.join(store, n)
        if n.startswith(".") or os.path.islink(p) or not os.path.isdir(p) or not RUN_RE.match(n):
            unmanaged.append((n, allocated(p)))
            continue
        ri = os.path.join(p, "run-info.txt")
        text = None
        if os.path.isfile(ri):
            with open(ri, errors="replace") as f:
                text = f.read()
        runs.append(
            {
                "name": n,
                "info": parse_run_info(text, script_env),
                "exit": os.path.isfile(os.path.join(p, "exit-code")),
                "size": allocated(p),
            }
        )
    return runs, unmanaged


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def remove_local(store, names, say):
    real = os.path.realpath(store)
    fails = 0
    for n in names:
        p = os.path.join(real, n)
        if not RUN_RE.match(n) or os.path.dirname(p) != real or os.path.islink(p):
            say("gc: refusing %s (not a run directory of the store)" % p)
            fails += 1
            continue
        errs = []

        def failed(fn, path, exc, errs=errs):
            errs.append(path)

        shutil.rmtree(p, onexc=failed)
        if errs or os.path.exists(p):
            say("gc: could not remove all of %s (%d errors)" % (p, len(errs)))
            fails += 1
    return fails


# ---- host store -----------------------------------------------------------------


def parse_host_listing(text, script_env):
    """gc_list.sh's output -> (runs, unmanaged, files {name: [rel]},
    runinfo_sha {name: sha|None}, frames_kb {name: kb})."""
    runs, unmanaged, files, sha, frames = {}, [], {}, {}, {}
    info_text = {}
    for line in text.splitlines():
        tag, _, rest = line.partition("\t")
        f = rest.split("\t")
        if tag == "R" and len(f) == 5:
            name, kb, s, ex, fkb = f
            runs[name] = {"name": name, "exit": ex == "1", "size": int(kb) * 1024}
            sha[name] = None if s == "-" else s
            frames[name] = int(fkb) * 1024
        elif tag == "F" and len(f) == 2:
            files.setdefault(f[0], []).append(f[1])
        elif tag == "I" and len(f) == 2:
            info_text.setdefault(f[0], []).append(f[1])
        elif tag == "N" and len(f) == 2:
            unmanaged.append((f[0], int(f[1]) * 1024))
    out = []
    for name, r in sorted(runs.items()):
        lines = info_text.get(name)
        r["info"] = parse_run_info("\n".join(lines) + "\n" if lines else None, script_env)
        out.append(r)
    return out, unmanaged, files, sha, frames


def pulled_state(store, runs, files, sha):
    """{name: number of host files the local copy lacks} (0: a whole copy;
    None: no local copy, or another run-info.txt or no exit-code)."""
    out = {}
    for r in runs:
        n = r["name"]
        loc = os.path.join(store, n)
        ri = os.path.join(loc, "run-info.txt")
        if not os.path.isdir(loc) or os.path.islink(loc):
            out[n] = None
            continue
        if sha.get(n) is None or not os.path.isfile(ri) or sha256_file(ri) != sha[n]:
            out[n] = None
            continue
        if not os.path.isfile(os.path.join(loc, "exit-code")):
            out[n] = None
            continue
        miss = 0
        for rel in files.get(n, []):
            if rel.startswith("frames/") or re.match(r"^steam-.*\.log$", rel):
                continue
            p = os.path.join(loc, rel)
            if not any(os.path.exists(p + s) for s in ("", ".zst", ".gz")):
                miss += 1
        out[n] = miss
    return out


# ---- the command ----------------------------------------------------------------


def parse_args(args, game):
    gc = game.m["bench"]["gc"]
    ap = argparse.ArgumentParser(
        prog="%s bench gc" % game.slug,
        description="List, and with --apply remove, bench runs nothing names any more.",
    )
    ap.add_argument("--apply", action="store_true", help="remove (default: a dry run)")
    ap.add_argument("--host", action="store_true", help="the bench host's store, not this one")
    ap.add_argument("--keep", type=int, default=gc["keep"], help="newest runs per group kept")
    ap.add_argument("--days", type=float, default=gc["days"], help="runs younger are kept")
    ap.add_argument("--include-unowned", action="store_true", help="unowned runs may go")
    ap.add_argument("--refs", action="append", default=[], metavar="PATH", help="more docs")
    ap.add_argument("--quiet", action="store_true", help="totals and sources only")
    return ap.parse_args(args)


def report(say, rows, unmanaged, refs, prot, a, where, frames=None):
    rem = [r for r in rows if r["remove"]]
    if not a.quiet:
        say("%-30s %-24s %-22s %9s  %s" % ("run", "owner", "group", "size", "reason"))
        for r in rows:
            owner_col = r["owner"] + (" [%s]" % r["info"]["tree"] if r["info"]["tree"] else "")
            size = fmt(r["size"])
            if frames and frames.get(r["name"]):
                size += " (frames %s)" % fmt(frames[r["name"]])
            say(
                "%-30s %-24s %-22s %9s  %s"
                % (r["name"], owner_col, "%s %s" % r["group"], size, r["reason"])
            )
    say("")
    by = {}
    for r in rows:
        if not r["remove"]:
            key = re.sub(r" \(.*\)$", "", r["reason"])
            key = "newest per group" if key.startswith("newest") else key
            c, s = by.get(key, (0, 0))
            by[key] = (c + 1, s + r["size"])
    say("%s: %s" % (where, "%d runs" % len(rows)))
    for k, (c, s) in sorted(by.items()):
        say("  protected, %-36s %4d runs %9s" % (k + ":", c, fmt(s)))
    say("  removable:%39d runs %9s" % (len(rem), fmt(sum(r["size"] for r in rem))))
    if unmanaged:
        say(
            "  not managed (loose files, named dirs; never touched): %d entries %s"
            % (len(unmanaged), fmt(sum(s for _, s in unmanaged)))
        )
    say("Sizes are allocated bytes; clones are counted in full; freed space may be less.")
    say("")
    stamps, six = set(), set()
    for t in refs.files.values():
        stamps.update(STAMP_RE.findall(t))
        six.update(SIX_RE.findall(t))
    say(
        "sources: %d worktrees, %d files, %d stamps, %d HHMMSS tokens"
        % (refs.worktrees, len(refs.files), len(stamps), len(six))
    )
    for p in refs.skipped_big:
        say("  skipped (over 8 MiB): %s" % p)
    sole = {}
    for files in prot.values():
        if len(files) == 1:
            sole[files[0]] = sole.get(files[0], 0) + 1
    if sole:
        say("  files that alone protect runs (the most first):")
        for p, c in sorted(sole.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
            say("  %6d %s" % (c, p))


def cmd_gc(b, args):
    c = b.cfg
    game = c.game
    a = parse_args(args, game)
    say = b.say
    script_env = game.m["input"]["script_env"]
    wts = worktrees(c.game_dir)
    if game.m["golden"]["json"] and not os.path.isfile(
        os.path.join(wts[0], game.m["golden"]["json"])
    ):
        raise BenchError(
            "gc: no %s in the main checkout %s; refusing to guess the protected runs"
            % (game.m["golden"]["json"], wts[0])
        )
    refs = read_refs(game, wts[0], wts[1:], a.refs)
    if refs.missing:
        raise BenchError(
            "gc: reference sources missing, refusing (a partial reference text deletes "
            "evidence): %s" % ", ".join(refs.missing)
        )
    for w in ("OPEN_QUESTIONS.md",):
        if not os.path.isfile(os.path.join(wts[0], w)):
            print("gc: warning: no %s in %s" % (w, wts[0]), file=sys.stderr)
    store = os.path.join(c.game_dir, "bench-logs")
    if not os.path.isdir(store):
        raise BenchError("gc: no store at %s (a worktree needs the bench-logs link)" % store)
    store = os.path.realpath(store)
    exe = our_exe(game)
    now = time.time()
    if not a.host:
        say("gc: store %s (dry run unless --apply)" % store)
        runs, unmanaged = scan_local(store, script_env)
        prot = protectors(refs, [r["name"] for r in runs])
        rows = plan(runs, prot, exe, a.keep, a.days, now, a.include_unowned)
        report(say, rows, unmanaged, refs, prot, a, "local")
        rem = [r["name"] for r in rows if r["remove"]]
        if not a.apply:
            say("DRY RUN: nothing removed")
            return 0
        fails = remove_local(store, rem, say)
        say("gc: removed %d runs, %d failures" % (len(rem) - fails, fails))
        return 2 if fails else 0
    b.need_host()
    say("gc: host %s:%s/bench-logs, against the local store %s" % (c.host, c.remote_game, store))
    rc, out = b.r.remote(b.r.ship("gc_list.sh"), capture=True)
    if rc != 0:
        raise BenchError("gc: listing the host's store failed (exit %d)" % rc)
    runs, unmanaged, files, sha, frames = parse_host_listing(out, script_env)
    prot = protectors(refs, [r["name"] for r in runs])
    pulled = pulled_state(store, runs, files, sha)
    rows = plan(runs, prot, exe, a.keep, a.days, now, a.include_unowned, pulled)
    report(say, rows, unmanaged, refs, prot, a, "host", frames)
    rem = [r["name"] for r in rows if r["remove"]]
    if not a.apply:
        say("DRY RUN: nothing removed")
        return 0
    if not rem:
        say("gc: nothing to remove")
        return 0
    rc = b.r.remote(b.r.ship("gc_apply.sh", "NAMES=(%s)\n" % quote_words(rem)))[0]
    if rc == 75:
        raise BenchError("gc: the run lock was held for an hour; nothing removed")
    return 0 if rc == 0 else 2
