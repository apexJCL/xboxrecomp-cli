#@ bench/host/doctor.sh: `<game> bench doctor`'s look at the host. New with
#@ the CLI (bench.sh had no doctor). The #@ lines this file starts with are
#@ this note and are not sent. Runs as: remote, after the prologue. Changes nothing: it never takes
#@ or waits on the run lock, it only asks lslocks who holds it.
bad=0
if command -v umu-run >/dev/null; then
    echo "umu-run:  $(command -v umu-run)"
else
    echo "umu-run:  MISSING on the host (Proton runs through it)"; bad=1
fi
if distrobox list 2>/dev/null | awk -F'|' -v box="$BENCH_BOX" 'NR>1 {gsub(/ /,"",$2)} $2 == box {f=1} END {exit !f}'; then
    echo "box:      $BENCH_BOX"
else
    echo "box:      MISSING: no distrobox $BENCH_BOX ($SLUG bench setup)"; bad=1
fi
cc="$LLVM_MINGW_ROOT/bin/x86_64-w64-mingw32-clang"
if [ -x "$cc" ]; then
    echo "llvm-mingw: $LLVM_MINGW_ROOT ($(cat "$LLVM_MINGW_ROOT/.tag" 2>/dev/null || echo 'no .tag'))"
else
    echo "llvm-mingw: MISSING at $LLVM_MINGW_ROOT ($SLUG bench setup)"; bad=1
fi
h=$(lslocks -n -o PID,BLOCKER,PATH 2>/dev/null | awk '!h && /recomp-run\.lock$/ && NF == 2 {h = $1} END {print h}' || true)
if [ -n "$h" ]; then
    echo "run lock: held by $(ps -o pid=,args= -p "$h" 2>/dev/null | cut -c1-200 || echo "$h")"
else
    echo "run lock: free"
fi
if [ -f "$REMOTE_GAME/bench-provenance.txt" ]; then
    # Its first line already says synced:; the tree lines go under it.
    sed '1s/^synced: */synced:   /; 2,$s/^/          /' "$REMOTE_GAME/bench-provenance.txt"
else
    echo "synced:   nothing yet in $REMOTE_GAME ($SLUG bench sync)"
fi
if [ -f "$REMOTE_GAME/$EXE_REL" ]; then
    echo "built:    $(sha256sum "$REMOTE_GAME/$EXE_REL" | cut -c1-16) $EXE_REL"
else
    echo "built:    no $EXE_REL yet ($SLUG bench build)"
fi
exit $bad
