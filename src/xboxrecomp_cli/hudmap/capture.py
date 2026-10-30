"""Reading a capture: capture.jsonl and its frames (design A15).

The runtime side is the toolkit's mapping capture (src/enhance/hud_capture.c,
docs/runtime/config.md "The mapping capture"). Its format is read here and
nowhere else. One JSON object per line, each VA a "0x%08X" string:

  sprite  {"type": "sprite", "level": 0|1, "frame": F, "queue": Q|null,
           "chain": [VA, ...], "ret": VA, "site": VA|null,
           "element": S|null, "group": S|null, "n": N,
           "bbox": [x0, y0, x1, y1]|null, "prims": P, "tex": S|null}
          level 0 adds "draw": VA and "name": S (the [capture] draws row)
  frame   {"type": "frame", "frame": F, "flip": N|null,
           "canvas": {w, h, origin, y, view, target, pad}|null,
           "bmp": "frame_NNNNN.bmp"|null}

- A frame's sprites come before its frame line, so a frame line means the
  frame is complete. Guest frames count from 1.
- chain is return addresses, outermost first (level 1: the wrappers';
  level 0: the stack scan's); ret is the hooked call's own.
- bbox is in canvas units, null when no vertex was read (a level-0 row
  without vtx and count). group is null for a site with its own kind.
- canvas is null when the capture ran without a table: a capture needs at
  least one, since boxes are in canvas units, so it is refused rather than
  read as 640x480. bmp is null when the picture was not taken; the frame
  then has no picture.
"""

import json
import os
import sys

from ..host import CliError

JSONL = "capture.jsonl"


def va(v):
    """An address from an integer or a "0x..." string; None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return None
    return None


class Canvas:
    """A frame's canvas snapshot and the map from its units to guest
    pixels of its target (A11: s = view_w / w)."""

    def __init__(self, d):
        d = d or {}
        tgt = d.get("target")
        view = d.get("view")
        self.w = float(d.get("w") or 0) or None
        self.h = float(d.get("h") or 0) or None
        if isinstance(tgt, list) and len(tgt) == 2:
            self.tw, self.th = float(tgt[0]), float(tgt[1])
        elif isinstance(view, list) and len(view) == 4:
            self.tw, self.th = float(view[0]) + float(view[2]), float(view[1]) + float(view[3])
        else:
            self.tw, self.th = self.w or 640.0, self.h or 480.0
        if isinstance(view, list) and len(view) == 4:
            self.view = [float(x) for x in view]
        else:
            self.view = [0.0, 0.0, self.tw, self.th]
        self.w = self.w or self.view[2]
        self.h = self.h or self.view[3]
        self.centre = d.get("origin", "centre") in ("centre", "center")
        self.up = d.get("y") == "up"
        self.raw = d

    def to_px(self, box):
        """A canvas-unit box as guest pixels [x0, y0, x1, y1], y down."""
        sx, sy = self.view[2] / self.w, self.view[3] / self.h
        x0, y0, x1, y1 = box
        ox = self.w / 2 if self.centre else 0.0
        oy = self.h / 2 if self.centre else 0.0
        if self.up:
            y0, y1 = -y1, -y0
        px = sorted((self.view[0] + (x0 + ox) * sx, self.view[0] + (x1 + ox) * sx))
        py = sorted((self.view[1] + (y0 + oy) * sy, self.view[1] + (y1 + oy) * sy))
        return [px[0], py[0], px[1], py[1]]

    def to_units(self, x):
        """A guest-pixel x back in canvas units."""
        ox = self.w / 2 if self.centre else 0.0
        return (x - self.view[0]) * self.w / self.view[2] - ox


class Sprite:
    def __init__(self, d, index):
        self.index = index
        self.level = d.get("level")
        self.frame = int(d.get("frame") or 0)
        self.queue = d.get("queue")
        self.chain = [x for x in (va(c) for c in d.get("chain") or []) if x is not None]
        self.ret = va(d.get("ret"))
        self.site = va(d.get("site"))
        self.element = d.get("element")
        self.group = d.get("group")
        self.draw = va(d.get("draw"))
        self.name = d.get("name")
        self.n = int(d.get("n") or 0)
        b = d.get("bbox")
        self.bbox = [float(x) for x in b] if isinstance(b, list) and len(b) == 4 else None
        self.prims = int(d.get("prims") or 0)
        self.tex = d.get("tex")

    def name_ret(self):
        """The return address the layer names this sprite by: the matched
        site, else at level 1 the outermost wrapper's return (hud_apply's
        name_ret), else the hooked call's own. A level-0 chain is a stack
        scan that stale words can pass, so an unlisted level-0 sprite is
        named by the draw's own return address."""
        if self.site:
            return self.site
        if self.level != 0 and self.chain:
            return self.chain[0]
        return self.ret


class Frame:
    def __init__(self, d, path):
        self.frame = int(d.get("frame") or 0)
        self.flip = d.get("flip")
        cv = d.get("canvas")
        self.canvas = Canvas(cv) if isinstance(cv, dict) and cv else None
        name = d.get("bmp")
        self.bmp = os.path.join(os.path.dirname(path), name) if isinstance(name, str) else None


class Capture:
    """One capture directory (or its capture.jsonl)."""

    def __init__(self, path):
        if os.path.isdir(path):
            path = os.path.join(path, JSONL)
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        self.sprites, self.frames, self.other = [], {}, []
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError as e:
            raise CliError("no capture at %s (%s)" % (path, e.strerror)) from e
        with f:
            lines = f.read().split("\n")
            last = max((i for i, ln in enumerate(lines, 1) if ln.strip()), default=0)
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError as e:
                    if i == last:
                        # A run killed mid-write leaves half a line; the
                        # lines before it are whole.
                        print(
                            "hud-map: %s:%d: partial last line skipped" % (path, i), file=sys.stderr
                        )
                        continue
                    raise CliError("%s:%d: not JSON (%s)" % (path, i, e)) from e
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "sprite":
                    self.sprites.append(Sprite(d, len(self.sprites)))
                elif t == "frame":
                    fr = Frame(d, path)
                    self.frames[fr.frame] = fr
                else:
                    self.other.append(d)
        if not any(fr.canvas is not None for fr in self.frames.values()):
            raise CliError(
                "%s: no frame line with a canvas (the capture ran without a table?): "
                "boxes cannot be placed" % path
            )

    def canvas(self, frame=None):
        """The frame's canvas, else the first one captured. A capture
        without one is refused when read: boxes are in canvas units, and a
        guessed canvas would place them anywhere."""
        fr = self.frames.get(frame)
        if fr is not None and fr.canvas is not None:
            return fr.canvas
        for k in sorted(self.frames):
            if self.frames[k].canvas is not None:
                return self.frames[k].canvas
        raise CliError("%s: no frame line with a canvas" % self.path)

    def last_frame(self):
        frames = list(self.frames) + [s.frame for s in self.sprites]
        return max(frames) if frames else None


def load(paths):
    return [Capture(p) for p in paths]
