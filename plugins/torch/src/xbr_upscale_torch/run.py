"""Load a model through spandrel's restricted loader, and run it. Never
torch.load by hand: a model file is someone else's pickle."""

import hashlib
import importlib.metadata

import numpy as np
import spandrel
import torch

from . import CONTRACT

DTYPES = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}
# Padding around a tile, thrown away after: a network's seams live there.
TILE_PAD = 8


class ModelError(Exception):
    pass


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_device(want):
    if want != "auto":
        return want
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Model:
    """A loaded model and what the record says about it."""

    def __init__(self, path, device="auto", dtype="fp32", tile=0, expected="", allow=False):
        self.sha256 = sha256_of(path)
        self.mismatch = bool(expected) and expected.lower() != self.sha256
        if self.mismatch and not allow:
            raise ModelError(
                "%s has sha256 %s, the config expects %s: not loaded "
                "(--allow-model-mismatch to load it anyway)" % (path, self.sha256, expected)
            )
        self.device = pick_device(device)
        self.dtype = dtype
        self.tile = tile
        # Host-bound at best: the CPU's algorithms made deterministic. A GPU's
        # results vary with its driver, so they promise nothing.
        self.scope = "host" if self.device == "cpu" else "none"
        if self.scope == "host":
            torch.use_deterministic_algorithms(True)
        try:
            loaded = spandrel.ModelLoader(device=self.device).load_from_file(path)
        except Exception as e:  # noqa: BLE001
            # The restricted unpickler and every unknown file end here alike.
            raise ModelError("%s: not loaded: %s" % (path, e)) from e
        if not isinstance(loaded, spandrel.ImageModelDescriptor):
            raise ModelError("%s: not an image-to-image model" % path)
        self.model = loaded
        self.arch = loaded.architecture.name
        self.native_scale = loaded.scale
        if dtype != "fp32":
            loaded.to(DTYPES[dtype])
        loaded.eval()

    def report(self):
        return {
            "arch": self.arch,
            "native_scale": self.native_scale,
            "model_sha256": self.sha256,
            "contract": CONTRACT,
            "device": self.device,
            "dtype": self.dtype,
            "tile": self.tile,
            "versions": {
                "torch": torch.__version__,
                "spandrel": importlib.metadata.version("spandrel"),
            },
            "reproducible": False,
            "reproducible_scope": self.scope,
            "allow_model_mismatch": self.mismatch,
        }

    def _forward(self, x):
        with torch.inference_mode():
            return self.model(x)

    def _tiled(self, x):
        _, _, h, w = x.shape
        s = self.native_scale
        out = None
        for y0 in range(0, h, self.tile):
            for x0 in range(0, w, self.tile):
                y1, x1 = min(y0 + self.tile, h), min(x0 + self.tile, w)
                py0, px0 = max(y0 - TILE_PAD, 0), max(x0 - TILE_PAD, 0)
                py1, px1 = min(y1 + TILE_PAD, h), min(x1 + TILE_PAD, w)
                part = self._forward(x[:, :, py0:py1, px0:px1])
                if out is None:
                    out = torch.zeros(1, part.shape[1], h * s, w * s, dtype=part.dtype)
                top, left = (y0 - py0) * s, (x0 - px0) * s
                out[:, :, y0 * s : y1 * s, x0 * s : x1 * s] = part[
                    :, :, top : top + (y1 - y0) * s, left : left + (x1 - x0) * s
                ].cpu()
        return out

    def upscale(self, w, h, channels, pixels):
        """(w, h, RGB bytes) of the model's output; a straight alpha is the
        CLI's to split off, so only RGB goes through."""
        a = np.frombuffer(pixels, dtype=np.uint8).reshape(h, w, channels)[:, :, :3]
        x = torch.from_numpy(a.copy()).permute(2, 0, 1).unsqueeze(0)
        x = (x.to(DTYPES[self.dtype]) / 255.0).to(self.device)
        out = self._tiled(x) if self.tile else self._forward(x)
        y = (out.squeeze(0).clamp(0, 1).float() * 255.0).round().to(torch.uint8)
        y = y.permute(1, 2, 0).cpu().numpy()
        return y.shape[1], y.shape[0], y.tobytes()
