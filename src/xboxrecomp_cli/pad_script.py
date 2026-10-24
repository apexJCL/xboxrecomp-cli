"""The toolkit's scripted pad (RECOMP_DEBUG=pad_script=), read and written.

The runtime's grammar (xboxrecomp src/usb/pad_script.c), mirrored so a
scenario's script is checked before a run spends minutes on a typo, and a
pad_record file becomes a scenario's pad_script:

  <ms>:<button>[+<button>...][:<hold ms>]         wall time from the first pad
                                                   report, default hold 150 ms
  f<flip>:<button>[+<button>...][:<hold>[f]]      the executor's flip count
                                                   ("[GPU] flip N"), the hold
                                                   in flips, default 9

Numbers are decimal (the runtime reads them base 10; a leading zero is
refused here, where it reads as a typo). A p2- .. p4- prefix on the
buttons targets that pad. A stick axis may be set to a value, lx=<v> (ly,
rx, ry; -32768..32767, up positive). Entries are comma, space or newline
separated; "#" comments run to the end of the line.
RECOMP_DEBUG=pad_record=FILE writes f<flip> steps, one per line, after a
"#" header: the buttons, and the sticks as lx=<v> steps.

Stdlib only."""

import os
import re

STICKS = ("lleft", "lright", "lup", "ldown", "rleft", "rright", "rup", "rdown")
BUTTONS = (
    "up",
    "down",
    "left",
    "right",
    "start",
    "back",
    "lthumb",
    "rthumb",
    "a",
    "b",
    "x",
    "y",
    "black",
    "white",
    "lt",
    "rt",
)
NAMES = frozenset(BUTTONS + STICKS)
AXES = ("lx", "ly", "rx", "ry")
_AXIS = re.compile(r"^(lx|ly|rx|ry)=(-?\d+)$")
HOLD_MS = 150
HOLD_FLIPS = 9
MAX_STEPS = 32768  # PAD_SCRIPT_MAX

_ENTRY = re.compile(
    r"^(?P<f>[fF])?(?P<at>\d+):(?:[pP](?P<pad>[1-4])-)?"
    r"(?P<buttons>[^:]+)(?::(?P<hold>\d+)(?P<hf>[fF])?)?$"
)


class PadScriptError(ValueError):
    pass


class Step:
    __slots__ = ("by_flip", "at", "hold", "pad", "buttons")

    def __init__(self, by_flip, at, hold, pad=0, buttons=()):
        self.by_flip = by_flip
        self.at = at
        self.hold = hold
        self.pad = pad
        self.buttons = tuple(buttons)

    def __eq__(self, other):
        return isinstance(other, Step) and all(
            getattr(self, k) == getattr(other, k) for k in self.__slots__
        )

    def __repr__(self):
        return "Step(%s)" % format_step(self)


def entries(text):
    """The script's entries, comments and separators dropped."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        out += [e for e in re.split(r"[,\s]+", line) if e]
    return out


def _number(e, v):
    if len(v) > 1 and v[0] == "0":
        raise PadScriptError("pad_script: %r: %s has a leading zero" % (e, v))
    return int(v, 10)


def _axis_ok(name):
    m = _AXIS.match(name)
    if not m:
        return False
    v = m.group(2).lstrip("-")
    return (len(v) == 1 or v[0] != "0") and -32768 <= int(m.group(2)) <= 32767


def parse(text):
    """The steps of a script; PadScriptError names the first bad entry.
    Stricter than the runtime, which warns and goes on: an unknown button
    there is a press that never happens."""
    steps = []
    for e in entries(text):
        m = _ENTRY.match(e)
        if not m:
            raise PadScriptError(
                "pad_script: bad entry %r (want <ms>:btn[+btn][:hold] or "
                "f<flip>:btn[+btn][:hold])" % e
            )
        by_flip = m.group("f") is not None
        if m.group("hf") and not by_flip:
            raise PadScriptError("pad_script: %r: a hold in flips on a step timed in ms" % e)
        names = m.group("buttons").lower().split("+")
        bad = [n for n in names if n not in NAMES and not _axis_ok(n)]
        if bad:
            raise PadScriptError(
                "pad_script: %r: unknown button %s (known: %s, and lx=<v> ly rx ry"
                " in -32768..32767)" % (e, ", ".join(bad), " ".join(BUTTONS + STICKS))
            )
        hold = m.group("hold")
        steps.append(
            Step(
                by_flip,
                _number(e, m.group("at")),
                _number(e, hold) if hold else HOLD_FLIPS if by_flip else HOLD_MS,
                int(m.group("pad") or 1) - 1,
                names,
            )
        )
    if len(steps) > MAX_STEPS:
        raise PadScriptError("pad_script: %d steps, the runtime keeps %d" % (len(steps), MAX_STEPS))
    return steps


def format_step(st):
    return "%s%d:%s%s:%d" % (
        "f" if st.by_flip else "",
        st.at,
        "p%d-" % (st.pad + 1) if st.pad else "",
        "+".join(st.buttons),
        st.hold,
    )


def to_script(steps):
    """The comma-joined form a scenario's RECOMP_DEBUG pad_script= takes."""
    return ",".join(format_step(s) for s in steps)


def from_record(text, start=None, end=None, shift=0):
    """A pad_record file's steps, cut to those starting in [start, end) and
    moved by shift flips. Only flip-timed steps: a millisecond step in a
    recording is not one the recorder wrote."""
    out = []
    for st in parse(text):
        if not st.by_flip:
            raise PadScriptError("pad_record: %s is timed in ms" % format_step(st))
        if start is not None and st.at < start:
            continue
        if end is not None and st.at >= end:
            continue
        at = st.at + shift
        if at < 0:
            raise PadScriptError("pad_record: --shift moves %s before flip 0" % format_step(st))
        out.append(Step(True, at, st.hold, st.pad, st.buttons))
    # The recorder writes a step when it ends, so a short press written
    # inside a long stick hold comes before it; by start flip (stable, so
    # same-flip steps keep the file's order), as a reader of the range and
    # a concatenation with another script expect.
    out.sort(key=lambda st: st.at)
    return out


_DEBUG_PAD = re.compile(r"(?:^|,)pad_script=(.*)")


_KEY = re.compile(r"[a-z_][a-z_0-9]*")
_FLIP = re.compile(r"f\d+")


def script_of(debug):
    """The pad_script= value in a RECOMP_DEBUG list, or None. pad_script is a
    comma key: the runtime glues each later fragment onto it until one whose
    key (the text before any '=', trimmed) is another key, whatever its
    value holds (pad_record=Z:/x.pad ends it too). The runtime knows its key
    table; here a key is any lower-case name, which no step is: a step's
    key part starts with a digit or holds a ':' (f10:a, f10:lx=0.5)."""
    m = _DEBUG_PAD.search(debug or "")
    if not m:
        return None
    parts = []
    for frag in m.group(1).split(","):
        key = frag.split("=", 1)[0].strip()
        if _KEY.fullmatch(key) and not _FLIP.fullmatch(key):
            break
        parts.append(frag)
    return ",".join(parts)


def host_path(path):
    """Whether path is an absolute or ~ one (POSIX or Windows), not one
    under the game root."""
    return (
        os.path.isabs(path)
        or path.startswith(("~", "\\"))
        or bool(re.match(r"[A-Za-z]:[\\/]", path))
    )


def scenario_errors(env, root=None):
    """Errors in the pad_script of a scenario's env (RECOMP_DEBUG or
    RECOMP_PAD_SCRIPT). An @file script relative to root is read and
    checked when it is there (a missing one is golden's seeds_missing to
    name); an absolute or ~ one is refused; a seed: one is the HDD seed's."""
    errs = []
    for v in (script_of(env.get("RECOMP_DEBUG")), env.get("RECOMP_PAD_SCRIPT")):
        if v is None:
            continue
        if v.startswith("@"):
            path = v[1:].split(",", 1)[0]
            if host_path(path):
                # Not shipped to a bench host and not this game's: the
                # runtime there only warns it cannot open it and runs with
                # no route, and the path itself would sit in golden.json.
                errs.append(
                    "pad script @%s: give a path under the game root, not an"
                    " absolute or ~ one" % path
                )
                continue
            if root is None or not path or path.startswith("seed:"):
                continue
            try:
                with open(os.path.join(root, path)) as f:
                    v = f.read()
            except OSError:
                continue
            try:
                parse(v)
            except PadScriptError as e:
                errs.append("%s: %s" % (path, e))
            continue
        try:
            parse(v)
        except PadScriptError as e:
            errs.append(str(e))
    return errs
