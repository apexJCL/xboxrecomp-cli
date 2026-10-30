"""hud-map emit: a proposal into the game's table, checked (design A15,
step 4).

The checks are the layer parser's (table.validate) and the row test's,
against the analysis:
- each ret_va follows a direct call to a hooked function or a listed
  wrapper;
- no ret_va lies inside a wrapper's body (that names the wrapper's own
  call, which every caller shares);
- every wrapper and every [capture] draw is in [mods] hook_functions, and
  every draw row is one the capture can use (table.check_draws);
- the text is one the toolkit's reader takes (table.c_readable).

"Direct call" means a call instruction whose target is the hooked
function itself: a return address reached through a thunk or a tail jump
into it is refused, since the hook sees the thunk's return, not the
site's.

Without an analysis (no disasm/) the row test cannot run: emit refuses to
write unless told --no-analysis; --check only warns.
"""

import os
import sys
import tomllib

from .. import hooks
from ..host import CliError
from . import table as T


class Hooked:
    """[mods] hook_functions, by entry and by VA. A sig: entry has a VA
    once analyze has resolved it (config/hook_functions.json)."""

    def __init__(self, entries, slots=None):
        self.entries = list(entries)
        self.vas = {}
        for e in self.entries:
            if not e.startswith("sig:"):
                try:
                    self.vas[int(e, 16)] = e
                except ValueError:
                    pass
        for s in slots or []:
            try:
                self.vas[int(s["va"], 16)] = s.get("entry") or s["va"]
            except (KeyError, TypeError, ValueError):
                continue

    @classmethod
    def of(cls, G):
        entries = hooks.entries(G.m)
        slots = None
        if any(e.startswith("sig:") for e in entries):
            try:
                slots = hooks.load_json(G)
            except CliError as e:
                print("hud-map: %s; sig: entries stay unresolved" % e, file=sys.stderr)
        return cls(entries, slots)

    def has(self, entry):
        """entry: "sig:NAME", or an address as the toolkit reads a draw's
        (strtoul base 0: "69632" is decimal, "0x11000" hex)."""
        if isinstance(entry, str) and entry.startswith("sig:"):
            return entry in self.entries
        v = T.c_ulong(entry) if isinstance(entry, str) else entry
        return T._int(v) and v in self.vas


def check_hooks(doc, hooked):
    errs = []
    for w in T._rows(doc, "wrapper"):
        va = w.get("va")
        if T._u32(va) and va and va not in hooked.vas:
            errs.append("wrapper 0x%08X: not in [mods] hook_functions (no hook slot)" % va)
    for entry, _ in T.draws(doc):
        if not T._draw_va_ok(entry):
            continue  # check_draws reports it
        if not hooked.has(entry):
            errs.append("capture.draws %s: not in [mods] hook_functions" % entry)
    return errs


def check_sites(doc, hooked, analysis):
    """The row test: errors for sites that do not follow a call to a hooked
    function or a wrapper, or that lie inside a wrapper."""
    wrappers = [w["va"] for w in T._rows(doc, "wrapper") if T._u32(w.get("va")) and w["va"]]
    rets = analysis.returns_to(set(hooked.vas) | set(wrappers))
    bodies = [(va, analysis.body(va)) for va in wrappers]
    errs = []
    for s in T._rows(doc, "site"):
        ret = s.get("ret_va")
        if not T._u32(ret) or not ret:
            continue
        if ret not in rets:
            errs.append(
                "site 0x%08X: does not follow a call to a hooked function or a wrapper" % ret
            )
        for va, body in bodies:
            if body and body[0] <= ret < body[1]:
                errs.append(
                    "site 0x%08X: inside wrapper 0x%08X's body (name its callers instead)"
                    % (ret, va)
                )
    return errs


def check(doc, hooked, analysis):
    """Every error in a parsed table."""
    errs = T.validate(doc)
    errs += T.check_draws(doc)
    errs += check_hooks(doc, hooked)
    if analysis is not None:
        errs += check_sites(doc, hooked, analysis)
    return errs


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def parse(text, where):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CliError("%s: %s" % (where, e)) from e


def changes(proposal):
    """[(kind, row)] from a proposal: its canvas, layout and capture
    tables, then its wrappers, groups and sites."""
    out = []
    for k in ("canvas", "layout", "capture"):
        if isinstance(proposal.get(k), dict):
            out.append((k, proposal[k]))
    for k in ("wrapper", "group", "site"):
        out += [(k, r) for r in T._rows(proposal, k)]
    return out


def still_proposed(existing, proposal):
    """Rows marked proposed that would add to or change the table."""
    out = []
    current = {}
    for k in T.ROW_ID:
        for r in T._rows(existing, k):
            current[(k, T._row_id(k, r))] = r
    for kind, row in changes(proposal):
        if not row.get("proposed"):
            continue
        rid = T._row_id(kind, row)
        if current.get((kind, rid)) != T.strip_proposal(row):
            out.append("%s %s" % (kind, "0x%08X" % rid if isinstance(rid, int) else rid))
    return out


NO_ANALYSIS = (
    "no analysis (disasm/): the row test cannot run; run analyze, or pass "
    "--no-analysis to write without it"
)


def emit(table_path, proposal_path, hooked, analysis, accept=False, dry=False, no_analysis=False):
    """(rc, messages, report). rc 0: written (or would be, dry), 1: refused."""
    text = read_text(table_path)
    existing = parse(text, table_path) if text.strip() else {}
    if proposal_path is None:
        if not text.strip():
            return 1, ["no table at %s" % table_path], []
        merged, report = text, []
    else:
        try:
            with open(proposal_path, "rb") as f:
                proposal = tomllib.load(f)
        except OSError as e:
            raise CliError("no proposal at %s (%s)" % (proposal_path, e.strerror)) from e
        except tomllib.TOMLDecodeError as e:
            raise CliError("%s: %s" % (proposal_path, e)) from e
        bad = T.check_keys(proposal, proposal=True)
        if bad:
            return 1, ["%s: %s" % (proposal_path, e) for e in bad], []
        held = still_proposed(existing, proposal)
        if held and not accept:
            return (
                1,
                ["%d rows still proposed (clear proposed or pass --accept-proposed):" % len(held)]
                + ["  " + h for h in held],
                [],
            )
        merged, report = T.merge(text, changes(proposal))
    doc = parse(merged, table_path)
    errs = T.c_readable(merged) + check(doc, hooked, analysis)
    if errs:
        return 1, errs, report
    write = proposal_path is not None and not dry and report
    if write and analysis is None and not no_analysis:
        return 1, [NO_ANALYSIS], report
    if write:
        os.makedirs(os.path.dirname(os.path.abspath(table_path)), exist_ok=True)
        with open(table_path, "w", encoding="utf-8") as f:
            f.write(merged)
    return 0, [], report
