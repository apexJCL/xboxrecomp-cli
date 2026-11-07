"""The ComfyUI adapter (D7): a running ComfyUI server and a workflow of the
owner's, exported in the API format ("Save (API)").

Every workflow differs, so the adapter fills only what it must: the image of
the load-image node, the file name prefix of the save-image node, and, when the
local file names a model, the model of each upscale-model loader (checked
against the server's own list first). Each texture is uploaded, queued, waited
for and fetched back:

  POST /upload/image      the texture, as a form upload
  POST /prompt            the workflow with those nodes filled
  GET  /history/{id}      until the prompt is done
  GET  /view              the saved image

The workflow decides the scale, so the table says it (`scales = [4]`), and
the answer's PNG header is held to it.
"""

import copy
import hashlib
import json
import os
import time
import urllib.parse
import uuid

from .. import png
from .base import UpscalerError
from .http import INFO_CAP, Service, _json, check_png, png_cap

LOAD = ("LoadImage",)
SAVE = ("SaveImage",)
MODEL_LOADER = "UpscaleModelLoader"
WORKFLOW_CAP = 4 << 20
POLL_FIRST = 0.1  # seconds, doubled up to POLL_MAX
POLL_MAX = 2.0


def _one_node(wf, kinds, key, table, name):
    """The id of the workflow's node of one of these class types: the table's
    `key` when it names one, else the only such node."""
    want = table.get(key)
    if want:
        node = wf.get(str(want))
        if not isinstance(node, dict) or node.get("class_type") not in kinds:
            raise UpscalerError(
                "%s: %s = %s is not a %s node of the workflow" % (name, key, want, "/".join(kinds))
            )
        return str(want)
    ids = sorted(k for k, n in wf.items() if n.get("class_type") in kinds)
    if len(ids) != 1:
        raise UpscalerError(
            "%s: the workflow has %d %s nodes: %s = the node id, in the local file"
            % (name, len(ids), "/".join(kinds), key)
        )
    return ids[0]


def load_workflow(path, name):
    """The workflow's nodes, {id: {"class_type", "inputs"}}, and its sha256."""
    if not path:
        raise UpscalerError(
            "%s: api = comfyui needs workflow = the API-format JSON of your workflow, "
            "in the local file" % name
        )
    try:
        with open(os.path.expanduser(path), "rb") as f:
            raw = f.read(WORKFLOW_CAP + 1)
    except OSError as e:
        raise UpscalerError("%s: workflow: %s" % (name, e)) from e
    if len(raw) > WORKFLOW_CAP:
        raise UpscalerError("%s: workflow: over %d bytes" % (name, WORKFLOW_CAP))
    try:
        wf = json.loads(raw)
    except ValueError as e:
        raise UpscalerError("%s: workflow: not JSON: %s" % (name, e)) from e
    if not (
        isinstance(wf, dict)
        and wf
        and all(
            isinstance(n, dict)
            and isinstance(n.get("class_type"), str)
            and isinstance(n.get("inputs"), dict)
            for n in wf.values()
        )
    ):
        raise UpscalerError(
            "%s: workflow: not the API format (each node a class_type and inputs): "
            "export it with Save (API)" % name
        )
    return wf, hashlib.sha256(raw).hexdigest()


def _combo(spec):
    """A combo input's choices, in either shape /object_info has had: the
    older [[choices], {options}] and the newer ["COMBO", {"options": [...]}]."""
    if isinstance(spec, list) and spec:
        if isinstance(spec[0], list):
            return spec[0]
        if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            names = spec[1].get("options")
            return names if isinstance(names, list) else []
    return []


def _multipart(fields, file_field, file_name, data):
    """A multipart/form-data body: (content type, bytes)."""
    boundary = "xbr" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            (
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)
            ).encode()
        )
    parts.append(
        (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            "Content-Type: image/png\r\n\r\n" % (boundary, file_field, file_name)
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(("--%s--\r\n" % boundary).encode())
    return "multipart/form-data; boundary=" + boundary, b"".join(parts)


class ComfyUI(Service):
    api = "comfyui"

    def __init__(self, name, table, transport, sleep=time.sleep):
        super().__init__(name, table, transport)
        if not self._scales:
            raise UpscalerError(
                "%s: api = comfyui: scales = [N] in [upscaler.%s], the scale your workflow's "
                "model makes" % (name, name)
            )
        self.wf, self.wf_sha256 = load_workflow(table.get("workflow", ""), name)
        self.load_id = _one_node(self.wf, LOAD, "load_node", table, name)
        self.save_id = _one_node(self.wf, SAVE, "save_node", table, name)
        self.model_ids = sorted(k for k, n in self.wf.items() if n["class_type"] == MODEL_LOADER)
        if self.model and not self.model_ids:
            raise UpscalerError(
                "%s: model = %s, and the workflow has no %s node to put it in"
                % (name, self.model, MODEL_LOADER)
            )
        self.sleep = sleep
        self.client_id = uuid.uuid4().hex

    def _fetch_info(self):
        stats = self.t.get_json("/system_stats")
        if not isinstance(stats, dict) or "system" not in stats:
            raise UpscalerError("%s: /system_stats: not a ComfyUI server" % self.name)
        system = stats["system"] if isinstance(stats["system"], dict) else {}
        models = []
        if self.model:
            doc = self.t.get_json("/object_info/" + MODEL_LOADER)
            try:
                names = _combo(doc[MODEL_LOADER]["input"]["required"]["model_name"])
            except (KeyError, TypeError):
                names = []
            models = [
                {"name": n, "scales": list(self._scales), "alpha": "rgb"}
                for n in names
                if isinstance(n, str)
            ]
        return {
            "name": "comfyui",
            "version": str(system.get("comfyui_version", "")),
            "scales": list(self._scales),
            "models": models,
            "max_side": 0,
            "batch": 1,
        }

    def identity(self):
        return dict(super().identity(), workflow_sha256=self.wf_sha256)

    def _upload(self, w, h, rgba):
        stem = "xbr-" + uuid.uuid4().hex
        ctype, body = _multipart(
            {"type": "input", "overwrite": "true"},
            "image",
            stem + ".png",
            png.encode(w, h, rgba, alpha=False),
        )
        _, data = self.t.request("POST", "/upload/image", body, ctype, accept="application/json")
        doc = _json(data, self.name, "/upload/image")
        if not isinstance(doc, dict) or not isinstance(doc.get("name"), str):
            raise UpscalerError("%s: /upload/image answered no name" % self.name)
        sub = doc.get("subfolder") or ""
        return stem, (sub + "/" if sub else "") + doc["name"]

    def _wait(self, prompt_id):
        """The prompt's history entry once it has run, or an UpscalerError on
        its error or the table's timeout."""
        deadline = time.monotonic() + self.t.timeout
        pause = POLL_FIRST
        path = "/history/" + urllib.parse.quote(prompt_id, safe="")
        while True:
            doc = self.t.get_json(path, cap=INFO_CAP)
            entry = doc.get(prompt_id) if isinstance(doc, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    msgs = [
                        m[1].get("exception_message", "")
                        for m in status.get("messages", [])
                        if isinstance(m, list) and len(m) == 2 and isinstance(m[1], dict)
                    ]
                    raise UpscalerError(
                        "%s: the workflow failed: %s"
                        % (self.name, "; ".join(x for x in msgs if x)[:300] or "no message")
                    )
                if entry.get("outputs") and status.get("completed", True):
                    return entry
            if time.monotonic() > deadline:
                raise UpscalerError("%s: the prompt took over %d s" % (self.name, self.t.timeout))
            self.sleep(pause)
            pause = min(pause * 2, POLL_MAX)

    def upscale_one(self, w, h, rgba, scale, params):
        stem, image = self._upload(w, h, rgba)
        wf = copy.deepcopy(self.wf)
        wf[self.load_id]["inputs"]["image"] = image
        wf[self.save_id]["inputs"]["filename_prefix"] = stem
        for k in self.model_ids if self.model else ():
            wf[k]["inputs"]["model_name"] = self.model
        doc = self.t.post_json("/prompt", {"prompt": wf, "client_id": self.client_id})
        if not isinstance(doc, dict) or not isinstance(doc.get("prompt_id"), str):
            raise UpscalerError("%s: /prompt answered no prompt_id" % self.name)
        if doc.get("node_errors"):
            raise UpscalerError(
                "%s: the workflow was refused: %s"
                % (self.name, json.dumps(doc["node_errors"])[:300])
            )
        entry = self._wait(doc["prompt_id"])
        out = entry["outputs"].get(self.save_id, {})
        images = out.get("images") if isinstance(out, dict) else None
        if not images or not isinstance(images[0], dict) or "filename" not in images[0]:
            raise UpscalerError("%s: the save node %s saved no image" % (self.name, self.save_id))
        img = images[0]
        q = urllib.parse.urlencode(
            {
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            }
        )
        _, data = self.t.request(
            "GET", "/view?" + q, cap=png_cap(w * scale, h * scale), accept="image/png"
        )
        return check_png(data, w * scale, h * scale, self.name)[2]
