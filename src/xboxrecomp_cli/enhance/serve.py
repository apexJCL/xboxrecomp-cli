"""xbr enhance serve: the reference server of the xbr upscale service contract
v1 (D7, docs/upscale-service.md), over any upscaler this CLI can run.

It is how the machine that builds a pack uses another machine's GPU: run it
there over the torch plugin or an ncnn tool, and point an [upscaler.NAME]
table's url at it. The network surface is here and nowhere else (the torch
plugin never listens), so its hardening is in one place:

- bound to 127.0.0.1 unless --host says otherwise, and off loopback it will
  not start without a token (--token-env), compared in constant time;
- the request body capped before it is read, and the PNG's header checked
  against the side and texel caps before it is inflated;
- `model` and `scale` held to its own info;
- one upscale at a time, with a timeout: a stuck backend answers 504, and a
  torch worker that hangs is killed and started again at the next request;
  a built-in that cannot be killed makes the next requests 503 until it ends;
- a Host header that is not this machine's refused when there is no token
  (a web page's DNS rebinding reaches 127.0.0.1 too);
- a read timeout on every connection, a deadline on a body, and a cap on
  connections at once.
"""

import concurrent.futures
import hashlib
import hmac
import http.server
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.parse

from .. import __version__, host
from ..host import CliError
from . import edges, png, textures_cfg
from .upscalers import Upscaler, UpscalerError, by_name
from .upscalers.http import CONTRACT, clean_token, is_loopback, png_cap

DEFAULT_PORT = 8711
DEFAULT_BACKEND = "xbr"
# A source texture's largest side and the pad around it (D7's edges): what a
# pack build ever sends. Its 4x stays within png.MAX_TEXELS.
MAX_SIDE = 4096 + 2 * edges.PAD
# An RGBA image of MAX_SIDE squared, stored, and room: a PNG over it is not a
# texture.
MAX_BODY = png_cap(MAX_SIDE, MAX_SIDE)
BACKEND_TIMEOUT = 600  # seconds for one image
# A command backend's own timeout stays under BACKEND_TIMEOUT, so it fails
# (502) and frees the GPU rather than running on after its 504.
COMMAND_MARGIN = 10
READ_TIMEOUT = 60  # seconds a connection may stay silent
BODY_DEADLINE = 300  # seconds to send a whole body: a trickle is cut
MAX_CONNECTIONS = 16
CHUNK = 1 << 16


class Busy(Exception):
    """A timed-out call that could not be killed still runs."""


class Worker(Upscaler):
    """The torch plugin as one long-lived `worker` child (D7.4): its model is
    loaded once, and each image is a frame on its stdin and one on its stdout
    (plugins/torch/README.md). No socket: the plugin never listens."""

    kind = "command"
    alpha = "rgb"
    deterministic = False

    def __init__(self, cmd):
        self.cmd = cmd
        self.name = cmd.name
        self.table = cmd.table
        self.model = self.table.get("model", "")
        if not self.model:
            raise UpscalerError(
                "%s: model = the model file, in [upscaler.%s] of the local file"
                % (self.name, self.name)
            )
        self.proc = None
        self.report = {}
        self.lock = threading.Lock()
        self.scales = ()

    def start(self):
        """The child, and its model loaded; the contract is checked first."""
        self.cmd.identity()
        self.proc = subprocess.Popen(
            [self.cmd.path, "worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        head, _ = self._ask(
            {
                "op": "load",
                "model": self.model,
                "device": self.table.get("device", "auto"),
                "tile": self.table.get("tile", 0),
                "expected_sha256": self.table.get("sha256", ""),
                "allow_model_mismatch": self.table.get("allow_model_mismatch", False),
            }
        )
        self.report = head
        native = head.get("native_scale")
        if type(native) is not int or native < 1:
            raise UpscalerError("%s: the worker reported no native scale" % self.name)
        self.scales = (native,)
        self.reproducible_scope = head.get("reproducible_scope", "none")

    def _ask(self, head, body=b""):
        raw = json.dumps(dict(head, size=len(body))).encode("utf-8")
        try:
            self.proc.stdin.write(struct.pack("<I", len(raw)) + raw + body)
            self.proc.stdin.flush()
            n = struct.unpack("<I", self._exact(4))[0]
            if n > 1 << 20:
                raise UpscalerError("%s: a worker frame header of %d bytes" % (self.name, n))
            answer = json.loads(self._exact(n))
            size = answer.get("size", 0)
            if type(size) is not int or not 0 <= size <= MAX_BODY * 16:
                raise UpscalerError("%s: a worker frame of %r bytes" % (self.name, size))
            out = self._exact(size) if size else b""
        except (OSError, ValueError, struct.error) as e:
            self.stop()
            raise UpscalerError("%s: the worker stopped: %s" % (self.name, e)) from e
        if not answer.get("ok"):
            raise UpscalerError("%s: %s" % (self.name, answer.get("error", "the worker failed")))
        return answer, out

    def _exact(self, n):
        data = self.proc.stdout.read(n)
        if len(data) != n:
            raise OSError("the worker closed its output")
        return data

    def upscale_one(self, w, h, rgba, scale, params):
        with self.lock:
            if self.proc is None:
                self.start()
            _, body = self._ask(
                {"op": "upscale", "id": 0, "scale": scale}, png.encode(w, h, rgba, alpha=False)
            )
        try:
            pw, ph, out = png.decode(body)
        except png.PngError as e:
            raise UpscalerError("%s: the worker's PNG: %s" % (self.name, e)) from e
        if (pw, ph) != (w * scale, h * scale):
            raise UpscalerError("%s: the worker made %dx%d" % (self.name, pw, ph))
        return out

    def identity(self):
        out = dict(self.cmd.identity())
        out.update(
            {k: self.report[k] for k in ("arch", "native_scale", "model_sha256", "device", "dtype")}
            if all(k in self.report for k in ("arch", "native_scale", "model_sha256"))
            else {}
        )
        return out

    def abort(self):
        """A stuck child is killed; the next request starts another."""
        p, self.proc = self.proc, None
        if p is not None:
            p.kill()
            p.wait(timeout=10)

    def stop(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.poll() is None:
                raw = json.dumps({"op": "quit", "size": 0}).encode("utf-8")
                p.stdin.write(struct.pack("<I", len(raw)) + raw)
                p.stdin.flush()
            p.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()
            p.wait(timeout=10)


def backend_of(name, tables):
    """The upscaler `serve` runs: a built-in, a preset, an [upscaler.NAME]; a
    plugin is driven as a worker, never a process per request."""
    try:
        up = by_name(name, tables)
        if getattr(up, "is_plugin", False):
            up = Worker(up)
            up.start()
        else:
            up.identity()
    except UpscalerError as e:
        raise CliError(str(e)) from e
    if up.kind == "http":
        raise CliError("%s is a service itself: serve what runs here" % name)
    return up


def info_of(up, table=None):
    model = os.path.basename(getattr(up, "model", "") or "") or up.name
    ident = {k: v for k, v in up.identity().items() if isinstance(v, (str, int, bool))}
    # The knobs set here (a built-in's level, a tool's tile) change what comes
    # back: their hash is in the identity, so a client's pack goes stale with
    # them. Local keys (paths, devices) stay out.
    knobs = {k: v for k, v in (table or {}).items() if k not in textures_cfg.LOCAL_KEYS}
    if knobs:
        raw = json.dumps(knobs, sort_keys=True, default=str).encode("utf-8")
        ident["params_sha256"] = hashlib.sha256(raw).hexdigest()
    return {
        "name": "xbr enhance serve",
        "version": __version__,
        "contract": CONTRACT,
        "scales": list(up.scales),
        "models": [{"name": model, "scales": list(up.scales), "alpha": up.alpha}],
        "max_side": MAX_SIDE,
        "batch": max(1, up.batch),
        "identity": ident,
    }


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        addr,
        backend,
        token="",
        max_body=MAX_BODY,
        max_side=MAX_SIDE,
        timeout=BACKEND_TIMEOUT,
        table=None,
    ):
        self.backend = backend
        self.token = token
        self.max_body = max_body
        self.max_side = max_side
        self.backend_timeout = timeout
        self.table = table or {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.stuck = None
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        host_ = addr[0].strip("[]")
        if ":" in host_:
            self.address_family = socket.AF_INET6
        self.bound = host_.lower()
        super().__init__((host_,) + tuple(addr[1:]), Handler)
        self.info = info_of(backend, self.table)
        self.models = {m["name"] for m in self.info["models"]}

    def process_request(self, request, client_address):
        # Over the cap, a connection is closed unread: idle sockets cannot
        # hold every thread.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def run_backend(self, w, h, rgba, scale):
        """The backend's result, or None when it took over the timeout. One at
        a time: a worker's frames must not interleave, and one GPU is one.
        Busy while a timed-out call that could not be killed still runs."""
        if self.stuck is not None:
            if not self.stuck.done():
                raise Busy()
            self.stuck = None
        fut = self.pool.submit(self.backend.upscale, [(w, h, rgba)], scale, self.table)
        try:
            return fut.result(timeout=self.backend_timeout)[0][2]
        except concurrent.futures.TimeoutError:
            abort = getattr(self.backend, "abort", None)
            if abort:
                abort()
            else:
                self.stuck = fut
            # The stuck call keeps its thread; the next request gets a new one.
            self.pool.shutdown(wait=False)
            self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            return None

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=False)
        stop = getattr(self.backend, "stop", None)
        if stop:
            stop()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "xbr-enhance-serve/%d" % CONTRACT
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = READ_TIMEOUT

    def log_message(self, fmt, *args):
        host.say("serve: %s %s" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if code >= 400:
            # What was not read of a refused body must not read as a request.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, why, extra=None):
        self._send(code, json.dumps({"error": why}).encode("utf-8"), extra=extra)

    def _host_ok(self):
        """Without a token, only this machine's names: a page whose domain
        resolves to 127.0.0.1 sends its own name as Host."""
        if self.server.token:
            return True
        name = urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        if is_loopback(name) or name == self.server.bound:
            return True
        self._error(421, "this server answers to this machine's names only")
        return False

    def _authorized(self):
        if not self._host_ok():
            return False
        token = self.server.token
        if not token:
            return True
        got = self.headers.get("Authorization", "")
        scheme, _, value = got.partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode("utf-8"), token.encode("utf-8")
        )
        if not ok:
            self._error(401, "a bearer token is required", {"WWW-Authenticate": "Bearer"})
        return ok

    def do_GET(self):
        if not self._authorized():
            return
        if urllib.parse.urlsplit(self.path).path != "/v1/info":
            return self._error(404, "GET /v1/info, POST /v1/upscale")
        self._send(200, json.dumps(self.server.info).encode("utf-8"))

    def do_POST(self):
        if not self._authorized():
            return
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/v1/upscale":
            return self._error(404, "GET /v1/info, POST /v1/upscale")
        n = self.headers.get("Content-Length", "")
        if not (n.isascii() and n.isdigit()):
            return self._error(411, "a Content-Length is required")
        if int(n) > self.server.max_body:
            return self._error(413, "the body is over %d bytes" % self.server.max_body)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "image/png":
            return self._error(415, "the body is image/png")
        q = urllib.parse.parse_qs(u.query)
        scale = q.get("scale", [""])[0]
        if (
            not (scale.isascii() and scale.isdigit())
            or int(scale) not in self.server.info["scales"]
        ):
            return self._error(
                400, "scale: one of %s" % ", ".join(str(s) for s in self.server.info["scales"])
            )
        scale = int(scale)
        model = q.get("model", [""])[0]
        if model and model not in self.server.models:
            return self._error(400, "model: one of %s" % ", ".join(sorted(self.server.models)))
        body = self._body(int(n))
        if body is None:
            return
        try:
            w, h = png.header(body)
        except png.PngError as e:
            return self._error(400, "not a PNG: %s" % e)
        side = self.server.max_side
        if w > side or h > side or w * scale * h * scale > png.MAX_TEXELS:
            return self._error(413, "%dx%d: the sides are capped at %d" % (w, h, side))
        try:
            w, h, rgba = png.decode(body)
        except png.PngError as e:
            return self._error(400, "a broken PNG: %s" % e)
        try:
            out = self.server.run_backend(w, h, rgba, scale)
        except UpscalerError as e:
            return self._error(502, str(e))
        except Busy:
            return self._error(
                503, "a call that timed out still runs", {"Retry-After": str(READ_TIMEOUT)}
            )
        if out is None:
            return self._error(504, "the backend took over %d s" % self.server.backend_timeout)
        alpha = self.server.backend.alpha == "rgba"
        self._send(200, png.encode(w * scale, h * scale, out, alpha=alpha), "image/png")

    def _body(self, n):
        """The body, read in chunks against a deadline; None, and the
        connection closed, when it comes too slowly or is cut."""
        deadline = time.monotonic() + BODY_DEADLINE
        parts, got = [], 0
        while got < n:
            if time.monotonic() > deadline:
                self._error(408, "the body took over %d s" % BODY_DEADLINE)
                return None
            try:
                chunk = self.rfile.read1(min(CHUNK, n - got))
            except (TimeoutError, OSError):
                chunk = b""
            if not chunk:
                self.close_connection = True
                return None
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)


def configure(p):
    p.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        metavar="NAME",
        help="the upscaler to serve: a built-in, a preset or an [upscaler.NAME] of the config "
        "(default %s; torch runs as one long-lived worker)" % DEFAULT_BACKEND,
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="default %d" % DEFAULT_PORT)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="the address to listen on (default 127.0.0.1; any other needs --token-env)",
    )
    p.add_argument(
        "--token-env",
        metavar="VAR",
        help="the environment variable holding the token clients must send",
    )
    p.add_argument(
        "--config",
        help="the config whose [upscaler] tables apply (default config/enhance-textures.toml, "
        "if there, with its local file)",
    )


def _tables(a):
    path = a.config
    if not path:
        try:
            root = host.g().root
        except Exception:  # noqa: BLE001
            root = os.getcwd()
        path = os.path.join(root, "config", "enhance-textures.toml")
        if not os.path.exists(path):
            return {}
    try:
        return textures_cfg.load(path).params
    except textures_cfg.ConfigError as e:
        raise CliError(str(e)) from e


def token_of(a):
    """The token, from the variable --token-env names: required off loopback."""
    if not a.token_env:
        if not is_loopback(a.host):
            raise CliError(
                "--host %s is reachable from other machines: --token-env VAR, with VAR holding "
                "the token clients send, or --host 127.0.0.1" % a.host
            )
        return ""
    try:
        token = clean_token(os.environ.get(a.token_env, ""), a.token_env, "--token-env")
    except UpscalerError as e:
        raise CliError(str(e)) from e
    if not token:
        raise CliError("--token-env %s: the variable is not set" % a.token_env)
    return token


def make(a):
    """The Server `run` serves, from the parsed arguments."""
    token = token_of(a)
    if not 0 <= a.port <= 65535:
        raise CliError("--port: 0 to 65535")
    tables = _tables(a)
    up = backend_of(a.backend, tables)
    table = tables.get(a.backend, {})
    if hasattr(up, "timeout") and not isinstance(up, Worker):
        up.timeout = min(up.timeout, BACKEND_TIMEOUT - COMMAND_MARGIN)
    if up.kind == "builtin":
        # A built-in's knobs only; the rest of a table is the build's.
        table = {k: v for k, v in table.items() if k in {p.name for p in up.params}}
    try:
        return Server((a.host, a.port), up, token, table=table)
    except (OSError, socket.gaierror) as e:
        stop = getattr(up, "stop", None)
        if stop:
            stop()
        raise CliError("%s:%d: %s" % (a.host, a.port, e)) from e


def run(a):
    srv = make(a)
    addr = srv.server_address
    shown = "[%s]" % addr[0] if ":" in addr[0] else addr[0]
    host.say(
        "serving %s (%s) on http://%s:%d, Ctrl-C stops"
        % (a.backend, ", ".join("%dx" % s for s in srv.info["scales"]), shown, addr[1])
    )
    if not srv.token:
        host.say("no token: only this machine can reach it")
    host.say("the textures it is sent are the owner's: keep the service private")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
