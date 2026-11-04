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

Apart from the verdict, the certificate tells the release: its region
flags and version against [xbe] region and version, the reference dump's.
A dump that differs there is another release (another region or
revision), whose code sits at other addresses than the ones the
hand-written parts (recomp_manual.c, hooks, seeds) were written for: the
verdict stays what the sections say, and the warning says "another
release", naming both.

Code is what the pipeline translates ([pipeline.disasm]): the executable
flag alone would not do, as a title may mark data sections executable.
Nothing here refuses: the CLI warns and builds, and only bench golden
keeps its whole-file rule (its frames were recorded on those files)."""

import hashlib
import json
import os
import re
from collections import namedtuple

from . import host, xbe
from .host import CliError

KNOWN, HEADER, DATA, CODE, UNKNOWN = "known", "header", "data", "code", "unknown"
# The section names a message lists before "and N more".
LIST_MAX = 8

# sha: the whole file's; sections: the differing names, in table order
# (the reference's missing ones after); why: "truncated" for an unknown
# XBE whose sections (then named) run past its end, else "". region and
# version: the dump's certificate fields (None when unreadable); ref: the
# reference release in words ("" when game.toml records neither); other:
# the dump is another release than the reference (never for a truncated
# one); title_id: the dump's certificate title ID when it is not [xbe]
# title_id, else None.
Verdict = namedtuple(
    "Verdict",
    "sha verdict sections why region version ref other title_id",
    defaults=("", None, None, "", False, None),
)
TRUNCATED = "truncated"


def cert_of(data):
    """xbe.cert_release(data) plus the title ID, all None when data is
    not an XBE."""
    try:
        out = dict(xbe.cert_release(data), title_id=xbe.title_id(data))
    except xbe.XbeError:
        return {"alt_title_ids": None, "region": None, "version": None, "title_id": None}
    return out


def reference(m):
    """The reference release's (region, version), each None when not
    recorded (a test manifest may lack the keys)."""
    x = m["xbe"]
    return x.get("region"), x.get("version")


def is_other_release(region, version, m):
    """Whether a dump's region or version differs from the recorded
    reference's. A field either side lacks says nothing."""
    rr, rv = reference(m)
    return (rr is not None and region is not None and region != rr) or (
        rv is not None and version is not None and version != rv
    )


def words_of(title_id, region, version):
    """A release in words: 'PAL, version 2', led by 'title ID 0x...' when
    the title IDs differ, so two releases never read the same."""
    parts = ["title ID 0x%08X" % title_id] if title_id is not None else []
    rel = xbe.release_text(region, version)
    return ", ".join(parts + ([rel] if rel else []))


def release_words(v):
    """v's release ("PAL, version 2"), or "an unreadable certificate"."""
    return words_of(v.title_id, v.region, v.version) or "an unreadable certificate"


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
    c = cert_of(data)
    # Another title ID is another release too: import lets one in when it
    # names this game (title_match), and its addresses are its own.
    ref_tid = m["xbe"].get("title_id")
    tid = c["title_id"] if c["title_id"] not in (None, ref_tid) and ref_tid else None
    ref_words = words_of(ref_tid if tid is not None else None, *reference(m))
    rel = (c["region"], c["version"], ref_words)
    other = tid is not None or is_other_release(c["region"], c["version"], m)

    def verdict(kind, sections, why=""):
        # A cut-short file's certificate may be garbage: it claims no release.
        mine = other and why != TRUNCATED
        return Verdict(sha, kind, sections, why, *rel, mine, tid if mine else None)

    # A listed dump is the reference, whatever its fields say.
    if sha in m["xbe"]["sha256"]:
        return Verdict(sha, KNOWN, [], "", *rel, False)
    # Neither list: the check is off, every dump of the reference release
    # is known (and every dump, when the release is not recorded either).
    if not (ref or m["xbe"]["sha256"]):
        return verdict(UNKNOWN if other else KNOWN, [])
    if not ref:
        return verdict(UNKNOWN, [])
    try:
        got = section_hashes(data)
    except xbe.XbeError:
        return verdict(UNKNOWN, [])
    # A cut-short copy is not a mod: its sections would all "differ".
    short = [s.name for s, _ in got if s.raw_off + s.raw_size > len(data)]
    if short:
        return verdict(UNKNOWN, short, TRUNCATED)
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
        return verdict(CODE, code_diff)
    if data_diff:
        return verdict(DATA, data_diff)
    return verdict(HEADER, [])


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
    if v.other and v.why != TRUNCATED:
        # Said first: "modded code" would send the player looking for a
        # mod they never made.
        head = (
            "another release: %s, where this port was made from %s; "
            "the build may fail or the game misbehave: XBE sha256 %s"
            % (release_words(v), v.ref, v.sha[:12])
        )
        if v.verdict == CODE:
            return head + "; code sections differ: " + names_text(v.sections)
        if v.verdict == DATA:
            return head + "; only data sections differ: " + names_text(v.sections)
        return head
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
        also = "; another release, %s" % release_words(v) if v.other else ""
        return " (XBE sha256 %s: header differs from the known dumps, sections match%s)" % (
            v.sha[:12],
            also,
        )
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
    """The plan's xbe field: the manifest's, with the warning and the
    release: the dump's (words and certificate fields), the reference's
    words ("" when not recorded) and whether they are two releases; why
    is the verdict's ("truncated" or "")."""
    if v is None:
        return None
    return dict(
        record(v),
        note=note(v),
        why=v.why,
        release=words_of(v.title_id, v.region, v.version),
        region=v.region,
        version=v.version,
        reference_release=v.ref,
        other_release=v.other,
    )


def stamp(v):
    """The launch.env value the game prints at boot: sha, verdict, and
    the differing sections, comma-joined (one token, so the line stays
    KEY=value for every launcher's reader)."""
    out = "%s %s" % (v.sha, v.verdict)
    out += " " + ",".join(v.sections) if v.sections else ""
    if v.other:
        # Last, and keyed, so the sections field keeps its place.
        rel = xbe.release_text(v.region, v.version).replace(", version ", ",v")
        if v.title_id is not None:
            rel += ("," if rel else "") + "id=0x%08X" % v.title_id
        out += " release=" + rel.replace(" ", "-")
    return out


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
    """game.toml's reference keys for data: the [xbe] ones (the sha256 to
    add to the list, the certificate's release fields), then the
    [xbe.sections] table."""
    lines = []
    h, c = xbe.read_header(data), xbe.cert_release(data)
    lines.append("# Under [xbe]. Add this to the sha256 list:")
    lines.append('#   "%s"' % hashlib.sha256(data).hexdigest())
    lines.append("# The release keys need a CLI newer than v0.20.0, which refuses them,")
    lines.append("# and [xbe.sections] beside them (see docs/manifest.md).")
    if c["region"] is not None:
        lines.append("region = 0x%X  # %s" % (c["region"], xbe.region_text(c["region"])))
        lines.append("version = %d" % c["version"])
    if h["title"]:
        lines.append("title = %s" % json.dumps(h["title"]))
    if c["alt_title_ids"]:
        # Commented out: a sequel may list its prequel's ID, which would
        # let the prequel's dump import as this game.
        lines.append("# Keep only IDs of this same game (other regions), never another game's:")
        lines.append("# alt_title_ids = [%s]" % ", ".join("0x%08X" % t for t in c["alt_title_ids"]))
    lines += ["", "[xbe.sections]"]
    for s, h in section_hashes(data):
        lines.append('%s = "%s"' % (json.dumps(s.name), h))
    return "\n".join(lines) + "\n"


def _norm_title(name):
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def title_match(h, c, m):
    """Why a dump whose title ID is not [xbe] title_id is still this game
    (h: xbe.read_header, c: xbe.cert_release), '' when it is another
    game: one side's alternate title IDs name the other's, or the
    certificate names it ([xbe] title, else the game's name; case, spaces
    and punctuation aside). The ID rules need one publisher on both sides."""
    x = m["xbe"]
    # The alternate-ID rules hold only within one publisher (the ID's high
    # half): a sequel that lists its prequel's ID is still another game.
    same_pub = (h["title_id"] >> 16) == (x["title_id"] >> 16)
    if same_pub and x["title_id"] in (c["alt_title_ids"] or ()):
        return "its alternate title IDs name 0x%08X" % x["title_id"]
    if same_pub and h["title_id"] in x.get("alt_title_ids", ()):
        return "game.toml's alternate title IDs name it"
    want = _norm_title(x.get("title") or m["game"]["name"])
    if want and _norm_title(h["title"]) == want:
        return 'its title is "%s"' % h["title"]
    return ""


def other_title_text(h, c, m, name):
    """The warning for a dump title_match accepts: what it is, against
    the reference."""
    why = title_match(h, c, m)
    ref = xbe.release_text(*reference(m))
    mine = xbe.release_text(c["region"], c["version"]) or "an unreadable certificate"
    return (
        "title ID 0x%08X, not %s's 0x%08X, but %s: another release (%s%s). "
        "The build may fail or the game misbehave"
        % (
            h["title_id"],
            name,
            m["xbe"]["title_id"],
            why,
            mine,
            "; this port was made from " + ref if ref else "",
        )
    )
