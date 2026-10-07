#@ bench/host/prune_frames.sh: a host script `<game> bench` ships over ssh.
#@ The #@ lines this file starts with are this note and are not sent. Runs
#@ as: remote (golden). After the prologue and STAMPS (the passing runs'
#@ stamps, an array). Removes each run's frames/: its whole fb_dump_at window
#@ and every 60th present, which no pull reads once the check has passed.
cd "$REMOTE_GAME"
total=0
for s in "${STAMPS[@]}"; do
    # Only a stamp: a name with a slash or .. could leave bench-logs/.
    [[ "$s" =~ ^[0-9]{8}-[0-9]{6}$ ]] || { echo "prune: not a run stamp: $s" >&2; exit 1; }
    d="bench-logs/$s/frames"
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    kb=$(du -sk "$d" | cut -f1)
    rm -rf -- "$d"
    total=$((total + kb))
    echo "prune: host $d removed ($((kb / 1024)) MB)"
done
echo "prune: host frames removed: $((total / 1024)) MB"
