"""A tiny random Compact model on the CPU: no model is downloaded."""

import hashlib
import json
import os
import pickle
import random
import struct
import subprocess
import sys

import pytest
import torch
from spandrel.architectures.Compact import Compact
from xbr_upscale_torch import CONTRACT, cli, pngio
from xbr_upscale_torch.run import Model, ModelError


@pytest.fixture
def model(tmp_path):
    torch.manual_seed(7)
    net = Compact(num_in_ch=3, num_out_ch=3, num_feat=4, num_conv=2, upscale=2)
    path = tmp_path / "tiny.pth"
    torch.save(net.state_dict(), path)
    return str(path)


def image(w=9, h=7):
    r = random.Random(1)
    return bytes(r.randrange(256) for _ in range(w * h * 3))


def folder(tmp_path):
    ind, out = tmp_path / "in", tmp_path / "out"
    ind.mkdir()
    out.mkdir()
    (ind / "000000.png").write_bytes(pngio.encode(9, 7, 3, image()))
    return str(ind), str(out)


def upscale_args(model, ind, out, *more):
    return ["upscale", "--model", model, "--scale", "2", "--in-dir", ind, "--out-dir", out, *more]


def test_output_and_the_record_line(model, tmp_path, capsys):
    ind, out = folder(tmp_path)
    assert cli.main(upscale_args(model, ind, out, "--device", "cpu")) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["contract"] == CONTRACT and doc["native_scale"] == 2
    assert doc["arch"] and doc["reproducible"] is False and doc["reproducible_scope"] == "host"
    assert doc["model_sha256"] == hashlib.sha256(open(model, "rb").read()).hexdigest()
    with open(os.path.join(out, "000000.png"), "rb") as f:
        w, h, channels, _ = pngio.decode(f.read())
    assert (w, h, channels) == (18, 14, 3)


def test_two_runs_on_the_cpu_agree(model):
    a = Model(model, "cpu").upscale(9, 7, 3, image())
    b = Model(model, "cpu").upscale(9, 7, 3, image())
    assert a == b


def test_tiling_keeps_the_size(model):
    w, h, rgb = Model(model, "cpu", tile=4).upscale(9, 7, 3, image())
    assert (w, h, len(rgb)) == (18, 14, 18 * 14 * 3)


def test_a_forbidden_global_is_refused(tmp_path):
    class Boom:
        def __reduce__(self):
            return (os.system, ("touch %s" % (tmp_path / "ran"),))

    path = tmp_path / "evil.pth"
    with open(path, "wb") as f:
        pickle.dump({"model": Boom()}, f)
    with pytest.raises(ModelError, match="not loaded"):
        Model(str(path), "cpu")
    assert not (tmp_path / "ran").exists()


def test_a_sha256_mismatch_is_refused_unless_allowed(model, tmp_path, capsys):
    ind, out = folder(tmp_path)
    bad = "0" * 64
    assert cli.main(upscale_args(model, ind, out, "--device", "cpu", "--expected-sha256", bad)) == 2
    assert "not loaded" in capsys.readouterr().err
    assert (
        cli.main(
            upscale_args(
                model,
                ind,
                out,
                "--device",
                "cpu",
                "--expected-sha256",
                bad,
                "--allow-model-mismatch",
            )
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["allow_model_mismatch"] is True


def test_another_scale_is_refused(model, tmp_path, capsys):
    ind, out = folder(tmp_path)
    args = upscale_args(model, ind, out, "--device", "cpu")
    args[args.index("--scale") + 1] = "4"
    assert cli.main(args) == 2
    assert "scales = [2]" in capsys.readouterr().err


def frame(head, body=b""):
    raw = json.dumps(dict(head, size=len(body))).encode()
    return struct.pack("<I", len(raw)) + raw + body


def test_the_worker_round_trip(model):
    png = pngio.encode(9, 7, 3, image())
    stdin = (
        frame({"op": "load", "model": model, "device": "cpu"})
        + frame({"op": "upscale", "id": 0, "scale": 2}, png)
        + frame({"op": "load", "model": model + ".missing"})
        + frame({"op": "quit"})
    )
    r = subprocess.run(
        [sys.executable, "-m", "xbr_upscale_torch.cli", "worker"],
        input=stdin,
        capture_output=True,
        check=True,
    )
    import io

    f = io.BytesIO(r.stdout)
    load, _ = cli.read_frame(f)
    assert load["ok"] and load["id"] == 0 and load["native_scale"] == 2
    done, body = cli.read_frame(f)
    assert done["ok"] and pngio.decode(body)[:2] == (18, 14)
    assert body == pngio.encode(18, 14, 3, Model(model, "cpu").upscale(9, 7, 3, image())[2])
    bad, _ = cli.read_frame(f)
    assert not bad["ok"]
    assert cli.read_frame(f)[0]["ok"]


def test_the_contract_line():
    r = subprocess.run(
        [sys.executable, "-m", "xbr_upscale_torch.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "xbr-upscale-torch 0.1.0 contract " + CONTRACT
