# Upscale services

A texture pack can be upscaled by a GPU on another machine, or by a tool you
already run (AUTOMATIC1111, ComfyUI). The build talks to it over HTTP, one
image at a time, and checks every answer before it uses it. The CLI downloads
no tool and no model; the service is yours.

- [The table](#the-table): pointing an `[upscaler.NAME]` at a service.
- [The contract](#the-contract): what an `xbr` service answers.
- [Adapters](#adapters): `a1111` and `comfyui`.
- [`enhance serve`](#enhance-serve): the reference server, over any upscaler
  of this machine.
- [Tokens and plain http](#tokens-and-plain-http), [caps](#caps).

## The table

A table with a `url` is a service. Where it is and how to reach it is yours,
so it goes in `config/enhance-textures.local.toml`, never in the tracked file:

```toml
# config/enhance-textures.toml (tracked)
[class.world]
upscaler = "gpu"

[upscaler.gpu]
api = "xbr"          # xbr (default), a1111 or comfyui
scales = [4]         # optional: narrows the service's own scales
timeout = 300        # seconds per request (default 300)

# config/enhance-textures.local.toml (yours)
[upscaler.gpu]
url = "http://192.0.2.20:8711"
token_env = "XBR_UPSCALE_TOKEN"   # the variable holding the token
allow_plain_http = true            # only on a network you trust
model = "4x-UltraSharp.pth"        # when the service has several
```

| Key | File | Meaning |
|---|---|---|
| `api` | tracked | the protocol: `xbr`, `a1111`, `comfyui` |
| `scales` | tracked | the scales to use; required for `comfyui` |
| `batch`, `timeout` | tracked | images per batch the build groups; seconds per request |
| `url` | local | `http://` or `https://` host and port, an optional path; no user, password, query or fragment |
| `token_env` | local | the name of the variable holding the token, read at each request and never written anywhere |
| `allow_plain_http` | local | send the token over plain http to a host off loopback |
| `model` | local | the service's model; checked against its own list first |
| `workflow`, `load_node`, `save_node` | local | ComfyUI only, [below](#comfyui) |

Listing upscalers makes no request. The first upscale fetches the service's
info, and refuses a `model` or `scale` it does not list before it sends an
image. The pack's record names the service, its version and the model, never
the url: a pack built over a service is not reproducible (`reproducible:
false`), and the stage key follows what answered, not where it was.

Requests go one at a time. A refused connection and 429, 502, 503 and 504 are
tried again three times, waiting 0.5, 1 and 2 s, and so is a GET that timed
out. A POST that timed out or was cut is not sent again: a ComfyUI prompt or an
A1111 job may have run, and would run twice. Any other error fails the build
at once with the service's message. A redirect is an error:
the token never follows one. Proxies from the environment are used, except
for a loopback url.

## The contract

Version 1. Two routes, JSON and PNG, with `Authorization: Bearer <token>` on
each when the service wants one.

`GET /v1/info`:

```json
{
  "name": "xbr enhance serve",
  "version": "0.21.0",
  "contract": 1,
  "scales": [2, 4],
  "models": [{"name": "4x-UltraSharp.pth", "scales": [4], "alpha": "rgb"}],
  "max_side": 4112,
  "batch": 1,
  "identity": {"backend": "torch", "model_sha256": "..."}
}
```

- `scales`: required, the integer scales.
- `models`: a list of `{name, scales, alpha}`; empty when the service has one
  fixed model. With several, the table names one.
- `alpha`: `rgba` when the answer keeps the alpha, `rgb` when the build splits
  it off and scales it itself.
- `max_side`: the largest side it takes, 0 for none.
- `identity`: what the pack's record keeps of the service: names, versions,
  hashes. Never a path or a url.
- A `contract` over 1 is refused.

`POST /v1/upscale?scale=S&model=M`, body `image/png`: answers `200 image/png`
of the image times `S`. `model` is left out when the service has none. An
error is a status and `{"error": "..."}`:

| Status | When |
|---|---|
| 400 | a scale or model it does not list, a body that is not a PNG |
| 401 | no token, or the wrong one |
| 404 | another route |
| 408 | the body came too slowly |
| 421 | a Host that is not this machine's, on a server with no token |
| 411, 413, 415 | no Content-Length; a body or a side over its caps; not `image/png` |
| 502 | the backend failed |
| 503, 429 | busy (`enhance serve`: a call that timed out still runs): the client tries again |
| 504 | the backend took over its timeout |

```sh
curl -s -H "Authorization: Bearer $XBR_UPSCALE_TOKEN" http://192.0.2.20:8711/v1/info
curl -s -H "Authorization: Bearer $XBR_UPSCALE_TOKEN" -H 'Content-Type: image/png' \
  --data-binary @in.png -o out.png 'http://192.0.2.20:8711/v1/upscale?scale=4'
```

## Adapters

### a1111

`api = "a1111"`: AUTOMATIC1111 (or Forge) started with `--api`. The models
are its upscalers (`GET /sdapi/v1/upscalers`, without `None`), and `model`
names one. Each image goes to `POST /sdapi/v1/extra-single-image` with
`upscaling_resize` set to the scale. Scales 2, 3 and 4; RGB, so the build
splits the alpha off.

A1111 cuts each side of its answer down to a multiple of 8. The adapter pads
the image right and down, repeating its last column and row, until both
scaled sides are multiples of 8, and crops the answer back; the build's own
edge pad is inside that, so the repeated texels never reach the pack.

The adapter is tested against a stand-in that answers as A1111's API is
documented to, not against a running A1111.

### comfyui

`api = "comfyui"`: a ComfyUI server and a workflow of yours.

1. Build the workflow in ComfyUI: a `LoadImage` node, your upscale nodes, a
   `SaveImage` node.
2. Export it with *Save (API)* (the API format; the plain save is refused).
3. In the local file: `workflow` = its path. In the table: `scales = [N]`, the
   scale it makes; the answer's size is held to it.

The adapter fills only the load node's `image`, the save node's file name
prefix, and, when the table has a `model`, the `model_name` of each
`UpscaleModelLoader` (checked against the server's list). A workflow with more
than one load or save node names the one to use: `load_node = "12"`,
`save_node = "31"` (node ids). Each image is uploaded (`/upload/image`),
queued (`/prompt`), waited for (`/history/{id}`, until the table's `timeout`)
and fetched (`/view`). The record keeps the workflow's sha256, not its path.

ComfyUI keeps every texture it is sent and every result in its `input` and
`output` folders on that machine (as `xbr-<random>.png`); the adapter deletes
nothing. Clear them when the pack is built: they are the owner's game data.

The model list is read from `/object_info/UpscaleModelLoader` in either shape
ComfyUI has used for a choice (`[[names], {...}]` and `["COMBO", {"options":
[names]}]`).

## `enhance serve`

The reference server, for the machine with the GPU:

```sh
export XBR_UPSCALE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
mygame enhance serve --backend torch --host 0.0.0.0 --token-env XBR_UPSCALE_TOKEN
```

| Option | Effect |
|---|---|
| `--backend NAME` | a built-in or an `[upscaler.NAME]` of the config (default `xbr`) |
| `--port N` | default 8711 |
| `--host H` | default 127.0.0.1; any other host needs `--token-env`; `::1` or `::` for IPv6 |
| `--token-env VAR` | the variable holding the token the clients send |
| `--config FILE` | the config with the backend's table (default the game's `config/enhance-textures.toml` and its local file) |

The torch plugin runs as one long-lived `worker` child: its model is loaded
once, from the table's `model`, and a worker that hangs is killed and started
again at the next request. A command preset runs once per image, under a
timeout just short of the server's. A built-in cannot be killed: after a 504
the server answers 503 until it ends. A service cannot be served again. The
server does one image at a time.

The info's `identity` carries a hash of the backend's table (its tracked keys,
such as a built-in's `level` or a tool's `tile`), so a client's pack goes
stale when they change here.

## Tokens and plain http

- The token lives in an environment variable on each side; the table holds
  only its name. It is read at each request, sent only in the
  `Authorization` header, and never logged or recorded. A trailing line end
  is stripped on both sides; any other space or unprintable character is
  refused, naming only the variable.
- The client refuses to send a token over plain http to a host that is not
  loopback. `allow_plain_http = true` lifts that, with a warning in the
  build: anyone on the network can read it. Prefer https (a reverse proxy in
  front of `enhance serve`) or an ssh tunnel to 127.0.0.1.
- `enhance serve` binds to 127.0.0.1 unless `--host` says otherwise, and off
  loopback it does not start without a token. It compares tokens in constant
  time and answers 401 to a wrong one.
- Without a token it answers only requests whose `Host` is this machine
  (`localhost`, a loopback address or the bound address), so a web page whose
  domain resolves to 127.0.0.1 cannot reach it (DNS rebinding).

## Caps

What either side takes from the other is bounded before it is read:

- `enhance serve` drops a connection silent for 60 s, cuts a body that takes
  over 300 s, and holds 16 connections at once; more are closed unread.
- `enhance serve` takes a body up to the size of a stored RGBA PNG of
  4112 x 4112 (a 4096 texture and its pad), plus room; over it is 413 before a
  byte is read. The PNG header is checked against that side and the texel cap
  before the image is inflated, and inflating stops at the size the header
  says.
- The client reads an answer up to the size a stored PNG of the asked size
  takes, plus 1 MiB of room (a third more for a1111's base64); info and
  error bodies up to 1 MiB and 64 KiB. What a service says of itself
  (`identity`) is cut to 32 entries of 80 characters before the record.
- The answer's PNG header must say the asked size (the source times the
  scale) before it is decoded.
