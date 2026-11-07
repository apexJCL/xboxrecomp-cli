# xbr-upscale-torch

Runs a model file you downloaded through
[spandrel](https://github.com/chaiNNer-org/spandrel), for
`xbr enhance textures`. It is optional and separate from the CLI: install it
only if you want it (`xbr enhance textures backend install torch` prints the
line for your platform, and never runs it).

It ships no model and fetches none. The model file and its licence are yours to
check; the textures it makes come from your own game files and stay private.

- Models load only through spandrel's `ModelLoader`, whose restricted unpickler
  refuses anything but tensors. Prefer `.safetensors`.
- `--expected-sha256` refuses a file that differs, unless
  `--allow-model-mismatch` says otherwise, and the record then says so.
- There is no server and no socket: `upscale` works on folders, and `worker`
  speaks over its own stdin and stdout.

`upscale` prints one JSON line: `arch`, `native_scale`, `model_sha256`,
`contract`, `device`, `dtype`, `tile`, `versions`, `reproducible`,
`reproducible_scope`. `--version` prints `xbr-upscale-torch <v> contract <major>.<minor>`.

`worker` reads frames from stdin and answers each on stdout. A frame is a
little-endian u32 length, that many bytes of JSON, and the `size` bytes of body
the JSON names. `{"op": "load", "model": path, ...}` answers `{"ok": true,
"id": n, ...}`; `{"op": "upscale", "id": n, "scale": s, "size": N}` with a PNG
body answers with a PNG body; `{"op": "quit"}` ends it. A failure answers
`{"ok": false, "error": "..."}` and the worker goes on.

Tests: `uv run --project plugins/torch pytest plugins/torch/tests` (a tiny random
model made in the test, on the CPU).
