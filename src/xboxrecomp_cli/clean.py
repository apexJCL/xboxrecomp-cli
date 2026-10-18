"""`clean` and `package --clean`: what a kept game folder can drop (D11).

Categories:
  bundles  the bundles package wrote in dist/ (the tars, folders, DMGs);
           their manifest records stay, so a patch can still be made
           against them
  build    package's build trees (build-pkg-*), the kept staging trees
           (dist/.stage-*) and the build state: the next package run
           rebuilds the program and restages, minutes
  gen      the generated sources, their key and the analysis cache
           (disasm, func_id, abi): the next run redoes the analysis,
           the slow part
  state    the rest of the state directory (the game-file hash cache, the
           bundle records), never the wizard's mark: without it the next
           wizard run would refuse a clone that is not at a tag

Nothing outside the game folder (or its state directory, which a git
worktree keeps in the main checkout's git directory) is ever deleted: a
dist/ that is a link to elsewhere is refused, not followed. The import
(game_files/), the venv and setup's downloads are never touched.
"""

import json
import os
import shutil

from . import buildstate, host
from .host import CliError
from .package import incr
from .package.lib import MARK_NAME

CATEGORIES = ("bundles", "build", "gen", "state")


def g():
    return host.g()


def _real(path):
    """Where deleting path acts: a link is removed itself, so its own
    folder decides, not its target."""
    path = os.path.abspath(path)
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


class Cleaner:
    """The paths one clean deletes, checked against the game folder."""

    def __init__(self, root, sdir):
        self.allowed = [os.path.realpath(root), os.path.realpath(sdir)]
        self.root = os.path.realpath(root)
        self.items = []  # (category, path, bytes)

    def add(self, cat, path):
        if not os.path.lexists(path):
            return
        real = _real(path)
        if not any(_inside(real, r) and real != r for r in self.allowed):
            raise CliError(
                "refusing to clean %s: it is outside the game folder (%s)" % (path, self.root)
            )
        self.items.append((cat, path, incr.tree_size(path)))

    def sizes(self):
        out = {c: 0 for c in CATEGORIES}
        for c, _p, n in self.items:
            out[c] += n
        return out

    def total(self):
        return sum(n for _c, _p, n in self.items)

    def lines(self):
        root = g().root
        rows = []
        for c, p, n in self.items:
            rel = os.path.relpath(p, root) if _inside(os.path.abspath(p), root) else p
            rows.append("  %-8s %9s  %s" % (c, human(n), rel.replace(os.sep, "/")))
        return rows

    def delete(self):
        for _c, p, _n in self.items:
            if os.path.isdir(p) and not os.path.islink(p):
                _rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)


def _rmtree(p):
    """A disc copy's r-x folders are made writable first."""

    def onexc(func, path, _exc):
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | 0o700)
        func(path)

    shutil.rmtree(p, onexc=onexc)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1000.0
    return "%d B" % n


def add_bundles(c, out):
    from . import package

    app = package.app()
    for target in g().package["targets"]:
        for patches in (False, True):
            for p, extra in incr.bundles_on_disk(out, target, app, patches):
                for q in [p] + extra:
                    c.add("bundles", q)


def add_build(c, out, sdir):
    from . import build as build_mod

    for t in ("windows", "macos"):
        c.add("build", build_mod.pkg_build_dir(t))
    if os.path.isdir(out):
        for n in sorted(os.listdir(out)):
            if n.startswith(".stage-"):
                c.add("build", os.path.join(out, n))
    c.add("build", os.path.join(sdir, buildstate.STATE_NAME))


def add_gen(c):
    G = g()
    for p in (G.gen, G.gen_key, G.regen_marker, G.stage_extras, G.icall_seeds):
        c.add("gen", p)
    # The analysis cache the generate stages write (the Ghidra export is a
    # developer's own, made by hand, and stays).
    for n in ("disasm", "func_id", "abi"):
        c.add("gen", os.path.join(G.out, n))
    c.add("gen", G.analysis_json)


def add_state(c, sdir):
    if not os.path.isdir(sdir):
        return
    for n in sorted(os.listdir(sdir)):
        if n in (MARK_NAME, buildstate.STATE_NAME):
            continue
        c.add("state", os.path.join(sdir, n))


def collect(cats, out=None, sdir=None):
    G = g()
    out = os.path.abspath(out or G.dist)
    sdir = sdir or buildstate.state_dir(G.root)
    c = Cleaner(G.root, sdir)
    if "bundles" in cats:
        add_bundles(c, out)
    if "build" in cats:
        add_build(c, out, sdir)
    if "gen" in cats:
        add_gen(c)
    if "state" in cats:
        add_state(c, sdir)
    return c


def package_clean(mode, out):
    """package --clean (build) or --clean=all (build and gen), before the
    plan: the run then rebuilds what was dropped. gen/ and its key stay
    for --clean, so generate is judged by its own key as always."""
    cats = ("build", "gen") if mode == "all" else ("build",)
    c = collect(cats, out)
    for line in c.lines():
        host.say("clean:" + line[1:])
    c.delete()
    if c.items:
        host.say("clean: %s freed" % human(c.total()))


def cmd_clean(a):
    cats = []
    if a.all:
        cats = list(CATEGORIES)
    else:
        if a.bundles:
            cats.append("bundles")
        if a.build:
            cats.append("build")
    if not cats:
        c = collect(CATEGORIES)
        if a.json:
            print(json.dumps({"sizes": c.sizes(), "total": c.total(), "deleted": False}))
            return
        for cat, n in c.sizes().items():
            host.say("  %-8s %9s" % (cat, human(n)))
        host.say("nothing deleted: name --bundles, --build or --all (and --yes)")
        return
    c = collect(cats)
    if a.json:
        if a.yes:
            c.delete()
        print(json.dumps({"sizes": c.sizes(), "total": c.total(), "deleted": bool(a.yes)}))
        return
    for line in c.lines():
        host.say(line)
    if not c.items:
        host.say("nothing to clean")
        return
    if not a.yes:
        host.say("%s would be freed; run again with --yes to delete" % human(c.total()))
        return
    c.delete()
    host.say("%s freed" % human(c.total()))
