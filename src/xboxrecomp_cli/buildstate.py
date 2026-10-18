"""Per-stage records of a package run, so a rerun reuses what is current.

Every stage the package pipeline runs (import, generate, compile, icon,
hud, stage, wrap) records under its name the key it ran with, the input
digests that made the key and the outputs it left. The next run skips a
stage only when the new key equals the stored one and every output is
still there unchanged; otherwise the stage runs, and every stage below it
too (its key holds the upstream keys).

The records live in the game checkout's git directory
($(git rev-parse --git-dir)/xbr/), never in the tree: `git status` would
count a state folder there, and the version would turn -dirty on the
second run. A game folder outside git uses <game>/.xbr/.

A stage drops its own record before it starts (begin), so a crash in the
middle leaves no key and the next run redoes it.
"""

import hashlib
import json
import os
import subprocess

SCHEMA = 1
STATE_NAME = "build-state.json"
GAME_FILES_NAME = "game-files.json"


def state_dir(root):
    """$(git rev-parse --git-dir)/xbr for a checkout (a worktree gets its
    own git dir, so its own state), else <root>/.xbr."""
    try:
        gd = subprocess.run(
            ["git", "-C", root, "rev-parse", "--absolute-git-dir"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return os.path.join(root, ".xbr")
    gd = gd.strip()
    return os.path.join(gd, "xbr") if gd else os.path.join(root, ".xbr")


def write_json(path, obj):
    """Atomically: a crash leaves the old file or the new one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="\n") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def stage_key(inputs, upstream=None):
    """sha256 of the canonical JSON of the inputs and the upstream keys."""
    return digest({"inputs": inputs, "upstream": dict(upstream or {})})


def sha256_file(path, bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


def listing(root, skip=None):
    """[(rel, size, mtime_ns)] of every regular file under root, sorted:
    what a directory output and the import's game_files/ are judged by."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if skip is not None and skip(rel):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((rel, st.st_size, st.st_mtime_ns))
    return sorted(out)


def listing_digest(root, skip=None):
    return digest([list(x) for x in listing(root, skip)])


def moved_keys(new, old):
    if not isinstance(new, dict) or not isinstance(old, dict):
        return []
    return [k for k in sorted(set(new) | set(old)) if new.get(k) != old.get(k)]


def moved(inputs, old):
    return ["%s changed" % k for k in moved_keys(inputs, old)]


class State:
    """build-state.json of one game folder. An unreadable or foreign file
    reads as empty: every stage runs once, and the tools' own reuse
    (ninja, the gen key, the import's "same") keeps that cheap."""

    def __init__(self, root, sdir=None):
        self.root = os.path.abspath(root)
        self.dir = sdir or state_dir(self.root)
        self.path = os.path.join(self.dir, STATE_NAME)
        data = load_json(self.path, None)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            data = {"schema": SCHEMA, "stages": {}}
        if not isinstance(data.get("stages"), dict):
            data["stages"] = {}
        self.data = data
        self.verify = False

    # Paths under the game folder are kept as $ROOT/...: a moved folder
    # keeps its records.
    def _store_path(self, p):
        p = os.path.abspath(p)
        if p == self.root or p.startswith(self.root + os.sep):
            return "$ROOT/" + os.path.relpath(p, self.root).replace(os.sep, "/")
        return p

    def _real_path(self, s):
        if s.startswith("$ROOT/"):
            return os.path.join(self.root, *s[len("$ROOT/") :].split("/"))
        return s

    def save(self):
        write_json(self.path, self.data)

    def get(self, stage):
        rec = self.data["stages"].get(stage)
        return rec if isinstance(rec, dict) else None

    def key_of(self, stage):
        rec = self.get(stage)
        return rec.get("key", "") if rec else ""

    def begin(self, stage):
        """Drop the stage's record before it runs."""
        if self.data["stages"].pop(stage, None) is not None:
            self.save()

    def forget(self, *stages):
        for s in stages:
            self.data["stages"].pop(s, None)
        self.save()

    @staticmethod
    def _skipper(skip):
        """A directory output's top-level names left out of its digest (the
        manifest files the wrap writes into the staged payload after the
        stage recorded it)."""
        names = frozenset(skip or ())
        return (lambda rel: rel in names) if names else None

    def output_record(self, path, skip=()):
        if not os.path.lexists(path):
            # A stage that left nothing there: the next check finds it gone.
            return {"gone": True}
        if os.path.isdir(path):
            return {"tree": listing_digest(path, self._skipper(skip))}
        st = os.stat(path)
        return {"sha256": sha256_file(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def record(self, stage, key, inputs, outputs=(), meta=None, skip=(), upstream=None):
        """The stage finished: its key, inputs, outputs (paths, each a file
        or a directory), the upstream keys it chained on (kept to name the
        stage that moved) and anything the next run needs to skip it."""
        rec = {
            "key": key,
            "inputs": inputs,
            "outputs": {self._store_path(p): self.output_record(p, skip) for p in outputs},
        }
        if upstream:
            rec["upstream"] = dict(upstream)
        if skip:
            rec["skip"] = sorted(skip)
        if meta is not None:
            rec["meta"] = meta
        self.data["stages"][stage] = rec
        self.save()
        return rec

    def output_problems(self, rec):
        why = []
        skip = self._skipper(rec.get("skip"))
        for s, want in sorted((rec.get("outputs") or {}).items()):
            p = self._real_path(s)
            name = s.replace("$ROOT/", "")
            if not os.path.exists(p):
                why.append("%s is gone" % name)
            elif "tree" in want:
                if not os.path.isdir(p) or listing_digest(p, skip) != want["tree"]:
                    why.append("%s changed" % name)
            elif os.path.isdir(p):
                why.append("%s changed" % name)
            else:
                st = os.stat(p)
                same = st.st_size == want.get("size") and st.st_mtime_ns == want.get("mtime_ns")
                if not same or self.verify:
                    # Doubt (or --verify): the content decides; a touched
                    # file with the same bytes is still current.
                    if st.st_size != want.get("size") or sha256_file(p) != want.get("sha256"):
                        why.append("%s changed" % name)
                    else:
                        want["mtime_ns"] = st.st_mtime_ns
        return why

    def check(self, stage, key, inputs=None, upstream=None):
        """[] when the stage can be reused, else why it runs: each input
        that moved by name, else the upstream stage whose key moved, or the
        outputs that are gone or changed."""
        rec = self.get(stage)
        if rec is None:
            return ["not built yet"]
        if rec.get("key") != key:
            return moved(inputs, rec.get("inputs")) or [
                "%s changed" % k for k in moved_keys(upstream, rec.get("upstream"))
            ] or ["an earlier stage ran"]
        return self.output_problems(rec)


class HashCache:
    """game-files.json: each game file's sha256 against its size and
    mtime_ns, so a wrap does not rehash 3 GB that did not change. Keyed by
    the path in the bundle (game_files/<rel>)."""

    def __init__(self, sdir):
        self.path = os.path.join(sdir, GAME_FILES_NAME)
        data = load_json(self.path, None)
        ok = isinstance(data, dict) and data.get("schema") == SCHEMA
        self.files = data.get("files", {}) if ok and isinstance(data.get("files"), dict) else {}
        self.verify = False
        self.dirty = False

    def sha256(self, rel, path):
        st = os.stat(path)
        e = self.files.get(rel)
        if (
            not self.verify
            and isinstance(e, list)
            and len(e) == 3
            and e[0] == st.st_size
            and e[1] == st.st_mtime_ns
        ):
            return e[2]
        h = sha256_file(path)
        self.files[rel] = [st.st_size, st.st_mtime_ns, h]
        self.dirty = True
        return h

    def save(self):
        if self.dirty:
            write_json(self.path, {"schema": SCHEMA, "files": self.files})
            self.dirty = False
