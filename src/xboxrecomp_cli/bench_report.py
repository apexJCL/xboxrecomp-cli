"""bench-matrix report: a matrix result rendered as a private HTML report,
a report.json model and, with --public, radar SVGs and a README fragment
for the game's public repo (bench-report D1-D8).

The report computes no frame statistics of its own: every figure is a
summary.json column (analyse) or a median of them. It adds the
share-of-target ratios, the sampler group per host, the best-settings
pick and the layout. Game constants (the target fps, the labels, the
preferences, the public hosts) come from the scene's [report] table; the
CLI holds only the generic defaults below.
"""

import datetime
import fractions
import glob
import html
import json
import math
import os
import random
import re
import statistics

from . import bench_matrix as B

# D4's bar, the same for every game; [report.best] and
# [report.best.hosts.<key>] override it.
DEFAULTS = {
    "mean_share_min": 0.97,
    "hitches_per_min_max": 15,
    "low1_share_min": 0.60,
    "min_pass": "2/3",
    "min_runs": 1,
    "unstable_share": 0.10,
}
THRESHOLD_KEYS = tuple(DEFAULTS)
PERMUTATIONS = 20000
PERM_SEED = 372
ALPHA = 0.05
RADAR_MAX_SPOKES = 24
RIM = 1.10


class ReportError(B.MatrixError):
    pass


# --- the scene's [report] (D6) ----------------------------------------------


def fraction(s, where):
    try:
        f = fractions.Fraction(str(s))
    except (ValueError, ZeroDivisionError) as e:
        raise ReportError("%s: min_pass %r is not a fraction" % (where, s)) from e
    if not 0 < f <= 1:
        raise ReportError("%s: min_pass %r is not in (0, 1]" % (where, s))
    return f


def check_report(scene, where="scene"):
    """The schema errors of D6, each naming its key."""
    rep = scene.get("report")
    if rep is None:
        return
    if not isinstance(rep, dict) or "target_fps" not in rep:
        raise ReportError("%s: [report] needs target_fps" % where)
    if not isinstance(rep["target_fps"], (int, float)) or rep["target_fps"] <= 0:
        raise ReportError("%s: report.target_fps must be a positive number" % where)
    axes = {a["name"]: a for a in scene["axis"]}
    for a in scene["axis"]:
        if "stock" not in a:
            raise ReportError(
                "%s: axis %s has no stock (needed once [report] exists)" % (where, a["name"])
            )
        if a["stock"] not in a["values"]:
            raise ReportError(
                "%s: axis %s: stock %r is not in values" % (where, a["name"], a["stock"])
            )
    best = rep.get("best") or {}
    for key in ("priority", "split"):
        for name in best.get(key) or []:
            if name not in axes:
                raise ReportError("%s: report.best.%s: no axis %r" % (where, key, name))
    both = set(best.get("priority") or []) & set(best.get("split") or [])
    if both:
        raise ReportError(
            "%s: report.best: axis %s is in both split and priority" % (where, sorted(both)[0])
        )
    for name, order in (best.get("prefer") or {}).items():
        if name not in axes:
            raise ReportError("%s: report.best.prefer: no axis %r" % (where, name))
        for v in order:
            if v not in axes[name]["values"]:
                raise ReportError(
                    "%s: report.best.prefer.%s: %r is not one of its values" % (where, name, v)
                )
    for k in best:
        if k in ("priority", "split", "prefer", "hosts"):
            continue
        if k not in THRESHOLD_KEYS:
            raise ReportError("%s: report.best: unknown key %s" % (where, k))
    for h, over in (best.get("hosts") or {}).items():
        if h not in scene["hosts"]:
            raise ReportError("%s: report.best.hosts.%s: no such host in [hosts]" % (where, h))
        for k in over:
            if k not in THRESHOLD_KEYS:
                raise ReportError("%s: report.best.hosts.%s: unknown key %s" % (where, h, k))
    for tag, t in [("report.best", best)] + [
        ("report.best.hosts." + h, o) for h, o in (best.get("hosts") or {}).items()
    ]:
        if "min_pass" in t:
            fraction(t["min_pass"], "%s: %s" % (where, tag))
    pub = rep.get("public") or {}
    slugs = set()
    for h, v in (pub.get("hosts") or {}).items():
        if h not in scene["hosts"]:
            raise ReportError("%s: report.public.hosts.%s: no such host in [hosts]" % (where, h))
        if not isinstance(v, dict) or not v.get("label") or not v.get("slug"):
            raise ReportError("%s: report.public.hosts.%s needs label and slug" % (where, h))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v["slug"]):
            raise ReportError(
                "%s: report.public.hosts.%s: slug %r is not [a-z0-9-]" % (where, h, v["slug"])
            )
        if v["slug"] in slugs:
            raise ReportError("%s: report.public.hosts: slug %s twice" % (where, v["slug"]))
        slugs.add(v["slug"])


def thresholds(scene, host):
    best = (scene.get("report") or {}).get("best") or {}
    t = dict(DEFAULTS)
    t.update({k: v for k, v in best.items() if k in THRESHOLD_KEYS})
    t.update(((best.get("hosts") or {}).get(host)) or {})
    return t


def display(axis, value):
    """What readers see for a value: display, then labels, then the value."""
    i = axis["values"].index(value)
    if "display" in axis:
        return str(axis["display"][i])
    return B.label(axis, value)


def arm_display(scene, values):
    return " · ".join(display(a, values[a["name"]]) for a in scene["axis"])


def preference(scene, axis_name):
    """An axis's preferred order: report.best.prefer, else stock first,
    then the listed order."""
    a = next(x for x in scene["axis"] if x["name"] == axis_name)
    prefer = (((scene.get("report") or {}).get("best") or {}).get("prefer") or {}).get(axis_name)
    if prefer:
        return list(prefer) + [v for v in a["values"] if v not in prefer]
    return [a["stock"]] + [v for v in a["values"] if v != a["stock"]]


def rank_key(scene, values):
    """Lower is richer: the priority axes in order, then the other
    non-split axes (stock first), so the pick is unique."""
    best = (scene.get("report") or {}).get("best") or {}
    split = best.get("split") or []
    order = list(best.get("priority") or [])
    order += [a["name"] for a in scene["axis"] if a["name"] not in order and a["name"] not in split]
    return tuple(preference(scene, n).index(values[n]) for n in order)


# --- the rows ---------------------------------------------------------------


def load_summary(result, scene):
    """summary.json's rows, with steady_worst_s_fps filled in from runs[]
    for a summary written before the column existed."""
    path = os.path.join(result, "summary.json")
    try:
        with open(path) as f:
            s = json.load(f)
    except (OSError, ValueError) as e:
        raise ReportError("%s: %s (run analyse first)" % (path, e)) from e
    warm = scene["scene"].get("warmup_s", 20)
    runs = s.get("runs") or []
    for i, r in enumerate(s["rows"]):
        if r.get("steady_worst_s_fps") is None and i < len(runs):
            d = runs[i]
            if (
                d.get("host") == r["host"]
                and (d.get("arm") or "").removesuffix("-noprof") == r["arm"]
            ):
                r["steady_worst_s_fps"] = B.worst_second(d.get("fps_per_s"), warm)
    return s


def steady_minutes(scene):
    sc = scene["scene"]
    return (sc["duration_s"] - sc.get("warmup_s", 20)) / 60.0


def med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 3) if vals else None


def figure(rows, col):
    vals = [r[col] for r in rows if r.get(col) is not None]
    if not vals:
        return None
    return {"median": med(vals), "min": round(min(vals), 3), "max": round(max(vals), 3)}


def sampler_group(clean_rows):
    """D2: the lowest prof rate at which every arm with a clean run has
    one; else the lowest rate per arm ("mixed")."""
    by_arm = {}
    for r in clean_rows:
        by_arm.setdefault(r["arm"], set()).add(r["prof"])
    if not by_arm:
        return {"hz": None, "mixed": False, "per_arm": {}}
    for hz in sorted({p for ps in by_arm.values() for p in ps}):
        if all(hz in ps for ps in by_arm.values()):
            return {"hz": hz, "mixed": False, "per_arm": {a: hz for a in by_arm}}
    return {"hz": None, "mixed": True, "per_arm": {a: min(ps) for a, ps in by_arm.items()}}


def run_checks(r, t, target, minutes):
    """(pass, {check: (value, threshold, shortfall)}) for one run."""
    hpm = r["steady_hitches"] / minutes if r.get("steady_hitches") is not None else None
    mean_min = t["mean_share_min"] * target
    low_min = t["low1_share_min"] * target
    out = {
        "mean": (r.get("steady_fps"), mean_min),
        "hitches_per_min": (hpm, t["hitches_per_min_max"]),
        "low1": (r.get("steady_low1_fps"), low_min),
    }
    ok = True
    for k, (v, th) in out.items():
        if v is None:
            ok = False
            continue
        ok = ok and (v <= th if k == "hitches_per_min" else v >= th)
    return ok, hpm


def shortfalls(fig, t, target):
    """D4's relative shortfall per check, 0 when it passes."""
    mean_min = t["mean_share_min"] * target
    low_min = t["low1_share_min"] * target
    lim = t["hitches_per_min_max"]
    sf = {}
    if fig.get("mean") is not None:
        sf["mean"] = max(0.0, (mean_min - fig["mean"]) / mean_min)
    if fig.get("low1") is not None:
        sf["low1"] = max(0.0, (low_min - fig["low1"]) / low_min)
    if fig.get("hitches_per_min") is not None:
        sf["hitches_per_min"] = max(0.0, (fig["hitches_per_min"] - lim) / lim)
    return sf


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def slow_share_range(runs, axis):
    counts = {}
    for r in runs:
        c = counts.setdefault(r["axis_" + axis], [0, 0])
        c[0] += r["verdict"] == "bad"
        c[1] += 1
    shares = [k / n for k, n in counts.values() if n]
    return (max(shares) - min(shares)) if shares else 0.0, counts


def mode_test(runs, axes, seed=PERM_SEED, shuffles=PERMUTATIONS):
    """D4 step 1: per axis, a permutation test of the slow share's range
    across the axis's values, at a Bonferroni level of ALPHA / axes."""
    tested = [a for a in axes if len({r.get("axis_" + a) for r in runs}) > 1]
    alpha = ALPHA / len(tested) if tested else ALPHA
    out = []
    for a in tested:
        obs, counts = slow_share_range(runs, a)
        rng = random.Random("%d:%s" % (seed, a))
        labels = [r["verdict"] == "bad" for r in runs]
        vals = [r["axis_" + a] for r in runs]
        idx = {}
        for i, v in enumerate(vals):
            idx.setdefault(v, []).append(i)
        hits = 0
        for _ in range(shuffles):
            rng.shuffle(labels)
            shares = [sum(labels[i] for i in ix) / len(ix) for ix in idx.values()]
            if max(shares) - min(shares) >= obs - 1e-12:
                hits += 1
        p = (hits + 1) / (shuffles + 1)
        out.append(
            {
                "axis": a,
                "p": round(p, 4),
                "statistic": round(obs, 4),
                "counts": {str(k): v for k, v in counts.items()},
                "significant": p < alpha,
            }
        )
    return {"alpha": round(alpha, 5), "shuffles": shuffles, "seed": seed, "axes": out}


def slow_line(k, n, where=""):
    lo, hi = wilson(k, n)
    return "starts in a slow mode in %d of %d runs (%d%%, %d-%d%%)%s" % (
        k,
        n,
        round(100.0 * k / n),
        round(100 * lo),
        round(100 * hi),
        where,
    )


def fmt(v, nd=1):
    if v is None:
        return "-"
    return ("%%.%df" % nd) % v


def fmt_g(v):
    """A threshold as written: 36, 58.2."""
    return ("%.2f" % v).rstrip("0").rstrip(".")


def none_text(scene, arm, t, target, split_txt=""):
    """D4's "no tested setting holds" line, naming the closest arm's figures."""
    f = arm["judged"]
    parts = ["%s fps average" % fmt(f["mean"])]
    low_min = t["low1_share_min"] * target
    mean_min = t["mean_share_min"] * target
    if f["mean"] is not None and f["mean"] < mean_min:
        parts[0] += " (needs %s)" % fmt_g(mean_min)
    p = "1%% low %d" % round(f["low1"]) if f["low1"] is not None else "1% low -"
    if f["low1"] is not None and f["low1"] < low_min:
        p += " (needs %s)" % fmt_g(low_min)
    parts.append(p)
    h = "%d hitches/min" % round(f["hitches_per_min"]) if f["hitches_per_min"] is not None else ""
    if h and f["hitches_per_min"] > t["hitches_per_min_max"]:
        h += " (limit %s)" % fmt_g(t["hitches_per_min_max"])
    if h:
        parts.append(h)
    tail = ""
    if not any(f.get("shortfall", {}).values()):
        # Its medians pass, so no figure explains the miss: the share of
        # runs that pass does.
        mp = fraction(t["min_pass"], "report.best")
        need = math.ceil(mp * f["n"])
        tail = "; it holds on the medians, but only %d of %d runs pass (needs %d)" % (
            f["passes"],
            f["n"],
            need,
        )
    return "No tested setting holds %s fps without frame drops%s. Closest: %s, %s%s." % (
        fmt_g(target),
        split_txt,
        arm["display"],
        ", ".join(parts),
        tail,
    )


def host_model(scene, host, rows, target, minutes):
    """One host's part of report.json: the arms with their four figures
    from the sampler group, and the best settings (D2, D4)."""
    plan = B.arms(scene)
    run_ids = {a["id"] for a in B.arms(scene, host)}
    mine = [r for r in rows if r["host"] == host]
    clean = [r for r in mine if not r["disturbed"]]
    # Rows of arms the plan no longer runs here (a retired or excluded arm,
    # a summary from an older scene) must not decide the group: they could
    # lift it to a rate the planned arms do not need, or make it mixed.
    planned = [r for r in clean if r["arm"] in run_ids]
    group = sampler_group(planned)
    t = thresholds(scene, host)
    has_verdict = any(r.get("verdict") in ("good", "bad") for r in planned)
    arms_out = []
    pooled = []
    for a in plan:
        e = {"id": a["id"], "display": arm_display(scene, a["values"]), "values": a["values"]}
        any_rows = [r for r in mine if r["arm"] == a["id"]]
        if a["id"] not in run_ids:
            e["status"] = "not run"
        elif not any_rows:
            e["status"] = "not run"
        elif a["id"] not in group["per_arm"]:
            e["status"] = "disturbed"
        else:
            hz = group["per_arm"][a["id"]]
            rs = [r for r in clean if r["arm"] == a["id"] and r["prof"] == hz]
            e.update(
                status="ok",
                prof=hz,
                n=len(rs),
                good=sum(r["verdict"] == "good" for r in rs),
                bad=sum(r["verdict"] == "bad" for r in rs),
                mean=figure(rs, "steady_fps"),
                low1=figure(rs, "steady_low1_fps"),
                worst_s=figure(rs, "steady_worst_s_fps"),
                p99=figure(rs, "steady_p99_ms"),
                hitches_per_min=med(
                    [
                        r["steady_hitches"] / minutes
                        for r in rs
                        if r.get("steady_hitches") is not None
                    ]
                ),
                runs=[r["run_dir"] for r in rs],
                _rows=rs,
            )
            pooled += rs
        arms_out.append(e)
    best = best_settings(scene, host, arms_out, pooled, t, target, minutes, has_verdict)
    for e in arms_out:
        e.pop("_rows", None)
    others = sorted({r["prof"] for r in planned} - set(group["per_arm"].values()))
    return {
        "key": host,
        "backend": (scene["hosts"].get(host) or {}).get("backend"),
        "sampler": dict(group, others=others),
        "has_verdict": has_verdict,
        "runs": len(mine),
        "clean_runs": len(clean),
        "group_runs": len(pooled),
        "arms": arms_out,
        "best": best,
    }


def best_settings(scene, host, arms_out, pooled, t, target, minutes, has_verdict):
    """D4: judge each arm, then pick per split value."""
    axes = [a["name"] for a in scene["axis"]]
    out = {"thresholds": t, "basis": "all clean runs", "mode": None, "unstable": None, "picks": []}
    tied_values = {}
    if has_verdict:
        mt = mode_test(pooled, axes)
        out["mode"] = mt
        k = sum(r["verdict"] == "bad" for r in pooled)
        out["pooled_slow"] = [k, len(pooled)]
        sig = [x for x in mt["axes"] if x["significant"]]
        for x in sig:
            for v, (kk, nn) in x["counts"].items():
                if nn and kk / nn > t["unstable_share"]:
                    tied_values.setdefault(x["axis"], set()).add(v)
        if not tied_values:
            out["basis"] = "good runs only (the slow mode follows no setting)"
            if len(pooled) and k / len(pooled) > t["unstable_share"]:
                lo, hi = wilson(k, len(pooled))
                out["unstable"] = {
                    "k": k,
                    "n": len(pooled),
                    "share": round(k / len(pooled), 4),
                    "lo": round(lo, 4),
                    "hi": round(hi, 4),
                    "text": slow_line(k, len(pooled)),
                }
        else:
            out["basis"] = "good runs only, except at the settings the slow mode follows"
            tied_runs = [
                r for r in pooled if any(str(r["axis_" + a]) in vs for a, vs in tied_values.items())
            ]
            rest = [r for r in pooled if r not in tied_runs]
            kt = sum(r["verdict"] == "bad" for r in tied_runs)
            where = " and ".join(
                "%s"
                % ", ".join(
                    display(next(x for x in scene["axis"] if x["name"] == a), v)
                    for v in sorted_vals(scene, a, vs)
                )
                for a, vs in tied_values.items()
            )
            out["tied"] = {
                "values": {a: sorted(vs) for a, vs in tied_values.items()},
                "k": kt,
                "n": len(tied_runs),
                "text": "slow mode in %d of %d runs at %s" % (kt, len(tied_runs), where),
            }
            kr = sum(r["verdict"] == "bad" for r in rest)
            if rest and kr / len(rest) > t["unstable_share"]:
                lo, hi = wilson(kr, len(rest))
                out["unstable"] = {
                    "k": kr,
                    "n": len(rest),
                    "share": round(kr / len(rest), 4),
                    "lo": round(lo, 4),
                    "hi": round(hi, 4),
                    "text": slow_line(kr, len(rest), " elsewhere"),
                }
    mp = fraction(t["min_pass"], "report.best")
    for e in arms_out:
        if e["status"] != "ok":
            continue
        rs = e["_rows"]
        tied = any(str(e["values"][a]) in vs for a, vs in tied_values.items())
        if has_verdict and not tied:
            judged = [r for r in rs if r["verdict"] == "good"]
            basis = "good"
        else:
            judged = rs
            basis = "all"
        j = {"basis": basis, "n": len(judged)}
        if len(judged) < t["min_runs"] or not judged:
            j["status"] = "not judged"
            e["judged"] = j
            continue
        passes = 0
        for r in judged:
            ok, _ = run_checks(r, t, target, minutes)
            if has_verdict and tied and r["verdict"] == "bad":
                ok = False
            passes += ok
        j.update(
            status="judged",
            passes=passes,
            qualifies=passes * mp.denominator >= mp.numerator * len(judged),
            single_run=len(judged) == 1,
            mean=med([r["steady_fps"] for r in judged]),
            low1=med([r["steady_low1_fps"] for r in judged]),
            p99=med([r["steady_p99_ms"] for r in judged]),
            hitches_per_min=med(
                [
                    r["steady_hitches"] / minutes
                    for r in judged
                    if r.get("steady_hitches") is not None
                ]
            ),
        )
        j["shortfall"] = {k: round(v, 4) for k, v in shortfalls(j, t, target).items()}
        e["judged"] = j
    split = ((scene.get("report") or {}).get("best") or {}).get("split") or []
    split_axes = [a for a in scene["axis"] if a["name"] in split]
    combos = [{}]
    for a in split_axes:
        combos = [dict(c, **{a["name"]: v}) for c in combos for v in a["values"]]
    for c in combos:
        cand = [e for e in arms_out if all(e["values"][k] == v for k, v in c.items())]
        split_txt = (" at " + " · ".join(display(a, c[a["name"]]) for a in split_axes)) if c else ""
        stock = next(
            (
                e
                for e in cand
                if all(
                    e["values"][a["name"]] == a["stock"]
                    for a in scene["axis"]
                    if a["name"] not in c
                )
            ),
            None,
        )
        p = {
            "split": {k: v for k, v in c.items()},
            "split_display": " · ".join(display(a, c[a["name"]]) for a in split_axes),
            "stock": stock["id"] if stock else None,
        }
        q = [e for e in cand if (e.get("judged") or {}).get("qualifies")]
        if q:
            pick = min(q, key=lambda e: rank_key(scene, e["values"]))
            p.update(
                arm=pick["id"], display=pick["display"], single_run=pick["judged"]["single_run"]
            )
            pk = rank_key(scene, pick["values"])
            p["not_judged_above"] = [
                e["id"]
                for e in cand
                if (e.get("judged") or {}).get("status") == "not judged"
                and rank_key(scene, e["values"]) < pk
            ]
        else:
            judged = [e for e in cand if (e.get("judged") or {}).get("status") == "judged"]
            p["arm"] = None
            if judged:
                close = min(
                    judged,
                    key=lambda e: (
                        max(e["judged"]["shortfall"].values() or [0]),
                        rank_key(scene, e["values"]),
                    ),
                )
                p["closest"] = close["id"]
                p["closest_shortfall"] = round(max(close["judged"]["shortfall"].values() or [0]), 4)
                p["text"] = none_text(scene, close, t, target, split_txt)
            else:
                p["text"] = "No tested setting%s was judged." % split_txt
        out["picks"].append(p)
    out["not_judged"] = [
        e["id"] for e in arms_out if (e.get("judged") or {}).get("status") == "not judged"
    ]
    return out


def sorted_vals(scene, axis_name, vs):
    a = next(x for x in scene["axis"] if x["name"] == axis_name)
    return [v for v in a["values"] if str(v) in vs]


# --- hw, run and build per host (D5) ------------------------------------------


def run_infos(result, host):
    """(run dir, run.json or run-info.json dict) for each run of a host."""
    out = []
    for d in sorted(glob.glob(os.path.join(result, host, "*", ""))):
        for name in ("run.json", "run-info.json"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    with open(p) as f:
                        out.append((os.path.relpath(d, result), json.loads(f.read().lstrip("﻿"))))
                except ValueError:
                    pass
                break
    return out


def variants(items):
    """[{value, runs}] for the distinct values, most runs first."""
    seen = {}
    for v in items:
        k = json.dumps(v, sort_keys=True)
        seen.setdefault(k, [v, 0])[1] += 1
    return [{"value": v, "runs": n} for v, n in sorted(seen.values(), key=lambda x: -x[1])]


def disclosure(result, host, group_runs, scene):
    """hw, run and build variants over the host's runs in the sampler
    group (all its runs when none), the run dates and the window mode."""
    from . import bench_hw as H

    infos = run_infos(result, host)
    keep = set(group_runs)
    used = [(d, i) for d, i in infos if not keep or d.rstrip("/") in keep] or infos
    hw = [H.sanitize_hw(i["hw"]) if i.get("hw") else None for _, i in used]
    build = []
    for _, i in used:
        b = i.get("build")
        if not b and i.get("version"):
            b = {"version": i["version"], "version_public": H.version_public(i["version"])}
        build.append(b)
    runb = []
    for _, i in used:
        rb = dict(i.get("run") or {})
        if not rb:
            rb = {"window": H.window_mode(i)}
        rb.pop("order", None)
        runb.append(rb)
    dates = sorted(i.get("started") for _, i in used if i.get("started"))
    refresh = []
    for (_, i), h in zip(used, hw):
        refresh.append(i.get("refresh_hz") or ((h or {}).get("display") or {}).get("refresh_hz"))
    target = scene["report"]["target_fps"]
    low_refresh = sorted({r for r in refresh if r and r < target - 0.5})
    gpu_seen = [i.get("gpu", {}) for _, i in used]
    seen_max = []
    for g in gpu_seen:
        for part in ("warmup", "steady"):
            m = (g.get(part) or {}).get("mhz_max") if isinstance(g, dict) else None
            if m:
                seen_max.append(m)
    hv = variants(hw)
    bv = variants(build)
    rv = variants(runb)
    return {
        "hw": hv,
        "build": bv,
        "run": rv,
        "hw_recorded": all(h is not None for h in hw) and bool(hw),
        "differs": len(hv) > 1 or len(bv) > 1,
        "dates": [dates[0][:10], dates[-1][:10]] if dates else None,
        "newest": dates[-1] if dates else None,
        "refresh_below_target": low_refresh,
        "refresh_hz": sorted({r for r in refresh if r}),
        "gpu_mhz_seen_max": max(seen_max) if seen_max else None,
        "windowed_runs": sum(1 for r in runb if r.get("window") == "windowed"),
        "window_unknown_runs": sum(1 for r in runb if r.get("window") in (None, "unknown")),
    }


# --- the model ----------------------------------------------------------------


def find_scene(result, scene_path=None):
    if scene_path:
        return B.load_scene(scene_path)
    newest = None
    for p in glob.glob(os.path.join(result, "*", "*", "run-info.json")):
        try:
            with open(p) as f:
                i = json.loads(f.read().lstrip("﻿"))
        except (OSError, ValueError):
            continue
        if i.get("scene") and (newest is None or (i.get("started") or "") > newest[0]):
            newest = (i.get("started") or "", i["scene"])
    if not newest:
        raise ReportError("%s: no run-info.json with a scene; pass --scene" % result)
    scene = newest[1]
    check_report(scene, "the runs' scene")
    return scene


def build_model(result, scene, baseline=None, baseline_scene=None):
    if not scene.get("report"):
        raise ReportError("the scene has no [report] table (target_fps at least); pass --scene")
    check_report(scene)
    target = scene["report"]["target_fps"]
    minutes = steady_minutes(scene)
    s = load_summary(result, scene)
    rows = s["rows"]
    hosts = [h for h in scene["hosts"] if any(r["host"] == h for r in rows)]
    # A host since removed from the scene has no plan to judge its arms
    # against: leave it out and say so, rather than fail the whole report.
    gone = sorted({r["host"] for r in rows} - set(scene["hosts"]))
    model = {
        "schema": 1,
        "scene": scene["scene"]["name"],
        "label": scene["report"].get("label") or scene["scene"]["name"],
        "target_fps": target,
        "warmup_s": scene["scene"].get("warmup_s", 20),
        "duration_s": scene["scene"]["duration_s"],
        "steady_minutes": minutes,
        "generated": datetime.date.today().isoformat(),
        "axes": [
            {
                "name": a["name"],
                "values": a["values"],
                "display": [display(a, v) for v in a["values"]],
                "stock": a.get("stock"),
            }
            for a in scene["axis"]
        ],
        "split": ((scene["report"].get("best") or {}).get("split")) or [],
        "verdict_rules": s.get("verdict_rules") or [],
        "hosts": {},
        "prof_overhead": s.get("prof_overhead") or [],
        "baseline": None,
        "hosts_not_in_scene": gone,
    }
    for h in hosts:
        hm = host_model(scene, h, rows, target, minutes)
        group_runs = [r for e in hm["arms"] for r in e.get("runs", [])]
        hm["disclosure"] = disclosure(result, h, group_runs, scene)
        hm["label"] = ((scene["report"].get("public") or {}).get("hosts") or {}).get(h, {}).get(
            "label"
        ) or h
        model["hosts"][h] = hm
    if baseline:
        add_baseline(model, result, baseline, baseline_scene or scene)
    return model, s


# Delta marks, explained under the table that uses them.
GROUPS_MARK = "§"
PRESENT_MARK = "‡"
DELTA_NOTES = {
    GROUPS_MARK: "the two sides' figures come from different sampler groups",
    PRESENT_MARK: "the window mode differs from the baseline's (windowed against "
    "fullscreen, or not recorded on one side): the change may come from it",
}


def delta_marks(d):
    return (GROUPS_MARK if (d or {}).get("groups_differ") else "") + (
        PRESENT_MARK if (d or {}).get("present_differs") else ""
    )


def add_baseline(model, result, baseline, scene):
    """--baseline: compare() in-process, paired by (host, arm) within each
    host's sampler group, and the baseline's own figures for the radar."""
    rows = B.compare(baseline, result)
    by = {(r["host"], r["arm"], r["prof"]): r for r in rows}
    bmodel, _ = build_model(baseline, scene)
    model["baseline"] = {
        "dir": os.path.basename(os.path.normpath(baseline)),
        "hosts": {},
    }
    for h, hm in model["hosts"].items():
        bh = bmodel["hosts"].get(h)
        if not bh:
            continue
        ver = (bh["disclosure"]["build"] or [{}])[0].get("value") or {}
        model["baseline"]["hosts"][h] = {
            "version_public": ver.get("version_public"),
            "date": (bh["disclosure"]["dates"] or [None, None])[1],
            "arms": {
                e["id"]: {
                    "mean": (e.get("mean") or {}).get("median"),
                    "low1": (e.get("low1") or {}).get("median"),
                }
                for e in bh["arms"]
                if e["status"] == "ok"
            },
        }
        # Windowed and fullscreen present differently (size, compositor):
        # compare() flags an arm whose sides both logged the mode, and a
        # side that never logged it (runs from before present_fullscreen)
        # against one that did differs at the host level.
        wins = [
            sorted(
                {str((v["value"] or {}).get("window") or "unknown") for v in x["disclosure"]["run"]}
            )
            for x in (bh, hm)
        ]
        host_differs = wins[0] != wins[1]
        model["baseline"]["hosts"][h]["window"] = {"baseline": wins[0], "now": wins[1]}
        model["baseline"]["hosts"][h]["present_differs"] = host_differs
        bgroup = {e["id"]: e.get("prof") for e in bh["arms"] if e["status"] == "ok"}
        barms = {e["id"]: e for e in bh["arms"]}
        wins_by = None
        for e in hm["arms"]:
            if e["status"] != "ok" or e["id"] not in bgroup:
                continue
            c = by.get((h, e["id"], e["prof"])) if bgroup[e["id"]] == e["prof"] else None
            if c:
                e["delta"] = {
                    "fps": c.get("fps_delta"),
                    "low1": c.get("low1_fps_delta"),
                    "p99": c.get("p99_ms_delta"),
                }
            else:
                # The groups differ (a rerun without the sampler against a
                # sampled baseline): the medians of each side's own group.
                b = model["baseline"]["hosts"][h]["arms"].get(e["id"]) or {}
                m, lo = (e.get("mean") or {}).get("median"), (e.get("low1") or {}).get("median")
                e["delta"] = {
                    "fps": round(m - b["mean"], 3)
                    if m is not None and b.get("mean") is not None
                    else None,
                    "low1": round(lo - b["low1"], 3)
                    if lo is not None and b.get("low1") is not None
                    else None,
                    "groups_differ": True,
                }
                # compare() paired nothing, so its per-arm window check is
                # missing too: a host that mixes modes within one side
                # would slip past the host-level check.
                if wins_by is None:
                    wins_by = (run_windows(baseline, h), run_windows(result, h))
                sides = [
                    {wins_by[i].get(d, "unknown") for d in x.get("runs") or []} - {"unknown"}
                    for i, x in enumerate((barms[e["id"]], e))
                ]
                if sides[0] and sides[1] and sides[0] != sides[1]:
                    e["delta"]["present_differs"] = True
            if host_differs or (c or {}).get("present_differs"):
                e["delta"]["present_differs"] = True
            judged_delta(e, barms[e["id"]])


def run_windows(result, host):
    """{run dir: window mode} over a host's runs."""
    from . import bench_hw as H

    return {
        d: (i.get("run") or {}).get("window") or H.window_mode(i)
        for d, i in run_infos(result, host)
    }


def judged_delta(e, b):
    """The change in the judged figures, for a best row that shows them
    (bench_report_public): both sides judged on their good runs, else None."""
    ej, bj = e.get("judged") or {}, b.get("judged") or {}
    if ej.get("status") != "judged" or ej.get("basis") != "good":
        return
    same = bj.get("status") == "judged" and bj.get("basis") == "good"
    out = {}
    for key, fig in (("fps", "mean"), ("low1", "low1")):
        now, was = ej.get(fig), bj.get(fig)
        out[key] = round(now - was, 3) if same and now is not None and was is not None else None
    e["delta"]["judged"] = out


def write_json(path, model):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(model, f, indent=1, default=str)
        f.write("\n")


def esc(s):
    return html.escape(str(s), quote=True)
