"""xbr wizard: the game's player build wizard (docs/wizard.md).

The wizard itself is generic Go (wizard/ beside src/); what makes it one
game's is the config this module resolves from game.toml's [wizard] table
at a release tag and embeds in the binary (`wizard config --json` prints
it). `wizard build` (wizard_build.py) makes the three distributables;
`wizard check` runs the Go module's gates.

The config is read at the release, never from the working tree: the
binary clones the game at that tag, so its options must be that tag's.
"""

import argparse
import json
import os
import subprocess
import tomllib

from . import gitpin, host, manifest
from .host import CliError

# The embedded config's format; the Go side refuses another.
CONFIG_SCHEMA = 1
PNG = b"\x89PNG\r\n\x1a\n"


def _git(root, *args, binary=False):
    r = subprocess.run(["git", "-C", root] + list(args), capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode(errors="replace").strip()


def tree_reader(root, commit):
    """read(rel) -> bytes or None: the file at that commit (./ keeps the
    path relative to the game root inside a larger repository), or in the
    working tree when commit is ''."""

    def read(rel):
        if not commit:
            try:
                with open(os.path.join(root, *rel.split("/")), "rb") as f:
                    return f.read()
            except OSError:
                return None
        return _git(root, "show", "%s:./%s" % (commit, rel), binary=True)

    return read


def release(root, tag="", dev=False):
    """{tag, commit, dev}: the release the wizard is built for. --tag names
    it (it must be a tag of this checkout); else the one tag on HEAD of a
    clean checkout; dev: the working tree as it is, no tag."""
    if dev:
        return {"tag": "", "commit": _git(root, "rev-parse", "HEAD") or "", "dev": True}
    if tag:
        if not gitpin.tag_ok(tag):
            raise CliError("--tag %r is not a plain tag name" % tag)
        commit = gitpin.local_tag(root, tag)
        if not commit:
            raise CliError(
                "--tag %s: no such tag in %s (git fetch --tags, or tag it)" % (tag, root)
            )
        return {"tag": tag, "commit": commit, "dev": False}
    if _git(root, "rev-parse", "HEAD") is None:
        raise CliError("%s is not a git checkout: a wizard is built for a tagged release" % root)
    tags = (_git(root, "tag", "--points-at", "HEAD") or "").split()
    if not tags:
        raise CliError(
            "HEAD has no tag: a wizard is built for a release. Pass --tag TAG (built from "
            "that tag's tree), or check out a tag"
        )
    if len(tags) > 1:
        raise CliError("HEAD has several tags (%s): pick one with --tag" % ", ".join(tags))
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise CliError(
            "the checkout has uncommitted changes: commit them, or pass --tag %s to build "
            "from the tag's tree" % tags[0]
        )
    return {"tag": tags[0], "commit": gitpin.local_tag(root, tags[0]), "dev": False}


def _lookup(doc, dotted):
    t = doc
    for part in dotted.split("."):
        if not isinstance(t, dict) or part not in t:
            return None, False
        t = t[part]
    return t, True


def toml_literal(v):
    """A scalar as config.toml writes it."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)


def _choice_value(raw, like):
    """A choice's string value as the type of the defaults file's value."""
    if isinstance(like, bool):
        raise ValueError("a boolean key is kind bool")
    if isinstance(like, int):
        return int(raw)
    if isinstance(like, float):
        return float(raw)
    if isinstance(like, str):
        return raw
    raise ValueError("a %s key is not offered" % type(like).__name__)


def package_flags():
    """The long options `package` takes that switch something on with no
    value of the wizard's (store_true, or an optional value with a const)."""
    from . import main

    p = argparse.ArgumentParser()
    main._cfg_package(p)
    out = set()
    for a in p._actions:
        if isinstance(a, argparse._StoreTrueAction) or (a.nargs == "?" and a.const is not None):
            out.update(o for o in a.option_strings if o.startswith("--"))
    return out


# What a flag option may never be: the wizard sets these itself.
WIZARD_OWN_FLAGS = {"--dump", "--plain", "--seed-config", "--out", "--replace", "--verbose"}


def problems(m, read, flags):
    """[wizard]'s file rules against the tree read() sees: the icon a PNG,
    each config key in the defaults file with the option's default as its
    value, each flag one package accepts. Returns (errors, options with
    their TOML literals)."""
    w = m["wizard"]
    errors = []
    icon = read(w["icon"])
    if icon is None:
        errors.append("wizard.icon: no %s" % w["icon"])
    elif not icon.startswith(PNG):
        errors.append("wizard.icon: %s is not a PNG" % w["icon"])
    defaults_rel = "%s/config.default.toml" % m["package"]["content"]
    defaults = None
    if any(o["config"] for o in w["option"]):
        raw = read(defaults_rel)
        if raw is None:
            errors.append("wizard.option: no %s to check the config keys against" % defaults_rel)
        else:
            try:
                defaults = tomllib.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                errors.append("%s: %s" % (defaults_rel, e))
    options = []
    for i, o in enumerate(w["option"]):
        name = "wizard.option[%d] (%s)" % (i, o["key"])
        out = dict(o)
        if o["flag"]:
            if o["flag"] in WIZARD_OWN_FLAGS:
                errors.append("%s.flag: %s is the wizard's own" % (name, o["flag"]))
            elif o["flag"] not in flags:
                errors.append("%s.flag: package takes no %s switch" % (name, o["flag"]))
        elif defaults is not None:
            like, found = _lookup(defaults, o["config"])
            if not found:
                errors.append("%s.config: %s is not in %s" % (name, o["config"], defaults_rel))
            elif o["kind"] == "bool":
                if not isinstance(like, bool):
                    errors.append("%s.kind: %s is not a boolean" % (name, o["config"]))
                elif like != o["default"]:
                    errors.append(
                        "%s.default: %s is %s in %s"
                        % (name, o["config"], toml_literal(like), defaults_rel)
                    )
                out["toml"] = {"true": "true", "false": "false"}
            elif o["kind"] == "range":
                if isinstance(like, bool) or not isinstance(like, int):
                    errors.append("%s.kind: %s is not a whole number" % (name, o["config"]))
                elif like != o["default"]:
                    errors.append(
                        "%s.default: %s is %s in %s"
                        % (name, o["config"], toml_literal(like), defaults_rel)
                    )
                out["toml"] = {str(v): str(v) for v in manifest.range_values(o)}
            else:
                try:
                    vals = {v: _choice_value(v, like) for v in o["values"]}
                except ValueError as e:
                    errors.append("%s.values: %s (%s)" % (name, e, o["config"]))
                    vals = None
                if vals is not None:
                    if vals[o["default"]] != like:
                        errors.append(
                            "%s.default: %s is %s in %s"
                            % (name, o["config"], toml_literal(like), defaults_rel)
                        )
                    out["toml"] = {v: toml_literal(x) for v, x in vals.items()}
        options.append(out)
    return errors, options


def resolve(G, tag="", dev=False):
    """(config dict, icon bytes) for game G at the release: the manifest
    read from the tag's tree, validated, with the file rules."""
    if "wizard" not in G.m:
        raise CliError("game.toml has no [wizard] table: this game has no wizard (docs/wizard.md)")
    rel = release(G.root, tag, dev)
    read = tree_reader(G.root, "" if dev else rel["commit"])
    text = read(manifest.FILE)
    if text is None:
        raise CliError("%s has no %s" % (rel["tag"] or "the tree", manifest.FILE))
    m = manifest.parse_text(text.decode("utf-8"), "%s:%s" % (rel["tag"] or ".", manifest.FILE))
    if "wizard" not in m:
        raise CliError("%s's game.toml has no [wizard] table" % rel["tag"])
    errors, options = problems(m, read, package_flags())
    if errors:
        raise CliError("game.toml [wizard]:\n  " + "\n  ".join(errors))
    w, pkg = m["wizard"], m["package"]
    cfg = {
        "schema": CONFIG_SCHEMA,
        "game": {
            "name": m["game"]["name"],
            "slug": m["game"]["slug"],
            "app": pkg["app"],
            "product": pkg["product"],
        },
        "release": {"repo": w["repo"], **rel},
        "cli": {k: m["cli"][k] for k in ("tag", "commit", "url")},
        "toolkit": {k: m["toolkit"][k] for k in ("tag", "commit")},
        "targets": list(w["targets"]),
        "data": {t: m["data"][t] for t in ("windows", "steamos", "macos")},
        "fetch": [dict(f) for f in w["fetch"]],
        "options": options,
    }
    return cfg, read(w["icon"])


def config_json(cfg):
    return json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def configure(p):
    sub = p.add_subparsers(dest="action", metavar="action")
    sub.required = True
    c = sub.add_parser("config", description="print the config the wizard binary embeds")
    c.add_argument("--json", action="store_true", help="as JSON (the only format; for scripts)")
    for q in (c,):
        q.add_argument("--tag", default="", help="the release tag (default: the one on HEAD)")
        q.add_argument(
            "--dev",
            action="store_true",
            help="developers: the working tree as it is, no tag (what --game DIR mode reads)",
        )
    b = sub.add_parser(
        "build",
        description="build the wizard's release assets: the Windows zip, the macOS app zip, "
        "the Linux tar.gz and SHA256SUMS",
    )
    b.add_argument("--tag", default="", help="the release tag (default: the one on HEAD)")
    b.add_argument("--dev", action="store_true", help="developers: the working tree, no tag")
    b.add_argument(
        "--only",
        default="",
        metavar="LIST",
        help="comma-separated subset of macos,windows,linux (default: all three)",
    )
    b.add_argument("--out", default="", help="output folder (default dist/wizard/)")
    k = sub.add_parser(
        "check",
        description="the Go module's gates: gofmt, vet, tests, staticcheck when present, "
        "and two builds with equal sha256s",
    )
    k.add_argument("--tag", default="", help="the release tag for the double build")
    k.add_argument(
        "--dev", action="store_true", help="the double build from the working tree, no tag"
    )
    k.add_argument("--no-build", action="store_true", help="skip the double build")


def run(a):
    G = host.g()
    if a.action == "config":
        cfg, _ = resolve(G, a.tag, a.dev)
        print(config_json(cfg), end="")
        return 0
    from . import wizard_build

    if a.action == "build":
        wizard_build.build(G, a.tag, a.dev, wizard_build.parse_only(a.only), a.out)
        return 0
    return wizard_build.check(G, a.tag, a.dev, a.no_build)
