#@ bench/host/ship_prep.sh: a host script `<game> bench` ships over ssh
#@ before it rsyncs a locally built exe (BENCH_BUILD=local). The #@ lines
#@ this file starts with are this note and are not sent. Runs as: remote
#@ (ship). After the prologue (REMOTE_GAME is the run directory) and DEST
#@ (the exe's directory under it) and SRC (the host's single game files
#@ copy, BENCH_GAME_FILES; a leading ~/ unquoted so ~ expands here).
mkdir -p "$REMOTE_GAME/$DEST" "$REMOTE_GAME/bench-logs" "$CLI_DIR/src/xboxrecomp_cli"
cd "$REMOTE_GAME"
# Its time is when it was last shipped to: benchdb clean keeps a run
# directory shipped to recently (D11 guard 5), whatever its files say.
touch .
# A new run directory starts as a copy of the newest other one: rsync then
# sends only the blocks of the exe that changed, not all of it.
if [ ! -f "$DEST/$EXE" ]; then
    seed=
    for d in ../*/"$DEST"; do
        [ -f "$d/$EXE" ] && ! [ "$d" -ef "$DEST" ] || continue
        if [ -z "$seed" ] || [ "$d/$EXE" -nt "$seed/$EXE" ]; then seed=$d; fi
    done
    if [ -n "$seed" ]; then
        # Not dot-files: an interrupted rsync leaves its temp files there.
        # The ship's rsync --delete then drops whatever else the new build
        # lacks (a PDB, a DLL), so the directory holds exactly its files.
        find "$seed" -maxdepth 1 -type f ! -name '.*' -exec cp -p --reflink=auto -t "$DEST" {} +
        echo "ship: seeded from ${seed%/"$DEST"}"
    fi
fi
if [ ! -e "$GAME_FILES" ] && [ ! -L "$GAME_FILES" ]; then
    ln -s "$SRC" "$GAME_FILES"
fi
# Every run reads this one copy. The runtime keeps saves in the game dir
# when UDATA/TDATA are there, so a run would change what the next one reads.
for d in UDATA TDATA; do
    [ ! -e "$GAME_FILES/$d" ] || { echo "ship: $SRC/$d: saves in the shared game files; move them out (every run reads this copy)" >&2; exit 1; }
done
[ ! -w "$GAME_FILES/" ] || echo "ship: warning: $SRC is writable; runs share it (sync --game-files leaves it read-only)" >&2
[ -f "$XBE" ] || { echo "ship: $GAME_FILES -> $SRC has no ${XBE##*/} (set BENCH_GAME_FILES, or run sync --game-files once from the tree that owns it)" >&2; exit 1; }
