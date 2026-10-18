"""`package --plan [--json]`: what a package run would reuse and what it
would run, and why, without running or recording anything (D13). The
wizard shows it as its plan, with the estimates from the folder's own
build-logs/timings.json, and offers its `bases` as the patch bases.

Each stage is judged as the run judges it: its key from the same inputs
and the upstream keys the plan predicts. A stage the plan sees run makes
every stage that chains on it run too, since its new key is known only
after it ran. Two places only estimate:
  - after an import, generate and the icon are "run" (the run checks them
    again once the new XBE is in place);
  - the wrap of a staged tree that did not change is judged by what its
    manifest is made of besides the files (the commits, the built time,
    the version), recorded at the last wrap; the run decides by the new
    manifest's sha256.
"""

import json
import os
import types

from .. import build as build_mod
from .. import buildstate, cli_dir, host, pipeline, setup, toolkit
from ..host import CliError
from . import incr
from .progress import load_timings

# enhance hud on BLiNX 2, measured 4 to 8 s: the estimate before its first
# run on this machine.
HUD_DEFAULT_S = 6.0
GENERATE_STEPS = ("parse", "disasm", "funcid", "abi", "ghidra", "names", "recomp")
WRAP_STEPS = {"steamos": "tar", "windows": "installer", "macos": "app and dmg"}


def g():
    return host.g()


class Plan:
    def __init__(self, timings):
        self.rows = []
        self.timings = timings

    def add(self, name, why, steps=(), default=None):
        est = None
        if why:
            vals = [self.timings[s] for s in steps if isinstance(self.timings.get(s), (int, float))]
            est = round(sum(vals), 1) if vals else default
        self.rows.append(
            {
                "stage": name,
                "action": "run" if why else "reuse",
                "why": list(why),
                "estimate_s": est,
            }
        )
        return not why


def make_plan(a, lib, imp=None, enhance=True, seed=None):
    """The plan as a dict (the --json document)."""
    from . import build_target, config_layer, exe_icon_args

    G = g()
    target = a.target
    btarget = build_target(target)
    bdir = build_mod.pkg_build_dir(btarget)
    st = buildstate.State(G.root)
    clean = getattr(a, "clean", None)
    if clean:
        # What the run sees after --clean dropped the state.
        st.data = {"schema": buildstate.SCHEMA, "stages": {}}
    st.verify = getattr(a, "verify", False)
    timings = load_timings(G.logs)
    p = Plan(timings)
    version = None

    def rest(why, names):
        for n in names:
            p.add(n, [why])

    importing = imp is not None and imp.writes
    if importing:
        p.add("import", [imp.plan_why()])
        imp_key = None
    else:
        imp_key = buildstate.stage_key(incr.import_inputs(lib))
        p.add("import", [])
    needs = [] if getattr(a, "no_setup", False) else setup.setup_needs(target, a.system_tools)
    if needs:
        p.add("setup", needs, [k for k in timings if k.startswith("setup: ")])
    cstage = incr.compile_stage(btarget)
    sname, wname = incr.stage_name(target), incr.wrap_name(target)
    hud_on = getattr(a, "hud_pack", None) is not None
    later = (["hud"] if hud_on else []) + [cstage, sname, wname]
    if "no toolkit" in needs:
        rest("checked after setup", ["generate", "icon"] + later)
        return finish(p, st, target, version, a)

    # generate: its own key decides, as in the run (gen_stale_reasons).
    gen_key = None
    if a.no_build:
        pass
    elif clean == "all":
        p.add("generate", ["--clean=all"], GENERATE_STEPS)
    elif importing:
        p.add("generate", ["checked after import"], GENERATE_STEPS)
    else:
        if p.add("generate", pipeline.gen_stale_reasons(), GENERATE_STEPS):
            gen_key = buildstate.stage_key(incr.generate_inputs())

    # icon: make_icons' own cache key is predictable from the XBE.
    from . import icon as icon_mod

    icon_dir = os.path.join(bdir, "icon")
    icon_key = None
    if importing:
        p.add("icon", ["checked after import"])
    else:
        xbe = G.xbe if G.package.get("icon", "xbe") == "xbe" else ""
        inputs = {
            "icon_key": buildstate.digest(icon_mod.icon_key(xbe)),
            "source": G.package.get("icon", "xbe"),
        }
        icon_key = buildstate.stage_key(inputs)
        # A rebuilt icon set with the same key keeps everything below.
        p.add("icon", st.check("icon", icon_key, inputs))

    hud_sha = ""
    hud_runs = False
    if hud_on:
        hud_runs, hud_sha = hud_row(a, p, st, imp_key)

    # compile
    exe = G.exe(bdir, btarget)
    compile_key = None
    if a.no_build:
        if os.path.isfile(exe):
            inputs = {"no_build": buildstate.sha256_file(exe)}
            compile_key = buildstate.stage_key(inputs)
            p.add(cstage, [])
        else:
            p.add(cstage, ["--no-build: no %s yet" % G.rel(exe)])
    elif gen_key is None or icon_key is None:
        p.add(
            cstage,
            ["generate runs first" if gen_key is None else "the icon is checked first"],
            [build_step(btarget, bdir)],
        )
    else:
        inputs = incr.compile_inputs(btarget, a, lib, exe_icon_args(btarget, icon_dir), bdir)
        up = {"generate": gen_key, "icon": icon_key}
        key = buildstate.stage_key(inputs, up)
        why = ["--reconfigure"] if a.reconfigure else st.check(cstage, key, inputs, up)
        if not why and not os.path.isfile(exe):
            why = ["%s is gone" % G.rel(exe)]
        if p.add(cstage, why, [build_step(btarget, bdir)]):
            compile_key = key

    cache = os.path.join(bdir, "CMakeCache.txt")
    if os.path.isfile(exe) and os.path.isfile(cache) and not (hud_on and hud_runs):
        # A HUD pack made again may change the -o part: known after it.
        options = lib.options_digest(lib.read_cache(cache), seed, hud_sha or None)
        try:
            version = lib.compute_version(
                G.root, toolkit.toolkit_dir(), G.gen, exe, cli_dir.cli_dir(), options
            )
        except (ValueError, OSError) as e:
            raise CliError(str(e)) from e

    # stage
    stage_key = None
    if compile_key is None:
        p.add(sname, ["the program is rebuilt"], ["stage the files"])
    elif hud_runs:
        p.add(sname, ["the HUD pack is made again"], ["stage the files"])
    elif imp_key is None:
        p.add(sname, ["checked after import"], ["stage the files"])
    else:
        layer, _cache = config_layer(enhance, lib, target)
        exe_sha = incr.output_sha(st, cstage, exe) or buildstate.sha256_file(exe)
        inputs = incr.stage_inputs(target, version, layer, seed, hud_sha, lib, exe_sha)
        up = {"compile": compile_key, "icon": icon_key, "import": imp_key}
        key = buildstate.stage_key(inputs, up)
        why = st.check(sname, key, inputs, up)
        if not why and not isinstance((st.get(sname) or {}).get("meta"), dict):
            why = ["not built yet"]
        if p.add(sname, why, ["stage the files"]):
            stage_key = key

    # wrap
    wsteps = [WRAP_STEPS[target]]
    if stage_key is None:
        p.add(wname, ["the files are staged again"], wsteps)
    else:
        archive = bool(getattr(a, "archive", False)) and target == "windows"
        inputs = incr.wrap_inputs(target, "", lib, exe=exe, archive=archive)
        basis = incr.wrap_basis(stage_key, version, lib, inputs)
        rec = st.get(wname)
        if rec is None:
            why = ["not built yet"]
        else:
            old = (rec.get("meta") or {}).get("basis")
            why = buildstate.moved(basis, old) if old else ["not built yet"]
            if not why:
                why = st.output_problems(rec)
        p.add(wname, why, wsteps)
    return finish(p, st, target, version, a)


def build_step(btarget, bdir):
    return "build %s (%s)" % (btarget, os.path.basename(bdir))


def hud_row(a, p, st, imp_key):
    """The pack `enhance hud` (as the wizard runs it, no options) would make
    for --hud-pack. Returns (runs, the pack's sha256 when reused)."""
    from ..enhance import hud

    G = g()
    out_dir = os.path.abspath(a.hud_pack or os.path.join(G.dist, "enhance", "hud"))
    pack = os.path.join(out_dir, "hud.pack")
    steps = [hud.TIMING_LABEL]
    if imp_key is None:
        p.add("hud", ["checked after import"], steps, HUD_DEFAULT_S)
        return True, ""
    cfg = hud.hud_config()
    ns = types.SimpleNamespace(scale=None, font=None, only=[], language=[])
    langs = hud.languages(cfg, [])
    _st, (key, inputs, up) = hud.hud_record(
        ns, cfg, cfg["scale"], langs, out_dir, st=st, imp=imp_key
    )
    why = st.check("hud", key, inputs, up)
    if not why and not os.path.isfile(pack):
        why = ["no hud.pack"]
    if not p.add("hud", why, steps, HUD_DEFAULT_S):
        return True, ""
    return False, host.sha256_path(pack)


def finish(p, st, target, version, a=None):
    run = [r for r in p.rows if r["action"] == "run"]
    known = [r["estimate_s"] for r in run if r["estimate_s"] is not None]
    bases, newest = plan_bases(st, target)
    return {
        "schema": 1,
        "target": target,
        "version": version,
        "stages": p.rows,
        "estimate_s": round(sum(known), 1) if known else 0,
        # Stages that run with no timing on this machine yet.
        "unknown": [r["stage"] for r in run if r["estimate_s"] is None],
        "bases": bases,
        # The full bundle's size as last made here (None before the first).
        "full_size": bases[0]["size"] if bases else None,
        "output": plan_output(a, st, target, version, newest),
    }


def plan_bases(st, target):
    """The bundle records, each with the size of a patch from it to the
    newest one (what this run would make if nothing changes), and the
    newest record's manifest (None before the first bundle)."""
    from . import patch

    bases = incr.bases(st, target)
    records = incr.bundles_dir(st, target)
    newest = None
    if bases:
        newest = buildstate.load_json(os.path.join(records, bases[0]["sha256"] + ".json"), None)
    newest = newest if isinstance(newest, dict) else None
    return patch.record_patch_sizes(bases, records, newest), newest


def plan_output(a, st, target, version, newest):
    """What the run is expected to make (D13's output line), as package
    would choose (patch.choose) but before the new manifest exists: the
    newest bundle record stands in for it, for the rules on product,
    target, layout and stub, and for the patch's size. The run decides
    again on the real manifest (the half-size rule among it).

    {"kind": "full" | "patch", "why", "base": {version, sha256, origin} or
    None, "patch_size": bytes or None}. "installed" is the host's current
    install when the target is the host's own platform, with "usable" and
    "why" (what keeps it from taking a patch), else None."""
    from . import patch

    out = {"kind": "full", "why": "", "base": None, "patch_size": None, "installed": None}

    # The newest record with this run's version: a base at the newest
    # record is no base only when the run makes that same version again.
    proxy = dict(newest, version=version or "") if newest else None

    def judge(base):
        if proxy is not None:
            return patch.problems(base, proxy, target)
        return ["it is this version already"] if version and base.version == version else []

    def size(base):
        if newest is None or patch.entries(base.manifest) is None:
            return None
        n = patch.payload_size(patch.diff(base.manifest, newest))
        # 0: the base is the newest record, and the patch is only what this
        # run changes, which is not known yet.
        return n or None

    def row(base):
        return {"version": base.version, "sha256": base.sha256, "origin": base.origin}

    try:
        inst = patch.host_base(target, g().package["app"], g().m)
    except (CliError, OSError, KeyError):
        inst = None
    if inst is not None:
        why = judge(inst)
        out["installed"] = dict(row(inst), usable=not why, why=why, patch_size=size(inst))
    spec = getattr(a, "patch_from", None) if a is not None else None
    if getattr(a, "full", False):
        out["why"] = "--full"
    elif spec:
        try:
            base = patch.load(spec, incr.bundles_dir(st, target))
        except CliError as e:
            out["why"] = "--patch-from %s: %s" % (spec, e)
            return out
        why = judge(base)
        if why:
            out["why"] = "--patch-from %s: %s" % (spec, "; ".join(why))
        else:
            out.update(kind="patch", why="from %s (--patch-from)" % base.version)
            out.update(base=row(base), patch_size=size(base))
    elif inst is None:
        out["why"] = "no install found"
    elif out["installed"]["why"]:
        out["why"] = "the installed %s: %s" % (
            inst.version or "?",
            "; ".join(out["installed"]["why"]),
        )
    else:
        out.update(kind="patch", why="from the installed %s" % inst.version)
        out.update(base=row(inst), patch_size=out["installed"]["patch_size"])
    return out


def show(plan, as_json):
    if as_json:
        print(json.dumps(plan, indent=1))
        return
    host.say(
        "plan for %s%s:" % (plan["target"], " (%s)" % plan["version"] if plan["version"] else "")
    )
    width = max(len(r["stage"]) for r in plan["stages"])
    for r in plan["stages"]:
        line = "  %-*s  %s" % (width, r["stage"], r["action"])
        if r["why"]:
            line += " (%s)" % ", ".join(r["why"])
        if r["estimate_s"]:
            line += ", ~%s" % fmt(r["estimate_s"])
        host.say(line)
    host.say(
        "estimate: ~%s%s"
        % (
            fmt(plan["estimate_s"]),
            " (plus %s, not timed here yet)" % ", ".join(plan["unknown"])
            if plan["unknown"]
            else "",
        )
    )
    for b in plan["bases"]:
        host.say(
            "  base: %s, made %s%s"
            % (b["version"], b["made"], "" if b["on_disk"] else " (bundle deleted)")
        )


def fmt(s):
    s = float(s)
    if s < 60:
        return "%d s" % round(s)
    return "%d min" % round(s / 60.0)
