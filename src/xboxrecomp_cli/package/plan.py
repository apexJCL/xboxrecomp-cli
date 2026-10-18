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
from . import incr, tweaks
from .progress import load_timings

# enhance hud on BLiNX 2, measured 4 to 8 s: the estimate before its first
# run on this machine.
HUD_DEFAULT_S = 6.0
TIMING_HUD = "enhance hud"
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

    def row(self, name, action, why, est=None):
        """A row whose action is neither run nor reuse: the HUD pack kept
        ("keep") or a run that would refuse ("refuse")."""
        self.rows.append({"stage": name, "action": action, "why": list(why), "estimate_s": est})


def make_plan(a, lib, imp=None, enhance=True, seed=None):
    """The plan as a dict (the --json document)."""
    from . import build_target, config_layer, exe_icon_args, fixed_fields

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
    p.base, p.base_error = chosen_base(a, st, target)
    p.hud = {"choice": tweaks.hud_choice(a), "refusal": "", "code": "", "pack": None}
    version = None

    def rest(why, names):
        for n in names:
            p.add(n, [why])

    importing = imp is not None and imp.writes
    p.imported = importing
    p.allow = bool(getattr(a, "allow_game_file_deletes", False))
    p.fixed = fixed_fields(target, lib)
    if importing:
        p.add("import", [imp.plan_why()])
        p.importing = {"needed": True, "why": imp.plan_why()}
        imp_key = None
    else:
        # D6: without --dump, whether the run needs the disc after all.
        need = import_needed(st, lib)
        p.importing = {"needed": bool(need), "why": need}
        if need:
            p.add("import", ["needs the disc", need])
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
    keeping = p.hud["choice"] == "keep"
    later = (["hud"] if hud_on or keeping else []) + [cstage, sname, wname]
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
    elif keeping:
        hud_sha = keep_row(p, st, timings)

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


def import_needed(st, lib):
    """Why the run needs the disc although no --dump was given ("" when it
    does not): game_files/ is missing, its dump marker is, or its listing
    moved since the import was recorded."""
    G = g()
    if not os.path.isdir(G.game_files):
        return "%s/ is missing" % G.rel(G.game_files)
    inputs = incr.import_inputs(lib)
    if inputs["marker"] == "absent":
        return "%s/ has no dump marker" % G.rel(G.game_files)
    rec = st.get("import") or {}
    old = (rec.get("inputs") or {}).get("listing")
    if old is not None and old != inputs["listing"]:
        return "%s/ changed since the import" % G.rel(G.game_files)
    return ""


def chosen_base(a, st, target):
    """(the base the run would patch, or None; the error naming it)."""
    try:
        return tweaks.run_base(a, target, st), ""
    except CliError as e:
        return None, str(e)


def keep_row(p, st, timings):
    """The hud row for --keep-hud-pack: where the base's pack would come
    from, or why the run would refuse. Returns the pack's sha256 ("" for
    none)."""
    base = p.base
    if base is None:
        why = p.base_error or (
            "--keep-hud-pack keeps the base's pack, and this run has no base: "
            "name it with --patch-from"
        )
        p.hud["refusal"], p.hud["code"] = why, patch_mod().NO_BASE
        p.row("hud", "refuse", [why])
        return ""
    where, why = tweaks.plan_keep(st, base)
    hp = tweaks.base_pack(base)
    if where == "none":
        p.row("hud", "keep", [why])
        return ""
    if where == "refuse":
        p.hud["refusal"], p.hud["code"] = why, patch_mod().HUD_KEEP
        p.row("hud", "refuse", [why])
        return ""
    p.hud["pack"] = hp
    if where == "remake":
        est = timings.get(TIMING_HUD)
        est = round(est, 1) if isinstance(est, (int, float)) else HUD_DEFAULT_S
        p.row("hud", "keep", ["made again"] + ([why] if why else []), est)
    else:
        p.row("hud", "keep", ["from the store" if where == "store" else "from dist/enhance/hud/"])
    return hp["sha256"]


def patch_mod():
    from . import patch

    return patch


def finish(p, st, target, version, a=None):
    run = [r for r in p.rows if r["action"] in ("run", "keep")]
    known = [r["estimate_s"] for r in run if r["estimate_s"] is not None]
    bases, newest = plan_bases(st, target)
    proxy = dict(newest, version=version or "") if newest else None
    for b in bases:
        b.update(judged(b.pop("base"), proxy, newest, target, version, p))
    out = plan_output(a, p, target, version, newest, proxy)
    build_pack = None
    choice = p.hud["choice"]
    if choice == "make" or (choice == "keep" and p.hud["pack"]):
        build_pack = "../Resources/enhance/hud.pack" if target == "macos" else "enhance/hud.pack"
    try:
        overrides = tweaks.config_overrides(target, build_pack)
    except (KeyError, OSError):
        overrides = []
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
        "output": out,
        "import": dict(getattr(p, "importing", None) or {"needed": False, "why": ""}),
        "config_overrides": overrides,
    }


def plan_bases(st, target):
    """The bundle records, newest first, each with its manifest as a Base
    (popped by finish once judged), and the newest record's manifest (None
    before the first bundle)."""
    from . import patch

    bases = incr.bases(st, target)
    records = incr.bundles_dir(st, target)
    newest = None
    for b in bases:
        b["base"] = None
        try:
            with open(os.path.join(records, b["sha256"] + ".json"), "rb") as f:
                b["base"] = patch.Base(f.read(), "bundle record %s" % b["sha256"][:12])
        except (OSError, CliError):
            continue
        if newest is None and b is bases[0]:
            newest = b["base"].manifest
    return bases, newest


def judged(base, proxy, newest, target, version, p=None):
    """A base's plan fields (D2, D7): its update ID, whether it can take
    this patch and why not (why, and why_codes for a front end), the
    patch's size and the game files it changes, adds and removes, its
    tweaks and seed. The newest record stands in for the new manifest,
    for the rules on product, target, layout, stub and game files and for
    the half-size rule; the run decides again on the real one. Without a
    record, the fields known before the build (fixed_fields) are judged."""
    from . import patch

    imported = bool(getattr(p, "imported", False))
    allow = bool(getattr(p, "allow", False))
    if base is None:
        return {
            "id": "",
            "usable": False,
            "why": ["its record cannot be read"],
            "why_codes": [patch.UNREADABLE],
            "tweaks": {"hud_pack": None},
            "seed_config": False,
            "game_files": None,
        }
    fixed = getattr(p, "fixed", None)
    if proxy is not None:
        why = patch.problem_codes(base, proxy, target, imported)
    else:
        why = patch.problem_codes(base, fixed, target, early=True) if fixed else []
        if not why and version and base.version == version:
            why = [(patch.SAME, "it is this version already")]
    size = None
    counts = None
    if newest is not None and patch.entries(base.manifest) is not None:
        d = patch.diff(base.manifest, newest)
        counts = patch.game_counts(d)
        # 0: the base is the newest record, and the patch is only what
        # this run changes, which is not known yet.
        size = patch.payload_size(d) or None
        if not why:
            dp = patch.deletes_problem(base, d, allow)
            share = patch.share_problem(d, newest) if size else ""
            if dp:
                why = [dp]
            elif share:
                why = [(patch.SHARE, share)]
    return {
        "id": base.sha256[:8],
        "usable": not why,
        "why": [m for _c, m in why],
        "why_codes": [c for c, _m in why],
        "patch_size": size,
        "tweaks": tweaks.tweaks_of(base.manifest),
        "seed_config": bool(base.manifest.get("seed_config")),
        "game_files": counts,
    }


def plan_output(a, p, target, version, newest, proxy):
    """What the run is expected to make (D13's output line), as package
    would choose (patch.choose) but before the new manifest exists.

    {"kind": "full" | "patch" | "none", "why", "base": the base's row or
    None, "patch_size": bytes or None}. "none": the run would refuse (a
    --patch-from base that cannot take this patch, the HUD flags the base
    asks for, a kept pack found nowhere), with the reason. "installed" is
    the host's current install when the target is the host's own
    platform, in a base's shape, else None."""
    from . import patch

    out = {
        "kind": "full",
        "why": "",
        "why_codes": [],
        "base": None,
        "patch_size": None,
        "installed": None,
    }

    def row(base):
        r = {"version": base.version, "made": "", "sha256": base.sha256, "origin": base.origin}
        r["path"] = getattr(base, "path", "")
        r["size"] = None
        r.update(judged(base, proxy, newest, target, version, p))
        return r

    try:
        inst = patch.host_base(target, g().package["app"], g().m)
    except (CliError, OSError, KeyError):
        inst = None
    if inst is not None:
        out["installed"] = row(inst)
    spec = getattr(a, "patch_from", None) if a is not None else None
    base = None
    if getattr(a, "full", False):
        out["why"] = "--full"
    elif spec:
        if p.base is None:
            out.update(
                kind="none",
                why=p.base_error or "--patch-from %s: not found" % spec,
                why_codes=[patch.NO_BASE],
            )
            return out
        base = row(p.base)
        out["base"] = base
        if base["why"]:
            out.update(
                kind="none",
                why="--patch-from %s: %s" % (spec, "; ".join(base["why"])),
                why_codes=list(base["why_codes"]),
            )
        else:
            out.update(kind="patch", why="from %s (--patch-from)" % p.base.version)
            out["patch_size"] = base["patch_size"]
    elif inst is None:
        out["why"] = "no install found"
    elif out["installed"]["why"]:
        out["why"] = "the installed %s: %s" % (
            inst.version or "?",
            "; ".join(out["installed"]["why"]),
        )
        out["why_codes"] = list(out["installed"]["why_codes"])
    else:
        base = out["installed"]
        out.update(kind="patch", why="from the installed %s" % inst.version, base=base)
        out["patch_size"] = base["patch_size"]
    # The HUD flags (D3) and a kept pack that cannot be found refuse the
    # run before it builds, as package() does: on any base it reads,
    # usable or not.
    if not getattr(a, "full", False):
        if p.hud["choice"] is None and tweaks.base_pack(p.base) is not None:
            out.update(kind="none", why=tweaks.guard_message(p.base), why_codes=[patch.HUD_GUARD])
        elif p.hud["refusal"]:
            out.update(kind="none", why=p.hud["refusal"], why_codes=[p.hud["code"]])
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
