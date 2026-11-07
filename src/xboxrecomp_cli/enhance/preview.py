"""xbr enhance preview: the local page over the texture pack's choices (D9,
D10, D15): the upscalers, the coverage and its cost, a crop of any texture
through each upscaler, and what the runtime said it missed.

One static page and JSON routes on 127.0.0.1. The page can register programs
and service URLs, so loopback is not enough: a page in another tab could post
to it (CSRF), a name could rebind to it (DNS rebinding). Every route takes the
session's token, the Host must be this server's, a foreign Origin is refused
and a POST is JSON only, which no cross-site form can send. The server makes
no request of its own: a registered service is reached only by the http
backend's own calls, on Test.

It writes nothing but the two config files; texels stay in memory. What it
shows is made from the owner's game files and stays on this machine.
"""

import argparse
import base64
import copy
import hmac
import http.server
import importlib.util
import json
import math
import os
import re
import secrets
import threading
import time
import tomllib
import urllib.parse
import webbrowser

from .. import host
from ..host import CliError
from ..package.progress import load_timings
from . import (
    alpha,
    bc1,
    bc3,
    build,
    classes,
    dds,
    edges,
    estimate,
    imported,
    mips,
    png,
    textures,
    textures_cfg,
    traced,
)
from .sources import SourceError
from .upscalers import BUILTINS, UpscalerError, by_name, command

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_page.html")
DEFAULT_CROP = 128
# Texels of the source around a crop that go to the upscaler with it, so the
# crop's border has the neighbours a whole-texture build would give it: the
# pad's width, past which no built-in kernel reaches.
MARGIN = edges.PAD
# A request body: a log's miss lines are the largest, a few MB at most.
MAX_BODY = 16 * 1024 * 1024
# Decoded textures kept, and renders: a page session clicks through dozens.
KEEP_IMAGES = 64
KEEP_RENDERS = 256
# ...and the renders' bytes: a whole 1024 texture at 4x is 64 MB of RGBA.
KEEP_RENDER_BYTES = 512 * 1024 * 1024
# The on-screen view's side, past which a magnified crop is cut down.
MAX_SCREEN = 1024
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
HASH = re.compile(r"[0-9A-Fa-f]{16}")
# A rule's key goes inside `[rule."KEY"]`: no quote, backslash or control.
RULE_KEY = re.compile(r'[^"\\\x00-\x1f\x7f]{1,200}')
TOP_EDITABLE = ("render_scale", "preset")
TABLES_EDITABLE = ("coverage", "budget")
HTTP_BACKEND = "xboxrecomp_cli.enhance.upscalers.http"


def configure(p):
    p.add_argument("--config", help="the config (default config/enhance-textures.toml)")
    p.add_argument("--inventory", help="the inventory file (default dist/enhance/inventory.json)")
    p.add_argument(
        "--dump",
        action="append",
        default=[],
        metavar="DIR",
        help="a dump to read texels from (default: the ones the inventory was made from)",
    )
    p.add_argument("--port", type=int, default=0, help="the port (default: a free one)")
    p.add_argument(
        "--no-browser", action="store_true", help="print the URL and open nothing (the wizard)"
    )


class Refused(Exception):
    """A request the page made wrong: 400 with the reason, which the page shows."""


def has_http_backend():
    try:
        return importlib.util.find_spec(HTTP_BACKEND) is not None
    except ImportError:
        return False


def data_uri(w, h, rgba):
    return "data:image/png;base64," + base64.b64encode(png.encode(w, h, rgba)).decode("ascii")


def _name(e):
    return e["origins"][0]["name"] if e.get("origins") else ""


def _linear(f):
    # A filter the sidecar did not record reads as linear, the usual 3D one.
    return not (f or "linear").startswith("point")


def sample(w, h, rgba, nw, nh, linear):
    """(w, h, rgba) resampled to nw x nh as a GPU samples it at each pixel's
    centre: the nearest texel, or the four around it weighted, clamped at the
    edge."""
    out = bytearray(nw * nh * 4)
    sx, sy = w / nw, h / nh
    if not linear:
        xs = [min(w - 1, int((i + 0.5) * sx)) * 4 for i in range(nw)]
        o = 0
        for j in range(nh):
            row = min(h - 1, int((j + 0.5) * sy)) * w * 4
            for x in xs:
                out[o : o + 4] = rgba[row + x : row + x + 4]
                o += 4
        return bytes(out)

    def taps(n, src, step):
        t = []
        for i in range(n):
            u = (i + 0.5) * step - 0.5
            i0 = math.floor(u)
            f = u - i0
            t.append((min(src - 1, max(0, i0)), min(src - 1, max(0, i0 + 1)), f))
        return t

    tx, ty = taps(nw, w, sx), taps(nh, h, sy)
    o = 0
    for y0, y1, fy in ty:
        r0, r1 = y0 * w * 4, y1 * w * 4
        for x0, x1, fx in tx:
            a, b = r0 + x0 * 4, r0 + x1 * 4
            c, d = r1 + x0 * 4, r1 + x1 * 4
            for ch in range(4):
                top = rgba[a + ch] + (rgba[b + ch] - rgba[a + ch]) * fx
                bot = rgba[c + ch] + (rgba[d + ch] - rgba[c + ch]) * fx
                out[o + ch] = int(top + (bot - top) * fy + 0.5)
            o += 4
    return bytes(out)


def on_screen(w, h, rgba, per_texel, levels_mode, source_levels, mag_filter, min_filter):
    """The image as drawn at `per_texel` screen pixels per texel (D15): the
    chain's level the LOD picks for that footprint, through the filter the
    guest set. {w, h, rgba, level, filter}."""
    nw = max(1, round(w * per_texel))
    nh = max(1, round(h * per_texel))
    scale = min(1.0, MAX_SCREEN / max(nw, nh))
    nw, nh = max(1, int(nw * scale)), max(1, int(nh * scale))
    if per_texel >= 1:
        lw, lh, img, level, filt = w, h, rgba, 0, mag_filter
    else:
        chain = mips.chain(w, h, rgba, levels_mode, source_levels)
        # The nearest level: what a point-mip sampler takes, and close to the
        # mix of two a linear-mip one blends.
        level = min(len(chain) - 1, int(math.log2(1 / per_texel) + 0.5))
        lw, lh, img = chain[level]
        filt = min_filter
    return {
        "w": nw,
        "h": nh,
        "rgba": sample(lw, lh, img, nw, nh, _linear(filt)),
        "level": level,
        "filter": filt or "linear (not recorded)",
    }


def miss_rows(lines):
    """(lacking, held) of [TEXPACK] miss lines, as imported.read_misses reads a
    file; the page sends only the lines that match, not the log."""
    lacking, held = set(), set()
    for line in lines:
        m = imported.MISS_LINE.search(line)
        if m:
            (held if m.group(2) else lacking).add(m.group(1).upper())
    return lacking, held - lacking


class App:
    """The page's state: the inventory, the configs, decoded texels and renders.
    One lock: renders run one at a time, as a build would run them."""

    def __init__(self, G, config_path, inv_path, dump_dirs, inv_arg=None):
        self.G = G
        self.cfg = classes.game_config(G.m)
        self.config_path = os.path.abspath(config_path)
        self.local_path = textures_cfg.local_path_of(self.config_path)
        self.inv_path = inv_path
        self.inv_arg = inv_arg
        self.dump_dirs = dump_dirs
        self.lock = threading.RLock()
        self._doc = None
        self._sources = None
        self.images = {}
        self.renders = {}
        self.render_bytes = 0
        self.versions = {}
        self.has_http = has_http_backend()

    # ---- inputs ---------------------------------------------------------

    def doc(self):
        if self._doc is None:
            a = argparse.Namespace(inventory=self.inv_arg, dump=self.dump_dirs)
            self._doc = textures._inventory_for_build(a, self.cfg, self.dump_dirs)
        return self._doc

    def entry(self, h):
        if not isinstance(h, str) or not HASH.fullmatch(h):
            raise Refused("a texture's hash: 16 hex digits")
        h = h.upper()
        e = next((x for x in self.doc()["entries"] if x["hash"] == h), None)
        if e is None:
            raise Refused("%s is not in the inventory" % h)
        return e

    def sources(self):
        if self._sources is None:
            try:
                self._sources = textures._open(self.cfg, self.dump_dirs, self.G)
            except CliError as e:
                raise Refused(str(e)) from e
        return self._sources

    def texels(self, e):
        """(w, h, rgba): the dump's, what the runtime decoded, else the feeder's
        (D10). Kept in memory, a few dozen at most."""
        h = e["hash"]
        img = self.images.get(h)
        if img is not None:
            return img
        feeders, dumps = self.sources()
        tex = build.dump_of(h, dumps)
        if tex is not None:
            try:
                w, ht, bgra = tex.read().levels_bgra()[0]
                img = build.decode_source(("bgra", w, ht, bgra))
            except SourceError:
                img = None
        if img is None:
            for _, _, src in build.sources_of({h: (e, {})}, feeders, []):
                if src is not None:
                    img = build.decode_source(src)
        if img is None:
            raise Refused(
                "%s: no dump or game file here holds its texels (enhance textures "
                "inventory --dump DIR adds a dump)" % h
            )
        if len(self.images) >= KEEP_IMAGES:
            self.images.pop(next(iter(self.images)))
        self.images[h] = img
        return img

    # ---- the configs ----------------------------------------------------

    def _read_text(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def files(self):
        """(tracked text, tracked doc, local text, local doc); a file that does
        not parse comes back as its text and an error the page shows."""
        tracked = self._read_text(self.config_path)
        local = self._read_text(self.local_path)
        out = {"tracked": {}, "local": {}, "error": ""}
        try:
            out["tracked"] = tomllib.loads(tracked)
            out["local"] = tomllib.loads(local)
        except tomllib.TOMLDecodeError as e:
            out["error"] = str(e)
        return tracked, local, out

    def merged(self, draft):
        """The draft as the build reads it: the local upscaler tables over the
        tracked ones; ConfigError on what the build would refuse."""
        if not isinstance(draft, dict):
            raise Refused("config: an object")
        tracked = copy.deepcopy(draft.get("tracked") or {})
        local = draft.get("local") or {}
        if not isinstance(tracked, dict) or not isinstance(local, dict):
            raise Refused("config: tracked and local are objects")
        self._check_local(local)
        try:
            textures_cfg._refuse_machine_keys(tracked, "the config")
            textures_cfg._refuse_non_tables(local, "the local file")
            for up, table in local.get("upscaler", {}).items():
                tracked.setdefault("upscaler", {}).setdefault(up, {}).update(table)
            textures_cfg.validate(tracked, "the config")
        except textures_cfg.ConfigError as e:
            raise Refused(str(e)) from e
        return tracked

    def build_command(self):
        cmd = "%s enhance textures build" % host.cli_name()
        default = os.path.join(self.G.root, textures.DEFAULT_CONFIG)
        if os.path.abspath(default) != self.config_path:
            cmd += " --config %s" % self.config_path
        return cmd

    def save(self, draft):
        """Both files from the draft. The tracked one keeps its comments: only
        the keys that changed are touched. Paths, URLs and token names go to
        the local file, and nothing else does."""
        self.merged(draft)
        new_t, new_l = draft.get("tracked") or {}, draft.get("local") or {}
        old_text, old_local_text, old = self.files()
        if old["error"]:
            raise Refused("the config does not parse, fix it by hand first: %s" % old["error"])
        text = old_text or "schema = %d\n" % classes.CONFIG_SCHEMA
        text = self._edit_tracked(text, old["tracked"], new_t)
        local_text = self._edit_local(old_local_text, old["local"], new_l)
        # What is written must read back as what was checked. The edits are
        # line edits: a table in a form they do not know (dotted keys, an
        # inline table) can come out unchanged, or twice.
        try:
            doc = tomllib.loads(text)
            again = tomllib.loads(local_text)
        except tomllib.TOMLDecodeError as e:
            raise Refused(
                "Save cannot edit the config in place (%s): a table there is written in a "
                "form it does not edit, such as dotted keys or an inline table. Edit it by "
                'hand, or write it as [class.NAME] or [rule."KEY"]' % e
            ) from e
        for name, part in self._unsaved(doc, new_t, again, new_l):
            raise Refused(
                "Save cannot edit %s in %s: it is written in a form Save does not edit. Edit "
                'it by hand, or write it as [class.NAME] or [rule."KEY"]' % (name, part)
            )
        try:
            textures_cfg._refuse_machine_keys(doc, self.config_path)
            for up, table in again.get("upscaler", {}).items():
                doc.setdefault("upscaler", {}).setdefault(up, {}).update(table)
            textures_cfg.validate(doc, self.config_path)
        except (textures_cfg.ConfigError, ValueError) as e:
            raise Refused(str(e)) from e
        wrote = []
        for path, body, before in (
            (self.config_path, text, old_text),
            (self.local_path, local_text, old_local_text),
        ):
            if body == before or (not body.strip() and not before):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.replace(tmp, path)
            wrote.append(self.G.rel(path))
        return {"wrote": wrote, "command": self.build_command()}

    def _unsaved(self, doc, draft, local_doc, local_draft):
        """(table, file) of each editable part the written files do not read
        back as the draft has it."""
        config, local = self.G.rel(self.config_path), self.G.rel(self.local_path)
        for k in TOP_EDITABLE:
            if doc.get(k) != draft.get(k):
                yield k, config
        for k in TABLES_EDITABLE:
            if doc.get(k, {}) != draft.get(k, {}):
                yield "[%s]" % k, config
        groups = [(g, doc, draft, config) for g in ("class", "rule", "upscaler")]
        groups.append(("upscaler", local_doc, local_draft, local))
        for g, got, want, part in groups:
            got, want = got.get(g, {}), want.get(g, {})
            if not isinstance(got, dict):
                yield g, part
                continue
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    yield '[%s."%s"]' % (g, k), part

    @staticmethod
    def _rows(doc, key):
        v = doc.get(key, {})
        if not isinstance(v, dict):
            raise Refused("%s: a table" % key)
        return v

    def _edit_tracked(self, text, old, new):
        try:
            top = {k: new[k] for k in TOP_EDITABLE if k in new}
            gone = [k for k in TOP_EDITABLE if k in old and k not in new]
            text = textures_cfg.set_top_keys(text, top, gone)
            for name in TABLES_EDITABLE:
                text = self._edit_table(text, name, self._rows(old, name), self._rows(new, name))
            for cls in self._rows(new, "class"):
                if cls not in classes.CLASSES:
                    raise Refused("class %s: one of %s" % (cls, ", ".join(classes.CLASSES)))
            text = self._edit_group(text, "class", old, new, lambda k: k)
            for key in self._rows(new, "rule"):
                if not RULE_KEY.fullmatch(key):
                    raise Refused("rule %r: no quote, backslash or line break" % key)
            text = self._edit_group(text, "rule", old, new, lambda k: '"%s"' % k)
            text = self._edit_group(text, "upscaler", old, new, self._up_name)
        except ValueError as e:
            raise Refused(str(e)) from e
        return text

    def _check_local(self, new):
        for k in new:
            if k != "upscaler":
                raise Refused("the local file holds [upscaler.NAME] tables only, not %s" % k)
        for up, table in self._rows(new, "upscaler").items():
            if not isinstance(table, dict):
                raise Refused("[upscaler.%s]: a table" % up)
            for k in table:
                if k not in textures_cfg.LOCAL_KEYS:
                    raise Refused(
                        "[upscaler.%s] %s: the local file holds %s"
                        % (up, k, ", ".join(textures_cfg.LOCAL_KEYS))
                    )

    def _edit_local(self, text, old, new):
        self._check_local(new)
        try:
            return self._edit_group(text, "upscaler", old, new, self._up_name)
        except ValueError as e:
            raise Refused(str(e)) from e

    @staticmethod
    def _up_name(k):
        if not NAME.fullmatch(k):
            raise Refused("upscaler %r: letters, digits, - and _" % k)
        return k

    def _edit_group(self, text, group, old, new, quote):
        olds, news = self._rows(old, group), self._rows(new, group)
        for k in olds:
            if k not in news:
                text = textures_cfg.drop_table(text, "%s.%s" % (group, quote(k)))
        for k, row in news.items():
            if not isinstance(row, dict):
                raise Refused("[%s.%s]: a table" % (group, k))
            text = self._edit_table(text, "%s.%s" % (group, quote(k)), olds.get(k, {}), row)
        return text

    @staticmethod
    def _edit_table(text, name, old, new):
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise Refused("[%s]: a table" % name)
        changed = {k: v for k, v in new.items() if old.get(k) != v}
        gone = [k for k in old if k not in new]
        if not new and old:
            return textures_cfg.drop_table(text, name)
        if changed:
            text = textures_cfg.set_table_keys(text, name, changed)
        if gone:
            text = textures_cfg.remove_table_keys(text, name, gone)
        return text

    # ---- routes ---------------------------------------------------------

    def state(self):
        tracked, local, parsed = self.files()
        doc = self.doc()
        presets = list(classes.PRESETS) + [
            p for p in self.cfg["presets"] if p not in classes.PRESETS
        ]
        return {
            "game": self.G.slug,
            "config": self.G.rel(self.config_path),
            "local": self.G.rel(self.local_path),
            "tracked_text": tracked,
            "local_text": local,
            "tracked": parsed["tracked"],
            "local_doc": parsed["local"],
            "config_error": parsed["error"],
            "presets": presets + [classes.CUSTOM],
            "classes": list(classes.CLASSES),
            "defaults": {
                c: classes.class_defaults(c, parsed["tracked"].get("render_scale", 1))
                for c in classes.CLASSES
            },
            "row_keys": textures_cfg.ROW_KEYS,
            "alpha_modes": list(alpha.MODES),
            "edge_modes": ["auto"] + list(edges.MODES),
            "dumps": [d["name"] for d in doc.get("dumps", [])],
            "entries": len(doc["entries"]),
            "traced": any(e.get("mag_scale1") is not None for e in doc["entries"]),
            "has_http": self.has_http,
            "command": self.build_command(),
        }

    def upscalers(self, draft=None):
        """Built-ins, then each preset with whether this machine has its tool,
        then the services and tables the config registers (D9)."""
        tables = self.merged(draft).get("upscaler", {}) if draft else self._saved_tables()
        rows = [dict(u.describe(), found=True, source="built-in") for u in BUILTINS]
        presets = command.presets().get("tool", {})
        for name, p in sorted(presets.items()):
            table = tables.get(name, {})
            path = command.find_tool(p, table)
            row = {
                "name": name,
                "kind": "command",
                "source": "preset",
                "found": path is not None,
                "binary": p.get("binary", ""),
                "path": table.get("path", ""),
                "licence": p.get("licence", ""),
                "homepage": p.get("homepage", ""),
                "flag": p.get("flag", ""),
                "experimental": p.get("experimental", False),
                "version": self.versions.get(path, {}).get("version") if path else None,
                "params": [],
            }
            if path:
                try:
                    row["params"] = by_name(name, tables).describe()["params"]
                except UpscalerError as e:
                    row["why"] = str(e)
            rows.append(row)
        for name, table in sorted(tables.items()):
            if name in presets or any(u.name == name for u in BUILTINS):
                continue
            row = {
                "name": name,
                "kind": "service" if "url" in table else "command",
                "source": "config",
                "url": table.get("url", ""),
                "token_env": table.get("token_env", ""),
                # Whether it is set, never its value.
                "token_set": bool(table.get("token_env") and os.environ.get(table["token_env"])),
                "found": False,
                "params": [],
            }
            if "url" not in table:
                try:
                    row["params"] = by_name(name, tables).describe()["params"]
                    row["found"] = True
                except UpscalerError as e:
                    row["why"] = str(e)
            rows.append(row)
        return {"upscalers": rows, "has_http": self.has_http}

    def _saved_tables(self):
        try:
            if os.path.exists(self.config_path):
                return textures_cfg.load(self.config_path).doc.get("upscaler", {})
        except textures_cfg.ConfigError:
            pass
        return {}

    def tool_version(self, name):
        """The first line of the tool's --version: run on a click, never on
        load, since it starts the owner's program."""
        p = command.presets().get("tool", {}).get(name)
        if p is None:
            raise Refused("no preset %s" % name)
        path = command.find_tool(p, self._saved_tables().get(name, {}))
        if path is None:
            raise Refused("%s: not found here" % name)
        if path not in self.versions:
            _, line = command.tool_identity(path)
            self.versions[path] = {"version": command.version_token(line) or "unknown"}
        return {"name": name, "version": self.versions[path]["version"]}

    def test_service(self, body):
        """Test: the http backend's own `info` call on the draft's table. The
        server itself requests nothing."""
        name = body.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Refused("a service's name: letters, digits, - and _")
        table = {k: body[k] for k in ("url", "token_env", "allow_plain_http") if k in body}
        if not isinstance(table.get("url"), str) or not table["url"]:
            raise Refused("a service needs its URL")
        if not self.has_http:
            raise Refused(
                "this CLI has no http backend yet: the service can be saved, and "
                "tested once a CLI with one runs this page"
            )
        tables = dict(self._saved_tables())
        tables[name] = dict(tables.get(name, {}), **table)
        try:
            up = by_name(name, tables)
            info = up.info() if hasattr(up, "info") else up.describe()
        except UpscalerError as e:
            raise Refused(str(e)) from e
        return {"name": name, "info": info}

    def listing(self):
        """Every entry, the game's preview picks first, then the dump's
        most-used (D10); the page groups by class and language and searches."""
        out = []
        picks = self.cfg["preview"]
        for e in self.doc()["entries"]:
            pick = bool(picks) and classes.matches(e, picks)
            out.append(
                {
                    "hash": e["hash"],
                    "name": _name(e),
                    "names": [o["name"] for o in e.get("origins", ())][:8],
                    "class": e["class"],
                    "language": e.get("language", ""),
                    "w": e["width"],
                    "h": e["height"],
                    "format": e.get("format", ""),
                    "levels": e.get("levels", 1),
                    "uses": e.get("uses", 0),
                    "scenes": sorted(e.get("scenes", {})),
                    "skip": classes.why(e["skip"]) if e.get("skip") else "",
                    "mag": e.get("mag_scale1"),
                    "pick": pick,
                }
            )
        out.sort(key=lambda r: (not r["pick"], -r["uses"], r["name"] or "~", r["hash"]))
        return {"entries": out}

    def texture(self, h):
        e = self.entry(h)
        w, ht, rgba = self.texels(e)
        x, y, cw, ch = _busiest(w, ht, rgba)
        return {
            "hash": e["hash"],
            "name": _name(e),
            "w": w,
            "h": ht,
            "image": data_uri(w, ht, rgba),
            "crop": [x, y, cw, ch],
            "mag": e.get("mag_scale1"),
            "min_filter": e.get("min_filter", ""),
            "mag_filter": e.get("mag_filter", ""),
        }

    def _treatment(self, e, body):
        draft = self.merged(body.get("config")) if body.get("config") else {}
        t = classes.treatment(e, draft, "all", self.cfg)
        scale = body.get("scale")
        if scale is not None:
            if scale not in (2, 3, 4):
                raise Refused("scale: 2, 3 or 4")
            t["scale"] = scale
        return draft, t

    def _crop_of(self, body, w, h):
        crop = body.get("crop")
        if crop is None:
            return 0, 0, w, h
        if (
            not isinstance(crop, list)
            or len(crop) != 4
            or not all(type(v) is int for v in crop)
            or crop[2] < 1
            or crop[3] < 1
        ):
            raise Refused("crop: [x, y, width, height]")
        x, y, cw, ch = crop
        cw, ch = min(cw, w), min(ch, h)
        x, y = max(0, min(x, w - cw)), max(0, min(y, h - ch))
        return x, y, cw, ch

    def render(self, body):
        """Each named upscaler over the crop, as the build would make that part
        of the texture: {results: [{name, image, w, h, seconds, ...}]}."""
        e = self.entry(body.get("hash"))
        w, h, rgba = self.texels(e)
        draft, t = self._treatment(e, body)
        crop = self._crop_of(body, w, h)
        names = body.get("upscalers") or [t["upscaler"]]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise Refused("upscalers: a list of names")
        tables = draft.get("upscaler", {}) if draft else self._saved_tables()
        out = []
        for name in names:
            out.append(self._render_one(name, tables, draft, e, t, (w, h, rgba), crop, body))
        return {
            "hash": e["hash"],
            "crop": list(crop),
            "scale": t["scale"],
            "stock": data_uri(crop[2], crop[3], edges.crop(w, h, rgba, *crop)),
            "results": out,
        }

    def _render_one(self, name, tables, draft, e, t, img, crop, body):
        row = {"name": name}
        if name == "atlas":
            row["error"] = "atlas: the font page's (enhance hud), not a texel upscaler"
            return row
        try:
            up = by_name(name, tables)
        except UpscalerError as ex:
            row["error"] = str(ex)
            return row
        params = build.params_of(draft, up) if up.kind == "builtin" else tables.get(name, {})
        key = (
            name,
            json.dumps(params, sort_keys=True),
            e["hash"],
            crop,
            t["scale"],
            t["alpha"],
            t["edges"],
            t.get("encode"),
            t.get("mips"),
        )
        hit = self.renders.get(key)
        if hit is None:
            try:
                hit = self._run(up, params, e, t, img, crop)
            except (UpscalerError, CliError) as ex:
                row["error"] = str(ex)
                return row
            self._keep_render(key, hit)
        uw, uh, out, seconds, texels = hit
        whole = e["width"] * e["height"]
        row.update(
            w=uw,
            h=uh,
            image=data_uri(uw, uh, out),
            seconds=round(seconds, 4),
            # What the whole texture would take, at the crop's rate.
            seconds_per_texture=round(seconds * whole / max(1, texels), 3),
            bytes={
                enc: classes.pack_bytes(e, t["scale"], enc, t["mips"])
                for enc in ("raw", "bc1", "bc3")
            },
            encode=classes.encode_of(e, t["encode"]),
            identity=up.identity(),
            flag=up.flag,
        )
        if body.get("bc"):
            row["bc"] = _bc_views(uw, uh, out)
        if body.get("screen") is not None:
            row["screen"] = self._screen(e, t, crop, img, (uw, uh, out), body["screen"])
        return row

    def _run(self, up, params, e, t, img, crop):
        """(w, h, rgba, seconds, texels) of the crop through `up`. The crop
        goes with a margin of the texture's own neighbours, wrapped or clamped
        as the entry's edges say, so its border is what the build's would be;
        cells are each padded alone, so a cell texture runs whole."""
        w, h, rgba = img
        x, y, cw, ch = crop
        mode_u, mode_v = edges.resolve(t["edges"], e)
        job = build.make_job(up, w, h, rgba, t, e)
        if "cells" in (mode_u, mode_v) or (cw, ch) == (w, h):
            t0 = time.perf_counter()
            outs = up.upscale(job.images(), job.native, params)
            seconds = time.perf_counter() - t0
            uw, uh, out = job.finish(outs)
            s = t["scale"]
            return (
                cw * s,
                ch * s,
                edges.crop(uw, uh, out, x * s, y * s, cw * s, ch * s),
                seconds,
                w * h,
            )
        m = MARGIN
        pw, ph, padded = edges.pad(w, h, rgba, mode_u, mode_v, m)
        ww, wh = cw + 2 * m, ch + 2 * m
        window = edges.crop(pw, ph, padded, x, y, ww, wh)
        sub = build.Job(
            ww, wh, window, t["scale"], job.native, job.alpha_mode, "clamp", "clamp", None, 0
        )
        if job.cover is not None:
            # The threshold is the texture's, not the window's.
            sub.alpha_mode, sub.cover = job.alpha_mode, job.cover
        t0 = time.perf_counter()
        outs = up.upscale(sub.images(), sub.native, params)
        seconds = time.perf_counter() - t0
        uw, uh, out = sub.finish(outs)
        s = t["scale"]
        return (
            cw * s,
            ch * s,
            edges.crop(uw, uh, out, m * s, m * s, cw * s, ch * s),
            seconds,
            ww * wh,
        )

    def _keep_render(self, key, hit):
        """The oldest renders go past either cap; one larger than the byte cap
        is not kept."""
        size = len(hit[2])
        if size > KEEP_RENDER_BYTES:
            return
        while self.renders and (
            len(self.renders) >= KEEP_RENDERS or self.render_bytes + size > KEEP_RENDER_BYTES
        ):
            self.render_bytes -= len(self.renders.pop(next(iter(self.renders)))[2])
        self.renders[key] = hit
        self.render_bytes += size

    def _screen(self, e, t, crop, img, result, opts):
        """D15: the stock crop and the result as they would be drawn at the
        render scale, from the trace's magnification or a typed one."""
        if not isinstance(opts, dict):
            raise Refused("screen: {mag, render_scale}")
        mag = opts.get("mag")
        if mag is None:
            mag = e.get("mag_scale1")
        from_trace = opts.get("mag") is None and mag is not None
        mag = traced.sane_mag(mag if mag is not None else 1.0)
        r = opts.get("render_scale", 1)
        if mag is None or type(r) is not int or r < 1 or mag <= 0:
            raise Refused("screen: a magnification over 0, a render scale of 1 or more")
        w, h, rgba = img
        x, y, cw, ch = crop
        f = mag * r
        mf, nf = e.get("mag_filter", ""), e.get("min_filter", "")
        stock = on_screen(
            cw, ch, edges.crop(w, h, rgba, *crop), f, "keep", e.get("levels", 1), mf, nf
        )
        uw, uh, out = result
        s = t["scale"]
        up = on_screen(uw, uh, out, f / s, t["mips"], e.get("levels", 1), mf, nf)
        return {
            "mag": mag,
            "from_trace": from_trace,
            "render_scale": r,
            "stock": dict(_img(stock), level=stock["level"], filter=stock["filter"]),
            "result": dict(_img(up), level=up["level"], filter=up["filter"]),
            "magnified": f > 1,
            "note": (
                ""
                if f > 1
                else "not magnified at this scale: %.2f screen px per texel at render scale %d, "
                "so the upscale does not show" % (f, r)
            ),
        }

    def coverage(self, body):
        """Per preset, the estimate's three bars (pack MB, VRAM MB per dumped
        scene, build minutes); the exclusions of the chosen one, with their
        reasons; the trace's table when the sidecars have a magnification."""
        draft = self.merged(body.get("config") or {})
        entries = self.doc()["entries"]
        timings = load_timings(self.G.logs)
        presets = list(classes.PRESETS) + [
            p for p in self.cfg["presets"] if p not in classes.PRESETS
        ]
        if not self.cfg["hud"]:
            presets.remove("hud")
        bars = []
        for p in presets:
            est = estimate.estimate(entries, p, draft, self.cfg, timings, 1)
            rows = est["classes"].values()
            unmeasured = sorted({u for r in rows for u in r["unmeasured"]})
            bars.append(
                {
                    "preset": p,
                    "pack_bytes": est["total_bytes"],
                    "entries": sum(r["entries"] for r in rows),
                    "scenes": {k: v["pack_bytes"] for k, v in sorted(est["scenes"].items())},
                    "build_seconds": sum(r["build_seconds"] for r in rows),
                    "unmeasured": unmeasured,
                    "over_budget": est["over_budget"],
                }
            )
        preset = body.get("preset") or draft.get("preset", "all")
        if preset not in presets + [classes.CUSTOM]:
            raise Refused("preset %s: one of %s" % (preset, ", ".join(presets)))
        excluded = []
        for e in entries:
            if not classes.in_preset(e, preset, self.cfg):
                continue
            t = classes.treatment(e, draft, preset, self.cfg)
            if t["skip"]:
                excluded.append(
                    {
                        "hash": e["hash"],
                        "name": _name(e),
                        "class": e["class"],
                        "why": classes.why(t["skip"]),
                    }
                )
        r = draft.get("render_scale", 1)
        trace = [
            x
            for x in traced.table(
                [e for e in entries if not e["skip"]],
                r,
                draft.get("coverage", {}).get("untraced", "class"),
                draft,
            )
            if x["mag_scale1"] is not None
        ]
        return {"bars": bars, "preset": preset, "excluded": excluded, "traced": trace}

    def log(self, body):
        """[TEXPACK] miss lines joined to the inventory: what the pack lacks,
        by name where the inventory knows the hash, by hash where not."""
        lines = body.get("lines")
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
            raise Refused("lines: a list of the log's lines")
        lacking, held = miss_rows(lines)
        if not lacking and not held:
            raise Refused(
                "no [TEXPACK] miss line: run with RECOMP_TRACE=tex_pack and a pack loaded "
                "(with no pack the runtime records no misses)"
            )
        by_hash = {e["hash"]: e for e in self.doc()["entries"]}
        draft = self.merged(body.get("config")) if body.get("config") else {}
        rows = []
        for h in sorted(lacking | held):
            e = by_hash.get(h)
            row = {"hash": h, "lacking": h in lacking}
            if e is not None:
                t = classes.treatment(e, draft, "all", self.cfg)
                row.update(
                    name=_name(e),
                    cls=e["class"],
                    w=e["width"],
                    h=e["height"],
                    why=classes.why(t["skip"]) if t["skip"] else "",
                )
            rows.append(row)
        return {"rows": rows, "lacking": len(lacking), "held": len(held)}


def _img(d):
    return {"image": data_uri(d["w"], d["h"], d["rgba"]), "w": d["w"], "h": d["h"]}


def _busiest(w, h, rgba):
    from .compare import busiest_crop

    return busiest_crop(w, h, rgba, DEFAULT_CROP)


def _bc_views(w, h, rgba):
    """The result as BC1 and BC3 decode it, beside raw: what the encode costs
    the eye. Block formats take whole blocks only."""
    if w % 4 or h % 4:
        return {"note": "%dx%d is not whole blocks: raw only" % (w, h)}
    return {
        "bc1": data_uri(w, h, dds.decode_level(b"DXT1", w, h, bc1.encode(w, h, rgba))),
        "bc3": data_uri(w, h, dds.decode_level(b"DXT5", w, h, bc3.encode(w, h, rgba))),
    }


# ---- the server --------------------------------------------------------------


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self.hosts = {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}
        self.origins = {"http://" + h for h in self.hosts}
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read().encode("utf-8")

    @property
    def url(self):
        return "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)

    def stop(self):
        threading.Thread(target=self.shutdown, daemon=True).start()


# The page's own script and style, inline; images as data: URLs; fetches to
# this server only. Nothing the page loads can come from elsewhere.
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "xbr-preview"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _headers(self, code, kind, n):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()

    def _refuse(self, code):
        # No body: a refused caller learns nothing of the page.
        self._headers(code, "text/plain", 0)
        self.close_connection = True

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self._headers(code, "application/json", len(body))
        self.wfile.write(body)

    def _allowed(self):
        """The D9 checks, before the route is even looked at."""
        s = self.server
        if self.headers.get("Host", "") not in s.hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in s.origins:
            return False
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return False
        url = urllib.parse.urlsplit(self.path)
        given = self.headers.get("X-Xbr-Token") or ""
        if not given:
            q = urllib.parse.parse_qs(url.query)
            given = (q.get("t") or [""])[0]
        return hmac.compare_digest(given.encode("utf-8"), s.token.encode("utf-8"))

    def do_GET(self):
        if not self._allowed():
            return self._refuse(403)
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/":
            self._headers(200, "text/html; charset=utf-8", len(self.server.page))
            self.wfile.write(self.server.page)
            return None
        routes = {
            "/api/state": lambda: self.server.app.state(),
            "/api/upscalers": lambda: self.server.app.upscalers(),
            "/api/inventory": lambda: self.server.app.listing(),
            "/api/texture": lambda: self.server.app.texture(q.get("h", "")),
        }
        return self._answer(routes.get(url.path))

    def do_POST(self):
        if not self._allowed():
            return self._refuse(403)
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            return self._refuse(415)
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._refuse(411)
        if not 0 <= n <= MAX_BODY:
            return self._refuse(413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "the body is not JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "the body is a JSON object"})
        app = self.server.app
        path = urllib.parse.urlsplit(self.path).path
        if path == "/quit":
            self._json(200, {"bye": True})
            self.close_connection = True
            self.server.stop()
            return None
        routes = {
            "/api/upscalers": lambda: app.upscalers(body.get("config")),
            "/api/render": lambda: app.render(body),
            "/api/coverage": lambda: app.coverage(body),
            "/api/save": lambda: app.save(body.get("config") or {}),
            "/api/service/test": lambda: app.test_service(body),
            "/api/tool/version": lambda: app.tool_version(body.get("name", "")),
            "/api/log": lambda: app.log(body),
        }
        return self._answer(routes.get(path))

    def _answer(self, route):
        if route is None:
            return self._refuse(404)
        try:
            with self.server.app.lock:
                result = route()
        except Refused as e:
            return self._json(400, {"error": str(e)})
        except CliError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            # A bug here: the page shows it, and the session goes on.
            return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
        return self._json(200, result)


def _parent_alive(ppid):
    if os.name != "nt":
        return os.getppid() == ppid
    import ctypes

    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(h)


def _watch_parent(server, ppid):
    """The wizard that started the page may die without a /quit: the server
    goes with it rather than hold the port and the texels."""
    while not server.done.wait(1.0):
        if not _parent_alive(ppid):
            server.shutdown()
            return


def run(a):
    G = host.g()
    config = a.config or os.path.join(G.root, textures.DEFAULT_CONFIG)
    inv_path = textures._inventory_path(textures._inv_only(a))
    dump_dirs = a.dump or textures._read_dump_paths(textures._inv_only(a))
    app = App(G, config, inv_path, dump_dirs, a.inventory)
    if not os.path.exists(inv_path) and not a.inventory:
        host.say(
            "no inventory yet: the page makes one from the sources (enhance textures inventory)"
        )
    try:
        app.doc()
        server = Server(app, a.port)
    except OSError as e:
        raise CliError("preview: %s" % e) from e
    server.done = threading.Event()
    threading.Thread(target=_watch_parent, args=(server, os.getppid()), daemon=True).start()
    # The URL holds the token: it goes to this terminal, never to a file.
    print("preview: %s" % server.url, flush=True)
    host.say("made from your game files: it stays on this machine. Ctrl-C or Quit stops it")
    if not a.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.done.set()
        server.server_close()
    host.say("preview stopped")
    return 0
