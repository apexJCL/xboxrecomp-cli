"""`upscale` over folders, `worker` over stdin and stdout. No network, ever."""

import argparse
import json
import os
import struct
import sys

from . import CONTRACT, __version__, pngio

DEVICES = ("auto", "cuda", "mps", "cpu")
DTYPES = ("fp32", "fp16", "bf16")


def version_line():
    return "xbr-upscale-torch %s contract %s" % (__version__, CONTRACT)


def _parser():
    p = argparse.ArgumentParser(prog="xbr-upscale-torch")
    p.add_argument("--version", action="version", version=version_line())
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("upscale", help="every PNG of a folder through a model file")
    u.add_argument("--model", required=True, help="the model file you downloaded")
    u.add_argument("--scale", type=int, required=True, help="the model's native scale")
    u.add_argument("--in-dir", required=True)
    u.add_argument("--out-dir", required=True)
    u.add_argument("--device", choices=DEVICES, default="auto")
    u.add_argument("--tile", type=int, default=0, help="tile size, 0 for the whole image")
    u.add_argument("--dtype", choices=DTYPES, default="fp32")
    u.add_argument("--expected-sha256", default="", help="refuse a model file that differs")
    u.add_argument("--allow-model-mismatch", action="store_true")
    sub.add_parser("worker", help="frames on stdin and stdout; models loaded once")
    return p


def cmd_upscale(a):
    from .run import Model, ModelError

    try:
        m = Model(a.model, a.device, a.dtype, a.tile, a.expected_sha256, a.allow_model_mismatch)
    except ModelError as e:
        print("xbr-upscale-torch: %s" % e, file=sys.stderr)
        return 2
    if m.native_scale != a.scale:
        print(
            "xbr-upscale-torch: the model is %dx, asked for %dx: scales = [%d] in its "
            "[upscaler] table, and the build shrinks from there"
            % (m.native_scale, a.scale, m.native_scale),
            file=sys.stderr,
        )
        return 2
    n = 0
    for name in sorted(os.listdir(a.in_dir)):
        if not name.lower().endswith(".png"):
            continue
        with open(os.path.join(a.in_dir, name), "rb") as f:
            w, h, channels, pixels = pngio.decode(f.read())
        ow, oh, rgb = m.upscale(w, h, channels, pixels)
        with open(os.path.join(a.out_dir, name), "wb") as f:
            f.write(pngio.encode(ow, oh, 3, rgb))
        n += 1
    print(json.dumps(dict(m.report(), images=n), sort_keys=True))
    return 0


def _read_exact(f, n):
    data = f.read(n)
    if len(data) != n:
        raise EOFError
    return data


def read_frame(f):
    n = struct.unpack("<I", _read_exact(f, 4))[0]
    head = json.loads(_read_exact(f, n))
    body = _read_exact(f, head["size"]) if head.get("size") else b""
    return head, body


def write_frame(f, head, body=b""):
    head = dict(head, size=len(body))
    raw = json.dumps(head).encode("utf-8")
    f.write(struct.pack("<I", len(raw)) + raw + body)
    f.flush()


def _worker_one(head, body, models):
    from .run import Model

    op = head.get("op")
    if op == "load":
        m = Model(
            head["model"],
            head.get("device", "auto"),
            head.get("dtype", "fp32"),
            head.get("tile", 0),
            head.get("expected_sha256", ""),
            head.get("allow_model_mismatch", False),
        )
        models.append(m)
        return dict(m.report(), ok=True, id=len(models) - 1), b""
    if op == "upscale":
        m = models[head["id"]]
        if head.get("scale", m.native_scale) != m.native_scale:
            raise ValueError("the model is %dx" % m.native_scale)
        w, h, channels, pixels = pngio.decode(body)
        ow, oh, rgb = m.upscale(w, h, channels, pixels)
        return {"ok": True, "width": ow, "height": oh}, pngio.encode(ow, oh, 3, rgb)
    raise ValueError("no op %r" % op)


def cmd_worker():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    models = []
    while True:
        try:
            head, body = read_frame(stdin)
        except EOFError:
            return 0
        if head.get("op") == "quit":
            write_frame(stdout, {"ok": True})
            return 0
        try:
            answer, out = _worker_one(head, body, models)
        except Exception as e:  # noqa: BLE001
            # One bad file does not cost the models the worker holds.
            answer, out = {"ok": False, "error": str(e)}, b""
        write_frame(stdout, answer, out)


def main(argv=None):
    a = _parser().parse_args(argv)
    if a.cmd == "upscale":
        return cmd_upscale(a)
    return cmd_worker()


if __name__ == "__main__":
    sys.exit(main())
