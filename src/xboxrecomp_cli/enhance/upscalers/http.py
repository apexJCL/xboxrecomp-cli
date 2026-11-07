"""Service backends (D7): an upscaler behind a URL, so the machine that builds
the pack can use another machine's GPU.

`api = "xbr"` (the default) speaks the xbr upscale service contract v1, which
`enhance serve` implements (docs/upscale-service.md):

  GET  /v1/info                       {"name", "version", "scales", "models":
                                       [{"name", "scales", "alpha"}],
                                       "max_side", "batch"}
  POST /v1/upscale?scale=S&model=M    image/png in, image/png out, or a JSON
                                       {"error"} with a 4xx or 5xx

`api = "a1111"` speaks AUTOMATIC1111's `--api`, and `api = "comfyui"` a
ComfyUI server over a workflow of the owner's (comfyui.py).

The URL, the token's variable and the model are the local file's: a tracked
config names no host. The token is read from the environment at each request
and kept nowhere. Whatever the peer is, it is not trusted: the model and the
scale are checked against what it said it has before a request, an answer's
bytes are capped, and a PNG's header is checked against the size asked for
before a byte of it is inflated.
"""

import base64
import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import png
from .base import Param, Upscaler, UpscalerError

CONTRACT = 1
APIS = ("xbr", "a1111", "comfyui")
# The tracked keys of a service's [upscaler.NAME] table; url, token_env,
# allow_plain_http, model and the workflow are textures_cfg.LOCAL_KEYS.
TABLE_KEYS = ("api", "scales", "batch", "timeout")
DEFAULT_TIMEOUT = 300  # seconds, per request: a big texture on a busy GPU
RETRIES = 3
BACKOFF = 0.5  # seconds, doubled per retry
INFO_CAP = 1 << 20
ERROR_CAP = 64 << 10
# What a retry can fix: the peer is busy or restarting, not refusing.
RETRY_STATUS = (429, 502, 503, 504)


def is_loopback(host):
    """Whether a host name stays on this machine. Only `localhost` and literal
    loopback addresses: another name could resolve anywhere."""
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def clean_token(value, var, who):
    """A token from the environment, without the line end a file or a shell
    leaves on it. Anything else unprintable is refused naming only the
    variable: http.client would refuse the header with the token in its
    message, and a traceback would print it."""
    token = value.strip()
    if not all(" " < c <= "~" for c in token):
        raise UpscalerError(
            "%s: %s holds a character a token cannot have (only printable ASCII, no space)"
            % (who, var)
        )
    return token


def png_cap(w, h):
    """The most bytes an honest w x h RGBA PNG takes: its scanlines stored
    with no compression at all, chunk overheads, and room for ancillary
    chunks a tool adds."""
    raw = (w * 4 + 1) * h
    return raw + raw // 32 + (1 << 20)


def check_png(data, w, h, who):
    """(w, h, rgba) of a peer's PNG, refused unless its header says w x h; the
    header is read before anything is inflated, so a peer's size is never
    what sets how much memory a decode takes."""
    try:
        got = png.header(data)
    except png.PngError as e:
        raise UpscalerError("%s answered something that is not a PNG: %s" % (who, e)) from e
    if got != (w, h):
        raise UpscalerError("%s answered %dx%d where %dx%d was asked" % (who, *got, w, h))
    try:
        return png.decode(data)
    except png.PngError as e:
        raise UpscalerError("%s answered a broken PNG: %s" % (who, e)) from e


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to wherever the peer points: refused,
    and said, rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    """Requests to one service: its URL, its token's variable, retries and a
    timeout. `sleep` is time.sleep, a test's stand-in otherwise."""

    def __init__(self, name, table, sleep=time.sleep):
        self.name = name
        url = table.get("url", "")
        if not url:
            raise UpscalerError(
                "%s: a service needs url = in [upscaler.%s] of the local file "
                "(config/enhance-textures.local.toml)" % (name, name)
            )
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise UpscalerError("%s: url %s: http:// or https:// and a host" % (name, url))
        if u.username or u.password:
            raise UpscalerError(
                "%s: no user or password in the url: token_env names the variable that "
                "holds the token" % name
            )
        if u.query or u.fragment:
            raise UpscalerError("%s: url %s: no query or fragment" % (name, url))
        self.base = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))
        self.token_env = table.get("token_env", "")
        self.timeout = table.get("timeout") or DEFAULT_TIMEOUT
        self.sleep = sleep
        self.warning = ""
        loop = is_loopback(u.hostname)
        if self.token_env and u.scheme == "http" and not loop:
            if not table.get("allow_plain_http"):
                raise UpscalerError(
                    "%s: refusing to send the token over plain http to %s, which anyone on "
                    "the way can read: an https:// url, or allow_plain_http = true in the "
                    "local file if this network is yours" % (name, u.hostname)
                )
            self.warning = "%s: the token goes over plain http to %s (allow_plain_http)" % (
                name,
                u.hostname,
            )
        handlers = [_NoRedirect()]
        if loop:
            # A proxy has no business between two processes of this machine.
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def _token(self):
        if not self.token_env:
            return ""
        token = clean_token(os.environ.get(self.token_env, ""), self.token_env, self.name)
        if not token:
            raise UpscalerError(
                "%s: token_env names %s, and it is not set here" % (self.name, self.token_env)
            )
        return token

    def request(self, method, path, body=None, ctype="", cap=INFO_CAP, accept=""):
        """(content type, body) of a 2xx answer. A refused connection or a busy
        answer is tried again with back-off, and so is a timeout of a GET; any
        other 4xx is not. A POST that timed out or was cut may have run (a
        ComfyUI prompt, an A1111 job): sent again, it would run twice."""
        headers = {"User-Agent": "xbr-upscale-client/%d" % CONTRACT}
        if ctype:
            headers["Content-Type"] = ctype
        if accept:
            headers["Accept"] = accept
        token = self._token()
        if token:
            headers["Authorization"] = "Bearer " + token
        url = self.base + path
        last = ""
        for attempt in range(RETRIES + 1):
            if attempt:
                self.sleep(BACKOFF * 2 ** (attempt - 1))
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    return r.headers.get("Content-Type", ""), self._read(r, cap, path)
            except urllib.error.HTTPError as e:
                why = self._error_of(e)
                if e.code in (301, 302, 303, 307, 308):
                    raise UpscalerError(
                        "%s: %s redirects to %s: put the final url in the local file"
                        % (self.name, path, e.headers.get("Location", "elsewhere"))
                    ) from e
                if e.code not in RETRY_STATUS:
                    raise UpscalerError("%s: %s %s: %s" % (self.name, method, path, why)) from e
                last = why
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                reason = getattr(e, "reason", e)
                last = str(reason)
                sent = not isinstance(reason, (ConnectionRefusedError, socket.gaierror))
                if method != "GET" and sent:
                    raise UpscalerError(
                        "%s: %s %s: %s (not sent again: it may have run)"
                        % (self.name, method, path, last)
                    ) from e
        raise UpscalerError(
            "%s: %s %s failed %d times, last: %s" % (self.name, method, path, RETRIES + 1, last)
        )

    def _read(self, r, cap, path):
        n = r.headers.get("Content-Length")
        if n is not None and n.isascii() and n.isdigit() and int(n) > cap:
            raise UpscalerError("%s: %s answered %s bytes, over %d" % (self.name, path, n, cap))
        data = r.read(cap + 1)
        if len(data) > cap:
            raise UpscalerError("%s: %s answered over %d bytes" % (self.name, path, cap))
        return data

    @staticmethod
    def _error_of(e):
        """The peer's own words for a failure, short: its JSON error when it
        sent one."""
        try:
            raw = e.read(ERROR_CAP)
        except OSError:
            raw = b""
        text = raw.decode("utf-8", "replace").strip()
        try:
            doc = json.loads(text)
            if isinstance(doc, dict):
                text = str(doc.get("error") or doc.get("detail") or doc)
        except ValueError:
            pass
        return "%d %s" % (e.code, text[:300] or e.reason)

    def get_json(self, path, cap=INFO_CAP):
        _, data = self.request("GET", path, cap=cap, accept="application/json")
        return _json(data, self.name, path)

    def post_json(self, path, doc, cap=INFO_CAP):
        body = json.dumps(doc).encode("utf-8")
        _, data = self.request(
            "POST", path, body, "application/json", cap=cap, accept="application/json"
        )
        return _json(data, self.name, path)


def _json(data, name, path):
    try:
        return json.loads(data)
    except ValueError as e:
        raise UpscalerError("%s: %s answered no JSON: %s" % (name, path, e)) from e


def _ints(v):
    return isinstance(v, list) and all(type(x) is int and x > 0 for x in v)


class Service(Upscaler):
    """The xbr contract's client, and the base of the adapters. What it can do
    is the service's own word, read once from `info()` when first asked; until
    then nothing goes on the network, so listing upscalers costs no request."""

    kind = "http"
    api = "xbr"
    deterministic = False
    reproducible_scope = "none"

    def __init__(self, name, table, transport):
        self.name = name
        self.table = table
        self.t = transport
        self.model = table.get("model", "")
        self.max_side = 0
        self._info = None
        self._scales = tuple(table.get("scales") or ())
        self._alpha = "rgb"
        self._batch = table.get("batch") or 0
        self.params = (
            Param("model", "str", self.model, None, None, "the service's model, from its info"),
        )

    @property
    def warning(self):
        return self.t.warning

    @property
    def scales(self):
        self.info()
        return self._scales

    @property
    def alpha(self):
        self.info()
        return self._alpha

    @property
    def batch(self):
        return self._batch or (self._info or {}).get("batch") or 8

    def info(self):
        if self._info is None:
            self._info = self._fetch_info()
            self._choose_model(self._info)
        return self._info

    def _fetch_info(self):
        doc = self.t.get_json("/v1/info")
        if not isinstance(doc, dict) or not _ints(doc.get("scales")):
            raise UpscalerError(
                "%s: /v1/info has no scales: not an xbr upscale service" % self.name
            )
        # A newer contract may change what an answer means: refused, not guessed.
        contract = doc.get("contract", CONTRACT)
        if type(contract) is not int or contract > CONTRACT:
            raise UpscalerError(
                "%s: the service speaks contract %s; this CLI speaks %d"
                % (self.name, contract, CONTRACT)
            )
        models = doc.get("models", [])
        if not isinstance(models, list) or not all(
            isinstance(m, dict) and isinstance(m.get("name"), str) for m in models
        ):
            raise UpscalerError("%s: /v1/info: models is not a list of named models" % self.name)
        return doc

    def _choose_model(self, info):
        """The model to ask for: the local file's, if the service has it; the
        service's only one; none if it lists none. Scales and alpha follow it."""
        models = {m["name"]: m for m in info.get("models", [])}
        if self.model and self.model not in models:
            raise UpscalerError(
                "%s: the service has no model %s (it has %s)"
                % (self.name, self.model, ", ".join(sorted(models)[:20]) or "none")
            )
        if not self.model and len(models) > 1:
            raise UpscalerError(
                "%s: the service has %d models: model = one of %s, in the local file"
                % (self.name, len(models), ", ".join(sorted(models)[:20]))
            )
        if not self.model and models:
            self.model = next(iter(models))
        row = models.get(self.model, {})
        scales = row.get("scales") if _ints(row.get("scales")) else info["scales"]
        # A table's scales narrow the service's, never widen them.
        if self._scales:
            scales = [s for s in scales if s in self._scales]
        self._scales = tuple(scales)
        self._alpha = "rgba" if row.get("alpha", info.get("alpha")) == "rgba" else "rgb"
        side = info.get("max_side", 0)
        self.max_side = side if type(side) is int and side > 0 else 0

    def describe(self):
        # No request: a form drawn from describe() must not reach the network.
        out = {
            "name": self.name,
            "kind": self.kind,
            "api": self.api,
            "scales": list(self._scales),
            "alpha": self._alpha,
            "deterministic": False,
            "batch": self._batch,
            "licence": "",
            "homepage": "",
            "flag": "",
            "params": [p.as_dict() for p in self.params],
        }
        return out

    def identity(self):
        """The service's name and version and the model: never its URL."""
        info = self.info()
        out = {
            "backend": self.kind,
            "name": self.name,
            "api": self.api,
            "service": str(info.get("name", ""))[:80],
            "service_version": str(info.get("version", ""))[:40],
            "model": self.model,
            "reproducible": False,
        }
        # What `enhance serve` ran behind it: a tool's or a model file's hash.
        # Capped: the peer's words go into the pack's record.
        if isinstance(info.get("identity"), dict):
            out["served"] = {
                str(k)[:40]: v[:80] if isinstance(v, str) else v
                for k, v in list(info["identity"].items())[:32]
                if isinstance(v, (str, int, bool)) and k not in ("path", "url")
            }
        return out

    def input_key(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True).encode()).hexdigest()

    def _check_request(self, w, h, scale):
        if scale not in self.scales:
            raise UpscalerError(
                "%s: %dx is not among the service's scales (%s)"
                % (self.name, scale, ", ".join(str(s) for s in self.scales) or "none")
            )
        if self.max_side and max(w, h) > self.max_side:
            raise UpscalerError(
                "%s: a %dx%d image is over the service's max_side %d"
                % (self.name, w, h, self.max_side)
            )

    def upscale(self, images, scale, params=None):
        out = []
        for w, h, rgba in images:
            self._check_request(w, h, scale)
            out.append((w * scale, h * scale, self.upscale_one(w, h, rgba, scale, params)))
        return out

    def upscale_one(self, w, h, rgba, scale, params):
        q = {"scale": scale}
        if self.model:
            q["model"] = self.model
        body = png.encode(w, h, rgba, alpha=self.alpha == "rgba")
        ctype, data = self.t.request(
            "POST",
            "/v1/upscale?" + urllib.parse.urlencode(q),
            body,
            "image/png",
            cap=png_cap(w * scale, h * scale),
            accept="image/png",
        )
        return check_png(data, w * scale, h * scale, self.name)[2]


class A1111(Service):
    """AUTOMATIC1111's web UI started with `--api`: its upscalers by name, one
    image per request, base64 both ways. It upscales by any factor, so the
    scales are the classes' own; it drops alpha."""

    api = "a1111"
    NOT_MODELS = ("None",)

    def _fetch_info(self):
        doc = self.t.get_json("/sdapi/v1/upscalers")
        if not isinstance(doc, list):
            raise UpscalerError(
                "%s: /sdapi/v1/upscalers is not a list: not an --api A1111" % self.name
            )
        names = [
            d["name"]
            for d in doc
            if isinstance(d, dict) and isinstance(d.get("name"), str)
            if d["name"] not in self.NOT_MODELS
        ]
        return {
            "name": "a1111",
            "version": "",
            "scales": [2, 3, 4],
            "models": [{"name": n, "scales": [2, 3, 4], "alpha": "rgb"} for n in names],
            "max_side": 0,
            "batch": 1,
        }

    # A1111 sizes its answer int(side * scale // 8 * 8): a side whose scaled
    # size is not a multiple of 8 comes back short.
    ALIGN = 8

    def upscale_one(self, w, h, rgba, scale, params):
        """The image padded right and down, by repeating its last column and
        row, until both scaled sides are multiples of 8; the answer is cropped
        back. The repeated texels are clamped edge, as the build's own pad."""
        pw, ph = _aligned(w, scale, self.ALIGN), _aligned(h, scale, self.ALIGN)
        sent = _pad_clamp(w, h, rgba, pw, ph)
        out = self._ask(pw, ph, sent, scale)
        return _crop(pw * scale, out, w * scale, h * scale)

    def _ask(self, w, h, rgba, scale):
        doc = self.t.post_json(
            "/sdapi/v1/extra-single-image",
            {
                "image": base64.b64encode(png.encode(w, h, rgba, alpha=False)).decode("ascii"),
                "resize_mode": 0,
                "upscaling_resize": scale,
                "upscaler_1": self.model,
            },
            # Base64 is 4 bytes for 3, plus the JSON around it.
            cap=png_cap(w * scale, h * scale) * 4 // 3 + ERROR_CAP,
        )
        b64 = doc.get("image") if isinstance(doc, dict) else None
        if not isinstance(b64, str) or not b64:
            raise UpscalerError("%s: the answer has no image" % self.name)
        if b64.startswith("data:"):
            b64 = b64.partition(",")[2]
        try:
            data = base64.b64decode(b64, validate=True)
        except ValueError as e:
            raise UpscalerError("%s: the image is not base64: %s" % (self.name, e)) from e
        return check_png(data, w * scale, h * scale, self.name)[2]


def _aligned(n, scale, align):
    """The least side >= n whose scaled size is a multiple of align."""
    while n * scale % align:
        n += 1
    return n


def _pad_clamp(w, h, rgba, pw, ph):
    if (pw, ph) == (w, h):
        return rgba
    rows = []
    for y in range(ph):
        row = rgba[min(y, h - 1) * w * 4 : (min(y, h - 1) + 1) * w * 4]
        rows.append(row + row[-4:] * (pw - w))
    return b"".join(rows)


def _crop(stride_w, rgba, w, h):
    if stride_w == w and len(rgba) == w * h * 4:
        return rgba
    return b"".join(rgba[y * stride_w * 4 : (y * stride_w + w) * 4] for y in range(h))


def from_config(name, config, local=None, sleep=time.sleep):
    """The service an [upscaler.NAME] table with a `url` (or an `api`) names."""
    tracked, own = config.get(name, {}), (local or {}).get(name, {})
    table = dict(tracked)
    table.update(own)
    api = table.get("api", "xbr")
    if api not in APIS:
        raise UpscalerError("[upscaler.%s] api = %r: one of %s" % (name, api, ", ".join(APIS)))
    t = Transport(name, table, sleep)
    if api == "a1111":
        return A1111(name, table, t)
    if api == "comfyui":
        from . import comfyui

        return comfyui.ComfyUI(name, table, t, sleep)
    return Service(name, table, t)
