#@ bench/host/gc_apply.sh: a host script `<game> bench gc --host --apply`
#@ ships over ssh. The #@ lines this file starts with are this note and are
#@ not sent. Runs as: remote (gc). After the prologue and NAMES (the runs
#@ to remove, an array). Holds the run lock exclusively, so no run is being
#@ written while its store shrinks; exit 75 when it waited an hour.
exec 9>>~/.recomp-run.lock
if ! flock -x -n 9; then
    echo "bench: gc waiting for the run lock" >&2
    flock -x -w 3600 -E 75 9 || exit 75
fi
cd "$REMOTE_GAME/bench-logs"
fails=0 done=0
for n in "${NAMES[@]}"; do
    # Only a run directory directly in the store: no slash, no .., no link.
    if [[ ! "$n" =~ ^[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?$ ]] || [ -L "$n" ] || [ ! -d "$n" ]; then
        echo "gc: refusing $n (not a run directory of the store)" >&2
        fails=$((fails + 1))
        continue
    fi
    if rm -rf -- "$n"; then done=$((done + 1)); else fails=$((fails + 1)); fi
done
echo "gc: host removed $done runs, $fails failures"
[ "$fails" = 0 ]
