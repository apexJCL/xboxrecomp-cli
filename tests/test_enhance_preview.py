"""enhance preview (texture-upscale phase 5): the D9 refusals, each route over
a synthetic game and dump, the crop through each built-in, the on-screen view
and the save that keeps paths and URLs in the local file."""

import base64
import http.client
import json
import os
import sys
import threading
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cli import fake_tree, write  # noqa: E402
from test_enhance_build import noise, world_game  # noqa: E402
from test_enhance_textures import dump_texture, inv_args  # noqa: E402

from xboxrecomp_cli.enhance import png, preview, textures, textures_cfg  # noqa: E402
from xboxrecomp_cli.enhance.upscalers import BUILTINS  # noqa: E402

TEX = "00000000000000AB"
SIDE = 32


def bgra(rgba):
    out = bytearray(rgba)
    out[0::4], out[2::4] = rgba[2::4], rgba[0::4]
    return bytes(out)


@pytest.fixture
def served(d):
    """A game with an archive and a dump holding one 32x32 opaque noise
    texture traced at magnification 1, its inventory, and the page's server
    in a thread; stopped after the test."""
    with fake_tree(d):
        G = world_game(d)
        dumpd = os.path.join(d, "dump")
        texels = bgra(noise(SIDE, SIDE, seed=7, opaque=True))
        dump_texture(
            dumpd,
            TEX,
            SIDE,
            SIDE,
            "A8R8G8B8",
            texels=texels,
            wrap_u="wrap",
            wrap_v="wrap",
            mag_scale1=1.0,
            mag_draws=1,
            mag_filter="linear",
            min_filter="linear",
        )
        assert textures.run(inv_args(dump=[dumpd])) == 0
        config = os.path.join(G.root, textures.DEFAULT_CONFIG)
        app = preview.App(G, config, textures._inventory_path(None), [dumpd])
        srv = preview.Server(app)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
        t.start()
        srv.thread = t
        try:
            yield G, srv, app
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(5)


def call(srv, method, path, body=None, token=True, headers=None, raw=None):
    """(status, headers, parsed body or bytes)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
    h = {"Host": "127.0.0.1:%d" % srv.port}
    if token:
        h["X-Xbr-Token"] = srv.token
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if data is not None:
        h["Content-Length"] = str(len(data))
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    c.endheaders(data)
    r = c.getresponse()
    out = r.read()
    c.close()
    if r.getheader("Content-Type") == "application/json":
        out = json.loads(out)
    return r.status, r, out


def image(uri):
    assert uri.startswith("data:image/png;base64,")
    return png.decode(base64.b64decode(uri.split(",", 1)[1]))


# ---- D9: the refusals --------------------------------------------------------


def test_page_and_headers(served):
    _, srv, _ = served
    status, r, body = call(srv, "GET", "/?t=" + srv.token, token=False)
    assert status == 200 and b"enhance preview" in body
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "img-src data:" in csp
    assert r.getheader("Cache-Control") == "no-store"
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("Referrer-Policy") == "no-referrer"
    assert srv.server_address[0] == "127.0.0.1"
    # Nothing from elsewhere in the page.
    assert b"http://" not in body and b"https://" not in body.replace(b"https://host", b"")


@pytest.mark.parametrize(
    "how",
    [
        dict(token=False),
        dict(token=False, path="/api/state?t=wrong"),
        dict(headers={"X-Xbr-Token": "wrong"}),
        dict(headers={"Host": "evil.example:80"}),
        dict(headers={"Host": None}),
        dict(headers={"Origin": "http://evil.example"}),
        dict(headers={"Origin": "null"}),
        dict(headers={"Sec-Fetch-Site": "cross-site"}),
        dict(headers={"Sec-Fetch-Site": "same-site"}),
    ],
)
def test_refused_with_no_body(served, how):
    _, srv, _ = served
    how = dict(how)
    path = how.pop("path", "/api/state")
    for method, body in (("GET", None), ("POST", {})):
        p = path if method == "GET" else path.replace("/api/state", "/api/coverage")
        status, _, out = call(srv, method, p, body=body, **how)
        assert status == 403 and out == b""


def test_post_must_be_json_with_a_length(served):
    _, srv, _ = served
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert call(srv, "POST", "/quit", raw=b"a=1", headers=form)[0] == 415
    assert (
        call(srv, "POST", "/api/save", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    )
    no_len = {"Content-Type": "application/json", "Content-Length": None}
    assert call(srv, "POST", "/api/log", raw=None, headers=no_len)[0] == 411
    big = {"Content-Type": "application/json", "Content-Length": str(preview.MAX_BODY + 1)}
    assert call(srv, "POST", "/api/log", headers=big)[0] == 413
    status, _, out = call(
        srv, "POST", "/api/log", raw=b"[1]", headers={"Content-Type": "application/json"}
    )
    assert status == 400 and "object" in out["error"]
    assert call(srv, "GET", "/api/nothing")[0] == 404
    # The page's own Origin is fine.
    origin = {"Origin": "http://127.0.0.1:%d" % srv.port, "Sec-Fetch-Site": "same-origin"}
    assert call(srv, "GET", "/api/state", headers=origin)[0] == 200
    assert call(srv, "GET", "/api/state", headers={"Host": "localhost:%d" % srv.port})[0] == 200


# ---- the routes ----------------------------------------------------------------


def test_state_inventory_texture(served):
    G, srv, _ = served
    status, _, s = call(srv, "GET", "/api/state")
    assert status == 200 and s["game"] == G.slug and s["entries"] >= 3
    assert "world" in s["classes"] and s["traced"] and not s["config_error"]
    assert s["config"] == "config/enhance-textures.toml"
    _, _, inv = call(srv, "GET", "/api/inventory")
    hashes = [e["hash"] for e in inv["entries"]]
    assert TEX in hashes
    assert any(
        e["name"] == "world/wall.dds" or e["name"] == "world\\wall.dds" for e in inv["entries"]
    )
    _, _, t = call(srv, "GET", "/api/texture?h=" + TEX.lower())
    assert (t["w"], t["h"], t["mag"], t["mag_filter"]) == (SIDE, SIDE, 1.0, "linear")
    w, h, rgba = image(t["image"])
    assert (w, h) == (SIDE, SIDE) and rgba == noise(SIDE, SIDE, seed=7, opaque=True)
    # An archive texture comes from the feeder (D10).
    wall = next(e for e in inv["entries"] if "wall" in e["name"])
    status, _, t = call(srv, "GET", "/api/texture?h=" + wall["hash"])
    assert status == 200 and (t["w"], t["h"]) == (16, 16)
    status, _, out = call(srv, "GET", "/api/texture?h=nothex")
    assert status == 400 and "16 hex" in out["error"]
    status, _, out = call(srv, "GET", "/api/texture?h=" + "F" * 16)
    assert status == 400 and "not in the inventory" in out["error"]


def test_upscalers_and_service_test(served):
    _, srv, app = served
    _, _, out = call(srv, "GET", "/api/upscalers")
    rows = {r["name"]: r for r in out["upscalers"]}
    for u in BUILTINS:
        assert rows[u.name]["found"] and rows[u.name]["source"] == "built-in"
    assert rows["xbr"]["params"]
    assert "realesrgan-ncnn-vulkan" in rows and rows["realesrgan-ncnn-vulkan"]["source"] == "preset"
    # The page's first call, on a game with no config yet.
    status, _, out = call(srv, "POST", "/api/upscalers", {"config": {"tracked": {}, "local": {}}})
    assert status == 200 and len(out["upscalers"]) == len(rows)
    draft = {"tracked": {}, "local": {"upscaler": {"mysvc": {"url": "https://127.0.0.1:9"}}}}
    _, _, out = call(srv, "POST", "/api/upscalers", {"config": draft})
    svc = next(r for r in out["upscalers"] if r["name"] == "mysvc")
    assert svc["kind"] == "service" and svc["source"] == "config" and not svc["token_set"]
    status, _, out = call(srv, "POST", "/api/service/test", {"name": "bad name", "url": "x"})
    assert status == 400
    status, _, out = call(srv, "POST", "/api/service/test", {"name": "mysvc"})
    assert status == 400 and "URL" in out["error"]
    if not app.has_http:
        status, _, out = call(
            srv, "POST", "/api/service/test", {"name": "mysvc", "url": "https://x"}
        )
        assert status == 400 and "no http backend" in out["error"]
    status, _, out = call(srv, "POST", "/api/tool/version", {"name": "nope"})
    assert status == 400


@pytest.mark.parametrize("name", [u.name for u in BUILTINS])
def test_a_crop_is_the_whole_render_cut(served, name):
    """The crop, with its margin of neighbours, is what the whole texture's
    render holds there."""
    _, srv, _ = served
    body = {"hash": TEX, "upscalers": [name], "scale": 2, "crop": [4, 6, 16, 12]}
    status, _, crop = call(srv, "POST", "/api/render", body)
    assert status == 200, crop
    r = crop["results"][0]
    assert "error" not in r and (r["w"], r["h"]) == (32, 24)
    assert r["bytes"]["raw"] > r["bytes"]["bc1"] > 0 and r["seconds_per_texture"] >= 0
    _, _, whole = call(srv, "POST", "/api/render", dict(body, crop=None))
    ww, wh, wrgba = image(whole["results"][0]["image"])
    assert (ww, wh) == (64, 64)
    cw, ch, crgba = image(r["image"])
    for y in range(ch):
        row = wrgba[((12 + y) * ww + 8) * 4 : ((12 + y) * ww + 8 + cw) * 4]
        assert crgba[y * cw * 4 : (y + 1) * cw * 4] == row, (name, y)
    sw, sh, stock = image(crop["stock"])
    assert (sw, sh) == (16, 12)


def test_render_refusals_and_bc_views(served):
    _, srv, _ = served
    for bad in (
        {"hash": TEX, "upscalers": ["nearest"], "scale": 5},
        {"hash": TEX, "upscalers": ["nearest"], "crop": [0, 0, 0, 4]},
        {"hash": TEX, "upscalers": "nearest"},
        {"hash": TEX, "upscalers": ["nearest"], "screen": {"mag": 1, "render_scale": 0}},
    ):
        assert call(srv, "POST", "/api/render", bad)[0] == 400, bad
    body = {
        "hash": TEX,
        "upscalers": ["nearest", "nope", "atlas"],
        "bc": True,
        "crop": [0, 0, 8, 8],
    }
    _, _, out = call(srv, "POST", "/api/render", body)
    ok, nope, atlas = out["results"]
    assert set(ok["bc"]) == {"bc1", "bc3"}
    assert "error" in nope and "font" in atlas["error"]


def test_on_screen_at_one_is_the_stock_render(served):
    """D15: magnification 1 at render scale 1 draws the 2x result at half its
    size, which is the stock crop again within one level."""
    _, srv, _ = served
    body = {
        "hash": TEX,
        "upscalers": ["nearest"],
        "scale": 2,
        "crop": [0, 0, 16, 16],
        "screen": {"mag": 1.0, "render_scale": 1},
    }
    _, _, out = call(srv, "POST", "/api/render", body)
    sc = out["results"][0]["screen"]
    assert not sc["magnified"] and "not magnified" in sc["note"]
    sw, sh, stock = image(sc["stock"]["image"])
    rw, rh, res = image(sc["result"]["image"])
    assert (sw, sh) == (rw, rh) == (16, 16)
    assert max(abs(a - b) for a, b in zip(stock, res)) <= 1
    # From the trace when none is typed; magnified at render scale 3.
    body["screen"] = {"render_scale": 3}
    _, _, out = call(srv, "POST", "/api/render", body)
    sc = out["results"][0]["screen"]
    assert sc["from_trace"] and sc["magnified"] and sc["stock"]["w"] == 48
    assert sc["result"]["filter"] == "linear"


def test_on_screen_picks_the_level_and_caps_the_size():
    w = h = 16
    rgba = noise(w, h, opaque=True)
    small = preview.on_screen(w, h, rgba, 0.25, "full", 1, "", "")
    assert (small["w"], small["level"], small["filter"]) == (4, 2, "linear (not recorded)")
    big = preview.on_screen(w, h, rgba, 200, "keep", 1, "point", "point")
    assert big["w"] == preview.MAX_SCREEN and big["level"] == 0
    same = preview.sample(w, h, rgba, w, h, True)
    assert same == rgba


def test_coverage_bars_exclusions_and_trace(served):
    _, srv, _ = served
    _, _, out = call(srv, "POST", "/api/coverage", {"config": {"tracked": {}, "local": {}}})
    names = [b["preset"] for b in out["bars"]]
    assert "all" in names and out["preset"] == "all"
    assert all(b["pack_bytes"] >= 0 for b in out["bars"])
    assert any(x["hash"] == TEX for x in out["traced"])
    rule = {"rule": {"hash:" + TEX: {"skip": True}}}
    _, _, out = call(srv, "POST", "/api/coverage", {"config": {"tracked": rule, "local": {}}})
    assert any(x["hash"] == TEX for x in out["excluded"])
    status, _, out = call(srv, "POST", "/api/coverage", {"config": {"tracked": {"url": "x"}}})
    assert status == 400
    status, _, _ = call(srv, "POST", "/api/coverage", {"preset": "nope"})
    assert status == 400


def test_log_import(served):
    _, srv, _ = served
    lines = [
        "[TEXPACK] miss %s 32x32 A8R8G8B8 x2 linear" % TEX.lower(),
        "[TEXPACK] miss 0123456789ABCDEF 8x8 DXT1 x1 linear (art off)",
    ]
    status, _, out = call(srv, "POST", "/api/log", {"lines": lines})
    if status == 400:
        pytest.fail(out["error"])
    rows = {r["hash"]: r for r in out["rows"]}
    assert rows[TEX]["lacking"] and rows[TEX]["cls"] and out["lacking"] == 1
    held = rows["0123456789ABCDEF"]
    assert not held["lacking"] and "name" not in held and out["held"] == 1
    status, _, out = call(srv, "POST", "/api/log", {"lines": ["nothing here"]})
    assert status == 400 and "RECOMP_TRACE=tex_pack" in out["error"]
    assert call(srv, "POST", "/api/log", {"lines": "x"})[0] == 400


def test_save_keeps_comments_and_paths_stay_local(served):
    G, srv, _ = served
    path = os.path.join(G.root, textures.DEFAULT_CONFIG)
    write(path, "# mine\nschema = 1\n\n[class.world]\n# why 3\nscale = 3\n")
    _, _, s = call(srv, "GET", "/api/state")
    tracked = s["tracked"]
    tracked["render_scale"] = 2
    tracked["class"]["world"]["upscaler"] = "lanczos"
    tracked["rule"] = {"hash:" + TEX: {"skip": True}}
    local = {"upscaler": {"realesrgan-ncnn-vulkan": {"path": "/opt/x/bin"}}}
    status, _, out = call(
        srv, "POST", "/api/save", {"config": {"tracked": tracked, "local": local}}
    )
    assert status == 200, out
    assert out["command"].endswith("enhance textures build")
    text = open(path, encoding="utf-8").read()
    assert "# mine" in text and "# why 3" in text and "/opt/x" not in text
    doc = tomllib.loads(text)
    assert doc["render_scale"] == 2 and doc["class"]["world"] == {"scale": 3, "upscaler": "lanczos"}
    assert doc["rule"]["hash:" + TEX] == {"skip": True}
    lpath = textures_cfg.local_path_of(path)
    assert tomllib.loads(open(lpath, encoding="utf-8").read()) == {"upscaler": local["upscaler"]}
    # Read back as the page loads it, then a removal.
    _, _, s = call(srv, "GET", "/api/state")
    assert s["local_doc"] == local and s["tracked"]["rule"]
    del s["tracked"]["rule"]
    call(srv, "POST", "/api/save", {"config": {"tracked": s["tracked"], "local": s["local_doc"]}})
    assert "rule" not in tomllib.loads(open(path, encoding="utf-8").read())
    # A path or URL in the tracked draft is refused, and nothing is written.
    before = open(path, encoding="utf-8").read()
    bad = dict(s["tracked"], upscaler={"realesrgan-ncnn-vulkan": {"path": "/x"}})
    status, _, out = call(srv, "POST", "/api/save", {"config": {"tracked": bad, "local": {}}})
    assert status == 400
    bad = {"upscaler": {"svc": {"scale": 2}}}
    status, _, out = call(srv, "POST", "/api/save", {"config": {"tracked": {}, "local": bad}})
    assert status == 400 and "local file holds" in out["error"]
    status, _, out = call(srv, "POST", "/api/save", {"config": {"tracked": {"rule": {'a"b': {}}}}})
    assert status == 400
    assert open(path, encoding="utf-8").read() == before


def save_draft(srv, path, text, edit):
    """Write `text`, load it as the page does, apply `edit` to the tracked
    draft and Save: (status, reply, file after)."""
    write(path, text)
    _, _, s = call(srv, "GET", "/api/state")
    edit(s["tracked"])
    status, _, out = call(
        srv, "POST", "/api/save", {"config": {"tracked": s["tracked"], "local": s["local_doc"]}}
    )
    return status, out, open(path, encoding="utf-8").read()


@pytest.mark.parametrize(
    "text, edit, want",
    [
        # A rule's key quoted with ' or bare, removed or changed.
        ("schema = 1\n[rule.'menu_*']\nskip = true\n", lambda t: t.pop("rule"), None),
        ("schema = 1\n[rule.menu]\nskip = true\n", lambda t: t.pop("rule"), None),
        (
            "schema = 1\n[rule.'menu_*']\nskip = true\n",
            lambda t: t["rule"]["menu_*"].update(skip=False),
            {"rule": {"menu_*": {"skip": False}}},
        ),
        # Spaces in the header, a quoted key, a multi-line array, a # in a string.
        (
            'schema = 1\n[ class.world ]\n"scale" = 2 # mine\n',
            lambda t: t["class"]["world"].update(scale=3),
            {"class": {"world": {"scale": 3}}},
        ),
        (
            'schema = 1\n[class.world]\nedges = "cells"\ncell = [\n  8,\n  8,\n]\n',
            lambda t: t["class"]["world"].update(cell=[16, 16]),
            {"class": {"world": {"edges": "cells", "cell": [16, 16]}}},
        ),
        (
            'schema = 1\npreset = "a#b" # mine\n',
            lambda t: t.update(preset="world"),
            {"preset": "world"},
        ),
    ],
)
def test_save_edits_each_form_of_a_table(served, text, edit, want):
    G, srv, _ = served
    path = os.path.join(G.root, textures.DEFAULT_CONFIG)
    status, out, after = save_draft(srv, path, text, edit)
    assert status == 200, out
    doc = tomllib.loads(after)
    doc.pop("schema")
    assert doc == (want or {})
    assert "# mine" in after or "# mine" not in text


@pytest.mark.parametrize(
    "text, edit, named",
    [
        # Dotted keys: the line edit would declare the table twice.
        ("schema = 1\nclass.world.scale = 2\n", lambda t: t["class"]["world"].update(scale=3), ""),
        # An inline table: removing it would leave it there.
        (
            "schema = 1\n[class]\ntext = {scale = 2}\n",
            lambda t: t["class"].pop("text"),
            '[class."text"]',
        ),
    ],
)
def test_save_refuses_a_form_it_cannot_edit(served, text, edit, named):
    G, srv, _ = served
    path = os.path.join(G.root, textures.DEFAULT_CONFIG)
    status, out, after = save_draft(srv, path, text, edit)
    assert status == 400 and "cannot edit" in out["error"] and named in out["error"]
    assert "by hand" in out["error"]
    assert after == text


def test_the_render_cache_keeps_to_its_bytes(monkeypatch):
    app = object.__new__(preview.App)
    app.renders, app.render_bytes = {}, 0
    monkeypatch.setattr(preview, "KEEP_RENDER_BYTES", 100)
    for i in range(5):
        app._keep_render(i, (1, 1, bytes(40), 0.0, 1))
    assert list(app.renders) == [3, 4] and app.render_bytes == 80
    app._keep_render("big", (1, 1, bytes(101), 0.0, 1))
    assert list(app.renders) == [3, 4] and app.render_bytes == 80


def test_the_server_goes_with_its_parent(served, monkeypatch):
    _, srv, _ = served
    srv.done = threading.Event()
    monkeypatch.setattr(preview, "_parent_alive", lambda ppid: False)
    w = threading.Thread(target=preview._watch_parent, args=(srv, 1), daemon=True)
    w.start()
    w.join(10)
    srv.thread.join(10)
    assert not w.is_alive() and not srv.thread.is_alive()


def test_the_parent_check_sees_its_own_parent():
    assert preview._parent_alive(os.getppid())
    assert not preview._parent_alive(-12345 if os.name != "nt" else 0)


def test_quit_stops_the_server(served):
    _, srv, _ = served
    status, _, out = call(srv, "POST", "/quit", {})
    assert status == 200 and out == {"bye": True}
    # serve_forever returns; run() then closes the port.
    srv.thread.join(10)
    assert not srv.thread.is_alive()


# ---- the config writer's helpers ------------------------------------------------


def test_cfg_helpers_edit_in_place():
    text = '# top\nschema = 1\npreset = "all"\n\n[class.world]\n# c\nscale = 3\nmips = "keep"\n\n[rule."a*"]\nskip = true\n'
    t = textures_cfg.set_top_keys(text, {"render_scale": 2}, remove=["preset"])
    doc = tomllib.loads(t)
    assert doc["render_scale"] == 2 and "preset" not in doc and "# top" in t
    t = textures_cfg.remove_table_keys(t, "class.world", ["mips"])
    assert tomllib.loads(t)["class"]["world"] == {"scale": 3} and "# c" in t
    t = textures_cfg.drop_table(t, 'rule."a*"')
    assert "rule" not in tomllib.loads(t)
    t = textures_cfg.set_table_keys(t, "upscaler.xbr", {"passes": [1, 2]})
    assert tomllib.loads(t)["upscaler"]["xbr"]["passes"] == [1, 2]
