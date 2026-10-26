"""The public export (bench-report D8): the radar SVGs per published host
and the README fragment, built from an allow-listed copy of report.json.
It writes into the game repo's work tree only, after the leak check, and
never commits or pushes."""

import html
import os
import re

from . import bench_report as R
from . import bench_report_svg as S
from . import public as P

BEGIN = "<!-- xbr:bench %s begin -->"
END = "<!-- xbr:bench %s end -->"
SINGLE = "†"

# Generic leaks, whatever the game's audit-private file says. Home paths
# and chat links come from public.BUILTIN; a Wine drive's home folder
# (Z: then the Linux home path) is caught by the home rule or this one.
GENERIC = (
    (r"[A-Za-z]:\\+home\\", re.I),
    (r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", 0),
    (r"(?<![0-9a-f:])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f:])", re.I),
    (r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])", 0),
    (r"(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![0-9a-f:])", re.I),
    (r"(?<![0-9a-f:])(?:[0-9a-f]{1,4}:)+:(?:[0-9a-f]{1,4}:)*[0-9a-f]{1,4}(?![0-9a-f:])", re.I),
)


class Refused(R.ReportError):
    """The export refuses this result: nothing was written."""


class LeakError(Refused):
    """The leak check found something: nothing was written."""


# --- the allow-list -------------------------------------------------------------


def pick(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def public_hw(hw):
    """The D8 fields of a sanitized hw block, and nothing else: no kernel,
    no serial-like fields, no resolution, no clocks seen."""
    if not hw:
        return None
    out = {
        "device": pick(hw, "device", "model"),
        "os": " ".join(str(x) for x in (pick(hw, "os", "name"), pick(hw, "os", "version")) if x)
        or None,
        "cpu": None,
        "gpu": [],
        "ram_gb": hw.get("ram_gb"),
        "refresh_hz": pick(hw, "display", "refresh_hz"),
        "power": {
            k: pick(hw, "power", k)
            for k in ("source", "profile", "tdp_w", "gpu_mhz_cap", "cpu_mhz_cap", "perf_level")
        },
        "backend": {
            k: pick(hw, "backend", k) for k in ("name", "translation", "translation_version")
        },
    }
    if pick(hw, "cpu", "model"):
        out["cpu"] = {k: pick(hw, "cpu", k) for k in ("model", "cores", "threads")}
    for g in hw.get("gpu") or []:
        if isinstance(g, dict) and g.get("model"):
            out["gpu"].append({"model": g.get("model"), "driver": g.get("driver")})
    return out


def public_run(rb):
    return {k: (rb or {}).get(k) for k in ("sampler_hz", "pacing", "present_mode", "window")}


def figs(e):
    return {
        k: {x: (e.get(k) or {}).get(x) for x in ("median", "min", "max")}
        for k in ("mean", "low1", "p99")
    }


def public_arm(e):
    out = {
        "id": e["id"],
        "display": e["display"],
        "values": dict(e["values"]),
        "status": e["status"],
        "n": e.get("n", 0),
    }
    if e["status"] == "ok":
        out.update(figs(e))
        j = e.get("judged") or {}
        out["judged"] = j.get("status")
        if j.get("status") == "judged" and j.get("basis") == "good":
            # The figures the pick was judged on: on a host with a slow
            # start mode, the medians over every run can miss the very bar
            # the best row claims.
            out["judged_figs"] = {k: j.get(k) for k in ("mean", "low1", "p99", "n")}
        if e.get("delta"):
            out["delta"] = {
                k: e["delta"].get(k)
                for k in ("fps", "low1", "groups_differ", "present_differs", "judged")
            }
    return out


def public_model(model, scene):
    """Built field by field from report.json's model: the four figures with
    n and the sampler group, the picks and the D4 lines, the allow-listed
    hw and run fields, build.version_public and the run dates. Nothing from
    env, exe, run dirs, profiles, hot paths, series, logs, notes,
    build.version or the shas, and never a host key."""
    pub = (scene.get("report") or {}).get("public") or {}
    hosts = pub.get("hosts") or {}
    if not hosts:
        raise R.ReportError("the scene's [report.public.hosts] lists no host to publish")
    out = {
        "scene": model["scene"],
        "label": model["label"],
        "target_fps": model["target_fps"],
        "warmup_s": model["warmup_s"],
        "hosts": [],
    }
    unrecorded, missing = [], []
    for key, cfg in hosts.items():
        hm = model["hosts"].get(key)
        if not hm:
            # The fragment replaces the scene's whole README block, so a
            # host left out here would silently drop its published figures
            # (often a bench that failed): refuse, as for no hw (D5).
            missing.append(cfg["label"])
            continue
        d = hm["disclosure"]
        if not d["hw_recorded"]:
            # D5: published figures always name the machine; runs from
            # before the hw block cannot (rerun them).
            unrecorded.append(cfg["label"])
            continue
        b = hm["best"]
        lines = []
        for p in b["picks"]:
            if not p.get("arm"):
                lines.append(p.get("text") or "No tested setting holds the target.")
        if b.get("tied"):
            lines.append(b["tied"]["text"][0].upper() + b["tied"]["text"][1:] + ".")
        if b.get("unstable"):
            lines.append("Unstable: %s." % b["unstable"]["text"])
        versions = []
        for v in d["build"]:
            vp = (v["value"] or {}).get("version_public")
            if vp and vp not in versions:
                versions.append(vp)
        base = None
        bh = pick(model, "baseline", "hosts", key)
        if bh:
            base = {
                "version_public": bh.get("version_public"),
                "date": (bh.get("date") or "")[:10] or None,
                "arms": {
                    a: {"mean": v.get("mean"), "low1": v.get("low1")} for a, v in bh["arms"].items()
                },
            }
        out["hosts"].append(
            {
                "label": cfg["label"],
                "slug": cfg["slug"],
                "sampler": {"hz": hm["sampler"]["hz"], "mixed": hm["sampler"]["mixed"]},
                "arms": [public_arm(e) for e in hm["arms"]],
                "best": {
                    "good_runs_only": b["basis"].startswith("good runs"),
                    "thresholds": {"mean_share_min": b["thresholds"]["mean_share_min"]},
                    "picks": [
                        {
                            "split": dict(p.get("split") or {}),
                            "split_display": p["split_display"],
                            "arm": p.get("arm"),
                            "single_run": bool(p.get("single_run")),
                            "stock": p.get("stock"),
                        }
                        for p in b["picks"]
                    ],
                },
                "lines": lines,
                "hw": [{"value": public_hw(v["value"]), "runs": v["runs"]} for v in d["hw"]],
                "hw_recorded": d["hw_recorded"],
                "run": [{"value": public_run(v["value"]), "runs": v["runs"]} for v in d["run"]],
                "version_public": versions,
                "dates": d["dates"],
                "refresh_below_target": d["refresh_below_target"],
                "baseline": base,
            }
        )
    why = []
    if missing:
        why.append(
            "no runs for %s in this result: run them, or drop them from "
            "[report.public.hosts] to publish without them" % ", ".join(missing)
        )
    if unrecorded:
        why.append(
            "no hardware recorded for %s (runs from before the hw block): "
            "rerun them to publish" % ", ".join(unrecorded)
        )
    if why:
        raise Refused("; ".join(why))
    return out


# --- the files ------------------------------------------------------------------


def radar_host(pm, h):
    """The shape bench_report_svg.radar reads, from the public model only."""
    return {"label": h["label"], "arms": h["arms"], "best": h["best"]}


def svgs(pm, h, groups):
    base = None
    if h["baseline"]:
        base = dict(h["baseline"]["arms"])
        base["_label"] = "baseline %s" % ", ".join(
            x for x in (h["baseline"]["version_public"], h["baseline"]["date"]) if x
        )
    m = {"target_fps": pm["target_fps"], "hosts": {h["slug"]: radar_host(pm, h)}}
    out = {}
    for mode, pal in (("light", S.PUBLIC_LIGHT), ("dark", S.PUBLIC_DARK)):
        for gi, (suffix, only) in enumerate(groups):
            tail = "" if len(groups) == 1 else "-%d" % (gi + 1)
            name = "%s%s-%s.svg" % (h["slug"], tail, mode)
            out[name] = S.radar(
                m,
                h["slug"],
                pal,
                "xbr-%s-%s%s-%s" % (pm["scene"], h["slug"], tail, mode),
                only=only,
                baseline=base,
                suffix=suffix,
                caption=S.CAPTION,
            )
    return out


def cell(s):
    return html.escape(str(s), quote=False).replace("|", "\\|")


def figure_cells(e, nd=1):
    if e is None:
        return None
    return [
        R.fmt((e.get(k) or {}).get("median"), 0 if k == "low1" else nd)
        for k in ("mean", "low1", "p99")
    ]


def sampler_text(h):
    sg = h["sampler"]
    if sg["mixed"]:
        return "mixed"
    if sg["hz"] is None:
        return "-"
    return "off" if sg["hz"] == 0 else "%d Hz" % sg["hz"]


def delta_text(e, k):
    d = (e or {}).get("delta", {}) or {}
    v = d.get(k)
    return "-" if v is None else "%+.1f%s" % (v, R.delta_marks(d))


def table_rows(pm, h, baseline):
    arms = {e["id"]: e for e in h["arms"]}
    target = S.num(pm["target_fps"])
    rows = []
    single = False
    for p in h["best"]["picks"]:
        at = (" at " + p["split_display"]) if p["split_display"] else ""
        stock = arms.get(p["stock"]) if p["stock"] else None
        best = arms.get(p["arm"]) if p["arm"] else None
        if best:
            mark = " " + SINGLE if p["single_run"] else ""
            single = single or p["single_run"]
            what = "best and stock" if stock and stock["id"] == best["id"] else "best"
            rows.append(("%s%s: %s%s" % (what, at, best["display"], mark), best, None, True))
        else:
            rows.append(("best%s" % at, None, "no tested setting holds", True))
        if stock and best and stock["id"] == best["id"]:
            continue
        if not stock:
            rows.append(("stock%s" % at, None, "not in the plan", False))
            continue
        reason = None
        if stock["status"] != "ok":
            reason = stock["status"]
        elif stock.get("judged") == "not judged":
            reason = "not judged"
        rows.append(
            ("stock%s: %s" % (at, stock["display"]), stock if not reason else None, reason, False)
        )
    out = []
    for i, (settings, e, reason, is_best) in enumerate(rows):
        r = [cell(h["label"]) if i == 0 else "", cell(settings), target]
        judged = is_best and not reason and e.get("judged_figs")
        if reason:
            r += [cell(reason), "", "", "", ""]
        elif judged:
            jf = e["judged_figs"]
            r += [R.fmt(jf["mean"]), R.fmt(jf["low1"], 0), R.fmt(jf["p99"])]
            r += ["%d of %d" % (jf["n"], e["n"]), sampler_text(h)]
        else:
            r += figure_cells(e) + [str(e["n"]), sampler_text(h)]
        if baseline:
            if reason:
                r += ["", ""]
            elif judged:
                # The row shows the judged runs, so its change must too: the
                # baseline's judged runs, not compare()'s every-run pairing.
                jd = dict((e.get("delta") or {}).get("judged") or {})
                jd.update(
                    {k: (e.get("delta") or {}).get(k) for k in ("groups_differ", "present_differs")}
                )
                r += [delta_text({"delta": jd}, "fps"), delta_text({"delta": jd}, "low1")]
            else:
                r += [delta_text(e, "fps"), delta_text(e, "low1")]
        out.append(r)
    return out, single


def hw_items(hv, rv, refresh_low, target):
    items = []
    for v in hv:
        hw = v["value"]
        if not hw:
            continue
        if hw["device"]:
            items.append(("device", hw["device"]))
        if hw["os"]:
            items.append(("OS", hw["os"]))
        if hw["cpu"]:
            items.append(
                (
                    "CPU",
                    "%s, %s cores / %s threads"
                    % (hw["cpu"]["model"], hw["cpu"]["cores"] or "?", hw["cpu"]["threads"] or "?"),
                )
            )
        for g in hw["gpu"]:
            items.append(
                ("GPU", g["model"] + ((", driver " + str(g["driver"])) if g["driver"] else ""))
            )
        if hw["ram_gb"]:
            items.append(("RAM", "%s GB" % S.num(hw["ram_gb"])))
        if hw["refresh_hz"]:
            items.append(("refresh", "%s Hz" % S.num(hw["refresh_hz"])))
        pw = ", ".join(
            "%s %s" % (k.replace("_", " "), S.num(x) if isinstance(x, (int, float)) else x)
            for k, x in hw["power"].items()
            if x is not None
        )
        if pw:
            items.append(("power", pw))
        be = " ".join(str(x) for x in hw["backend"].values() if x)
        if be:
            items.append(("backend", be))
        if len(hv) > 1:
            items.append(("runs", v["runs"]))
    for v in rv:
        rb = v["value"]
        if rb.get("sampler_hz") is not None:
            items.append(("sampler", "off" if not rb["sampler_hz"] else "%s Hz" % rb["sampler_hz"]))
        for k in ("pacing", "present_mode", "window"):
            if rb.get(k) is not None:
                items.append((k.replace("_", " "), rb[k]))
        if len(rv) > 1:
            items.append(("runs", v["runs"]))
    if refresh_low:
        items.append(
            (
                "note",
                "the display refreshes at %s Hz, below the %s fps target; the target is kept"
                % (", ".join(S.num(r) for r in refresh_low), S.num(target)),
            )
        )
    return items


def build_text(hosts):
    """The heading's build: one line when every host ran the same builds,
    else each host's, so one host's build is never read as everyone's."""
    per = [(h["label"], " and ".join(h["version_public"]) or "not recorded") for h in hosts]
    if not per:
        return ""
    if len({v for _, v in per}) == 1:
        v = per[0][1]
        return "" if v == "not recorded" else "build %s" % v
    return "builds: %s" % ", ".join("%s %s" % lv for lv in per)


def fragment(pm, img_rel):
    """The README text between the markers. img_rel: the images folder of
    this scene, relative to the README, with forward slashes."""
    newest = max((h["dates"][1] for h in pm["hosts"] if h["dates"]), default=None)
    bases = []
    for h in pm["hosts"]:
        v = (h["baseline"] or {}).get("version_public")
        if v and v not in bases:
            bases.append(v)
    head = ["### Performance: %s" % cell(pm["label"])]
    sub = []
    if newest:
        sub.append("runs up to %s" % newest)
    build = build_text(pm["hosts"])
    if build:
        sub.append(build)
    if bases:
        sub.append("since %s" % ", ".join(bases))
    if sub:
        head.append("")
        head.append(cell(sub[0][0].upper() + "; ".join(sub)[1:] + "."))
    out = head + [""]
    for h in pm["hosts"]:
        for name in sorted(n for n in h["_svgs"] if n.endswith("-light.svg")):
            dark = name[: -len("-light.svg")] + "-dark.svg"
            out += [
                "**%s**" % cell(h["label"]),
                "",
                "<picture>",
                '  <source media="(prefers-color-scheme: dark)" srcset="%s/%s">' % (img_rel, dark),
                '  <img alt="%s: mean fps and 1%% low per setting, as a share of %s fps" src="%s/%s" width="780">'
                % (html.escape(h["label"]), S.num(pm["target_fps"]), img_rel, name),
                "</picture>",
                "",
            ]
    # A baseline with no public build still gets its delta columns.
    baseline = any(h["baseline"] for h in pm["hosts"])
    cols = ["Platform", "Settings", "Target fps", "Mean fps", "1% low", "p99 ms", "n", "Sampler"]
    if baseline:
        since = bases[0] if len(bases) == 1 else "baseline"
        cols += ["Mean since %s" % since, "1%% low since %s" % since]
    out.append("| %s |" % " | ".join(cols))
    out.append("|%s|" % "|".join("---" if i < 2 else "--:" for i in range(len(cols))))
    single = False
    for h in pm["hosts"]:
        rows, s = table_rows(pm, h, baseline)
        single = single or s
        out += ["| %s |" % " | ".join(r) for r in rows]
    out.append("")
    notes = [
        "Each figure is the median over n runs, after a %s s warm-up. p99 is the flip-to-flip interval, "
        "not CPU or GPU work time, so it is never under one refresh period on a vsync-bound host."
        % S.num(pm["warmup_s"])
    ]
    if any(h["best"]["good_runs_only"] for h in pm["hosts"]):
        notes.append(
            "Where runs start in a slow mode, a setting is judged on its runs that did not: "
            "a best row shows those runs (n reads judged of all), the other rows every run."
            + (
                " A best row's change compares them with the baseline's judged runs"
                " (- where it has none)."
                if baseline
                else ""
            )
        )
    if single:
        notes.append("%s The pick rests on a single judged run." % SINGLE)
    if baseline:
        marks = "".join(R.delta_marks(a.get("delta")) for h in pm["hosts"] for a in h["arms"])
        notes += [
            "%s Since the baseline: %s." % (m, t) for m, t in R.DELTA_NOTES.items() if m in marks
        ]
    out += [cell(n) + "  " for n in notes[:-1]] + [cell(notes[-1]), ""]
    for h in pm["hosts"]:
        for line in h["lines"]:
            out += ["**%s**: %s" % (cell(h["label"]), cell(line)), ""]
    for h in pm["hosts"]:
        items = hw_items(h["hw"], h["run"], h["refresh_below_target"], pm["target_fps"])
        out += ["<details><summary>Hardware: %s</summary>" % html.escape(h["label"]), ""]
        out += ["- %s: %s" % (cell(k), cell(v)) for k, v in items]
        out += ["", "</details>", ""]
    return "\n".join(out).rstrip("\n") + "\n"


# --- the leak check -------------------------------------------------------------


def host_key_patterns(keys):
    """A host key only where an identifier goes, so prose labels pass:
    a file name, a path part, a URL, user@key, key: before a path, and
    after ssh, scp or rsync."""
    out = []
    for k in keys:
        e = re.escape(k)
        word = r"(?<![\w-])%s(?![\w-])" % e
        for rx in (
            r"(?<![\w-])%s\.[A-Za-z0-9]{1,5}\b" % e,
            r"[/\\]%s[/\\]" % e,
            r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]*?%s" % word,
            r"@%s(?![\w-])" % e,
            r"(?<![\w-])%s:[~/\\]" % e,
            r"\b(?:ssh|scp|rsync)\s+(?:-\S+\s+)*(?:\S+@)?%s" % word,
        ):
            out.append(("host key %s" % k, re.compile(rx, re.I)))
    return out


def leak_patterns(repo, host_keys):
    pats = [("built-in", re.compile(rx, fl)) for rx, fl in P.BUILTIN + GENERIC]
    pats += [("audit-private", c) for _, c in P.private_patterns(P.Git(repo))]
    return pats + host_key_patterns(host_keys)


def rewrite_rules(repo):
    """The repo's public-export rewrite file, or None without one: what
    the export would rewrite or fail on, it must not meet here."""
    try:
        return P.load_rules(P.Git(repo))
    except P.Cannot:
        return None


def rewrite_hits(files, rules):
    """[(file, what, line)]: a !fail check matched, or a rule would change
    the text (`public export` must carry the fragment and the SVGs through
    unchanged)."""
    if rules is None:
        return []
    hits = []
    for name, text in files.items():
        for src, rx in rules.fails:
            m = rx.search(text)
            if m:
                hits.append((name, "rewrite !fail %s" % src, m.group(0)))
        if rules.apply(text) != text:
            old = text.splitlines()
            new = rules.apply(text).splitlines()
            line = next((a for a, b in zip(old, new) if a != b), old[-1] if old else "")
            hits.append((name, "a rewrite rule changes it", line[:80]))
    return hits


def game_of(repo):
    """The game repo's manifest, or None when it has no game.toml (the
    export then checks the generic rules only)."""
    from . import manifest

    if not os.path.isfile(os.path.join(repo, manifest.FILE)):
        return None
    try:
        return manifest.load(repo)
    except manifest.ManifestError as e:
        raise R.ReportError("%s: %s" % (repo, e)) from e


def export_path_hits(files, game):
    """[(file, why, path)]: the game's own `public export` would drop the
    file ([public] exclude, the private set) or refuse it (its layout
    refusals, a data extension), so the fragment would point at an image
    the public repo never gets."""
    from . import layout

    if game is None:
        return []
    hits = []
    for name in files:
        rel = name.replace(os.sep, "/")
        if layout.private(game, rel):
            hits.append((name, "the game's export drops it ([public] exclude)", rel))
            continue
        why = layout.refused_path(game, rel) or (
            layout.data_ext(rel) and "a data extension (.%s)" % layout.data_ext(rel)
        )
        if why:
            hits.append((name, "the game's export refuses it: %s" % why, rel))
    return hits


def leak_hits(files, pats):
    """[(file, what, match)] over every file's name and text."""
    hits = []
    for name, text in files.items():
        for where, s in (("name", name), ("text", text)):
            for label, rx in pats:
                m = rx.search(s)
                if m:
                    hits.append((name, "%s in the %s" % (label, where), m.group(0)))
    return hits


# --- the export -----------------------------------------------------------------


def replace_between(text, scene_name, body):
    b, e = BEGIN % scene_name, END % scene_name
    i = text.find(b)
    j = text.find(e, i + len(b)) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return text[: i + len(b)] + "\n" + body + text[j:]


def export(model, scene, repo, stdout=None):
    """Build, check, then write. Returns (exit code, written paths)."""
    import sys

    stdout = stdout or sys.stdout
    pubcfg = scene["report"].get("public") or {}
    readme = os.path.join(repo, pubcfg.get("readme") or "README.md")
    images = os.path.join(repo, pubcfg.get("images") or "docs/images/bench", model["scene"])
    pm = public_model(model, scene)
    groups = S.radar_groups(
        {
            "hosts": {h["slug"]: h for h in pm["hosts"]},
            "split": model["split"],
            "axes": model["axes"],
        }
    )
    for h in pm["hosts"]:
        h["_svgs"] = svgs(pm, h, groups)
    img_rel = os.path.relpath(images, os.path.dirname(readme)).replace(os.sep, "/")
    frag = fragment(pm, img_rel)
    files = {os.path.relpath(readme, repo): frag}
    for h in pm["hosts"]:
        for n, t in h["_svgs"].items():
            files[os.path.relpath(os.path.join(images, n), repo)] = t
    hits = leak_hits(files, leak_patterns(repo, list(scene["hosts"])))
    hits += rewrite_hits(files, rewrite_rules(repo))
    hits += export_path_hits(files, game_of(repo))
    if hits:
        raise LeakError(
            "the public export would leak; nothing written:\n"
            + "\n".join("  %s: %s: %r" % h for h in hits)
        )
    try:
        with open(readme, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise R.ReportError("%s: %s" % (readme, e.strerror)) from e
    new = replace_between(text, model["scene"], frag)
    if new is None:
        print(
            "%s has no %s ... %s markers; the fragment, to place by hand:\n"
            % (readme, BEGIN % model["scene"], END % model["scene"]),
            file=sys.stderr,
        )
        stdout.write(frag)
        return 1, []
    os.makedirs(images, exist_ok=True)
    written = []
    for h in pm["hosts"]:
        for n, t in h["_svgs"].items():
            p = os.path.join(images, n)
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
            written.append(p)
    with open(readme, "w", encoding="utf-8") as f:
        f.write(new)
    written.append(readme)
    return 0, written
