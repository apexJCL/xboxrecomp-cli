"""The pipeline stages and the generation key.

Every intermediate lives in the game, not in the toolkit clone, whose tools
otherwise default to paths under xboxrecomp/tools/*/output (two games could
not share one toolkit). The stages and their order follow the toolkit's
docs/GETTING_STARTED.md, Steps 2-7, and the upstream README's Quick Start.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

from . import doctor, hooks, host, resources, toolkit
from .host import CliError


def g():
    return host.g()


def split_size():
    """Functions per generated file: SPLIT in the environment, else the
    manifest's pipeline.split."""
    return os.environ.get("SPLIT", str(g().m["pipeline"]["split"]))


def tool_cmd(name, *args):
    """One toolkit tool run, as data: ("tool", module, args). The stage
    functions below return lists of these so the generation key can hash
    exactly what a stage would run (gen_key) and run_cmds can run it."""
    return ("tool", name, [str(x) for x in args])


def py_cmd(script, *args):
    """A script run with the tools' Python: ("py", script, args)."""
    return ("py", script, [str(x) for x in args])


def run_cmds(cmds):
    tk = toolkit.toolkit_dir()
    if not os.path.isdir(os.path.join(tk, "tools")):
        raise CliError("no toolkit at %s: run '%s setup' or set XBOXRECOMP_DIR" % (tk, host.prog()))
    for kind, what, args in cmds:
        if kind == "tool":
            host.run([toolkit.tool_python(), "-m", "tools." + what] + args, cwd=tk)
            # The largest single tool process (disasm) is warned about
            # before the next generate when it no longer fits.
            resources.record("tool:" + what, host.LAST_PEAK)
        else:
            host.run([toolkit.tool_python(), what] + args)


def icall_db():
    """The toolkit's icall_feedback database (gitignored there), or ICALL_DB;
    '' when the game turns icall seeds off."""
    if not g().m["pipeline"]["icall_seeds"]:
        return ""
    return os.environ.get("ICALL_DB") or os.path.join(
        toolkit.toolkit_dir(), "tools", "recomp", "output", "icall_targets.json"
    )


def has_icall_db():
    db = icall_db()
    return bool(db) and os.path.isfile(db)


def parse_cmds(extra=()):
    return [tool_cmd("xbe_parser", g().xbe, "--json", g().analysis_json, *extra)]


def disasm_cmds(extra=()):
    """Indirect-call targets measured at runtime: the game's seed lists, plus
    the toolkit's icall_feedback database once one exists. The database is
    never seeded directly: it holds targets that are not function starts,
    and seeding those split real functions (toolkit
    docs/technical/indirect-calls.md). The filtered seed file is
    regenerated from it on every disasm, decoding each target against the
    XBE."""
    G, d = g(), g().m["pipeline"]["disasm"]
    cmds = []
    seeds = []
    for s in G.seeds:
        seeds += ["--seed-functions", s]
    if has_icall_db():
        cmds.append(
            tool_cmd(
                "recomp.icall_feedback",
                "--db",
                icall_db(),
                "seeds",
                "--out",
                G.icall_seeds,
                "--xbe",
                G.xbe,
            )
        )
        seeds += ["--seed-functions", G.icall_seeds]
    # Every executable section by default, so the XDK library sections are
    # swept. A title whose XBE flags data sections executable sets
    # --text-only, to keep them from yielding phantom functions, and names
    # its code outside .text in extra_sections.
    opts = (["--text-only"] if d["text_only"] else []) + (
        ["--extra-sections", ",".join(d["extra_sections"])] if d["extra_sections"] else []
    )
    cmds.append(
        tool_cmd(
            "disasm",
            G.xbe,
            "--analysis-json",
            G.analysis_json,
            "-o",
            os.path.join(G.out, "disasm"),
            *opts,
            *(seeds + ["-v"] + list(extra)),
        )
    )
    return cmds


def funcid_cmds(extra=()):
    d = os.path.join(g().out, "disasm")
    return [
        tool_cmd(
            "func_id",
            g().xbe,
            "--functions",
            os.path.join(d, "functions.json"),
            "--strings",
            os.path.join(d, "strings.json"),
            "--xrefs",
            os.path.join(d, "xrefs.json"),
            "-o",
            os.path.join(g().out, "func_id"),
            "-v",
            *extra,
        )
    ]


def abi_cmds(extra=()):
    out = g().out
    return [
        tool_cmd(
            "abi_analysis",
            g().xbe,
            "--disasm-dir",
            os.path.join(out, "disasm"),
            "--func-id-dir",
            os.path.join(out, "func_id"),
            "--output-dir",
            os.path.join(out, "abi"),
            "-v",
            *extra,
        )
    ]


def names_cmds(extra=()):
    G = g()
    funcs = os.path.join(G.out, "disasm", "functions.json")
    # merge_names only reserves ISO C names; a game's names_hooks reserve
    # what else it needs (a recovered "read" or "write" would replace libc's
    # for the whole executable off Windows).
    return [
        py_cmd(
            os.path.join(toolkit.toolkit_dir(), "tools", "ghidra_naming", "merge_names.py"),
            "--export-dir",
            os.path.dirname(G.ghidra_export),
            "--out",
            os.path.join(G.out, "ghidra", "ghidra_names.json"),
            "--functions-json",
            funcs,
            "--apply",
            *extra,
        )
    ] + [py_cmd(hook, funcs) for hook in G.names_hooks]


_flag_probe = {}


def _recomp_has(flag, missing_note):
    """Whether the toolkit's recomp takes flag (toolkit.recomp_has_flag).
    Cached per process; prints missing_note once when it is missing and
    the note is not empty."""
    tk = toolkit.toolkit_dir()
    if not os.path.isdir(os.path.join(tk, "tools", "recomp")):
        # No toolkit yet (before setup): nothing to say, and nothing to
        # cache, so the answer comes from the checkout once it exists.
        return False
    if (tk, flag) not in _flag_probe:
        hit = toolkit.recomp_has_flag(tk, flag)
        _flag_probe[(tk, flag)] = hit
        if not hit and missing_note:
            print("recomp: " + missing_note, file=sys.stderr)
    return _flag_probe[(tk, flag)]


def toolkit_has_seeds():
    """--seeds came with toolkit b0eb653."""
    return _recomp_has(
        "--seeds",
        "the toolkit has no --seeds (before b0eb653); the observed-seed report stays empty"
        if g().seeds
        else "",
    )


def toolkit_has_spin_waits():
    """--spin-waits is the fork's (upstream xboxrecomp has none): there the
    loops stay as the XBE has them, and gen/ is what the toolkit's own
    commands give."""
    return _recomp_has(
        "--spin-waits",
        "the toolkit has no --spin-waits (upstream xboxrecomp); %s is not applied, the "
        "busy-wait loops stay as on the Xbox" % g().rel(g().spin_waits)
        if g().spin_waits
        else "",
    )


def toolkit_has_back_edge_yield():
    """--back-edge-yield came with upstream v0.16.0 (default off). Before it
    the fork's recomp yielded at every loop back edge with no flag, and
    upstream's had no yield at all, so a toolkit without the flag needs no
    note: the commands are what that toolkit always ran."""
    return _recomp_has("--back-edge-yield", "")


def recomp_cmds(extra=()):
    G = g()
    out = G.out
    # --exclude-manual: functions the game defines by hand are declared in
    # gen/, not emitted. --game-name is fixed by the manifest, not the
    # checkout folder's name: recomp writes it into every gen/ file, so a
    # clone in another folder would generate different bytes from the same
    # inputs. --spin-waits: the busy-wait loops recomp lowers, when the
    # toolkit's recomp has the flag. --back-edge-yield: every loop back edge
    # checks the guest scheduler's preempt flag, so a guest thread that
    # spins on another's store hands the one guest CPU on. It is how the
    # fork's toolkit lifted before the flag existed, so gen/ stays the same.
    args = ["--all", "--split", split_size(), "--gen-dir", G.gen]
    # With [mods] hook slots recomp scans the manual file plus the slots'
    # wrap declarations (hooks.write_scan), so it emits each hooked body as
    # sub_X_gen; without any the argv is what it always was.
    if hooks.entries(G.m):
        args += ["--exclude-manual", hooks.scan_path(G)]
    elif G.recomp_manual:
        args += ["--exclude-manual", G.recomp_manual]
    args += [
        "--game-name",
        G.m["pipeline"]["game_name"],
        "--disasm-dir",
        os.path.join(out, "disasm"),
        "--func-id-dir",
        os.path.join(out, "func_id"),
        "--abi-dir",
        os.path.join(out, "abi"),
    ]
    if G.spin_waits and toolkit_has_spin_waits():
        args += ["--spin-waits", G.spin_waits]
    if toolkit_has_back_edge_yield():
        args.append("--back-edge-yield")
    # --seeds: the flag-fallback report lists the sites inside seeds marked
    # "observed" (seen at runtime); without it that list is empty. Only the
    # game's own seed files: icall_seeds.json carries no "observed" field.
    if G.seeds and toolkit_has_seeds():
        for s in G.seeds:
            args += ["--seeds", s]
    args += ["-o", os.path.join(out, "recomp")]
    return [tool_cmd("recomp", G.xbe, *args, *extra)]


def dump_hint():
    return "(%s)" % doctor.dump_fix()


def require_dump():
    """The first thing a new game's pipeline needs, said plainly: there is
    no stage that makes a dump."""
    G = g()
    if not os.path.isfile(G.xbe):
        raise CliError("no %s: %s" % (G.rel(G.xbe), doctor.dump_fix()))


def stage_parse(extra=()):
    host.step("parse")
    begin_stage("parse", extra)
    require_dump()
    # xbe_parser makes no directory; pipeline.analysis_json may name one
    # under pipeline.out that does not exist yet.
    os.makedirs(os.path.dirname(g().analysis_json), exist_ok=True)
    run_cmds(parse_cmds(extra))


def stage_disasm(extra=()):
    host.step("disasm")
    begin_stage("disasm", extra)
    host.require(g().analysis_json, "parse")
    if has_icall_db():
        os.makedirs(g().out, exist_ok=True)
    run_cmds(disasm_cmds(extra))


def stage_funcid(extra=()):
    host.step("funcid")
    begin_stage("funcid", extra)
    host.require(os.path.join(g().out, "disasm", "functions.json"), "disasm")
    run_cmds(funcid_cmds(extra))


def stage_abi(extra=()):
    host.step("abi")
    begin_stage("abi", extra)
    host.require(os.path.join(g().out, "func_id"), "funcid")
    run_cmds(abi_cmds(extra))


def ghidra_home():
    if os.environ.get("GHIDRA_HOME"):
        return os.environ["GHIDRA_HOME"]
    if host.host_os() == "macos" and shutil.which("brew"):
        r = subprocess.run(
            ["brew", "--prefix", "ghidra"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        if r.returncode == 0:
            return os.path.join(r.stdout.decode().strip(), "libexec")
    return ""


def stage_ghidra(extra=()):
    """Optional: better function names. Ghidra 12 ships no Jython, so the
    toolkit's export script runs under PyGhidra, from a Python 3.13 venv at
    .venv-ghidra (JPype has no 3.14 wheels yet)."""
    G = g()
    if not G.m["pipeline"]["ghidra"]:
        raise CliError("game.toml turns the ghidra stage off (pipeline.ghidra = false)")
    host.step("ghidra")
    # Its extras are not recorded: the export they shape is hashed into the
    # key as an input, so recording them too would keep the key stale forever.
    begin_stage("ghidra")
    host.require(G.xbe, dump_hint())
    home = ghidra_home()
    headless = os.path.join(home, "support", "analyzeHeadless")
    if not home or not os.path.exists(headless):
        raise CliError("no Ghidra at %r: set GHIDRA_HOME (the ghidra stage is optional)" % home)
    gpy = host.venv_python(G.venv_ghidra)
    if not os.path.isfile(gpy):
        raise CliError("no %s: see README (PyGhidra venv)" % gpy)
    gd = os.path.join(G.out, "ghidra")
    src = os.path.join(toolkit.toolkit_dir(), "tools", "ghidra_naming")
    for d in ("work", "project", "export", "scripts"):
        os.makedirs(os.path.join(gd, d), exist_ok=True)
    # Same scripts as upstream; only the runtime tag changes.
    shutil.copy2(
        os.path.join(src, "ghidra_scripts", "SetAnalysisOptions.java"), os.path.join(gd, "scripts")
    )
    with open(os.path.join(src, "ghidra_scripts", "ExportXbeNames.py")) as f:
        text = f.read()
    text = "\n".join(
        "# @runtime PyGhidra" if line == "# @runtime Jython" else line for line in text.split("\n")
    )
    with open(os.path.join(gd, "scripts", "ExportXbeNames.py"), "w", newline="\n") as f:
        f.write(text)
    host.run(
        [
            toolkit.tool_python(),
            os.path.join(src, "extract_for_ghidra.py"),
            G.xbe,
            "--out-dir",
            os.path.join(gd, "work"),
        ]
    )
    # Flags mirror tools/ghidra_naming/run_ghidra.sh, which only drives the
    # Windows .bat. No -analysisTimeoutPerFile: 0 there means zero seconds.
    host.run(
        [
            gpy,
            os.path.join(home, "Ghidra", "Features", "PyGhidra", "support", "pyghidra_launcher.py"),
            home,
            "-H",
            os.path.join(gd, "project"),
            G.m["pipeline"]["game_name"],
            "-import",
            os.path.join(gd, "work", "xbe_flat.bin"),
            "-overwrite",
            "-loader",
            "BinaryLoader",
            "-loader-baseAddr",
            "0x10000",
            "-processor",
            "x86:LE:32:default",
            "-cspec",
            "windows",
            "-scriptPath",
            os.path.join(gd, "scripts"),
            "-preScript",
            "SetAnalysisOptions.java",
            "-postScript",
            "ExportXbeNames.py",
            os.path.join(gd, "export"),
            "nodecompile",
        ]
        + list(extra)
    )
    stage_names()


def stage_names(extra=()):
    host.step("names")
    begin_stage("names", extra)
    host.require(g().ghidra_export, "ghidra")
    host.require(os.path.join(g().out, "disasm", "functions.json"), "disasm")
    run_cmds(names_cmds(extra))


def maybe_names():
    if os.path.isfile(g().ghidra_export):
        stage_names()


def stage_hooks(extra=()):
    """[mods] hook_functions -> config/hook_functions.json: each entry's VA,
    ret_pop and x87 return, checked against the disassembly and the manual
    file. Runs in analyze only when the list is not empty."""
    host.step("hooks")
    if [x for x in extra if x != "--"]:
        raise CliError("hooks runs no tool, so it takes no extra arguments")
    begin_stage("hooks")
    G = g()
    host.require(os.path.join(G.out, "disasm", "functions.json"), "disasm")
    slots = hooks.resolve(G)
    hooks.write_json(G.hook_functions, slots, G.m)
    host.say("%d hook slot(s) -> %s" % (len(slots), G.rel(G.hook_functions)))
    # Like the seeds, the file is the game's: a fresh clone recomps from
    # it, so it belongs in the repo.
    if hooks.untracked(G.root, G.hook_functions):
        host.say(
            "warning: %s is not tracked by git; commit it with game.toml" % G.rel(G.hook_functions)
        )


def write_hook_slots():
    """Before recomp: the scan file its --exclude-manual reads. Returns the
    slots for after_recomp_hooks. Without slots, a stale slot file goes."""
    G = g()
    if not hooks.entries(G.m):
        stale = os.path.join(G.gen, hooks.SLOTS_C)
        if os.path.exists(stale):
            os.remove(stale)
        return []
    slots = hooks.load_json(G)
    header = os.path.join(toolkit.toolkit_dir(), *hooks.RUNTIME_HEADER_PATH)
    if not os.path.isfile(header):
        raise CliError(
            "game.toml lists [mods] hook_functions, but the toolkit at %s has no %s "
            "(the hook slot runtime): move the toolkit pin to one that has it, or "
            "empty the list" % (toolkit.toolkit_dir(), "/".join(hooks.RUNTIME_HEADER_PATH))
        )
    hooks.write_scan(G, slots)
    return slots


def stage_recomp(extra=()):
    """gen/ is half-written while this runs; bench sync and the build
    refuse while the marker exists. It is removed only on success, so a
    failed run stays locked. The generation key is written last."""
    host.step("recomp")
    begin_stage("recomp", extra)
    # recomp only warns when this is missing, then guesses cdecl for everything.
    host.require(os.path.join(g().out, "abi", "abi_functions.json"), "abi")
    marker = g().regen_marker
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    open(marker, "a").close()
    slots = write_hook_slots()
    # -v makes the translator print "[i/n] Translating" every 500 functions,
    # the progress view's only signal in a 10-20 minute step. It changes no
    # output, so it is left out of the argv the generation key hashes.
    run_cmds(recomp_cmds(("-v",) + tuple(extra)))
    if slots:
        with open(os.path.join(g().gen, hooks.SLOTS_C), "w", newline="\n") as f:
            f.write(hooks.slots_c(slots))
    os.remove(marker)
    write_gen_key()


def analyze_stages():
    """analyze's stages in order, as (name, fn): the verb times each one."""
    return [
        ("parse", stage_parse),
        ("disasm", stage_disasm),
        ("funcid", stage_funcid),
        ("abi", stage_abi),
        ("names", maybe_names),
    ] + ([("hooks", stage_hooks)] if hooks.entries(g().m) else [])


def analyze():
    for _name, fn in analyze_stages():
        fn()


# ── generation key ───────────────────────────────────────────────────────
#
# What gen/ was generated from, so `package` regenerates only when needed:
# the XBE, the toolkit's tools, every game file a stage reads, and the
# exact commands the stages run (with the game root and the toolkit as
# placeholders, so a stage change stales gen/ without a constant to bump).
# Every stage deletes the key before it runs and records its extra
# arguments; recomp writes the key on success. A developer who reruns a
# stage with other arguments has therefore always invalidated it.

# 2: the toolkit field holds the tree ids of tools/ and templates/runtime/,
# not the toolkit's HEAD.
GEN_KEY_VERSION = 2


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="\n") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def begin_stage(name, extra=()):
    """Invalidate the key and record this stage's extra arguments."""
    G = g()
    if os.path.exists(G.gen_key):
        os.remove(G.gen_key)
    extras = _load_json(G.stage_extras, {})
    if list(extra):
        extras[name] = [str(x) for x in extra]
    else:
        extras.pop(name, None)
    if extras or os.path.exists(G.stage_extras):
        _write_json(G.stage_extras, extras)


def reset_stage_extras():
    """Forget the extras of earlier hand-run stages. The package regenerate
    runs every stage plain, so what they recorded no longer describes gen/."""
    if os.path.exists(g().stage_extras):
        os.remove(g().stage_extras)


def _placeholders(text):
    for path, tag in ((toolkit.toolkit_dir(), "$TK"), (g().root, "$ROOT")):
        text = text.replace(path, tag)
    return text.replace("\\", "/")


def gen_inputs():
    """{path with placeholders: sha256 | 'absent'}: every file outside gen/
    the stages read that the pipeline does not produce itself."""
    G = g()
    files = list(G.seeds) + [icall_db(), G.recomp_manual, G.spin_waits]
    files += list(G.names_hooks) + [G.ghidra_export]
    # Only with slots, so a game without [mods] keeps its key. The table
    # itself is an input too: an edited list stales gen/ before analyze
    # rewrites the json.
    if hooks.entries(G.m):
        files.append(G.hook_functions)
    out = {
        _placeholders(p): (host.sha256_path(p) if os.path.isfile(p) else "absent")
        for p in files
        if p
    }
    if hooks.entries(G.m):
        out["game.toml [mods]"] = hooks.mods_digest(G.m)
    return out


def stage_argv():
    """The commands a fresh `analyze` + `recomp` runs, with placeholders."""
    cmds = parse_cmds() + disasm_cmds() + funcid_cmds() + abi_cmds()
    if os.path.isfile(g().ghidra_export):
        cmds += names_cmds()
    cmds += recomp_cmds()
    return _placeholders(json.dumps(cmds))


def gen_key(extra=None):
    G = g()
    return {
        "version": GEN_KEY_VERSION,
        "xbe_sha256": host.sha256_path(G.xbe) if os.path.isfile(G.xbe) else "absent",
        "toolkit": toolkit.toolkit_state(),
        "inputs": gen_inputs(),
        "argv": hashlib.sha256(stage_argv().encode()).hexdigest(),
        "extra": _load_json(G.stage_extras, {}) if extra is None else extra,
        "ghidra": os.path.isfile(G.ghidra_export),
    }


def write_gen_key():
    _write_json(g().gen_key, gen_key())


GEN_KEY_FIELD_NAMES = {
    "version": "key format changed",
    "xbe_sha256": "the XBE changed",
    "toolkit": "toolkit changed",
    "argv": "stage commands changed",
    "extra": "a stage ran with extra arguments",
    "ghidra": "Ghidra export appeared or went",
}


def gen_stale_reasons(extra=None):
    """[] when gen/ is fresh, else why not, field by field. extra is the
    stage extras gen/ must have been made with ({} by default)."""
    G = g()
    if os.path.exists(G.regen_marker):
        return ["the last recomp did not finish"]
    if not os.path.isfile(os.path.join(G.gen, "recomp_funcs.h")):
        return ["no gen/"]
    rec = _load_json(G.gen_key, None)
    if not isinstance(rec, dict):
        return ["no key"]
    # package's plan regenerates every stage plain, so any recorded extras
    # stale gen/ for it ({}); build and integrate pass the extras recorded
    # now, so only a change since the last recomp does.
    now = gen_key(extra={} if extra is None else extra)
    why = []
    for k, v in now.items():
        if k == "inputs":
            old = rec.get("inputs") or {}
            why += [
                "%s changed" % p.replace("$ROOT/", "").replace("$TK/", "toolkit:")
                for p in sorted(set(v) | set(old))
                if v.get(p) != old.get(p)
            ]
        elif rec.get(k) != v:
            why.append(GEN_KEY_FIELD_NAMES.get(k, k))
    return why


def refuse_stale_gen(override):
    """Raise when gen/ is stale against its key, naming why and the
    regenerate; `override` names the flag that builds anyway."""
    why = gen_stale_reasons(extra=_load_json(g().stage_extras, {}))
    if why:
        raise CliError(
            "%s/ is stale: %s; run '%s analyze && %s recomp' (%s to build anyway)"
            % (g().rel(g().gen), ", ".join(why), host.prog(), host.prog(), override)
        )


def stages():
    """(name, function, help) of each stage, in order; the help names the
    game's own paths."""
    G = g()
    out = G.rel(G.out)
    return (
        (
            "parse",
            stage_parse,
            "XBE headers, sections, kernel imports -> %s" % G.rel(G.analysis_json),
        ),
        ("disasm", stage_disasm, "find functions, build xrefs -> %s/disasm/" % out),
        ("funcid", stage_funcid, "classify CRT / XDK / game functions -> %s/func_id/" % out),
        ("abi", stage_abi, "recover calling conventions -> %s/abi/" % out),
        (
            "ghidra",
            stage_ghidra,
            "optional: headless Ghidra names (slow, cached) -> %s/ghidra/" % out,
        ),
        ("names", stage_names, "write Ghidra's names into functions.json"),
        (
            "hooks",
            stage_hooks,
            "resolve [mods] hook_functions -> %s" % G.rel(G.hook_functions),
        ),
        ("recomp", stage_recomp, "lift x86 to C -> %s/" % G.rel(G.gen)),
    )
