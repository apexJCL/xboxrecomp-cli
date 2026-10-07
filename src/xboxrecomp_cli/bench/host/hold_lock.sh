#@ bench/host/hold_lock.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (pacing). No prologue; after MAX (BENCH_HOLD_MAX seconds).
# A transient unit of the user's systemd: a process left behind by this ssh
# session (nohup, setsid) is killed with the session's scope when it closes.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# Runs on the host are one agent at a time, so a holder still alive here is
# a leftover of a series that died: stop it, or the new unit cannot start
# under the same name.
systemctl --user stop recomp-pacing-hold 2>/dev/null || true
systemctl --user reset-failed recomp-pacing-hold 2>/dev/null || true
rm -f ~/.recomp-run.held
touch ~/.recomp-run.hold
# A unit that does not start would leave the wait below polling for an hour.
systemd-run --user --quiet --collect --unit=recomp-pacing-hold \
    flock -w 3600 -E 75 "$HOME/.recomp-run.lock" bash -c '
    echo "$(date -Is) bench.sh pacing hold (pid $$)" > "$HOME/.recomp-run.lock"
    touch "$HOME/.recomp-run.held"
    t=0
    while [ -f "$HOME/.recomp-run.hold" ] && [ "$t" -lt "$1" ]; do sleep 1; t=$((t + 1)); done
    rm -f "$HOME/.recomp-run.hold" "$HOME/.recomp-run.held"' _ "$MAX" \
    || { rm -f ~/.recomp-run.hold; exit 75; }
for i in $(seq 3700); do
    [ -f ~/.recomp-run.held ] && exit 0
    [ "$i" = 2 ] && echo "bench: pacing waiting for the run lock" >&2
    sleep 1
done
rm -f ~/.recomp-run.hold
exit 75
