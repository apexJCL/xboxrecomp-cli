"""The interface every upscaler has (D7): built-in, command, later a service."""


class UpscalerError(Exception):
    pass


class Param:
    """One knob, as `describe()` hands it to whoever draws a form."""

    __slots__ = ("name", "type", "default", "low", "high", "help")

    def __init__(self, name, type_, default, low, high, help_):
        self.name = name
        self.type = type_
        self.default = default
        self.low = low
        self.high = high
        self.help = help_

    def as_dict(self):
        out = {"name": self.name, "type": self.type, "default": self.default, "help": self.help}
        if self.low is not None:
            out["min"], out["max"] = self.low, self.high
        return out

    def check(self, value):
        """Why a config's value will not do, or ""."""
        if self.type == "bool":
            return "" if isinstance(value, bool) else "true or false"
        if self.type == "str":
            return "" if isinstance(value, str) else "a string"
        values = value if self.type == "ints" else [value]
        if self.type == "ints" and (not isinstance(value, list) or len(value) != len(self.default)):
            return "%d whole numbers" % len(self.default)
        for v in values:
            if not isinstance(v, int) or isinstance(v, bool):
                return "a whole number"
            if self.low is not None and not self.low <= v <= self.high:
                return "from %s to %s" % (self.low, self.high)
        return ""


class Upscaler:
    """`upscale(images, scale, params)` maps (w, h, rgba) images to images
    `scale` times as large. `scales` are what it does natively (the build
    reaches others through down-to-scale); `alpha` is `rgba` when it takes alpha
    as it is, `rgb` when it needs the split; `batch` how many images one call
    is worth (1: one per task, in a pool of the build's)."""

    name = ""
    kind = "builtin"
    scales = ()
    alpha = "rgba"
    deterministic = True
    batch = 1
    params = ()
    licence = ""
    homepage = ""
    flag = ""
    reproducible_scope = "none"

    def upscale(self, images, scale, params=None):
        params = params or {}
        return [
            (w * scale, h * scale, self.upscale_one(w, h, rgba, scale, params))
            for w, h, rgba in images
        ]

    def upscale_one(self, w, h, rgba, scale, params):
        raise NotImplementedError

    def describe(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "scales": list(self.scales),
            "alpha": self.alpha,
            "deterministic": self.deterministic,
            "batch": self.batch,
            "licence": self.licence,
            "homepage": self.homepage,
            "flag": self.flag,
            "params": [p.as_dict() for p in self.params],
        }

    def identity(self):
        """What the pack's record says this was: no path, no URL."""
        return {"backend": self.kind, "name": self.name}

    def native_for(self, scale):
        """The scale to run at for a target: the target, else the smallest
        native one over it (the build then shrinks to the target), or None."""
        if scale in self.scales:
            return scale
        over = [s for s in self.scales if s > scale]
        return min(over) if over else None

    def check_params(self, params):
        """Refuse a config's knob this upscaler does not have, or a bad value."""
        known = {p.name: p for p in self.params}
        for k, v in params.items():
            p = known.get(k)
            if p is None:
                raise UpscalerError(
                    "%s has no parameter %s (%s)"
                    % (self.name, k, ", ".join(sorted(known)) or "it has none")
                )
            why = p.check(v)
            if why:
                raise UpscalerError("%s %s: %s" % (self.name, k, why))
