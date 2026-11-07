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
import json
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
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
ARRAY_HEADER = re.compile(r"^\s*\[\[.*\]\]\s*(#.*)?$")


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
        # JSON's escapes are TOML's, but for DEL, which TOML wants escaped.
        return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(v, list):
        # A cell's [width, height], a model's scales.
        return "[%s]" % ", ".join(_toml_value(x) for x in v)
    raise ValueError("no TOML form for %r" % (v,))


def _keys(s, i=0):
    """The dotted key at s[i:] as a tuple and the index past it, or None. The
    owner may write a key bare, quoted either way, with spaces round the dots:
    `[rule.'menu_*']` and `[ rule."menu_*" ]` are one table."""
    parts = []
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if s.startswith('"', i):
            j = i + 1
            while j < len(s) and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            try:
                parts.append(tomllib.loads("k = " + s[i : j + 1])["k"])
            except tomllib.TOMLDecodeError:
                return None
            i = j + 1
        elif s.startswith("'", i):
            j = s.find("'", i + 1)
            if j < 0:
                return None
            parts.append(s[i + 1 : j])
            i = j + 1
        elif m := BARE_KEY.match(s, i):
            parts.append(m[0])
            i = m.end()
        else:
            return None
        while i < len(s) and s[i] in " \t":
            i += 1
        if not s.startswith(".", i):
            return tuple(parts), i
        i += 1


def _header(line):
    """`[a.b]`'s keys, or None for any other line."""
    s = line.lstrip()
    if not s.startswith("[") or s.startswith("[["):
        return None
    k = _keys(s, 1)
    if k is None or not s.startswith("]", k[1]):
        return None
    rest = s[k[1] + 1 :].strip()
    return k[0] if not rest or rest.startswith("#") else None


def _is_header(line):
    return _header(line) is not None or bool(ARRAY_HEADER.match(line))


def _table_keys(table):
    k = _keys(table)
    if k is None or k[1] != len(table):
        raise ValueError("no TOML table name %r" % table)
    return k[0]


def _value_end(lines, i, end, at):
    """(last line, what follows the value on it) of the value at lines[i][at:],
    which may run over several lines (an array), or None."""
    text = lines[i][at:]
    for j in range(i, end):
        if j > i:
            text += lines[j]
        body = text.rstrip("\n")
        last = body.rfind("\n") + 1
        # A `#` in a string is not a comment: the value is the shortest text
        # that reads as one.
        for p in [q for q in range(last, len(body)) if body[q] == "#"] + [len(body)]:
            v = body[:p].rstrip()
            try:
                tomllib.loads("k = " + v)
            except tomllib.TOMLDecodeError:
                continue
            return j, body[len(v) :]
    return None


def _entries(lines, start, end):
    """(first line, last line, keys, value's column, what follows the value)
    of each key in lines[start:end]."""
    i = start
    while i < end:
        x = lines[i]
        k = None if x.lstrip().startswith("#") else _keys(x)
        if k and x.startswith("=", k[1]):
            at = k[1] + 1
            while at < len(x) and x[at] in " \t":
                at += 1
            span = _value_end(lines, i, end, at)
            if span:
                yield i, span[0], k[0], at, span[1]
                i = span[0] + 1
                continue
        i += 1


def _edit_entries(lines, start, end, values, remove=()):
    """lines[start:end] with the keys in `values` set, in place or after the
    last key, and those in `remove` gone; every other line as it was."""
    todo = dict(values)
    out, prev = [], start
    for first, last, keys, at, tail in _entries(lines, start, end):
        out += lines[prev:first]
        prev = last + 1
        key = keys[0] if len(keys) == 1 else None
        if key in remove:
            continue
        if key in todo:
            out.append("%s%s%s\n" % (lines[first][:at], _toml_value(todo.pop(key)), tail))
        else:
            out += lines[first:prev]
    out += lines[prev:end]
    # New keys before the blank lines that end the block.
    at = len(out)
    while at and not out[at - 1].strip():
        at -= 1
    new = ["%s = %s\n" % (k, _toml_value(v)) for k, v in todo.items()]
    return out[:at] + new + out[at:]


def _lines(text):
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def set_table_keys(text, table, values):
    """The config's text with `[table]`'s keys set, every other line as it was:
    the owner's comments are theirs. A new table goes at the end, or before the
    first rule, since rules read best last and class tables together."""
    lines = _lines(text)
    span = _table_span(lines, table)
    if span is None:
        at = next(
            (i for i, x in enumerate(lines) if (h := _header(x)) and h[0] == "rule"),
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
    start, end = span
    return "".join(lines[: start + 1] + _edit_entries(lines, start + 1, end, values) + lines[end:])


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


def _table_span(lines, table):
    """(header, end) of `[table]`'s lines, or None."""
    want = _table_keys(table)
    start = next((i for i, x in enumerate(lines) if _header(x) == want), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _is_header(lines[i])), len(lines))
    return start, end


def remove_table_keys(text, table, keys):
    """The text with these keys of `[table]` gone, the rest as it was."""
    lines = _lines(text)
    span = _table_span(lines, table)
    if span is None:
        return text
    start, end = span
    kept = _edit_entries(lines, start + 1, end, {}, remove=keys)
    return "".join(lines[: start + 1] + kept + lines[end:])


def drop_table(text, table):
    """The text without `[table]` and its keys; its comments above stay, they
    may be about the next one."""
    lines = _lines(text)
    span = _table_span(lines, table)
    if span is None:
        return text
    start, end = span
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    rest = lines[end:]
    if rest and not rest[0].strip() and start and not lines[start - 1].strip():
        rest = rest[1:]
    return "".join(lines[:start] + rest)


def set_top_keys(text, values, remove=()):
    """The keys before the first table (`render_scale`, `preset`), set or gone."""
    lines = _lines(text)
    first = next((i for i, x in enumerate(lines) if _is_header(x)), len(lines))
    return "".join(_edit_entries(lines, 0, first, values, remove) + lines[first:])
