"""bench gc: replaced by `benchdb plan` and `benchdb clean`, which apply
its rules (and stricter ones) over every game, host and worktree from one
place. For one release round it keeps its arguments, prints the matching
`benchdb plan` call and exits 2; then it goes."""

import argparse
import sys


def parse_args(args, game):
    gc = game.m["bench"]["gc"]
    ap = argparse.ArgumentParser(
        prog="%s bench gc" % game.slug,
        description="Replaced by benchdb plan and benchdb clean: prints the benchdb plan "
        "call and exits 2.",
    )
    ap.add_argument("--apply", action="store_true", help="(benchdb clean PLAN_ID)")
    ap.add_argument("--host", action="store_true", help="the bench host's store")
    ap.add_argument("--keep", type=int, default=gc["keep"], help="(D10 retention decides)")
    ap.add_argument("--days", type=float, default=gc["days"], help="(benchdb plan --older-than)")
    ap.add_argument("--include-unowned", action="store_true", help="(no longer a rule)")
    ap.add_argument("--refs", action="append", default=[], metavar="PATH", help="([refs] paths)")
    ap.add_argument("--quiet", action="store_true", help="(ignored)")
    return ap.parse_args(args)


def plan_call(b, a):
    """The benchdb plan call that replaces this gc call."""
    days = "" if a.days == b.cfg.game.m["bench"]["gc"]["days"] else " --older-than %g" % a.days
    if not a.host:
        return "benchdb plan%s" % days
    host = b.cfg.host.rpartition("@")[2] or "HOST"
    return "benchdb plan%s %s:%s/bench-logs" % (days, host, b.cfg.remote_game)


def cmd_gc(b, args):
    a = parse_args(args, b.cfg.game)
    print(
        "bench gc is replaced by benchdb (one deletion path for every game and host):\n"
        "  %s\n"
        "then `benchdb clean PLAN_ID` with the id it prints." % plan_call(b, a),
        file=sys.stderr,
    )
    return 2
