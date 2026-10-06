"""bench pacing: frame-pacing A/B. One golden scenario's route under two
environments, N runs each, alternating so host drift lands on both arms,
all under one hold of the host's run lock (host/hold_lock.sh: a systemd-run
unit on the host that gives the lock up after BENCH_HOLD_MAX seconds if
this controller dies), then pacing_stats over both arms."""

import os
import subprocess
import sys
import time

from .golden import engine_argv, golden_py
from .remote import BenchError, shell_quote


def hold_run_lock(b):
    script = "MAX=%s\n" % shell_quote(b.cfg.get("BENCH_HOLD_MAX") or "14400")
    if b.r.remote(b.r.ship("hold_lock.sh", script, prologue=False))[0] != 0:
        raise BenchError("pacing: could not get the run lock")


def release_run_lock(b):
    b.r.command("rm -f ~/.recomp-run.hold")


def cmd_pacing(b, args):
    b.need_host()
    scen, runs, gate, envs = b.cfg.game.m["bench"]["pacing_scenario"], "3", "", []
    args = list(args)
    while args:
        a = args.pop(0)
        if a in ("--scen", "--runs", "--gate"):
            v = args.pop(0) if args else ""
            if a == "--scen":
                scen = v
            elif a == "--runs":
                runs = v
            else:
                gate = v
        elif a.startswith("--"):
            raise BenchError("pacing: unknown option %s" % a)
        else:
            envs.append(a)
    if not envs:
        envs = ["RECOMP_PRESENT_PACING=spin", "RECOMP_PRESENT_PACING=sleep"]
    if len(envs) != 2:
        raise BenchError("pacing: two environments, A and B")
    if not runs.isdigit() or runs == "0" or not runs.isascii():
        raise BenchError("pacing: --runs takes a count")
    if gate not in ("", "clock", "sleep"):
        raise BenchError("pacing: --gate clock or sleep")
    rc, plan = golden_py(b, "plan", capture=True)
    if rc != 0:
        raise BenchError("pacing: no golden plan")
    if not scen:  # game.toml names none: the first scenario in golden.json
        scen = plan.splitlines()[0].split("\t")[0] if plan.strip() else ""
    row = next((x for x in plan.splitlines() if x.split("\t")[0] == scen), None)
    if not row:
        raise BenchError("pacing: no scenario %s in golden.json" % scen)
    f = row.split("\t")
    secs, genv = f[1], (f[3] if len(f) > 3 else "")
    pstamp = time.strftime("%Y%m%d-%H%M%S") + "-pacing"
    out = os.path.join(b.cfg.game_dir, "bench-logs", pstamp)
    os.makedirs(out, exist_ok=True)
    logs = ([], [])
    benv = b.cfg["BENCH_ENV"]
    b.step("pacing: %s, %s runs each: A=%s  B=%s" % (scen, runs, envs[0], envs[1]))
    hold_run_lock(b)
    try:
        for _ in range(int(runs)):
            for k in (0, 1):
                with b.no_errexit():
                    stamp = b.run_game(
                        [],
                        bench_env="%s %s RECOMP_TRACE=flip,pacing=all %s" % (genv, benv, envs[k]),
                        timeout=secs,
                        lock_held="1",
                    )
                with open(os.path.join(out, "runs.txt"), "a") as fh:
                    fh.write("%d %s\n" % (k, stamp))
                logs[k].append(os.path.join(b.cfg.game_dir, "bench-logs", stamp, "game-stdio.log"))
    finally:
        release_run_lock(b)
    b.step("pacing: report")
    head = "scenario: %s (%s s), %s runs each, alternating\nA: %s\nB: %s\nBENCH_ENV: %s\n\n" % (
        scen,
        secs,
        runs,
        envs[0],
        envs[1],
        benv or "(none)",
    )
    argv = (
        engine_argv(b.cfg.game, "pacing_stats")
        + ["--scen", scen]
        + (["--gate", gate] if gate else [])
        + ["--arm", "A"]
        + logs[0]
        + ["--arm", "B"]
        + logs[1]
    )
    sys.stdout.flush()
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = head + r.stdout.decode(errors="replace")
    with open(os.path.join(out, "pacing-report.txt"), "w") as fh:
        fh.write(text)
    b.say(text, end="")
    b.say("report: %s" % os.path.join(out, "pacing-report.txt"))
    return r.returncode
