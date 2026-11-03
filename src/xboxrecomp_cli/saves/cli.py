"""`<game> saves list` and `<game> saves import` (openspec change
save-import, D4): the player-facing front of importer.py."""

import json
import os
import sys

from . import importer, sources
from .importer import SaveError
from .sources import SourceError


def configure(p):
    sub = p.add_subparsers(dest="action", metavar="action")
    sub.required = True
    ls = sub.add_parser(
        "list",
        description="the saves in a folder or zip (--from), or in the installed game's save folder",
    )
    ls.add_argument("--from", dest="src", default="", help="a folder or a zip of saves")
    _to(ls)
    ls.add_argument("--json", action="store_true", help="the listing as JSON")
    im = sub.add_parser(
        "import",
        description="bring saves from a folder or zip into the game's save folder: backs up "
        "what is there first, never overwrites a save unasked",
    )
    im.add_argument(
        "--from",
        dest="src",
        required=True,
        help="a folder (a dashboard or FTP copy, an E: drive copy, another install's hdd "
        "folder) or a zip (the in-game menu's saves export, or a zip of such a folder)",
    )
    _to(im)
    im.add_argument(
        "--save",
        action="append",
        default=[],
        metavar="ID",
        help="import only this save folder (repeatable; default: every save)",
    )
    im.add_argument(
        "--replace",
        action="store_true",
        help="overwrite saves (and title data files) already there, after the backup",
    )
    im.add_argument(
        "--with-tdata",
        action="store_true",
        help="bring the title's own data (TDATA) too, where game.toml makes it optional",
    )
    im.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    im.add_argument("--json", action="store_true", help="the plan and the result as JSON")


def _to(p):
    p.add_argument(
        "--to",
        default="",
        help="the game's hdd folder (the one holding UDATA); default: the installed game's",
    )


def _target(cfg, a):
    from .. import host

    if a.to:
        return os.path.abspath(os.path.expanduser(a.to)), "--to"
    return importer.default_target(cfg, host.host_os())


def run(a):
    from .. import host

    cfg = importer.Config.from_game(host.g())
    try:
        if a.action == "list":
            return _list(cfg, a)
        return _import(cfg, a)
    except SourceError as e:
        raise host.Refusal("%s: %s" % (a.src, e), "source") from e
    except SaveError as e:
        raise host.Refusal(str(e), e.code) from e


def _show_saves(saves, out):
    if not saves:
        out("  (none)")
    for s in saves:
        status = s.why and "not importable: %s" % s.why
        sign = {"resign": "re-signed for this build", "unknown": "signing unknown"}.get(
            s.signing, ""
        )
        if s.signing == "resign" and s.signed_here:
            sign = "signed for this build"
        tail = "; ".join(x for x in (status or s.note, sign if not status else "") if x)
        out(
            "  %08x/%-14s %-24s %7d bytes%s"
            % (s.tid, s.sid, s.name or "(no name)", s.size, "  " + tail if tail else "")
        )


def _show_rest(c, out):
    for tid, n in c.foreign.items():
        out("  another game's saves (title %s): %d, left out" % (tid, n))
    for p in c.loose:
        out("  %s: a save folder with no title folder above it, left out" % (p or "."))
    for p, why in c.notes:
        out("  %s: %s, left out" % (p, why))


def _list(cfg, a):
    from .. import host

    out = host.say
    if a.src:
        with sources.open_source(a.src) as tree:
            c = importer.classify(tree, cfg)
            where = tree.where
    else:
        target, why = _target(cfg, a)
        if not os.path.isdir(target):
            if a.json:
                print(json.dumps({"target": target, "saves": []}, indent=2))
            else:
                out("no saves yet in %s (%s)" % (target, why))
            return 0
        c = importer.target_contents(target, cfg)
        where = target
    if a.json:
        print(
            json.dumps(
                {
                    "source": a.src or None,
                    "root": where,
                    "saves": [s.as_json() for s in c.saves],
                    "tdata": {"%08x" % t: len(v) for t, v in c.tdata.items()},
                    "foreign": c.foreign,
                    "no_title": c.loose,
                    "ignored": [{"path": p, "why": w} for p, w in c.notes],
                    "manifest": c.manifest,
                },
                indent=2,
            )
        )
        return 0
    out(
        "%s saves in %s%s:"
        % (cfg.name, a.src or where, " (at %s)" % where if a.src and where else "")
    )
    _show_saves(c.saves, out)
    for tid, v in sorted(c.tdata.items()):
        out("  title data (TDATA/%08x): %d files" % (tid, len(v)))
    _show_rest(c, out)
    return 0


def _import(cfg, a):
    from .. import host

    out = host.say if not a.json else (lambda *x: None)
    target, why = _target(cfg, a)
    with sources.open_source(a.src) as tree:
        c = importer.classify(tree, cfg)
        if not c.saves and not c.tdata:
            raise SaveError(
                "%s holds no %s saves%s"
                % (
                    a.src,
                    cfg.name,
                    " (only other games': %s)" % ", ".join(c.foreign) if c.foreign else "",
                ),
                "empty",
            )
        warnings = importer.safety(cfg, target, a.src)
        plan = importer.make_plan(
            a.src, target, c, cfg, only=a.save, replace=a.replace, with_tdata=a.with_tdata
        )
        plan.warnings = warnings + plan.warnings
        plan.backup_dir = importer.backup_dir(target)
        if importer.stale_staging(target):
            plan.warnings.append(
                "a stopped import left %s; it is finished first (a save it moved aside "
                "goes back)" % os.path.join(target, importer.STAGING)
            )
        out("%s saves from %s into %s (%s):" % (cfg.name, a.src, target, why))
        _show_saves(plan.saves, out)
        for tid, e, action, _ in plan.tdata:
            out("  TDATA/%08x/%s: %s" % (tid, "/".join(e.path[2:]), action))
        _show_rest(c, out)
        for w in plan.warnings:
            out("warning: %s" % w)
        if a.dry_run:
            if a.json:
                print(json.dumps(dict(plan.as_json(), dry_run=True), indent=2))
            else:
                out("dry run: nothing written (the backup would go to %s)" % plan.backup_dir)
            return 0
        if not plan.writes() and not plan.title_files:
            if a.json:
                print(json.dumps(dict(plan.as_json(), imported=0), indent=2))
            else:
                out("nothing to import")
            return 0 if any(s.action == "skip" for s in plan.saves) else 1
        os.makedirs(target, exist_ok=True)
        result = importer.execute(plan, cfg)
    n = sum(1 for s in plan.saves if s.action in ("import", "replace"))
    if a.json:
        print(json.dumps(dict(plan.as_json(), imported=n, **result), indent=2))
        return 0
    for p in result["recovered"]:
        out("put back from a stopped import: %s" % p)
    for b in result["backup"]:
        out("backed up the saves that were there to %s" % b)
    out("imported %d save%s into %s" % (n, "" if n == 1 else "s", target))
    sys.stdout.flush()
    return 0
