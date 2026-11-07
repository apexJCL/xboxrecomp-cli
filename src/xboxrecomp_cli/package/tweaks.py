"""The tweaks a patch takes from its base: its texture pack (D3, D4).

A bundle carries one pack, of either kind: the HUD's (enhance hud) or the
game's (enhance textures build). A base's manifest records it
(enhance.hud_pack or enhance.texture_pack); a patch made without one would
list the pack as deleted. So when the run has a base that carries a pack,
package asks for one of three flags before anything is built:

  --hud-pack [DIR], --texture-pack [DIR], --texture-preset NAME
                     a pack made now;
  --keep-hud-pack, --keep-texture-pack
                     the base's pack, the same bytes;
  --clear-hud-pack, --clear-texture-pack
                     no pack.

A kept HUD pack is looked up by its sha256: the pack store beside the bundle
records (.git/xbr/packs/), then dist/enhance/hud/hud.pack, then made
again from the inputs the base recorded (made_with) into a folder under
the build state. Each is accepted only when its sha256 is the base's. A
whole-game pack is never made again: it can take hours, or a tool and a model
that this machine has not got.
"""

import os
import shutil
import tomllib

from .. import host
from ..host import CliError
from . import incr, patch

REMAKE_DIR = "hud-remake"


class PackKind:
    """A kind of pack the bundle can carry: the file names and the flags."""

    def __init__(self, key, field, sub, name, toml, label, make, keep, clear):
        self.key = key
        self.field = field  # the manifest's enhance.<field>
        self.sub = sub  # dist/enhance/<sub>/
        self.name = name
        self.toml = toml
        self.label = label
        self.make = make
        self.keep = keep
        self.clear = clear
        self.rel = "enhance/" + name

    @property
    def flags(self):
        return (self.keep, self.clear, self.make)


HUD = PackKind(
    "hud",
    "hud_pack",
    "hud",
    "hud.pack",
    "hud-pack.toml",
    "HUD pack",
    "--hud-pack",
    "--keep-hud-pack",
    "--clear-hud-pack",
)
TEXTURES = PackKind(
    "texture",
    "texture_pack",
    "textures",
    "textures.pack",
    "textures-pack.toml",
    "texture pack",
    "--texture-pack",
    "--keep-texture-pack",
    "--clear-texture-pack",
)
PACKS = (HUD, TEXTURES)
HUD_FLAGS = HUD.flags
PACK_FLAGS = HUD.flags + TEXTURES.flags + ("--texture-preset",)


def kind_of_field(field):
    return next(k for k in PACKS if k.field == field)


def g():
    return host.g()


def hud_choice(a):
    """ "make", "keep", "clear", or None when no pack flag was given."""
    if getattr(a, "hud_pack", None) is not None or making_textures(a):
        return "make"
    if getattr(a, "keep_hud_pack", False) or getattr(a, "keep_texture_pack", False):
        return "keep"
    if getattr(a, "clear_hud_pack", False) or getattr(a, "clear_texture_pack", False):
        return "clear"
    return None


def making_textures(a):
    return (
        getattr(a, "texture_pack", None) is not None
        or getattr(a, "texture_preset", None) is not None
    )


def flag_kind(a):
    """The PackKind the run's own flag names."""
    textures = (
        making_textures(a)
        or getattr(a, "keep_texture_pack", False)
        or getattr(a, "clear_texture_pack", False)
    )
    return TEXTURES if textures else HUD


def run_base(a, target, st):
    """The base this run patches, as patch.choose would take it, known
    before anything is built (both read only manifests): --patch-from's,
    else the host's own install without --full; None for a full bundle."""
    if getattr(a, "full", False):
        return None
    spec = getattr(a, "patch_from", None)
    if spec:
        return patch.load(spec, incr.bundles_dir(st, target))
    try:
        return patch.host_base(target, g().package["app"], g().m)
    except (CliError, OSError, KeyError):
        return None


def manifest_pack(manifest):
    """(PackKind, record) of the pack a manifest records, or (None, None); a
    schema 1 manifest made before packs reads as none."""
    enh = (manifest.get("enhance") or {}) if isinstance(manifest, dict) else {}
    for kind in PACKS:
        rec = enh.get(kind.field)
        if isinstance(rec, dict) and isinstance(rec.get("sha256"), str):
            return kind, rec
    return None, None


def base_pack(base):
    """The base manifest's pack record, of either kind, or None."""
    return manifest_pack(base.manifest)[1] if base is not None else None


def base_kind(base):
    return manifest_pack(base.manifest)[0] if base is not None else None


def tweaks_of(manifest):
    """The plan's tweaks of a manifest: {"hud_pack": {sha256, scale,
    entries} | None}, and "texture_pack" alike when that is what it has."""
    kind, rec = manifest_pack(manifest)
    if kind is None:
        return {"hud_pack": None}
    out = {kind.field: {k: rec.get(k) for k in ("sha256", "scale", "entries")}}
    return dict({"hud_pack": None}, **out)


def guard_message(base):
    k = base_kind(base) or HUD
    return (
        "the base %s carries a %s: pass %s to keep it, %s to remove it, or %s to make a new one"
        % (
            base.version or "?",
            k.label,
            k.keep,
            k.clear,
            k.make,
        )
    )


def guard(a, base):
    """D3: refuse, before anything is built, a run whose base carries a
    pack and that names none of the three flags."""
    if base_pack(base) is not None and hud_choice(a) is None:
        raise host.Refusal(guard_message(base), patch.HUD_GUARD)


# ── made_with: what makes the same pack again ──────────────────────────


def read_pack_toml(pack_path):
    """The record beside a pack, parsed; None when missing or unreadable."""
    name = TEXTURES.toml if os.path.basename(pack_path) == TEXTURES.name else HUD.toml
    p = os.path.join(os.path.dirname(pack_path), name)
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def made_with(pack_path, sha):
    """The inputs that made the pack at pack_path, from the record
    beside it when that file names this pack (its sha256): scale,
    languages, --only, the font's sha256, the [enhance.hud] digest and the
    CLI's version. No paths, so the manifest stays free of private data.
    None when the file is another pack's or older than these fields."""
    doc = read_pack_toml(pack_path)
    if not isinstance(doc, dict) or doc.get("pack_sha256") != sha or "config" not in doc:
        return None
    tool = str(doc.get("tool", ""))
    return {
        "scale": doc.get("scale"),
        "languages": list(doc.get("languages", [])),
        "only": list(doc.get("only", [])),
        "font_sha256": doc.get("font_sha256"),
        "config": doc.get("config"),
        "cli": tool.rsplit(" ", 1)[-1] if tool else "",
    }


# ── a kept pack ─────────────────────────────────────────────────────────


def dist_pack(kind=HUD):
    return os.path.join(g().dist, "enhance", kind.sub, kind.name)


def found_pack(st, sha, kind=HUD):
    """(path, where) of a pack with this sha256 on this machine: the store,
    then dist/enhance/<kind>/; (None, None)."""
    p = incr.pack_in_store(st, sha)
    if os.path.isfile(p) and host.sha256_path(p) == sha:
        return p, "store"
    d = dist_pack(kind)
    if os.path.isfile(d) and host.sha256_path(d) == sha:
        return d, "dist"
    return None, None


def remake_problem(made):
    """Why the pack cannot be made again the same ("" when it can), and the
    font to draw it with: the recorded inputs, the current [enhance.hud]
    table and the installed font."""
    from ..enhance import hud

    if not isinstance(made, dict):
        return "it records no settings to make it again", None
    try:
        cfg = hud.hud_config()
    except CliError as e:
        return str(e), None
    if hud.config_digest(cfg) != made.get("config"):
        return "the HUD settings changed", None
    want = made.get("font_sha256")
    font_path = None
    if want:
        font_path, _face = hud.resolve_font(None, cfg.get("font", ""))
        if font_path is None or host.sha256_path(font_path) != want:
            return "its font is not installed", None
    if not os.path.isdir(g().game_files):
        return "the game files are missing", font_path
    return "", font_path


def keep_refusal(base, why):
    hp = base_pack(base)
    k = base_kind(base)
    return (
        "%s: the %s of %s (sha256 %s...) is not in this build folder and "
        "could not be made again the same (%s). Make a new one with %s, or remove it "
        "with %s." % (k.keep, k.label, base.version or "?", hp["sha256"][:8], why, k.make, k.clear)
    )


NO_REMAKE = "a whole-game pack is not made again"


def remake_why(base, hp):
    """Why the base's pack cannot be made again, and the font to draw it with."""
    if base_kind(base) is TEXTURES:
        return NO_REMAKE, None
    return remake_problem(hp.get("made_with"))


def plan_keep(st, base):
    """What --keep-hud-pack would do, without writing: ("store" | "dist" |
    "remake" | "none", why). "none": the base has no pack."""
    hp = base_pack(base)
    if hp is None:
        return "none", "the base has no pack"
    _p, where = found_pack(st, hp["sha256"], base_kind(base))
    if where:
        return where, ""
    why, _font = remake_why(base, hp)
    if why:
        if why == "the game files are missing":
            return "remake", why
        return "refuse", keep_refusal(base, why)
    return "remake", ""


def keep_early(st, base):
    """--keep-hud-pack before the build: (info, remake) where info is the
    pack hud_pack_path would give (with the base's made_with), or None
    when the base has none; remake true when it must be made again after
    the import. Refuses when it can be found nowhere and not made again."""
    hp = base_pack(base)
    if hp is None:
        host.say("keep: the base has no pack")
        return None, False
    kind = base_kind(base)
    p, where = found_pack(st, hp["sha256"], kind)
    if p is not None:
        host.say("keep: the %s of %s, from the %s" % (kind.label, base.version, where))
        return info_of(p, hp, kind), False
    why, _font = remake_why(base, hp)
    # Missing game files are the import's to bring (--dump runs first).
    if why and why != "the game files are missing":
        raise host.Refusal(keep_refusal(base, why), patch.HUD_KEEP)
    return {
        "path": None,
        "entries": hp.get("entries"),
        "scale": hp.get("scale"),
        "base": hp,
        "kind": kind,
    }, True


def info_of(path, hp, kind=HUD):
    """pack_path's dict for a kept pack; "base" is the base's record,
    which the new manifest repeats."""
    return {
        "path": path,
        "entries": hp.get("entries"),
        "scale": hp.get("scale"),
        "base": hp,
        "kind": kind,
    }


def remake(st, base, jobs=0):
    """The base's pack made again into a folder under the build state, by
    enhance hud with the recorded settings, and moved to the store when its
    sha256 is the base's. Returns hud_pack_path's dict. Refuses otherwise.
    jobs: package's --jobs (0: sized to the free memory)."""
    from ..enhance import hud

    hp = base_pack(base)
    made = hp.get("made_with")
    why, font_path = remake_problem(made)
    if why:
        raise host.Refusal(keep_refusal(base, why), patch.HUD_KEEP)
    tmp = os.path.join(st.dir, REMAKE_DIR)
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    host.say("keep: making the HUD pack of %s again" % base.version)
    ns = _hud_args(made, tmp, font_path, jobs)
    try:
        hud.run(ns)
        path = os.path.join(tmp, "hud.pack")
        sha = host.sha256_path(path) if os.path.isfile(path) else ""
        if sha != hp["sha256"]:
            raise host.Refusal(
                keep_refusal(base, "made again, it came out different"), patch.HUD_KEEP
            )
        stored = incr.store_pack(st, path, sha)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info_of(stored, hp)


def _hud_args(made, out, font_path, jobs=0):
    import types

    return types.SimpleNamespace(
        scale=made.get("scale"),
        out=out,
        font=font_path,
        only=list(made.get("only") or ()),
        language=list(made.get("languages") or ()),
        list=False,
        jobs=jobs or 0,
        no_record=True,
    )


# ── a player's config.toml that overrides the pack (D8) ────────────────


def scan_pack_key(text):
    """textures.pack in a config.toml, by a line scan as the appliers do it
    (a [textures] header, then pack = before the next header): the value
    without quotes, or None. Not a TOML parser: an unusual layout only
    loses the note."""
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            inside = line.split("#", 1)[0].strip().replace(" ", "") == "[textures]"
            continue
        if not inside or not line.startswith("pack"):
            continue
        key, eq, val = line.partition("=")
        if not eq or key.strip() != "pack":
            continue
        val = val.strip()
        if val[:1] in ('"', "'"):
            end = val.find(val[0], 1)
            return val[1:end] if end > 0 else val[1:]
        return val.split("#", 1)[0].strip()
    return None


def override_value(text, build_pack):
    """The textures.pack value a player's config.toml sets that hides or
    breaks this build's choice, or None: with a pack in the build, any
    other value; without one, a value other than "none" (a path to a pack
    that is gone)."""
    val = scan_pack_key(text)
    if val is None:
        return None
    if build_pack:
        return val if val != build_pack else None
    return val if val != "none" else None


def host_config_toml(target):
    """This host's config/config.toml for the target when it is the host's
    own platform, else None."""
    m = g().m
    hos = host.host_os()
    if target != {"linux": "steamos"}.get(hos, hos):
        return None
    env = m["data"].get("dir_env")
    if target == "steamos":
        # install.sh's root, which holds config/ beside versions/.
        root = patch.steamos_root(m)
    elif env and os.environ.get(env):
        root = os.environ[env]
    else:
        root = os.path.expanduser(os.path.expandvars(m["data"][target]))
    return os.path.join(root, "config", "config.toml")


def config_overrides(target, build_pack):
    """[{"key": "textures.pack", "value": ...}] from this host's
    config.toml (D8), [] when there is none or it overrides nothing."""
    p = host_config_toml(target)
    if not p or not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            val = override_value(f.read(), build_pack)
    except OSError:
        return []
    return [] if val is None else [{"key": "textures.pack", "value": val}]
