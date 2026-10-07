#@ bench/host/run_game.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (run, golden, pacing, all). After the prologue and STAMP, PROTONPATH,
#@ GAME_ARGS (array), GAME_ENV (array, word-split from BENCH_ENV), TIMEOUT, FRAMES,
#@ KILL_GAME, LOCK_HELD, PROTON_LOG_MODE (cap, full or off), PROTON_LOG_CAP (bytes).
#@ Takes the run lock on fd 9 itself.
cd "$REMOTE_GAME"
[ -f "$EXE_REL" ] || { echo "no $EXE_REL -- run build first" >&2; exit 1; }
[ -f "$XBE" ]     || { echo "no $GAME_FILES/ in this tree -- run: sync --game-files" >&2; exit 1; }

# SSH has no display; borrow the logged-in desktop session's, which KDE and
# GNOME import into the systemd user environment.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
eval "$(systemctl --user show-environment 2>/dev/null \
        | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY)=' | sed 's/^/export /')" || true
export DISPLAY="${DISPLAY:-:0}"
[ -n "${WAYLAND_DISPLAY:-}" ] || echo "warning: no desktop session found; the window may not open" >&2

LOG="bench-logs/$STAMP"
mkdir -p "$LOG" "$BENCH_PREFIX"
# Proton drops the game's stdout/stderr (GUI or console subsystem alike), so
# the game writes them to $RECOMP_STDIO_LOG itself; console.log only holds
# umu/Proton's own output.
export RECOMP_STDIO_LOG="$PWD/$LOG/game-stdio.log"

command -v umu-run >/dev/null || { echo "host has no umu-run" >&2; exit 1; }

# One game at a time on this host: every agent's runs take the same lock, so
# flips/s are not skewed by an overlapping run. Held on fd 9 until this
# script ends; the game itself does not inherit it (9>&- below).
LOCK="$HOME/.recomp-run.lock"
exec 9>>"$LOCK"
waited=0
# cmd_pacing holds the lock across all its runs (hold_run_lock); its runs
# must not wait on it again.
if [ "$LOCK_HELD" = 1 ]; then
    echo "bench: the run lock is held for this run's series (bench.sh pacing)"
elif ! flock -n 9; then
    # The holder is the lock's one entry with no BLOCKER; the file's text
    # names only the last bench.sh run (a bare `flock FILE cmd` writes none).
    # awk reads to the end (no exit): under pipefail an early exit can
    # SIGPIPE lslocks, and set -e would then end this script silently.
    holder=$(lslocks -n -o PID,BLOCKER,PATH 2>/dev/null | awk '!h && /recomp-run\.lock$/ && NF == 2 {h = $1} END {print h}' || true)
    echo "bench: waiting for the run lock $LOCK, held by: $( [ -n "$holder" ] && ps -o pid=,args= -p "$holder" | cut -c1-200 || echo '?')"
    echo "bench:   (last bench.sh run to take it: $(cat "$LOCK" 2>/dev/null || echo none))"
    t0=$(date +%s)
    # Bounded: a wedged holder (a hung game run with no limit) fails every
    # waiter visibly instead of queueing them behind one line forever.
    flock -w 3600 9 || { echo "bench: FAIL the run lock $LOCK was held for an hour by: ${holder:-?}" >&2; exit 1; }
    waited=$(( $(date +%s) - t0 ))
    echo "bench: got the run lock after ${waited}s"
fi
[ "$LOCK_HELD" = 1 ] || echo "$(date -Is) $REMOTE_GAME/$LOG (pid $$)" > "$LOCK"
# A game started outside the bench (the installed copy, or one by hand)
# takes no lock, and every bench run holds this one, so any game process now
# is foreign and would make this run's timings noisy: warn (into the run's
# log dir too), or end it with --kill-game / BENCH_KILL_GAME=1.
rg="$CLI_DIR/src/xboxrecomp_cli/running_game.py"
if [ -f "$rg" ] && games=$(python3 "$rg" --exe "$EXE" 2>/dev/null) \
        && [ -n "$games" ]; then
    if [ "$KILL_GAME" = 1 ]; then
        python3 "$rg" --exe "$EXE" --kill | sed 's/^/bench: ended a game outside the bench: /' \
            | tee -a "$LOG/warnings.txt" || true
    else
        printf '%s\n' "$games" \
            | sed 's/^/bench: WARN a game outside the bench is running; timings will be noisy (--kill-game ends it): /' \
            | tee -a "$LOG/warnings.txt"
    fi
fi
# Only under the lock: a run from this same checkout may be writing it.
rm -f xbox_kernel.log

export WINEPREFIX="$BENCH_PREFIX" GAMEID=umu-default PROTONPATH
# Proton's own log (steam-default.log): its loader trace names where the exe
# was loaded (symbolize's fallback), but with +seh on, a run that traps
# often writes gigabytes of it. The game's evidence is game-stdio.log, so
# by default only its head and tail are kept (cap_log, after the run);
# BENCH_PROTON_LOG=full keeps all of it, =off does not write it.
if [ "$PROTON_LOG_MODE" = off ]; then
    unset PROTON_LOG PROTON_LOG_DIR
else
    export PROTON_LOG=1 PROTON_LOG_DIR="$PWD/$LOG"
fi
# RECOMP_TRACE and RECOMP_DEBUG are lists: a scenario's pins and BENCH_ENV
# each add their keys (docs/env.md) instead of the last one winning. Start
# from empty so a list left in the calling shell does not leak in.
unset RECOMP_TRACE RECOMP_DEBUG
for kv in ${GAME_ENV[@]+"${GAME_ENV[@]}"}; do
    case "$kv" in
        RECOMP_TRACE=*|RECOMP_DEBUG=*)
            k=${kv%%=*}; cur=${!k:-}
            export "$k=${cur:+$cur,}${kv#*=}" ;;
        *) export "$kv" ;;
    esac
done
# RECOMP_SAVE_DIR=@run: the title's UDATA/TDATA go to a fresh, empty save/
# in this run's log dir (a Z: path for Wine), so every story run starts with
# no save game. Nothing outside $LOG is created or deleted.
if [ "${RECOMP_SAVE_DIR:-}" = @run ]; then
    mkdir -p "$LOG/save"
    export RECOMP_SAVE_DIR="Z:$PWD/$LOG/save"
    for i in "${!GAME_ENV[@]}"; do
        [ "${GAME_ENV[$i]}" != RECOMP_SAVE_DIR=@run ] || GAME_ENV[$i]="RECOMP_SAVE_DIR=$RECOMP_SAVE_DIR"
    done
fi
# RECOMP_FB_DUMP=@run: the CPU backend's frame dumps (RECOMP_DEBUG=fb_dump)
# go to this run's frames/ (a Z: path for Wine), as d3d11_dump's do below;
# not pulled by logs.
if [ "${RECOMP_FB_DUMP:-}" = @run ]; then
    unset RECOMP_FB_DUMP
    mkdir -p "$LOG/frames"
    export RECOMP_DEBUG="${RECOMP_DEBUG:+$RECOMP_DEBUG,}fb_dump=Z:$PWD/$LOG/frames/f"
    for i in "${!GAME_ENV[@]}"; do
        [ "${GAME_ENV[$i]}" != RECOMP_FB_DUMP=@run ] || GAME_ENV[$i]="RECOMP_DEBUG=fb_dump=Z:$PWD/$LOG/frames/f"
    done
fi
if [ "$FRAMES" = 1 ]; then
    # Wine sees the host's / as Z:.
    mkdir -p "$LOG/frames"
    export RECOMP_DEBUG="${RECOMP_DEBUG:+$RECOMP_DEBUG,}d3d11_dump=Z:$PWD/$LOG/frames/"
    GAME_ENV+=("RECOMP_DEBUG=d3d11_dump=Z:$PWD/$LOG/frames/")
fi

{
    echo "host:   $(hostname)  $(date -Is)"
    echo "proton: $PROTONPATH  prefix: $WINEPREFIX"
    echo "env:    ${GAME_ENV[*]:-(none)}"
    echo "args:   ${GAME_ARGS[*]:-(none)}"
    echo "limit:  ${TIMEOUT:-none}${TIMEOUT:+ s (SIGINT)}"
    echo "lock:   $LOCK, waited ${waited}s"
    sha256sum "$EXE_REL"
    if [ -f build-win/provenance.txt ]; then
        sed 's/^/built:  /' build-win/provenance.txt
    else
        echo "built:  unknown sources (build-win/provenance.txt missing)"
    fi
} > "$LOG/run-info.txt"

# The game files are found relative to the working directory: run from here
# (umu-run keeps it). stdin is /dev/null: this script itself arrives on stdin,
# and anything reading it would swallow the lines below.
# With a limit: SIGINT, then SIGKILL 10 s later if umu-run is still up. The
# game's output goes to the file, not through a pipe, so a Wine process that
# outlives umu-run cannot hold the run open; tail shows it live meanwhile.
LIMIT=()
[ -n "$TIMEOUT" ] && LIMIT=(timeout -k 10 -s INT "$TIMEOUT")

# This run's processes, whatever they are called: everything umu-run starts
# (pressure-vessel, Proton, wineserver, the game) inherits RECOMP_BENCH_RUN,
# which only this run's game command is given. No cwd or path assumption
# (the container's mount layout), and no tool that merely names the exe
# (llvm-symbolizer from a concurrent symbolize) matches. The ps pipeline is
# || true: a pid that exits between pgrep and ps must not end this script
# under pipefail + errexit.
survivors() {
    local p
    for p in $(pgrep -u "$(id -u)" .); do
        [ "$p" != $$ ] || continue
        grep -qxzF "RECOMP_BENCH_RUN=$STAMP" "/proc/$p/environ" 2>/dev/null || continue
        ps -o pid=,args= -p "$p" 2>/dev/null | cut -c1-300 || true
    done
}
loadavg() { cut -d' ' -f1-3 /proc/loadavg; }
# CPU pressure (PSI): microseconds during which some runnable task waited
# for a cpu. Empty where the kernel has PSI off.
psi_cpu() { sed -n 's/^some .*total=\([0-9]*\).*/\1/p' /proc/pressure/cpu 2>/dev/null || true; }
load_start=$(loadavg)
psi_start=$(psi_cpu); t_start=$(date +%s.%N)

set +e
: > "$LOG/console.log"
RECOMP_BENCH_RUN="$STAMP" ${LIMIT[@]+"${LIMIT[@]}"} umu-run "$PWD/$EXE_REL" ${GAME_ARGS[@]+"${GAME_ARGS[@]}"} </dev/null >"$LOG/console.log" 2>&1 9>&- &
pid=$!
tail -n +1 -f --pid="$pid" "$LOG/console.log" 9>&- &
tailpid=$!
# Once, 10 s in: what the sweep below would see. An empty list while
# umu-run is up means the sweep is blind (the tag did not propagate).
(
    sleep 10
    kill -0 "$pid" 2>/dev/null || exit 0
    procs=$(survivors)
    echo "procs:  $(printf '%s' "$procs" | grep -c . || true) tagged RECOMP_BENCH_RUN=$STAMP at +10 s"
    printf '%s\n' "$procs" | sed '/^$/d; s/^/procs:    /'
) >> "$LOG/run-info.txt" 9>&- &
procpid=$!
wait "$pid"
rc=$?
psi_end=$(psi_cpu); t_end=$(date +%s.%N)
sleep 1; kill "$tailpid" 2>/dev/null; wait "$tailpid" 2>/dev/null
wait "$procpid" 2>/dev/null
set -e
echo "$rc" > "$LOG/exit-code"

# For check_run_end: the guest paces itself at 30 Hz, so a clean run's flip
# count is pinned and a low one means a slow run. busy says whether the game
# was starved of cpu, measured, not guessed from who else is running (an
# idle-ish xemu on 16 cpus is not contention): busy if the PSI cpu stall
# share over the run is 5%+. Where PSI is off: a 1-min load at the end (the
# game's own 3-4 cores included) of nproc-2+, or at the start of nproc/2+.
# top and hog stay as attribution only. hog: the first process outside the
# game's stack at 50%+ of a cpu (ps pcpu is a lifetime average; the 2 s
# minimum age skips this script's own ps and sed).
ncpu=$(nproc)
top=$(ps -eo pcpu=,etimes=,args= --sort=-pcpu | sed -n '1,8p' | cut -c1-160)
hog=$(awk -v exe="${EXE%.exe}" '$2 >= 2 && $1 >= 50 && index($0, exe) == 0 && $0 !~ /wine|[Pp]roton|pressure-vessel|pv-adverb|srt-bwrap|umu|steam/ {
           if (!n++) { $2 = ""; print } }' <<<"$top")
load_end=$(loadavg)
stall=
if [ -n "$psi_start" ] && [ -n "$psi_end" ]; then
    stall=$(awk -v a="$psi_start" -v b="$psi_end" -v t0="$t_start" -v t1="$t_end" \
        'BEGIN {w = t1 - t0; printf "%.1f", (w > 0 ? (b - a) / 1e6 / w * 100 : 0)}') || stall=
fi
why=()
if [ -n "$stall" ]; then
    awk -v x="$stall" 'BEGIN {exit !(x >= 5)}' && why+=("cpu stall $stall% of the run")
else
    awk -v l="${load_end%% *}" -v n="$ncpu" 'BEGIN {exit !(l >= n - 2)}' && why+=("load $load_end at end")
    awk -v l="${load_start%% *}" -v n="$ncpu" 'BEGIN {exit !(l >= n / 2)}' && why+=("load $load_start at start")
fi
busy=no
if [ "${#why[@]}" != 0 ]; then
    busy="yes ($(IFS=';'; echo "${why[*]}"))"
    [ -z "$hog" ] || busy="$busy; top non-game:$hog"
fi
{
    if [ -f "$LOG/game-stdio.log" ]; then
        echo "flips:  $(grep -c '^\[D3D11\] flip' "$LOG/game-stdio.log" || true)"
    else
        echo "flips:  ? (no game-stdio.log)"
    fi
    echo "load:   $load_start at start, $load_end at end ($ncpu cpus)"
    if [ -n "$stall" ]; then
        echo "cpu-stall: $stall% (PSI cpu some over the run; busy at 5%)"
    else
        echo "cpu-stall: ? (PSI off; busy by load: end >= $((ncpu - 2)) or start >= $((ncpu / 2)))"
    fi
    echo "busy:   $busy"
    [ -z "$hog" ] || echo "hog:   $hog"
    sed -n '1,4p' <<<"$top" | sed 's/^ */top:    /'
} >> "$LOG/run-info.txt"

# Anything of this run still alive? Proton's pressure-vessel wrapper takes a
# few seconds to tear down after umu-run returns, so give it 20 s before
# calling anything a survivor.
for _ in $(seq 20); do [ -n "$(survivors)" ] || break; sleep 1; done
if [ -n "$(survivors)" ]; then
    # $LOG/survived: what outlived umu-run, and whether the sweep cleared it.
    # check_run_end fails the run only on "still alive"; a survivor that
    # was killed is a warning (the display is clean for the next run).
    {
        echo "survived umu-run exit $rc (after 20 s grace):"
        survivors
    } > "$LOG/survived"
    echo "!!! bench: the game outlived umu-run (exit $rc); killing it:" >&2
    cat "$LOG/survived" >&2
    ws=$(ls -d "$HOME"/.local/share/Steam/compatibilitytools.d/*/files/bin/wineserver 2>/dev/null | tail -1)
    [ -n "$ws" ] && WINEPREFIX="$WINEPREFIX" "$ws" -k || true
    sleep 2
    survivors | awk '{print $1}' | xargs -r kill -9 2>/dev/null || true
    sleep 1
    left=$(survivors)
    if [ -n "$left" ]; then
        { echo "still alive after wineserver -k and kill -9:"; echo "$left"; } >> "$LOG/survived"
        echo "!!! bench: still alive after wineserver -k and kill -9:" >&2
        echo "$left" >&2
    else
        echo "killed: none left after wineserver -k and kill -9" >> "$LOG/survived"
    fi
fi

# The first and last $2 bytes of $1, with a line between them saying how
# much was cut; a file no larger than twice that is left alone. After the
# sweep above: nothing of this run writes the log any more.
cap_log() {
    local f=$1 n=$2 size
    size=$(wc -c < "$f" | tr -d ' ')
    [ "$size" -gt $((2 * n)) ] || return 0
    {
        head -c "$n" "$f"
        printf '\n[bench: %s bytes cut here; BENCH_PROTON_LOG=full keeps them]\n' $((size - 2 * n))
        tail -c "$n" "$f"
    } > "$f.cap" && mv -f "$f.cap" "$f" || rm -f "$f.cap"
}
if [ "$PROTON_LOG_MODE" = cap ]; then
    for f in "$LOG"/steam-*.log; do
        if [ -f "$f" ]; then cap_log "$f" "$PROTON_LOG_CAP"; fi
    done
fi
[ -f xbox_kernel.log ] && cp xbox_kernel.log "$LOG/"
echo "exit code $rc; logs in $REMOTE_GAME/$LOG"
