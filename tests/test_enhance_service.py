"""The service backends (texture-upscale phase 4): the xbr upscale service
contract's client, the a1111 and comfyui adapters, and `enhance serve`, all
against servers on 127.0.0.1 in this process. Nothing reaches another host."""

import argparse
import base64
import email.parser
import http.client
import http.server
import json
import os
import random
import struct
import sys
import threading
import urllib.parse

import pytest

from xboxrecomp_cli.enhance import build, png, serve, textures_cfg
from xboxrecomp_cli.enhance.upscalers import UpscalerError, by_name, command, get
from xboxrecomp_cli.enhance.upscalers import http as service
from xboxrecomp_cli.host import CliError

# The fixture's own decode: a test watches the client's through png.decode.
DECODE = png.decode


def noise(w, h, seed=1):
    return bytes(random.Random(seed).randbytes(w * h * 4))


def nearest(w, h, rgba, s):
    return get("nearest").upscale([(w, h, rgba)], s)[0][2]


class Fixture(http.server.ThreadingHTTPServer):
    """One server speaking the xbr contract, AUTOMATIC1111's API and ComfyUI's,
    with what each route answers set by the test through `state`."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.state = {
            "info": {
                "name": "fixture",
                "version": "1.2",
                "scales": [2, 4],
                "models": [{"name": "m", "scales": [2, 4], "alpha": "rgba"}],
                "max_side": 512,
                "batch": 4,
            },
            "fail": [],  # statuses to answer first, one per request
            "mode": "ok",
            "seen": [],
            "a1111": [{"name": "None"}, {"name": "Lanczos"}, {"name": "R-ESRGAN 4x+"}],
            "uploads": {},
            "prompts": {},
            "polls": 0,
            "comfy_models": ["4x-UltraSharp.pth", "2x-Compact.safetensors"],
        }

    def handle_error(self, request, client_address):
        pass  # a client that hangs up on an oversize answer, as it should

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, doc, code=200):
        self._send(code, json.dumps(doc).encode())

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _note(self, method):
        st = self.server.state
        st["seen"].append((method, self.path, self.headers.get("Authorization")))
        if st["fail"]:
            code = st["fail"].pop(0)
            self._body() if method == "POST" else None
            if code == 302:
                self._send(302, b"", extra={"Location": "http://192.0.2.1/v1/info"})
            else:
                self._json({"error": "busy %d" % code}, code)
            return True
        return False

    def do_GET(self):
        if self._note("GET"):
            return
        st = self.server.state
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/v1/info":
            return self._json(st["info"])
        if u.path == "/sdapi/v1/upscalers":
            return self._json(st["a1111"])
        if u.path == "/system_stats":
            return self._json({"system": {"comfyui_version": "0.3.9"}})
        if u.path == "/object_info/UpscaleModelLoader":
            return self._json(
                {
                    "UpscaleModelLoader": {
                        "input": {
                            "required": {
                                "model_name": ["COMBO", {"options": st["comfy_models"]}]
                                if st["mode"] == "combo-v3"
                                else [st["comfy_models"]]
                            }
                        }
                    }
                }
            )
        if u.path.startswith("/history/"):
            pid = u.path.rsplit("/", 1)[1]
            st["polls"] += 1
            if st["polls"] == 1:
                return self._json({})  # queued, not run yet
            p = st["prompts"][pid]
            if st["mode"] == "comfy-error":
                return self._json(
                    {
                        pid: {
                            "outputs": {},
                            "status": {
                                "status_str": "error",
                                "messages": [["execution_error", {"exception_message": "OOM"}]],
                            },
                        }
                    }
                )
            prefix = p["4"]["inputs"]["filename_prefix"]
            return self._json(
                {
                    pid: {
                        "outputs": {
                            "4": {
                                "images": [
                                    {
                                        "filename": prefix + "_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                        "status": {"status_str": "success", "completed": True},
                    }
                }
            )
        if u.path == "/view":
            q = urllib.parse.parse_qs(u.query)
            assert q["type"] == ["output"]
            prefix = q["filename"][0].rsplit("_00001_.png", 1)[0]
            p = next(
                p for p in st["prompts"].values() if p["4"]["inputs"]["filename_prefix"] == prefix
            )
            w, h, rgba = DECODE(st["uploads"][p["1"]["inputs"]["image"]])
            return self._send(200, png.encode(w * 2, h * 2, nearest(w, h, rgba, 2)), "image/png")
        self._json({"error": "no route"}, 404)

    def do_POST(self):
        if self._note("POST"):
            return
        st = self.server.state
        u = urllib.parse.urlsplit(self.path)
        body = self._body()
        if u.path == "/v1/upscale":
            q = urllib.parse.parse_qs(u.query)
            st["last_query"] = q
            s = int(q["scale"][0])
            w, h, rgba = DECODE(body)
            if st["mode"] == "slow":
                import time

                time.sleep(1)
            if st["mode"] == "big":
                return self._send(200, b"\0" * (3 << 20), "image/png")
            if st["mode"] == "wrong-size":
                s += 1
            return self._send(200, png.encode(w * s, h * s, nearest(w, h, rgba, s)), "image/png")
        if u.path == "/sdapi/v1/extra-single-image":
            doc = json.loads(body)
            st["last_a1111"] = {k: v for k, v in doc.items() if k != "image"}
            w, h, rgba = DECODE(base64.b64decode(doc["image"]))
            s = doc["upscaling_resize"]
            st["a1111_sent"] = (w, h)
            # A1111's own sizing: int(side * scale // 8 * 8), the rest cut off.
            ow, oh = w * s // 8 * 8, h * s // 8 * 8
            big = nearest(w, h, rgba, s)
            cut = b"".join(big[y * w * s * 4 : (y * w * s + ow) * 4] for y in range(oh))
            out = base64.b64encode(png.encode(ow, oh, cut, alpha=False))
            return self._json({"image": "data:image/png;base64," + out.decode(), "html_info": ""})
        if u.path == "/upload/image":
            msg = email.parser.BytesParser().parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
            )
            fields = {
                p.get_param("name", header="content-disposition"): p for p in msg.get_payload()
            }
            f = fields["image"]
            name = f.get_filename()
            assert fields["type"].get_payload() == "input"
            st["uploads"][name] = f.get_payload(decode=True)
            return self._json({"name": name, "subfolder": "", "type": "input"})
        if u.path == "/prompt":
            doc = json.loads(body)
            pid = "p%d" % len(st["prompts"])
            st["prompts"][pid] = doc["prompt"]
            return self._json({"prompt_id": pid, "number": 1, "node_errors": {}})
        self._json({"error": "no route"}, 404)


@pytest.fixture
def fx():
    srv = Fixture()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def client(fx, sleeps=None, **table):
    table.setdefault("url", fx.url)
    rec = sleeps if sleeps is not None else []
    return service.from_config("remote", {"remote": table}, sleep=rec.append)


# --- the xbr contract's client ---------------------------------------------


def test_contract_round_trip_and_the_token(fx, monkeypatch):
    monkeypatch.setenv("XBR_TEST_TOKEN", "s3cret")
    up = client(fx, token_env="XBR_TEST_TOKEN")
    rgba = noise(5, 3)
    assert up.scales == (2, 4) and up.alpha == "rgba" and up.batch == 4
    [(w, h, out)] = up.upscale([(5, 3, rgba)], 2)
    assert (w, h) == (10, 6) and out == nearest(5, 3, rgba, 2)
    assert fx.state["last_query"] == {"scale": ["2"], "model": ["m"]}
    assert {a for _, _, a in fx.state["seen"]} == {"Bearer s3cret"}
    ident = up.identity()
    assert ident["service"] == "fixture" and ident["model"] == "m"
    assert fx.url not in json.dumps(ident) and "s3cret" not in json.dumps(ident)
    # The token is the environment's at each request, never kept.
    assert "s3cret" not in json.dumps(vars(up), default=str)


def test_listing_a_service_costs_no_request(fx):
    up = client(fx)
    assert up.describe()["kind"] == "http"
    assert fx.state["seen"] == []


def test_unknown_model_and_scale_are_refused_before_a_request(fx):
    with pytest.raises(UpscalerError, match="no model nope"):
        client(fx, model="nope").upscale([(2, 2, noise(2, 2))], 2)
    up = client(fx)
    with pytest.raises(UpscalerError, match="3x is not among"):
        up.upscale([(2, 2, noise(2, 2))], 3)
    with pytest.raises(UpscalerError, match="max_side 512"):
        up.upscale([(513, 1, noise(513, 1))], 2)
    assert not any(m == "POST" for m, _, _ in fx.state["seen"])
    fx.state["info"]["models"].append({"name": "n", "scales": [4], "alpha": "rgb"})
    with pytest.raises(UpscalerError, match="2 models"):
        client(fx).info()
    up = client(fx, model="n")
    assert up.scales == (4,) and up.alpha == "rgb"


def test_a_newer_contract_is_refused(fx):
    fx.state["info"]["contract"] = service.CONTRACT + 1
    with pytest.raises(UpscalerError, match="speaks contract 2"):
        client(fx).info()


def test_the_table_scales_narrow_the_service(fx):
    assert client(fx, scales=[4, 3]).scales == (4,)


def test_an_oversize_answer_is_refused(fx):
    fx.state["mode"] = "big"
    with pytest.raises(UpscalerError, match="over"):
        client(fx).upscale([(2, 2, noise(2, 2))], 2)


def test_a_wrong_ihdr_is_refused_before_decoding(fx, monkeypatch):
    fx.state["mode"] = "wrong-size"
    monkeypatch.setattr(png, "decode", lambda data: pytest.fail("decoded"))
    with pytest.raises(UpscalerError, match="answered 6x6 where 4x4 was asked"):
        client(fx).upscale([(2, 2, noise(2, 2))], 2)


def test_busy_answers_are_retried_and_a_refusal_is_not(fx):
    sleeps = []
    fx.state["fail"] = [503, 429]
    assert client(fx, sleeps).info()["name"] == "fixture"
    assert sleeps == [service.BACKOFF, service.BACKOFF * 2]
    fx.state["fail"] = [400]
    with pytest.raises(UpscalerError, match="400 busy 400"):
        client(fx, sleeps).info()
    fx.state["fail"] = [503] * (service.RETRIES + 1)
    with pytest.raises(UpscalerError, match="failed 4 times"):
        client(fx, []).info()


def test_a_redirect_is_not_followed(fx, monkeypatch):
    monkeypatch.setenv("XBR_TEST_TOKEN", "s3cret")
    fx.state["fail"] = [302]
    with pytest.raises(UpscalerError, match="redirects to http://192.0.2.1"):
        client(fx, token_env="XBR_TEST_TOKEN").info()


def test_a_connection_refused_is_retried_then_said(fx):
    port = fx.server_address[1]
    fx.shutdown()
    fx.server_close()
    sleeps = []
    up = service.from_config(
        "remote", {"remote": {"url": "http://127.0.0.1:%d" % port}}, sleep=sleeps.append
    )
    with pytest.raises(UpscalerError, match="failed 4 times"):
        up.info()
    assert len(sleeps) == service.RETRIES


def test_the_token_never_goes_over_plain_http_off_loopback(monkeypatch):
    monkeypatch.setenv("XBR_TEST_TOKEN", "s3cret")
    remote = {"url": "http://192.0.2.7:8711", "token_env": "XBR_TEST_TOKEN"}
    with pytest.raises(UpscalerError, match="refusing to send the token over plain http"):
        service.from_config("remote", {"remote": remote})
    up = service.from_config("remote", {"remote": dict(remote, allow_plain_http=True)})
    assert "plain http" in up.warning
    # https, loopback, or no token at all: no refusal, no warning.
    for url, tok in (
        ("https://192.0.2.7:8711", "XBR_TEST_TOKEN"),
        ("http://localhost:8711", "XBR_TEST_TOKEN"),
        ("http://[::1]:8711", "XBR_TEST_TOKEN"),
        ("http://192.0.2.7:8711", ""),
    ):
        assert service.from_config("r", {"r": {"url": url, "token_env": tok}}).warning == ""


def test_urls_that_are_not_a_service_are_refused():
    for url, why in (
        ("file:///etc/passwd", "http:// or https://"),
        ("ftp://h/", "http:// or https://"),
        ("http://user:pw@127.0.0.1:1", "no user or password"),
        ("http://127.0.0.1:1/?x=1", "no query"),
        ("", "needs url"),
    ):
        with pytest.raises(UpscalerError, match=why):
            service.from_config("r", {"r": {"url": url}})
    with pytest.raises(UpscalerError, match="api"):
        service.from_config("r", {"r": {"url": "http://127.0.0.1:1", "api": "x"}})


def test_an_unset_token_variable_is_said(fx, monkeypatch):
    monkeypatch.delenv("XBR_TEST_TOKEN", raising=False)
    with pytest.raises(UpscalerError, match="XBR_TEST_TOKEN, and it is not set"):
        client(fx, token_env="XBR_TEST_TOKEN").info()


def test_loopback():
    assert service.is_loopback("127.0.0.1") and service.is_loopback("127.5.0.1")
    assert service.is_loopback("localhost") and service.is_loopback("::1")
    assert not service.is_loopback("0.0.0.0") and not service.is_loopback("198.51.100.2")
    assert not service.is_loopback("localhost.example.com")


def test_a_build_job_goes_through_a_service(fx):
    """The build's pad, alpha split and crop around a service, as for a command."""
    fx.state["info"]["models"][0]["alpha"] = "rgb"
    up = client(fx)
    rgba = noise(6, 4, 3)
    t = {"scale": 2, "alpha": "split", "edges": "clamp"}
    job = build.make_job(up, 6, 4, rgba, t, {"hash": "0"})
    uw, uh, out = job.finish(up.upscale(job.images(), job.native, {}))
    assert (uw, uh) == (12, 8) and len(out) == 12 * 8 * 4


# --- the registry and the config -----------------------------------------


def test_a_url_table_is_a_service_and_its_keys_are_local(tmp_path):
    up = by_name("remote", {"remote": {"api": "a1111"}}, {"remote": {"url": "http://127.0.0.1:1"}})
    assert up.kind == "http" and up.api == "a1111"
    cfg = tmp_path / "enhance-textures.toml"
    cfg.write_text('schema = 1\n[class.ui]\nupscaler = "remote"\n[upscaler.remote]\napi = "xbr"\n')
    (tmp_path / textures_cfg.LOCAL_NAME).write_text(
        '[upscaler.remote]\nurl = "http://127.0.0.1:8711"\ntoken_env = "T"\nmodel = "m"\n'
    )
    conf = textures_cfg.load(str(cfg))
    assert conf.params["remote"]["url"] == "http://127.0.0.1:8711"
    for key, value in (
        ("url", '"http://h"'),
        ("token_env", '"T"'),
        ("allow_plain_http", "true"),
        ("workflow", '"w.json"'),
    ):
        cfg.write_text("schema = 1\n[upscaler.remote]\n%s = %s\n" % (key, value))
        with pytest.raises(textures_cfg.ConfigError, match="belongs in"):
            textures_cfg.load(str(cfg))
    cfg.write_text('schema = 1\n[upscaler.remote]\napi = "grpc"\n')
    with pytest.raises(textures_cfg.ConfigError, match="api"):
        textures_cfg.load(str(cfg))
    # Where a service is, is not what the stage key follows: its identity is.
    st = command.stage_table({"url": "http://h", "token_env": "T", "workflow": "/w", "api": "xbr"})
    assert st == {"api": "xbr"}


# --- a1111 -----------------------------------------------------------------


def test_a1111_round_trip(fx):
    up = client(fx, api="a1111", model="Lanczos")
    rgba = noise(4, 3, 5)
    [(w, h, out)] = up.upscale([(4, 3, rgba)], 3)
    assert (w, h) == (12, 9)
    opaque = bytearray(rgba)
    opaque[3::4] = b"\xff" * 12
    assert out == nearest(4, 3, bytes(opaque), 3)
    assert fx.state["last_a1111"] == {
        "resize_mode": 0,
        "upscaling_resize": 3,
        "upscaler_1": "Lanczos",
    }
    assert up.alpha == "rgb" and up.identity()["api"] == "a1111"


def test_a1111_pads_to_its_multiple_of_8_and_crops(fx):
    up = client(fx, api="a1111", model="Lanczos")
    for w, h, s in ((1, 1, 4), (5, 3, 3), (3, 7, 2), (8, 8, 4)):
        rgba = noise(w, h, w * 7 + h)
        [(ow, oh, out)] = up.upscale([(w, h, rgba)], s)
        opaque = bytearray(rgba)
        opaque[3::4] = b"\xff" * (w * h)
        assert (ow, oh) == (w * s, h * s) and out == nearest(w, h, bytes(opaque), s)
        sw, sh = fx.state["a1111_sent"]
        assert sw * s % 8 == 0 and sh * s % 8 == 0 and sw >= w and sh >= h


def test_a_post_that_timed_out_is_not_sent_again(fx):
    fx.state["mode"] = "slow"
    sleeps = []
    up = client(fx, sleeps, timeout=0.3)
    with pytest.raises(UpscalerError, match="not sent again"):
        up.upscale([(2, 2, noise(2, 2))], 2)
    assert [m for m, _, _ in fx.state["seen"]].count("POST") == 1 and sleeps == []


def test_served_identity_values_are_capped(fx):
    fx.state["info"]["identity"] = {"backend": "x" * 500, "k" * 100: 1}
    served = client(fx).identity()["served"]
    assert served["backend"] == "x" * 80 and "k" * 40 in served


def test_a1111_refusals(fx):
    with pytest.raises(UpscalerError, match="no model Waifu"):
        client(fx, api="a1111", model="Waifu").info()
    with pytest.raises(UpscalerError, match="2 models"):
        client(fx, api="a1111").info()
    fx.state["a1111"] = {"detail": "Not Found"}
    with pytest.raises(UpscalerError, match="not a list"):
        client(fx, api="a1111", model="Lanczos").info()


# --- comfyui ---------------------------------------------------------------

WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "old.pth"}},
    "3": {
        "class_type": "ImageUpscaleWithModel",
        "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]},
    },
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "x"}},
}


def workflow(tmp_path, wf=None):
    p = tmp_path / ("wf-%d.json" % len(list(tmp_path.glob("wf-*.json"))))
    p.write_text(json.dumps(wf or WORKFLOW))
    return str(p)


def test_comfyui_round_trip(fx, tmp_path):
    up = client(
        fx, api="comfyui", workflow=workflow(tmp_path), scales=[2], model="2x-Compact.safetensors"
    )
    rgba = noise(3, 5, 9)
    [(w, h, out)] = up.upscale([(3, 5, rgba)], 2)
    opaque = bytearray(rgba)
    opaque[3::4] = b"\xff" * 15
    assert (w, h) == (6, 10) and out == nearest(3, 5, bytes(opaque), 2)
    [prompt] = fx.state["prompts"].values()
    [name] = fx.state["uploads"]
    assert prompt["1"]["inputs"]["image"] == name
    assert prompt["2"]["inputs"]["model_name"] == "2x-Compact.safetensors"
    assert name.startswith(prompt["4"]["inputs"]["filename_prefix"])
    assert prompt["3"] == WORKFLOW["3"]
    assert fx.state["polls"] == 2
    ident = up.identity()
    assert ident["service_version"] == "0.3.9" and len(ident["workflow_sha256"]) == 64
    assert str(tmp_path) not in json.dumps(ident)


def test_comfyui_refusals(fx, tmp_path):
    wf = workflow(tmp_path)
    with pytest.raises(UpscalerError, match="scales = \\[N\\]"):
        client(fx, api="comfyui", workflow=wf)
    with pytest.raises(UpscalerError, match="needs workflow"):
        client(fx, api="comfyui", scales=[2])
    with pytest.raises(UpscalerError, match="no model evil.pth"):
        client(fx, api="comfyui", workflow=wf, scales=[2], model="evil.pth").info()
    two = dict(WORKFLOW, **{"5": {"class_type": "LoadImage", "inputs": {}}})
    with pytest.raises(UpscalerError, match="2 LoadImage nodes"):
        client(fx, api="comfyui", workflow=workflow(tmp_path, two), scales=[2])
    up = client(fx, api="comfyui", workflow=workflow(tmp_path, two), scales=[2], load_node="1")
    assert up.load_id == "1"
    with pytest.raises(UpscalerError, match="load_node = 3 is not"):
        client(fx, api="comfyui", workflow=wf, scales=[2], load_node="3")
    (tmp_path / "ui.json").write_text(json.dumps({"nodes": [], "links": []}))
    with pytest.raises(UpscalerError, match="Save \\(API\\)"):
        client(fx, api="comfyui", workflow=str(tmp_path / "ui.json"), scales=[2])
    # The table's scale is held to the answer: a 2x workflow told 4x fails.
    up = client(fx, api="comfyui", workflow=wf, scales=[4])
    with pytest.raises(UpscalerError, match="answered 4x4 where 8x8"):
        up.upscale([(2, 2, noise(2, 2))], 4)


def test_comfyui_reads_the_newer_combo_shape(fx, tmp_path):
    fx.state["mode"] = "combo-v3"
    up = client(
        fx, api="comfyui", workflow=workflow(tmp_path), scales=[2], model="4x-UltraSharp.pth"
    )
    assert up.info()["models"][0]["name"] == "4x-UltraSharp.pth"
    with pytest.raises(UpscalerError, match="no model evil.pth"):
        client(fx, api="comfyui", workflow=workflow(tmp_path), scales=[2], model="evil.pth").info()


def test_comfyui_a_failed_prompt_is_said(fx, tmp_path):
    fx.state["mode"] = "comfy-error"
    up = client(fx, api="comfyui", workflow=workflow(tmp_path), scales=[2])
    with pytest.raises(UpscalerError, match="the workflow failed: OOM"):
        up.upscale([(2, 2, noise(2, 2))], 2)


# --- enhance serve -----------------------------------------------------------


def args(**kw):
    base = {
        "backend": "nearest",
        "port": 0,
        "host": "127.0.0.1",
        "token_env": None,
        "config": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def served():
    """Start `enhance serve`'s server from its arguments; stopped after."""
    started = []

    def start(srv=None, **kw):
        srv = srv or serve.make(args(**kw))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def raw(srv, method, path, body=b"", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_serve_over_nearest_answers_info_and_a_round_trip(served):
    srv, url = served()
    up = service.from_config("remote", {"remote": {"url": url}})
    info = up.info()
    assert info["name"] == "xbr enhance serve" and info["scales"] == [2, 3, 4]
    assert info["models"] == [{"name": "nearest", "scales": [2, 3, 4], "alpha": "rgba"}]
    rgba = noise(7, 5, 2)
    [(w, h, out)] = up.upscale([(7, 5, rgba)], 3)
    assert (w, h) == (21, 15) and out == nearest(7, 5, rgba, 3)
    served = up.identity()["served"]
    assert served["backend"] == "builtin" and served["name"] == "nearest"


def test_serve_off_loopback_needs_a_token(monkeypatch):
    with pytest.raises(CliError, match="--token-env"):
        serve.make(args(host="0.0.0.0"))
    monkeypatch.delenv("XBR_TEST_TOKEN", raising=False)
    with pytest.raises(CliError, match="not set"):
        serve.make(args(token_env="XBR_TEST_TOKEN"))


def test_serve_a_wrong_token_is_401(served, monkeypatch):
    monkeypatch.setenv("XBR_TEST_TOKEN", "right")
    srv, url = served(token_env="XBR_TEST_TOKEN")
    assert raw(srv, "GET", "/v1/info")[0] == 401
    assert raw(srv, "GET", "/v1/info", headers={"Authorization": "Bearer wrong"})[0] == 401
    assert raw(srv, "GET", "/v1/info", headers={"Authorization": "Bearer right"})[0] == 200
    monkeypatch.setenv("XBR_CLIENT_TOKEN", "wrong")
    up = service.from_config("r", {"r": {"url": url, "token_env": "XBR_CLIENT_TOKEN"}})
    with pytest.raises(UpscalerError, match="401"):
        up.info()


def test_serve_caps_and_allowlists(served):
    up = get("nearest")
    srv, _ = served(serve.Server(("127.0.0.1", 0), up, max_body=4096, max_side=16))
    small = png.encode(2, 2, noise(2, 2))
    ok = {"Content-Type": "image/png"}
    assert raw(srv, "POST", "/v1/upscale?scale=2", b"\0" * 5000, ok)[0] == 413
    big = png.encode(17, 1, noise(17, 1))
    assert raw(srv, "POST", "/v1/upscale?scale=2", big, ok)[0] == 413
    assert raw(srv, "POST", "/v1/upscale?scale=5", small, ok)[0] == 400
    assert raw(srv, "POST", "/v1/upscale?scale=2&model=other", small, ok)[0] == 400
    assert raw(srv, "POST", "/v1/upscale?scale=2", b"GIF89a" + b"\0" * 40, ok)[0] == 400
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert raw(srv, "POST", "/v1/upscale?scale=2", small, form)[0] == 415
    assert raw(srv, "GET", "/v1/other")[0] == 404
    code, body = raw(srv, "POST", "/v1/upscale?scale=2&model=nearest", small, ok)
    assert code == 200 and png.decode(body)[:2] == (4, 4)


def test_serve_times_out_a_stuck_backend(served):
    gate = threading.Event()

    class Stuck(type(get("nearest"))):
        name = "stuck"

        def upscale(self, images, scale, params=None):
            gate.wait(10)
            return super().upscale(images, scale, params)

    srv, _ = served(serve.Server(("127.0.0.1", 0), Stuck(), timeout=0.2))
    small = png.encode(2, 2, noise(2, 2))
    code, body = raw(srv, "POST", "/v1/upscale?scale=2", small, {"Content-Type": "image/png"})
    assert code == 504 and b"took over" in body
    # A built-in cannot be killed: while it still runs, the GPU is not free.
    code, body = raw(srv, "POST", "/v1/upscale?scale=2", small, {"Content-Type": "image/png"})
    assert code == 503 and b"still runs" in body
    gate.set()
    srv.stuck.result(timeout=10)
    code, _ = raw(srv, "POST", "/v1/upscale?scale=2", small, {"Content-Type": "image/png"})
    assert code == 200


def test_serve_holds_a_command_under_its_own_timeout(monkeypatch):
    up = get("nearest")
    up.timeout = 3600
    monkeypatch.setattr(serve, "backend_of", lambda name, tables: up)
    srv = serve.make(args())
    try:
        assert up.timeout == serve.BACKEND_TIMEOUT - serve.COMMAND_MARGIN
    finally:
        srv.server_close()
        del up.timeout


def test_serve_checks_the_host_without_a_token(served):
    srv, _ = served()
    ok = raw(srv, "GET", "/v1/info", headers={"Host": "localhost:1"})
    assert ok[0] == 200
    code, _ = raw(srv, "GET", "/v1/info", headers={"Host": "evil.example:8711"})
    assert code == 421


def test_serve_refuses_unicode_digits(served):
    srv, _ = served()
    small = png.encode(2, 2, noise(2, 2))
    code, _ = raw(srv, "POST", "/v1/upscale?scale=%C2%B2", small, {"Content-Type": "image/png"})
    assert code == 400


def test_serve_on_ipv6_loopback(served):
    try:
        srv, url = served(host="::1")
    except CliError as e:
        pytest.skip("no IPv6 loopback here: %s" % e)
    port = srv.server_address[1]
    up = service.from_config("r", {"r": {"url": "http://[::1]:%d" % port}})
    assert up.info()["name"] == "xbr enhance serve"
    with pytest.raises(CliError, match="--token-env"):
        serve.make(args(host="::"))


def test_serve_caps_connections_and_idle_ones(served, monkeypatch):
    import socket

    monkeypatch.setattr(serve, "MAX_CONNECTIONS", 1)
    monkeypatch.setattr(serve.Handler, "timeout", 0.5)
    srv, _ = served()
    idle = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    try:
        # The one slot is the idle socket's: the next connection is closed unread.
        second = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
        second.sendall(b"GET /v1/info HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert second.recv(100) == b""
        second.close()
        # The idle one is dropped after the read timeout, and the slot is back.
        assert idle.recv(100) == b""
    finally:
        idle.close()
    deadline = __import__("time").monotonic() + 5
    while __import__("time").monotonic() < deadline:
        try:
            if raw(srv, "GET", "/v1/info")[0] == 200:
                break
        except (ConnectionError, http.client.HTTPException):
            pass
    assert raw(srv, "GET", "/v1/info")[0] == 200


def test_serve_identity_follows_the_served_knobs():
    up = get("xbr")
    a = serve.info_of(up, {})["identity"]
    b = serve.info_of(up, {"level": 1})["identity"]
    c = serve.info_of(up, {"level": 1, "path": "/opt/tools/upscaler"})["identity"]
    assert "params_sha256" not in a and b["params_sha256"] == c["params_sha256"]
    assert "/opt/tools" not in json.dumps(c)


def test_tokens_are_stripped_and_odd_ones_refused_unprinted(served, monkeypatch, capsys):
    monkeypatch.setenv("XBR_SRV", "right\r\n")
    monkeypatch.setenv("XBR_CLI", "right\n")
    srv, url = served(token_env="XBR_SRV")
    up = service.from_config("r", {"r": {"url": url, "token_env": "XBR_CLI"}})
    assert up.info()["name"] == "xbr enhance serve"
    for bad in ("ri\u00e9ght", "ri\x07ght", "r\u4e2dght"):
        monkeypatch.setenv("XBR_CLI", bad)
        up = service.from_config("r", {"r": {"url": url, "token_env": "XBR_CLI"}})
        with pytest.raises(UpscalerError) as e:
            up.info()
        assert "XBR_CLI" in str(e.value) and "ght" not in str(e.value)
        monkeypatch.setenv("XBR_SRV", bad)
        with pytest.raises(CliError) as e:
            serve.make(args(token_env="XBR_SRV"))
        assert "XBR_SRV" in str(e.value) and "ght" not in str(e.value)
    out = capsys.readouterr()
    assert "ght" not in out.out + out.err


def test_serve_refuses_to_serve_a_service(fx):
    with pytest.raises(CliError, match="a service itself"):
        serve.backend_of("remote", {"remote": {"url": fx.url}})


FAKE_WORKER = r"""#!%s
# The torch plugin's worker protocol, with nearest 2x for a model.
import json, struct, sys, zlib
if sys.argv[1:] == ["--version"]:
    print("xbr-upscale-torch 0.1.0 contract 1.0")
    sys.exit(0)
assert sys.argv[1:] == ["worker"]
sys.path.insert(0, %r)
from xboxrecomp_cli.enhance import png
i, o = sys.stdin.buffer, sys.stdout.buffer
def frame(head, body=b""):
    raw = json.dumps(dict(head, size=len(body))).encode()
    o.write(struct.pack("<I", len(raw)) + raw + body)
    o.flush()
while True:
    n = i.read(4)
    if len(n) < 4:
        break
    head = json.loads(i.read(struct.unpack("<I", n)[0]))
    body = i.read(head["size"]) if head.get("size") else b""
    if head["op"] == "quit":
        frame({"ok": True})
        break
    if head["op"] == "load":
        if head["model"].endswith("bad.pth"):
            frame({"ok": False, "error": "no such model"})
            continue
        frame({"ok": True, "id": 0, "arch": "Compact", "native_scale": 2,
               "model_sha256": "ab" * 32, "device": head["device"], "dtype": "fp32"})
    elif head["op"] == "upscale":
        w, h, rgba = png.decode(body)
        out = b"".join(
            b"".join(rgba[(y // 2 * w + x // 2) * 4:(y // 2 * w + x // 2) * 4 + 4]
                     for x in range(w * 2)) for y in range(h * 2))
        frame({"ok": True, "width": w * 2, "height": h * 2}, png.encode(w * 2, h * 2, out, alpha=False))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="a shebang script stands in for the plugin")
def test_serve_drives_the_torch_plugin_as_one_worker(tmp_path, served):
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(serve.__file__))))
    tool = tmp_path / "xbr-upscale-torch"
    tool.write_text(FAKE_WORKER % (sys.executable, src))
    tool.chmod(0o755)
    tables = {"torch": {"path": str(tool), "model": str(tmp_path / "2x-Compact.pth")}}
    up = serve.backend_of("torch", tables)
    pid = up.proc.pid
    srv, url = served(serve.Server(("127.0.0.1", 0), up, table=tables["torch"]))
    client_up = service.from_config("r", {"r": {"url": url}})
    info = client_up.info()
    assert info["scales"] == [2] and info["models"][0]["name"] == "2x-Compact.pth"
    assert info["identity"]["model_sha256"] == "ab" * 32
    assert str(tmp_path) not in json.dumps(info)
    rgba = noise(3, 2, 4)
    opaque = bytearray(rgba)
    opaque[3::4] = b"\xff" * 6
    for _ in range(2):
        [(w, h, out)] = client_up.upscale([(3, 2, rgba)], 2)
        assert (w, h) == (6, 4) and out == nearest(3, 2, bytes(opaque), 2)
    # One child for every request: the model is loaded once.
    assert up.proc.pid == pid
    up.abort()
    client_up.upscale([(3, 2, rgba)], 2)
    assert up.proc.pid != pid
    with pytest.raises(CliError, match="no such model"):
        serve.backend_of("torch", {"torch": dict(tables["torch"], model="bad.pth")})
    with pytest.raises(CliError, match="model = the model file"):
        serve.backend_of("torch", {"torch": {"path": str(tool)}})


def test_worker_frames_are_little_endian_u32():
    # The plugin's own framing (plugins/torch/README.md), as Worker writes it.
    assert struct.pack("<I", 7) == b"\x07\0\0\0"
