"""The upscaler registry (D7): every backend behind one interface, so the build,
`compare` and a later launcher treat them alike.

Built-ins are always here. Command presets (presets.toml) are registered by
`available()` from a config's choices; the CLI runs nothing it did not find.
A table with a `url` (or an `api`) is a service (http.py).
"""

from .base import Param, Upscaler, UpscalerError
from .builtin import ALL as BUILTINS

__all__ = ["BUILTINS", "Param", "Upscaler", "UpscalerError", "by_name", "get"]


def get(name):
    return next((u for u in BUILTINS if u.name == name), None)


def by_name(name, config=None, local=None):
    """The upscaler a config names: a built-in, or an `[upscaler.NAME]` table's
    service (http.py) or command preset (command.py). UpscalerError names what
    is missing."""
    up = get(name)
    if up is not None:
        return up
    tables = [t for t in ((config or {}).get(name), (local or {}).get(name)) if isinstance(t, dict)]
    if any("url" in t or "api" in t for t in tables):
        from . import http

        return http.from_config(name, config or {}, local or {})
    from . import command

    return command.from_config(name, config or {}, local or {})
