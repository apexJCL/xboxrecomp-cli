"""Pending work: whether a citation still pins.

D11's guard 3 pinned a run whenever any reference text cited it, so a
note of an item accepted weeks ago pinned its runs for ever. With
[pending] in benchdb.toml (or `plan --states FILE`) each citation is
judged by the work it belongs to:

  1. a game's golden json or golden audio file always pins;
  2. a file under openspec/changes/archive/ never does, and one under
     openspec/changes/<name>/ pins while that change is not archived;
  3. otherwise the work refs (BLX-n, and openspec/changes/<name>
     mentions) of the citation's block (its table row, list item or
     paragraph), of its markdown headings (not for a table row: an index
     table's heading names the index, not each row's owner; never a `#`
     line inside fenced code) and of the file's path (a blxNNN, or a
     notes folder named as an agent:<name> label), all together: a
     closed item in a bullet does not hide the open one heading it;
  4. the citation pins when any ref is open (not Done or Cancelled, not
     archived) or unknown, and stops pinning when all are closed;
  5. with no ref at all, the whole file's refs only say why it is kept:
     a guess from far away never releases a citation (fail safe).

A verdict from the whole file or from an unknown ref is counted as fail
safe: benchdb kept it without knowing what it belongs to.

The tracker's item states come from a command or a file (see load_states);
benchdb never writes to the tracker."""

import bisect
import datetime
import json
import os
import re
import subprocess
import time

from .store import DbError

CACHE = "tracker-states.json"
FETCH_TIMEOUT = 300
CLOSED_GROUPS = ("completed", "cancelled")
CLOSED_STATES = ("Done", "Cancelled")
# The states an agent is (about to be) working in: its topic dir is being
# written. Ready items are dispatched or about to be.
STARTED_STATES = ("Ready", "In Progress", "Blocked", "In Review")
# A states doc older than this is refused: an item reopened since would
# lose its runs.
STATES_MAX_AGE = 3600
SPEC_RE = re.compile(r"openspec/changes/(?!archive/)([A-Za-z0-9][A-Za-z0-9_.-]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
ARCHIVED_RE = r"\d{4}-\d{2}-\d{2}-%s"
LIST_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")


def path_item_re(prefix):
    """An item number in a path: the prefix, then the number with or
    without a hyphen (blxNNN, blx-NNN-inst). The prefix and a lone 1 are
    not item 1: that is BLiNX 1's slug, as in its public worktree."""
    return re.compile(r"(?<![a-z])%s(?!1(?!\d))-?(\d+)(?!\d)" % re.escape(prefix.lower()), re.I)


def ref_re(prefixes):
    alt = "|".join(re.escape(p) for p in prefixes)
    return re.compile(r"(?<![A-Za-z0-9])(%s)-(\d+)(?!\d)" % alt)


def load_states(cfg, states_file=None, now=None):
    """(items, source, warning): the tracker's {key: {state, group,
    labels}}, where they came from, and why they could not be read (items
    is then None). A states file given on the command line wins over
    [pending]; a command's output is cached in the store for
    cache_minutes, so one plan and its clean cost one fetch."""
    now = now if now is not None else time.time()
    p = cfg.pending
    if states_file is None and p.get("states_file"):
        states_file = p["states_file"]
    if states_file is not None:
        try:
            return _items(_read(states_file), now), states_file, None
        except (OSError, ValueError) as e:
            return None, states_file, "tracker states %s not read (%s)" % (states_file, e)
    cmd = p.get("states")
    if not cmd:
        return None, None, "no [pending] states source"
    cache = None
    st = cfg.store()
    if st is not None:
        cache = os.path.join(os.path.realpath(st.path), CACHE)
        try:
            if now - os.stat(cache).st_mtime < 60 * float(p.get("cache_minutes", 10)):
                return _items(_read(cache), now), cache, None
        except (OSError, ValueError):
            pass
    try:
        r = subprocess.run(
            cmd, cwd=cfg.dir, capture_output=True, timeout=FETCH_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, " ".join(cmd), "tracker states command failed (%s)" % e
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        return (
            None,
            " ".join(cmd),
            "tracker states command exited %d (%s)" % (r.returncode, err[-1] if err else ""),
        )
    try:
        doc = json.loads(r.stdout.decode())
        items = _items(doc, now)
    except ValueError as e:
        return None, " ".join(cmd), "tracker states command printed no states (%s)" % e
    if cache:
        tmp = cache + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1, sort_keys=True)
            os.replace(tmp, cache)
        except OSError:
            pass
    return items, cache or " ".join(cmd), None


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _items(doc, now):
    """The items of a states doc; ValueError for one that is empty or not
    fetched within STATES_MAX_AGE (a stale doc would release the runs of
    an item reopened since)."""
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, dict):
        raise ValueError("no items table")
    if not items:
        raise ValueError("no items")
    try:
        t = datetime.datetime.fromisoformat(str(doc.get("fetched"))).timestamp()
    except ValueError:
        raise ValueError("no fetched time") from None
    if now - t > STATES_MAX_AGE:
        raise ValueError(
            "fetched %s, over %d minutes ago" % (doc.get("fetched"), STATES_MAX_AGE // 60)
        )
    out = {}
    for k, v in items.items():
        if not isinstance(v, dict):
            raise ValueError("item %s is not a table" % k)
        out[k.upper()] = dict(
            state=v.get("state") or "",
            group=v.get("group") or "",
            labels=list(v.get("labels") or []),
        )
    return out


class Text:
    """One reference file's lines, line starts and headings."""

    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.starts = [0]
        for ln in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(ln) + 1)
        # Lines inside fenced code (and the fences): a `# ` there is a
        # shell comment, not a heading.
        self.fenced = set()
        inside = False
        for i, ln in enumerate(self.lines):
            if FENCE_RE.match(ln):
                self.fenced.add(i)
                inside = not inside
            elif inside:
                self.fenced.add(i)
        self.heads = [
            (i, len(m.group(1)))
            for i, ln in enumerate(self.lines)
            if i not in self.fenced and (m := HEADING_RE.match(ln))
        ]

    def heading(self, i):
        return i not in self.fenced and HEADING_RE.match(self.lines[i])

    def line(self, pos):
        return bisect.bisect_right(self.starts, pos) - 1

    def _starter(self, i):
        ln = self.lines[i]
        if not ln.strip():
            return False
        if self.heading(i) or LIST_RE.match(ln) or ln.startswith("|"):
            return True
        # An unindented line after a blank one starts a paragraph; an
        # indented one continues the list item above it.
        return not ln[0].isspace() and (i == 0 or not self.lines[i - 1].strip())

    def block(self, i):
        """(text, is a table row) of the block holding line i."""
        ln = self.lines[i]
        if ln.startswith("|") or self.heading(i):
            return ln, ln.startswith("|")
        a = i
        while a > 0 and not self._starter(a):
            a -= 1
        if self.heading(a) and a != i:
            a += 1
        b = i + 1
        while b < len(self.lines) and not self._starter(b):
            b += 1
        return "\n".join(self.lines[a:b]), False

    def headings(self, i):
        """The heading lines above line i, nearest first, each of a higher
        level than the one before it."""
        out = []
        k = bisect.bisect_right(self.heads, (i, 99)) - 1
        level = 99
        while k >= 0:
            j, lv = self.heads[k]
            if lv < level and j != i:
                out.append(self.lines[j])
                level = lv
                if lv == 1:
                    break
            k -= 1
        return out


class Pending:
    """The judge over one Refs, the tracker's items and the games'
    OpenSpec changes. items None: the tracker was not read, so every item
    is unknown and every citation that names one pins."""

    def __init__(self, cfg, refs, items, prefixes=("BLX",), base=None):
        self.cfg = cfg
        self.refs = refs
        self.items = items
        self.base = base or cfg.dir
        self.ref_re = ref_re(prefixes)
        self.prefix = prefixes[0] if prefixes else "BLX"
        self.path_re = path_item_re(self.prefix)
        self.labels = {}
        for k, v in (items or {}).items():
            for lb in v["labels"]:
                self.labels.setdefault(lb, []).append(k)
        self.golden = set(getattr(refs, "golden", ()))
        self.notes = [
            os.path.realpath(r.path) for r in cfg.roots.values() if r.local and r.kind == "notes"
        ]
        self._texts = {}
        self._specs = {}
        self._judged = {}

    # ---- the work refs' states ---------------------------------------------

    def item(self, key):
        """('open' | 'closed' | 'unknown', 'BLX-n State')."""
        if self.items is None:
            return "unknown", "%s (tracker not read)" % key
        it = self.items.get(key)
        if it is None:
            return "unknown", "%s (not in the tracker)" % key
        closed = it["group"] in CLOSED_GROUPS or it["state"] in CLOSED_STATES
        return ("closed" if closed else "open"), "%s %s" % (key, it["state"] or it["group"])

    def spec(self, name):
        """A change's state over every game's main checkout: open when one
        has it under openspec/changes/, closed when one archived it and
        none has it open, unknown when none has it (it lives on a branch:
        open work)."""
        if name not in self._specs:
            got = "unknown"
            for d in self.cfg.games.values():
                ch = os.path.join(d, "openspec", "changes")
                if os.path.isdir(os.path.join(ch, name)):
                    got = "open"
                    break
                arch = os.path.join(ch, "archive")
                pat = re.compile(ARCHIVED_RE % re.escape(name))
                if os.path.isdir(arch) and any(pat.fullmatch(n) for n in os.listdir(arch)):
                    got = "closed"
            self._specs[name] = got
        st = self._specs[name]
        return st, "openspec %s %s" % (
            name,
            {"open": "open", "closed": "archived", "unknown": "not on main"}[st],
        )

    def refs_in(self, text):
        out = []
        for m in self.ref_re.finditer(text):
            out.append(("item", "%s-%s" % (m.group(1).upper(), m.group(2))))
        for m in SPEC_RE.finditer(text):
            out.append(("spec", m.group(1).rstrip(".")))
        return list(dict.fromkeys(out))

    def weigh(self, refs):
        """(live, failsafe, why) over a non-empty set of work refs."""
        states = [self.item(k) if t == "item" else self.spec(k) for t, k in refs]
        opened = [d for s, d in states if s == "open"]
        if opened:
            return True, False, "pending: " + ", ".join(opened[:3])
        unknown = [d for s, d in states if s == "unknown"]
        if unknown:
            return True, True, "fail-safe: " + ", ".join(unknown[:3])
        return False, False, "closed: " + ", ".join(d for _s, d in states[:4])

    # ---- the judge -----------------------------------------------------------

    def text(self, path):
        t = self._texts.get(path)
        if t is None:
            t = self._texts[path] = Text(self.refs.files[path])
        return t

    def judged(self):
        """((path, pos), (live, failsafe, why, line)) of every citation
        judged so far, for the plan's counts."""
        return list(self._judged.items())

    def judge(self, path, pos):
        """(live, failsafe, why, line) of the citation at pos in path."""
        k = (path, pos)
        if k in self._judged:
            return self._judged[k]
        t = self.text(path)
        i = t.line(pos)
        got = self._judge(path, t, i) + (i + 1,)
        self._judged[k] = got
        return got

    def _judge(self, path, t, i):
        if any(path == g or path.startswith(g.rstrip(os.sep) + os.sep) for g in self.golden):
            return True, False, "golden reference"
        norm = path.replace(os.sep, "/")
        if "/openspec/changes/archive/" in norm:
            return False, False, "closed: an archived openspec change"
        m = re.search(r"/openspec/changes/([^/]+)/", norm)
        if m:
            live, fs, why = self.weigh([("spec", m.group(1))])
            return live, fs, why
        block, row = t.block(i)
        refs = self.refs_in(block)
        if not row:
            for h in t.headings(i):
                refs += self.refs_in(h)
        refs += self.path_refs(path)
        refs = list(dict.fromkeys(refs))
        if refs:
            return self.weigh(refs)
        refs = self.refs_in(t.text)
        if refs:
            # The whole file says nothing about this citation's own work:
            # it is kept without knowing why, and never released on a guess.
            _live, _fs, why = self.weigh(refs)
            if why.startswith("closed: "):
                why = "fail-safe: only " + why
            return True, True, why + " (anywhere in the file)"
        return True, True, "fail-safe: no item"

    def path_refs(self, path):
        """Work refs from a file's path: a blxNNN in it, or the notes
        folder holding it named as some item's agent:<name> label."""
        rel, folder = path, None
        for n in self.notes:
            if path.startswith(n.rstrip(os.sep) + os.sep):
                rel = os.path.relpath(path, n)
                if os.sep in rel:
                    folder = rel.split(os.sep)[0]
                break
        else:
            if path.startswith(self.base.rstrip(os.sep) + os.sep):
                rel = os.path.relpath(path, self.base)
        out = [("item", "%s-%s" % (self.prefix, m.group(1))) for m in self.path_re.finditer(rel)]
        if folder:
            out += [("item", k) for k in sorted(self.labels.get("agent:" + folder, []))]
        return list(dict.fromkeys(out))

    # ---- live topics -----------------------------------------------------------

    def live_topic(self, topic, worktrees):
        """Why a runs-root topic is being written now, or None: an agent's
        worktree of the same name, or a started item that carries its name
        as agent:<topic> or whose number the topic holds."""
        for w in worktrees:
            if os.path.isdir(os.path.join(w, topic)):
                return "live: wt/%s exists" % topic
        if self.items is None:
            return None
        for k in sorted(self.labels.get("agent:" + topic, [])):
            st = self.items[k]["state"]
            if st in STARTED_STATES:
                return "live: %s %s is agent:%s" % (k, st, topic)
        for m in self.path_re.finditer(topic):
            k = "%s-%s" % (self.prefix, m.group(1))
            it = self.items.get(k)
            if it and it["state"] in STARTED_STATES:
                return "live: %s %s" % (k, it["state"])
        return None


def require_states(items, warning):
    """A plan that was asked to judge by the tracker and could not read it
    still runs (every item is unknown), but says so."""
    if items is None and warning:
        return [warning + ": every citation that names an item keeps pinning"]
    return []


def check_guard(hours):
    if hours is None:
        return None
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise DbError("guard_hours %r is not a number of hours" % (hours,)) from None
    if not hours >= 1:
        raise DbError("guard_hours %g: at least 1 hour (a run being shipped would go)" % hours)
    return hours
