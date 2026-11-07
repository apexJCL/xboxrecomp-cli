"""Command backends (D7): a tool the owner installed, run as a subprocess.

A preset (presets.toml) is a template over a tool, never a binary or a model:
the CLI downloads nothing and runs nothing it did not find. The template is
split first and the placeholders put into the argument list after, so a file
name with shell characters in it stays one argument and no shell is involved.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib

from .. import png
from .base import Param, Upscaler, UpscalerError

PLACEHOLDERS = (
    "in",
    "out",
    "in_dir",
    "out_dir",
    "scale",
    "model",
    "model_dir",
    "tile",
    "gpu",
    "device",
    "sha256",
    "overrides",
)
# The tracked keys of an [upscaler.NAME] table a command backend reads; the
# machine's own, a program and a model among them, are textures_cfg.LOCAL_KEYS.
TABLE_KEYS = (
    "preset",
    "scales",
    "batch",
    "timeout",
    "tile",
    "sha256",
    "allow_model_mismatch",
)
# The torch plugin's interface: another major is refused, a newer minor warned of.
PLUGIN_CONTRACT = (1, 0)
# plugins/torch/pyproject.toml's exact pins; a test keeps the two equal.
PLUGIN_PINS = ("torch==2.6.0", "spandrel==0.4.1")
PLUGIN_REPO = "https://github.com/apexJCL/xboxrecomp-cli"
PLUGIN_INDEX = {
    "cuda": "https://download.pytorch.org/whl/cu126",
    "rocm": "https://download.pytorch.org/whl/rocm6.2.4",
}
DEFAULT_TIMEOUT = 3600
PRESETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.toml")
FLAGGED = ("CC-BY-NC", "custom", "disputed")
# 1.2, v1.2.3-rc1, or a build date such as 20220728.
# A suffix only from this short list: anything longer can be a build's name, a
# user's or a host's, which stays out of the record.
VERSION = re.compile(r"v?(\d+(?:\.\d+)+(?:-(?:rc|alpha|beta|pre|dev)\.?\d*)?|\d{8})")
# A bare major is any number's shape, so only where a tool prints its version.
VERSION_MAJOR = re.compile(r"v?(\d{1,3})")


def presets():
    with open(PRESETS_FILE, "rb") as f:
        return tomllib.load(f)


def model_flag(licence, restricted=False):
    """Why a model's licence is worth a look; it informs, it never refuses."""
    if restricted or any(x.lower() in licence.lower() for x in FLAGGED):
        return "check its licence: %s" % licence
    return ""


def substitute(template, values):
    """The argv of a template: split as a shell would, with no shell. A value
    stays one argument whatever it holds, and a placeholder this template did
    not expect a value for is refused rather than run as literal text."""

    def value(m):
        if m.group(1) not in values:
            raise UpscalerError("%s: no value for %s" % (template, m.group(0)))
        return str(values[m.group(1)])

    # One pass, so a file name holding "{out}" stays that name.
    mark = re.compile(r"\{(%s)\}" % "|".join(PLACEHOLDERS))
    return [mark.sub(value, arg) for arg in shlex.split(template)]


def contract_of(line):
    """(major, minor) of a `... contract M.N` version line, or None."""
    m = re.search(r"contract\s+(\d+)\.(\d+)", line)
    return (int(m.group(1)), int(m.group(2))) if m else None


def pad_to(w, h, rgba, side):
    """Replicate the last column and row until both sides reach `side`. The
    image stays at the top left, so the crop back is the same corner."""
    nw, nh = max(w, side), max(h, side)
    if (nw, nh) == (w, h):
        return w, h, rgba
    rows = []
    for y in range(h):
        row = rgba[y * w * 4 : (y + 1) * w * 4]
        rows.append(row + row[-4:] * (nw - w))
    rows += [rows[-1]] * (nh - h)
    return nw, nh, b"".join(rows)


def crop_to(w, h, rgba, keep_w, keep_h):
    """The top left keep_w x keep_h of an image."""
    return b"".join(rgba[y * w * 4 : (y * w + keep_w) * 4] for y in range(keep_h))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_token(line):
    """The version in a tool's first line, or "". A tool with no --version
    answers with a usage or an error line, which can hold anything of the
    owner's (a path, a user name), and the record is the owner's to share, so
    only what has a version's shape goes in it."""
    words = re.sub(r"contract\s+\d+\.\d+", "", line).split()
    for i, word in enumerate(words):
        word = word.strip("()[],;:")
        m = VERSION.fullmatch(word) or (VERSION_MAJOR.fullmatch(word) if i < 2 else None)
        if m:
            return m.group(1)
    return ""


def tool_identity(path, run=subprocess.run):
    """(sha256, first line of its --version or nothing): the line is for the
    contract check; version_token is what the record keeps of it."""
    sha = _sha256_file(path)
    line = ""
    try:
        r = run(
            [path, "--version"],
            capture_output=True,
            timeout=30,
            text=True,
            errors="replace",
            check=False,
        )
        for text in (r.stdout, r.stderr):
            if text.strip():
                line = text.strip().splitlines()[0][:200]
                break
    except (OSError, subprocess.SubprocessError):
        pass
    return sha, line


class Command(Upscaler):
    """One tool. `mode` is `dir` (it takes an input and an output folder, the
    whole batch per call) or `file` (one call per image)."""

    kind = "command"
    alpha = "rgb"
    deterministic = False
    warning = ""
    reproducible_scope = "none"

    def __init__(self, name, preset, table, path):
        self.name = name
        self.preset = preset
        self.table = table
        self.path = path
        self.template = table.get("template") or preset["template"]
        self.mode = preset.get("mode", "dir" if "{in_dir}" in self.template else "file")
        # A model does what it does: its native scale, the build shrinks from it.
        model = table.get("model") or preset.get("model", "")
        model_row = presets().get("model", {}).get(_stem(model), {})
        native = [model_row["native_scale"]] if "native_scale" in model_row else None
        native = model_row.get("scales") or native
        self.scales = tuple(table.get("scales") or native or preset.get("scales", (2, 3, 4)))
        self.batch = table.get("batch") or (64 if self.mode == "dir" else 1)
        self.licence = preset.get("licence", "")
        self.homepage = preset.get("homepage", "")
        self.flag = preset.get("flag", "")
        self.timeout = table.get("timeout", preset.get("timeout", DEFAULT_TIMEOUT))
        self.input_name = preset.get("input_name", "")
        self.min_side = preset.get("min_side", 0)
        self.is_plugin = "contract" in preset
        self.reported = {}
        self.params = (
            Param("model", "str", preset.get("model", ""), None, None, "the model's name"),
            Param("tile", "int", 0, 0, 4096, "tile size, 0 for the tool's own choice"),
        )
        self._identity = None

    def values(self, scale, table):
        # No default for model_dir: a template that needs it says so at the run.
        extra = {"model_dir": table["model_dir"]} if table.get("model_dir") else {}
        return {
            **extra,
            "scale": scale,
            "model": table.get("model", self.preset.get("model", "")),
            "tile": table.get("tile", 0),
            "gpu": table.get("gpu", -1 if table.get("device") == "cpu" else 0),
            "device": table.get("device", "auto"),
            "sha256": table.get("sha256", ""),
        }

    def identity(self):
        if self._identity is None:
            sha, line = tool_identity(self.path)
            self._identity = {
                "backend": self.kind,
                "name": self.name,
                "preset": self.preset.get("name", self.name),
                "tool_sha256": sha,
                "tool_version": version_token(line),
                "licence": self.licence,
                "reproducible": False,
            }
            if self.is_plugin:
                self._check_contract(line)
            model = self.table.get("model") or self.preset.get("model")
            if model:
                # A file's name, never the folder it is in.
                self._identity["model"] = os.path.basename(model)
                row = presets().get("model", {}).get(_stem(model), {})
                flag = row.get("flag") or model_flag(row.get("licence", ""))
                if row:
                    self._identity["model_licence"] = row.get("licence", "")
                if flag:
                    self._identity["flag"] = flag
        return dict(self._identity, **self.reported)

    def input_key(self):
        """What the result follows besides the config's text: the tool's bytes
        and the model files', wherever they live. Names only: the key is the
        state file's, and a moved file with the same bytes is the same tool."""
        return {"tool": self.identity()["tool_sha256"], "model": self._model_files()}

    def _model_files(self):
        """{name: sha256} of the files `model` names: the file or folder, at a
        path as written or a bare name beside the tool (a folder is how waifu2x
        and RealCUGAN name one), and <name>.* or <name>-x<scale>.* in
        model_dir, or in the models folder beside the tool, where ncnn looks
        when it is given none. A model a wrapper or a plugin finds by itself is
        not seen here."""
        model = self.table.get("model") or self.preset.get("model", "")
        if not model:
            return {}
        files = {}
        beside = os.path.dirname(os.path.realpath(self.path))
        # A bare name is the tool's to look up, not the working directory's: a
        # folder of that name there says nothing of the model.
        where = model if "/" in model or os.sep in model else os.path.join(beside, model)
        if os.path.isfile(where):
            files[os.path.basename(where)] = _sha256_file(where)
        elif os.path.isdir(where):
            for root, dirs, names in os.walk(where):
                dirs.sort()
                for name in sorted(names):
                    path = os.path.join(root, name)
                    files[os.path.relpath(path, where)] = _sha256_file(path)
        folder = self.table.get("model_dir") or os.path.join(beside, "models")
        if os.path.isdir(folder):
            stem = _stem(model)
            # An ncnn model is <name>.param and <name>.bin, or <name>-x2.bin for
            # the one it names by scale; a name that only starts the same is
            # another model.
            ours = re.compile(re.escape(stem) + r"(?:-x\d)?(?:\..*)?", re.DOTALL)
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if ours.fullmatch(name) and os.path.isfile(path):
                    files[name] = _sha256_file(path)
        return files

    def _check_contract(self, line):
        got = contract_of(line)
        install = "xbr enhance textures backend install torch prints the line that matches"
        if got is None or got[0] != PLUGIN_CONTRACT[0]:
            raise UpscalerError(
                "%s: contract %s, this CLI speaks %d.x: %s"
                % (self.name, "%d.%d" % got if got else "unknown", PLUGIN_CONTRACT[0], install)
            )
        if got[1] > PLUGIN_CONTRACT[1]:
            self.warning = "%s: contract %d.%d is newer than this CLI's %d.%d" % (
                self.name,
                *got,
                *PLUGIN_CONTRACT,
            )

    def _run(self, argv):
        argv = [self.path] + argv[1:]
        try:
            r = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired as e:
            raise UpscalerError("%s: over its %d s" % (self.name, self.timeout)) from e
        except OSError as e:
            raise UpscalerError("%s: %s" % (self.name, e)) from e
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            raise UpscalerError("%s: exit %d: %s" % (self.name, r.returncode, " / ".join(tail)))
        if self.is_plugin:
            self._take_report(r.stdout)

    def _take_report(self, stdout):
        """The plugin's one JSON line says what really ran: the record's."""
        for line in reversed(stdout.strip().splitlines()):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                self.reported = {
                    k: doc[k]
                    for k in (
                        "arch",
                        "native_scale",
                        "model_sha256",
                        "versions",
                        "device",
                        "dtype",
                        "tile",
                        "allow_model_mismatch",
                    )
                    if k in doc
                }
                # Host-bound at best: same machine, CPU, deterministic algorithms.
                self.reproducible_scope = doc.get("reproducible_scope", "none")
                self.reported["reproducible_scope"] = self.reproducible_scope
                return

    def upscale(self, images, scale, params=None):
        # The contract is checked by identity(), before the tool sees an image.
        self.identity()
        table = dict(self.table)
        table.update(params or {})
        values = self.values(scale, table)
        with tempfile.TemporaryDirectory(prefix="xbr-upscale-") as tmp:
            in_dir, out_dir = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(in_dir)
            os.makedirs(out_dir)
            names = []
            sizes = []
            for i, (w, h, rgba) in enumerate(images):
                name = "%06d.png" % i
                if self.min_side:
                    w, h, rgba = pad_to(w, h, rgba, self.min_side)
                sizes.append((w, h))
                png.write(os.path.join(in_dir, name), w, h, rgba, alpha=self.alpha == "rgba")
                names.append(name)
            over = self._overrides(tmp, in_dir, out_dir)
            if over:
                values["overrides"] = over
            if self.mode == "dir":
                self._run(substitute(self.template, dict(values, in_dir=in_dir, out_dir=out_dir)))
            else:
                for name in names:
                    src = os.path.join(in_dir, name)
                    if self.input_name:
                        # A tool that names its input blob: its own folder per call.
                        one = os.path.join(tmp, "one")
                        os.makedirs(one, exist_ok=True)
                        link = os.path.join(one, self.input_name)
                        shutil.copyfile(src, link)
                        src = link
                    self._run(
                        substitute(
                            self.template,
                            {
                                **values,
                                "in": src,
                                "out": os.path.join(out_dir, name),
                                "in_dir": in_dir,
                                "out_dir": out_dir,
                            },
                        )
                    )
            out = []
            for i, name in enumerate(names):
                pw, ph, rgba = self._read_out(out_dir, name, sizes[i], scale)
                iw, ih = images[i][:2]
                if (pw, ph) != (iw * scale, ih * scale):
                    rgba = crop_to(pw, ph, rgba, iw * scale, ih * scale)
                out.append((iw * scale, ih * scale, rgba))
            return out

    def _overrides(self, tmp, in_dir, out_dir):
        """A chain's folder nodes, as chaiNNer's --override file wants them: the
        node ids are the owner's chain's, so the config names them."""
        rows = self.table.get("overrides")
        if not rows:
            return ""
        path = os.path.join(tmp, "overrides.json")
        marks = {"{in_dir}": in_dir, "{out_dir}": out_dir}
        doc = {"inputs": {k: marks.get(v, v) for k, v in rows.items()}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def _read_out(self, out_dir, name, image, scale):
        path = os.path.join(out_dir, name)
        try:
            w, h, rgba = png.read(path)
        except (OSError, png.PngError) as e:
            raise UpscalerError("%s wrote no usable %s: %s" % (self.name, name, e)) from e
        if (w, h) != (image[0] * scale, image[1] * scale):
            raise UpscalerError(
                "%s made %dx%d of a %dx%d at %dx, not the size its scale says"
                % (self.name, w, h, image[0], image[1], scale)
            )
        return w, h, rgba


def _stem(model):
    name = os.path.basename(model)
    for ext in (".safetensors", ".pth", ".pt", ".ckpt"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def find_tool(preset, table):
    """The tool's path: the local file's, else on PATH; None when not found."""
    path = table.get("path")
    if path:
        return path if os.path.isfile(path) else None
    return shutil.which(preset.get("binary", ""))


def from_config(name, config, local):
    """The Command an `[upscaler.NAME]` table names, or the preset of that name.
    Tracked and local tables are merged by textures_cfg before this."""
    tracked, own = config.get(name, {}), local.get(name, {})
    if not (isinstance(tracked, dict) and isinstance(own, dict)):
        raise UpscalerError("[upscaler.%s]: not a table" % name)
    table = dict(tracked)
    table.update(own)
    all_presets = presets().get("tool", {})
    preset_name = table.get("preset", name)
    preset = all_presets.get(preset_name)
    if preset is None and "template" not in table:
        raise UpscalerError(
            "no upscaler %s: built-in nearest, box, epx, lanczos, xbr; presets %s; or an "
            "[upscaler.%s] table with a template" % (name, ", ".join(sorted(all_presets)), name)
        )
    preset = dict(preset or {}, name=preset_name)
    if "binary" in table:
        preset["binary"] = table["binary"]
    elif "template" in table and "binary" not in preset:
        preset["binary"] = shlex.split(table["template"])[0]
    path = find_tool(preset, table)
    if path is None:
        raise UpscalerError(
            "%s: %s is not installed here. It is yours to install (%s, %s), and its models "
            "its own; the CLI downloads nothing. A path goes in %s"
            % (
                name,
                preset.get("binary", "its program"),
                preset.get("homepage", "no homepage known"),
                preset.get("licence", "licence unknown"),
                "config/enhance-textures.local.toml",
            )
        )
    return Command(name, preset, table, path)


def stage_table(table):
    """An [upscaler.NAME] table as the build's stage key digests it: without
    where things are, since input_key has their bytes. Moving a tool or a model
    is no reason to rebuild. A service's URL and workflow file are where it
    is too: its identity says what answered."""
    where = ("path", "model_dir", "url", "token_env", "allow_plain_http", "workflow")
    out = {k: v for k, v in table.items() if k not in where}
    if "model" in out:
        out["model"] = os.path.basename(out["model"])
    return out


def plugin_install_lines(version):
    """(platform, line) for `backend install torch`: printed, never run. The
    plugin is not on PyPI, so its source is this CLI's repository at this CLI's
    tag. `uv tool install` ignores a lockfile: the pins go on the line too. A
    GPU index because PyPI's Windows torch is CPU-only."""
    source = "xbr-upscale-torch @ git+%s@v%s#subdirectory=plugins/torch" % (PLUGIN_REPO, version)
    pins = " ".join("--with %s" % p for p in PLUGIN_PINS)
    base = 'uv tool install "%s" %s' % (source, pins)
    return [
        ("macOS (MPS) and CPU-only hosts", base),
        ("Windows and Linux with NVIDIA", base + " --index %s" % PLUGIN_INDEX["cuda"]),
        ("Linux with AMD", base + " --index %s" % PLUGIN_INDEX["rocm"]),
        ("from a clone of this repository", "uv tool install ./plugins/torch %s" % pins),
    ]


def available():
    """Every preset with whether it was found, for `backend` and `compare`."""
    rows = []
    for name, p in sorted(presets().get("tool", {}).items()):
        rows.append(
            {
                "name": name,
                "found": bool(shutil.which(p.get("binary", ""))),
                "binary": p.get("binary", ""),
                "licence": p.get("licence", ""),
                "homepage": p.get("homepage", ""),
                "flag": p.get("flag", ""),
                "experimental": p.get("experimental", False),
            }
        )
    return rows


def model_rows():
    return [dict(v, name=k) for k, v in sorted(presets().get("model", {}).items())]
