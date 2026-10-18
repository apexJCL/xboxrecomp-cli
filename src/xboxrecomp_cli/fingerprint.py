"""The dump's XBE, judged by its sections: whether the generated code
still matches it. game.toml's [xbe] sha256 lists the whole files the
goldens were recorded on, and [xbe.sections] the sha256 of each section's
raw bytes. An XBE outside the list is classified by what differs:

  known    its sha256 is listed
  header   every section matches: only the header differs (another
           pressing of the same code)
  data     only data sections differ: modded data, the code is stock
  code     a code section differs, is added or is missing: the generated
           code no longer matches it, and the game may crash
  unknown  no [xbe.sections] to compare with, or no section table

Code is what the pipeline translates ([pipeline.disasm]): the executable
flag alone would not do, as a title may mark data sections executable.
Nothing here refuses: the CLI warns and builds, and only bench golden
keeps its whole-file rule (its frames were recorded on those files)."""

import hashlib
import json
import os
from collections import namedtuple

from . import host, xbe
from .host import CliError

KNOWN, HEADER, DATA, CODE, UNKNOWN = "known", "header", "data", "code", "unknown"
# The section names a message lists before "and N more".
LIST_MAX = 8

# sha: the whole file's; sections: the differing names, in table order
# (the reference's missing ones after); why: "truncated" for an unknown
# XBE whose sections (then named) run past its end, else "".
Verdict = namedtuple("Verdict", "sha verdict sections why", defaults=("",))
TRUNCATED = "truncated"


def section_hashes(data):
    """[(Section, sha256 of its raw bytes)] in table order."""
    out = []
    for s in xbe.sections(data):
        raw = data[s.raw_off : s.raw_off + s.raw_size]
        out.append((s, hashlib.sha256(raw).hexdigest()))
    return out


def code_names(m):
    """The names the pipeline disassembles, or None: every executable
    section (pipeline.disasm.text_only off)."""
    d = m["pipeline"]["disasm"]
    if not d["text_only"]:
        return None
    return {".text", *d["extra_sections"]}


def _is_code(name, flags, code, new=False):
    # A section the reference lacks is code when executable, even with
    # text_only (D2): an added code section is not in the code set.
    executable = flags is not None and bool(flags & xbe.SECTION_EXECUTABLE)
    if code is None:
        return flags is None or executable
    return name in code or (new and executable)


def classify(data, m, sha=None):
    """The Verdict for data (a whole XBE) against manifest m."""
    sha = sha or hashlib.sha256(data).hexdigest()
    ref = m["xbe"]["sections"]
    # Neither list: the check is off, every dump is known.
    if sha in m["xbe"]["sha256"] or not (ref or m["xbe"]["sha256"]):
        return Verdict(sha, KNOWN, [])
    if not ref:
        return Verdict(sha, UNKNOWN, [])
    try:
        got = section_hashes(data)
    except xbe.XbeError:
        return Verdict(sha, UNKNOWN, [])
    # A cut-short copy is not a mod: its sections would all "differ".
    short = [s.name for s, _ in got if s.raw_off + s.raw_size > len(data)]
    if short:
        return Verdict(sha, UNKNOWN, short, TRUNCATED)
    code = code_names(m)
    seen, code_diff, data_diff = set(), [], []
    for s, h in got:
        seen.add(s.name)
        if ref.get(s.name) != h:
            is_code = _is_code(s.name, s.flags, code, new=s.name not in ref)
            (code_diff if is_code else data_diff).append(s.name)
    for name in ref:
        if name not in seen:
            # Its flags went with it: the code set decides, and without
            # one a missing section is taken as code.
            (code_diff if _is_code(name, None, code) else data_diff).append(name)
    if code_diff:
        return Verdict(sha, CODE, code_diff)
    if data_diff:
        return Verdict(sha, DATA, data_diff)
    return Verdict(sha, HEADER, [])


_CACHE = {}


def read_xbe(path):
    """The whole XBE at path: an XBE file, or the one in a folder or disc
    image (read in place, as import's title check does)."""
    if os.path.isfile(path):
        with open(path, "rb") as f:
            if f.read(4) == b"XBEH":
                f.seek(0)
                return f.read()
    from . import dump

    try:
        with dump.Source(path) as src:
            # Exactly its size: a folder's read(n) would allocate n first.
            return src.xbe_read(0, src.xbe_size())
    except CliError:
        if not os.path.isfile(path):
            raise
    # A file that is neither: its bytes, which classify calls unknown.
    with open(path, "rb") as f:
        return f.read()


def of_path(path, m=None):
    """The Verdict of the XBE at path (read_xbe's kinds), or None when
    there is nothing there. One hash per path and manifest: import, then
    package, read it once."""
    m = m or host.g().m
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns, id(m))
    if key not in _CACHE:
        _CACHE[key] = classify(read_xbe(path), m)
    return _CACHE[key]


def current():
    """The Verdict of this game's game_files XBE, or None."""
    return of_path(host.g().xbe)


def names_text(names):
    shown = ", ".join(names[:LIST_MAX])
    more = len(names) - LIST_MAX
    return shown + (" and %d more" % more if more > 0 else "")


def note(v):
    """The warning for v, '' when there is nothing to warn about."""
    if v is None or v.verdict in (KNOWN, HEADER):
        return ""
    if v.verdict == CODE:
        return "modded code, unsupported, may crash: XBE sha256 %s; code sections differ: %s" % (
            v.sha[:12],
            names_text(v.sections),
        )
    if v.verdict == DATA:
        return "modded data, code stock: XBE sha256 %s; data sections differ: %s" % (
            v.sha[:12],
            names_text(v.sections),
        )
    if v.why == TRUNCATED:
        return "truncated dump: XBE sha256 %s; sections run past the file's end: %s" % (
            v.sha[:12],
            names_text(v.sections),
        )
    known = host.g().m["xbe"]["sha256"]
    return "unknown dump: sha256 %s is not one game.toml lists (%s)" % (
        v.sha,
        ", ".join(k[:12] for k in known),
    )


def doctor_text(v):
    """doctor's words for v, after the XBE's path."""
    if v.verdict == KNOWN:
        return ""
    if v.verdict == HEADER:
        return " (XBE sha256 %s: header differs from the known dumps, sections match)" % v.sha[:12]
    return " (%s)" % note(v)


def warn(v, what=""):
    """Print v's warning, if any: the run goes on."""
    n = note(v)
    if n:
        host.say("WARNING: %s%s" % (n, what))


def record(v):
    """The manifest's xbe field."""
    return {"sha256": v.sha, "verdict": v.verdict, "sections": list(v.sections)}


def plan_record(v):
    """The plan's xbe field: the manifest's, with the warning."""
    if v is None:
        return None
    return dict(record(v), note=note(v))


def stamp(v):
    """The launch.env value the game prints at boot: sha, verdict, and
    the differing sections, comma-joined (one token, so the line stays
    KEY=value for every launcher's reader)."""
    out = "%s %s" % (v.sha, v.verdict)
    return out + (" " + ",".join(v.sections) if v.sections else "")


def stamp_line(m=None):
    """The launch.env line ([package] stamp_env=stamp) for this game's
    dump, '' when stamp_env is empty or there is no XBE."""
    m = m or host.g().m
    key = m["package"].get("stamp_env", "")
    v = current()
    if not key or v is None:
        return ""
    return "%s=%s" % (key, stamp(v))


def toml_table(data):
    """game.toml's [xbe.sections] for data."""
    lines = ["[xbe.sections]"]
    for s, h in section_hashes(data):
        lines.append('%s = "%s"' % (json.dumps(s.name), h))
    return "\n".join(lines) + "\n"
