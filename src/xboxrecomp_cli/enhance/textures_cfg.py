"""config/enhance-textures.toml, and its untracked half (D8).

The tracked file holds names, numbers and hashes; the local file beside it
holds what is this machine's: a program's path, a model folder, a service's
URL. It also holds what says which program runs and on what (a template, a
binary, a model file): a repo someone cloned must not be able to run a program
of its choosing through its config. A preset is still chosen by name there,
since its template ships in the CLI. A tracked file with one of these keys is
refused, with the line that says where it goes.
"""

import hashlib
import os
import re
import tomllib

from . import alpha, classes, edges
from .upscalers import UpscalerError, command, get, http

LOCAL_NAME = "enhance-textures.local.toml"
LOCAL_KEYS = (
    "path",
    "model_dir",
    "url",
    "token_env",
    "allow_plain_http",
    "gpu",
    "device",
    "template",
    "binary",
    "model",
    "overrides",
    "workflow",
    "load_node",
    "save_node",
)
TOP_KEYS = {
    "schema",
    "preset",
    "render_scale",
    "coverage",
    "class",
    "rule",
    "upscaler",
    "budget",
    "font",
}
ROW_KEYS = {
    "scale": "2, 3 or 4",
    "upscaler": "an upscaler's name",
    "alpha": "|".join(alpha.MODES),
    "edges": "|".join(("auto",) + edges.MODES),
    "cell": "[width, height]",
    "mips": "none|keep|full",
    "encode": "raw|bc1|bc3|auto",
    "max_texels": "0 (no cap) or more",
    "max_side": "0 (no cap) or more",
    "skip": "true or false",
}
TABLE_LINE = re.compile(r"^\s*\[(?P<name>[^\[\]]+)\]\s*(#.*)?$")


class ConfigError(Exception):
    pass


class Config:
    """`doc` is the tracked file with the local one's upscaler tables over it.
    `path`, `sha256` and `text` are the tracked file's: "" with no file."""

    def __init__(self, doc, path="", sha256="", local_path=""):
        self.doc = doc
        self.path = path
        self.sha256 = sha256
        self.local_path = local_path

    @property
    def params(self):
        return self.doc.get("upscaler", {})


def local_path_of(path):
    return os.path.join(os.path.dirname(os.path.abspath(path)), LOCAL_NAME)


def _refuse_non_tables(doc, name):
    """Before anything reads them: a list would pass the key check below and
    read as a table's pairs further on, which would let a tracked file run a
    program."""
    ups = doc.get("upscaler", {})
    if not isinstance(ups, dict):
        raise ConfigError("%s: upscaler: not a table; one [upscaler.NAME] each" % name)
    for up, table in ups.items():
        if not isinstance(table, dict):
            raise ConfigError("%s: [upscaler.%s]: not a table" % (name, up))


def _refuse_machine_keys(doc, name):
    _refuse_non_tables(doc, name)
    for up, table in doc.get("upscaler", {}).items():
        for k in LOCAL_KEYS:
            if k in table:
                raise ConfigError(
                    "%s: [upscaler.%s] %s belongs in %s: this file is tracked and holds names, "
                    "numbers and hashes; what runs, on what model, and where, is this machine's "
                    "(a preset's name stays here)" % (name, up, k, LOCAL_NAME)
                )


def _check_row(where, row):
    if not isinstance(row, dict):
        raise ConfigError("%s: not a table" % where)
    for k, v in row.items():
        if k not in ROW_KEYS:
            raise ConfigError("%s: no key %s (%s)" % (where, k, ", ".join(sorted(ROW_KEYS))))
        bad = (
            # 2.0 == 2 and True == 1: a scale is a whole number, not what equals one.
            (k == "scale" and (type(v) is not int or v not in (2, 3, 4)))
            or (k == "alpha" and v not in alpha.MODES)
            or (k == "edges" and v not in ("auto",) + edges.MODES)
            or (k == "mips" and v not in ("none", "keep", "full"))
            or (k == "encode" and v not in ("raw", "bc1", "bc3", "auto"))
            or (k in ("max_texels", "max_side") and (type(v) is not int or v < 0))
            or (k == "skip" and not isinstance(v, bool))
            or (k == "upscaler" and not isinstance(v, str))
            or (
                k == "cell"
                and not (
                    isinstance(v, list) and len(v) == 2 and all(type(x) is int and x > 0 for x in v)
                )
            )
        )
        if bad:
            raise ConfigError("%s: %s = %r: %s" % (where, k, v, ROW_KEYS[k]))
    if row.get("edges") == "cells" and "cell" not in row:
        raise ConfigError('%s: edges = "cells" needs cell = [width, height]' % where)
    # Any other edges pads the image whole, which would ignore the cell in silence.
    if "cell" in row and row.get("edges") != "cells":
        raise ConfigError('%s: cell needs edges = "cells" in the same table' % where)


# What a key must be, where a wrong type would surface as a raw error at the
# run or pass unnoticed into the tool's arguments.
_TABLE_TYPES = {
    **dict.fromkeys(
        (
            "path",
            "model_dir",
            "url",
            "token_env",
            "device",
            "template",
            "binary",
            "model",
            "workflow",
            "load_node",
            "save_node",
        ),
        (str, "a string"),
    ),
    "allow_plain_http": (bool, "true or false"),
    "allow_model_mismatch": (bool, "true or false"),
    "gpu": (int, "a whole number"),
    "overrides": (dict, "a table"),
}


def _check_table_numbers(name, up, table):
    for k, (kind, say) in _TABLE_TYPES.items():
        # `type`: True is an int to isinstance.
        if k in table and type(table[k]) is not kind:
            raise ConfigError("%s: [upscaler.%s] %s: %s" % (name, up, k, say))
    for k in ("batch", "timeout", "tile"):
        if k in table and (type(table[k]) is not int or table[k] < 0):
            raise ConfigError("%s: [upscaler.%s] %s: a whole number" % (name, up, k))
    if "scales" in table:
        v = table["scales"]
        if not (isinstance(v, list) and v and all(type(x) is int and x > 0 for x in v)):
            raise ConfigError("%s: [upscaler.%s] scales: a list of whole numbers" % (name, up))
    if "api" in table and table["api"] not in http.APIS:
        raise ConfigError("%s: [upscaler.%s] api: %s" % (name, up, " or ".join(http.APIS)))
    # It goes on the tool's command line, so it holds a hash and nothing else.
    if "sha256" in table and not (
        isinstance(table["sha256"], str) and re.fullmatch(r"[0-9a-fA-F]{64}", table["sha256"])
    ):
        raise ConfigError("%s: [upscaler.%s] sha256: 64 hex digits" % (name, up))


def validate(doc, name="the config"):
    """ConfigError on what the build could not honour. A table of an upscaler no
    row uses may stay, so the owner's other experiments stay in the file, but it
    is checked like the rest."""
    _refuse_non_tables(doc, name)
    if doc.get("schema", classes.CONFIG_SCHEMA) != classes.CONFIG_SCHEMA:
        raise ConfigError(
            "%s: schema %r, this reads %d" % (name, doc.get("schema"), classes.CONFIG_SCHEMA)
        )
    for k in doc:
        if k not in TOP_KEYS:
            raise ConfigError("%s: no key %s (%s)" % (name, k, ", ".join(sorted(TOP_KEYS))))
    r = doc.get("render_scale", 1)
    if not isinstance(r, int) or isinstance(r, bool) or r < 1:
        raise ConfigError("%s: render_scale: a whole number of 1 or more" % name)
    cov = doc.get("coverage", {})
    if cov.get("mode", "classes") not in ("classes", "traced"):
        raise ConfigError("%s: coverage.mode: classes or traced" % name)
    if cov.get("untraced", "class") not in ("class", "skip"):
        raise ConfigError("%s: coverage.untraced: class or skip" % name)
    for cls, row in doc.get("class", {}).items():
        if cls not in classes.CLASSES:
            raise ConfigError(
                "%s: [class.%s]: the classes are %s" % (name, cls, ", ".join(classes.CLASSES))
            )
        _check_row("%s: [class.%s]" % (name, cls), row)
    for key, row in doc.get("rule", {}).items():
        _check_row('%s: [rule."%s"]' % (name, key), row)
    for key in ("pack_mb", "vram_mb"):
        v = doc.get("budget", {}).get(key, 0)
        if not isinstance(v, int) or v < 0:
            raise ConfigError("%s: budget.%s: 0 (no warning) or more" % (name, key))
    # Every table, used or not: `compare` runs them all.
    for up, table in sorted(doc.get("upscaler", {}).items()):
        u = get(up)
        if u is None:
            known = command.TABLE_KEYS + http.TABLE_KEYS + LOCAL_KEYS
            for k in table:
                if k not in known:
                    raise ConfigError("%s: [upscaler.%s] no key %s" % (name, up, k))
            _check_table_numbers(name, up, table)
            continue
        try:
            u.check_params({k: v for k, v in table.items() if k not in LOCAL_KEYS})
        except UpscalerError as e:
            raise ConfigError("%s: %s" % (name, e)) from e
    used = {
        row["upscaler"]
        for tab in ("class", "rule")
        for row in doc.get(tab, {}).values()
        if "upscaler" in row
    }
    for up in sorted(used - {"atlas"}):
        table = doc.get("upscaler", {}).get(up, {})
        # A tool not on this machine is the build's to say: the listing and the
        # estimate of a config from another machine still work.
        if (
            get(up) is None
            and table.get("preset", up) not in command.presets()["tool"]
            and "template" not in table
            and "url" not in table
            and "api" not in table
        ):
            raise ConfigError(
                "%s: no upscaler %s: a built-in, a preset, or an [upscaler.%s] table "
                "with a template or a url" % (name, up, up)
            )


def _read(path):
    with open(path, "rb") as f:
        raw = f.read()
    return raw, tomllib.loads(raw.decode("utf-8"))


def _merge_local(doc, path):
    """The local file's upscaler tables over `doc`; its path, or "" with none."""
    local = local_path_of(path)
    if not os.path.exists(local):
        return ""
    try:
        _, over = _read(local)
    except (OSError, ValueError) as e:
        raise ConfigError("%s: %s" % (local, e)) from e
    _refuse_non_tables(over, local)
    for up, table in over.get("upscaler", {}).items():
        doc.setdefault("upscaler", {}).setdefault(up, {}).update(table)
    return local


def load(path):
    """The Config at `path`, the local file beside it merged over it."""
    try:
        raw, doc = _read(path)
    except (OSError, ValueError) as e:
        raise ConfigError("%s: %s" % (path, e)) from e
    _refuse_machine_keys(doc, path)
    local = _merge_local(doc, path)
    validate(doc, path)
    return Config(doc, path, hashlib.sha256(raw).hexdigest(), local)


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')
    raise ValueError("no TOML form for %r" % (v,))


def set_table_keys(text, table, values):
    """The config's text with `[table]`'s keys set, every other line as it was:
    the owner's comments are theirs. A new table goes at the end, or before the
    first rule, since rules read best last and class tables together."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next(
        (i for i, x in enumerate(lines) if (m := TABLE_LINE.match(x)) and m["name"] == table),
        None,
    )
    if start is None:
        at = next(
            (
                i
                for i, x in enumerate(lines)
                if (m := TABLE_LINE.match(x)) and m["name"].startswith("rule.")
            ),
            len(lines),
        )
        while at and lines[at - 1].lstrip().startswith("#"):
            at -= 1
        block = ["[%s]\n" % table] + ["%s = %s\n" % (k, _toml_value(v)) for k, v in values.items()]
        before = lines[:at]
        if before and before[-1].strip():
            block.insert(0, "\n")
        after = lines[at:]
        if after:
            block.append("\n")
        return "".join(before + block + after)
    end = next((i for i in range(start + 1, len(lines)) if TABLE_LINE.match(lines[i])), len(lines))
    todo = dict(values)
    for i in range(start + 1, end):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*=\s*)([^#]*?)(\s*(#.*)?)$", lines[i])
        if m and m[2] in todo:
            lines[i] = "%s%s%s%s%s\n" % (m[1], m[2], m[3], _toml_value(todo.pop(m[2])), m[5])
    # New keys after the table's last key, before the blank line that ends it.
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    new = ["%s = %s\n" % (k, _toml_value(v)) for k, v in todo.items()]
    return "".join(lines[:at] + new + lines[at:])


def write_class_row(path, cls, upscaler, scale=None):
    """`pick`: the class's row in the tracked file. A file that would not read
    back leaves the old one in place."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = "schema = %d\n" % classes.CONFIG_SCHEMA
    values = {"upscaler": upscaler}
    if scale:
        values["scale"] = scale
    new = set_table_keys(text, "class." + cls, values)
    doc = tomllib.loads(new)
    _refuse_machine_keys(doc, path)
    # A class may name a table only the local file has.
    _merge_local(doc, path)
    validate(doc, path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    os.replace(tmp, path)
