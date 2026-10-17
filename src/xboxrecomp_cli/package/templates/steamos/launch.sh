#!/usr/bin/env bash
#
# @NAME@ launcher for a SteamOS PC (a Steam Deck, or a gaming distribution
# that ships umu-run): runs the Windows build under Proton through umu-run.
# Installed as <root>/versions/<version>/launch.sh by install.sh and started
# through the stable <root>/@APP@ (the Steam shortcut's target).
#
# Everything the game keeps lives under <root>, outside the program files
# and outside the Wine prefix (Steam or a Proton update may recreate a
# prefix): game_files/ (the dump), hdd/ (saves), config/, logs/. Settings:
# launch.env.default beside this script, then <root>/config/launch.env
# (KEY=value lines, yours win). Options: the game reads config.default.toml
# beside its executable, then <root>/config/config.toml, which holds only
# the keys you change; it writes that file's comment header itself, and
# renames an earlier version's config/enhance.toml to it once.
#
# Proton is the release the CLI pins, which install.sh put in
# compatibilitytools.d: no launch downloads one, and the Steam Linux Runtime
# is updated only on the first launch after an install.
#
# One launch at a time: <root>/state/launch.lock is held while the game
# runs, so a second tap in Game Mode says so instead of starting a second
# game on the same prefix and saves. It is not the bench's run lock; a bench
# on the same machine checks for a running game itself
# (scripts/running_game.py).
#
set -euo pipefail

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # versions/<v>, not current
ROOT="$(cd "$HERE/../.." && pwd)"

fail() {
    echo "@NAME@: $*" >&2
    if { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && command -v notify-send >/dev/null; then
        notify-send -a "@NAME@" "@NAME@ cannot start" "$*" || true
    fi
    exit 1
}

# A message for the player, not an error: a notification, else a window
# that closes by itself.
tell() {
    echo "@NAME@: $*" >&2
    { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } || return 0
    if command -v notify-send >/dev/null && notify-send -a "@NAME@" -t 10000 "@NAME@" "$*" 2>/dev/null; then
        return 0
    fi
    if command -v zenity >/dev/null; then
        zenity --info --title "@NAME@" --text "$*" --timeout 10 >/dev/null 2>&1 || true
    elif command -v kdialog >/dev/null; then
        kdialog --title "@NAME@" --passivepopup "$*" 10 >/dev/null 2>&1 || true
    fi
}

# KEY=value lines; blank lines and # comments skipped; anything else is an
# error with its line number, so a typo is not silently ignored.
load_env() {
    local file=$1 n=0 line
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        line="${line%$'\r'}"
        case "$line" in ''|'#'*) continue ;; esac
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || fail "$file:$n: not a KEY=value line: $line"
        export "${line%%=*}=${line#*=}"
    done < "$file"
}

# Steam's Game Mode, or a nested gamescope on a desktop. The game then
# hands gamescope the frame to scale (present.surface=auto -> internal),
# so Steam's scaling filter (FSR) applies. A line in config/launch.env wins.
if [ -z "${RECOMP_HOST_GAMESCOPE:-}" ] \
    && { [ "${XDG_CURRENT_DESKTOP:-}" = gamescope ] || [ -n "${GAMESCOPE_WAYLAND_DISPLAY:-}" ]; }; then
    export RECOMP_HOST_GAMESCOPE=1
fi

# The bundle's defaults never pick the Proton: a game shares that file with
# its Windows installer, and a PROTONPATH=GE-Proton line there would undo
# the pin (umu-run asking GitHub at every launch). The environment's
# PROTONPATH and config/launch.env's still win.
proton_set="${PROTONPATH+set}" proton_env="${PROTONPATH-}"
load_env "$HERE/launch.env.default"
if [ -n "$proton_set" ]; then export PROTONPATH="$proton_env"; else unset PROTONPATH; fi
load_env "$ROOT/config/launch.env"

# Before anything touches logs/: the running game's logs are trimmed by the
# next launch, which must not be a second one. fd 9 is closed for umu-run
# and the notice, so a wineserver that outlives the game never holds it.
mkdir -p "$ROOT/state"
if command -v flock >/dev/null; then
    exec 9>>"$ROOT/state/launch.lock"
    if ! flock -n 9; then
        tell "@NAME@ is already starting or running."
        exit 0
    fi
fi

[ -f "$ROOT/game_files/default.xbe" ] || fail "no game files at $ROOT/game_files/default.xbe; run install.sh from the bundle again"

# umu-run: launch.env's UMU_RUN, then PATH, then ~/.local/bin (Game Mode's
# PATH lacks it), then the copy install.sh recorded or fetched; install.sh
# searches the same places.
find_umu() {
    local c recorded=""
    if [ -f "$ROOT/state/umu-run" ]; then
        IFS= read -r recorded < "$ROOT/state/umu-run" || true
    fi
    for c in "${UMU_RUN:-}" "$(command -v umu-run || true)" "$HOME/.local/bin/umu-run" \
        "$recorded" "${XDG_DATA_HOME:-$HOME/.local/share}/xboxrecomp/umu/umu-run"; do
        if [ -n "$c" ] && [ -f "$c" ] && [ -x "$c" ]; then
            echo "$c"
            return 0
        fi
    done
    return 1
}
UMU=$(find_umu) || fail "umu-run is not installed; it runs the game under Proton. In Desktop Mode, run './install.sh --fetch-umu' from the unpacked bundle folder."

mkdir -p "$ROOT/config" "$ROOT/logs" "$ROOT/prefix"
# An earlier version kept a reference copy of the defaults here; the game
# reads them beside its executable now, so the copy would only mislead.
rm -f "$ROOT/config/enhance.toml.default"

stamp=$(date +%Y%m%d-%H%M%S)
log="$ROOT/logs/game-$stamp.log"
# The newest LOG_KEEP logs, this one included.
keep=${LOG_KEEP:-10}
ls -1t "$ROOT"/logs/game-*.log 2>/dev/null | tail -n +"$keep" | while IFS= read -r old; do rm -f -- "$old"; done || true
# And no log of an earlier launch over LOG_MAX_MB (64): a trace left on in
# launch.env, or PROTON_LOG=1, can write gigabytes. Its first and last
# halves are kept. The running launch's own logs are trimmed next time.
cap_log() {   # $1: file, $2: bytes kept at each end
    local f=$1 n=$2 size
    size=$(wc -c < "$f" | tr -d ' ')
    [ "$size" -gt $((2 * n)) ] || return 0
    {
        head -c "$n" "$f"
        printf '\n[launcher: %s bytes cut here (LOG_MAX_MB)]\n' $((size - 2 * n))
        tail -c "$n" "$f"
    } > "$f.cap" && mv -f "$f.cap" "$f" || rm -f "$f.cap"
}
max_mb=${LOG_MAX_MB:-64}
for old in "$ROOT"/logs/*.log; do
    if [ -f "$old" ] && [ "$max_mb" -gt 0 ] 2>/dev/null; then
        cap_log "$old" $((max_mb * 1024 * 1024 / 2)) || true
    fi
done

# Paths the game opens are Windows paths: Wine maps the host's / to Z:.
export RECOMP_GAME_FILES="Z:$ROOT/game_files"
export RECOMP_HDD_DIR="Z:$ROOT/hdd"
export RECOMP_CONFIG="Z:$ROOT/config/config.toml"
export RECOMP_STDIO_LOG="Z:$log"
# The pinned Proton by its path: a codename (GE-Proton) makes umu-run ask
# GitHub for the newest release, and download it, on every launch. Setting
# PROTONPATH=GE-Proton in config/launch.env opts into exactly that.
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PINNED_PROTON="$DATA/Steam/compatibilitytools.d/@PROTON_DIR@"
if [ -z "${PROTONPATH:-}" ]; then
    [ -f "$PINNED_PROTON/proton" ] \
        || fail "@PROTON_VERSION@ is not installed (it runs the game); run './install.sh' from the bundle folder again"
    PROTONPATH="$PINNED_PROTON"
fi
export WINEPREFIX="$ROOT/prefix" GAMEID="${GAMEID:-umu-default}" PROTONPATH
# The runtime: downloaded by umu-run when absent, and otherwise checked for
# updates only on the first launch after an install or rollback
# (state/runtime-update, which install.sh writes). A download is the newest
# runtime, so it uses up the mark too. A value in config/launch.env wins.
runtime_check=0
if [ -z "${UMU_RUNTIME_UPDATE+set}" ]; then
    if ! compgen -G "$DATA/umu/steamrt*" >/dev/null; then
        rm -f "$ROOT/state/runtime-update"
    elif [ -f "$ROOT/state/runtime-update" ]; then
        runtime_check=1
        rm -f "$ROOT/state/runtime-update"
    else
        export UMU_RUNTIME_UPDATE=0
    fi
fi
# PROTON_LOG=1 in launch.env: Proton's log goes to logs/ (trimmed above),
# not to the home folder.
[ -z "${PROTON_LOG:-}" ] || export PROTON_LOG_DIR="${PROTON_LOG_DIR:-$ROOT/logs}"

# A launch that downloads shows nothing for minutes while umu-run fetches
# its runtime (or a codename's Proton) and makes the Wine prefix.
# setup_pending says which step is still to come; none means a quick launch
# and no notice.
setup_pending() {
    if ! compgen -G "$DATA/umu/steamrt*" >/dev/null; then
        echo "Downloading the Steam Linux Runtime ..."
    elif [ "${PROTONPATH#/}" = "$PROTONPATH" ]; then
        # A codename: umu-run asks GitHub for the newest release and
        # downloads it when it is not the one installed, which no local
        # check can tell beforehand.
        echo "Checking for the newest $PROTONPATH ..."
    elif [ ! -f "$ROOT/prefix/system.reg" ]; then
        echo "Creating the Wine prefix ..."
    elif [ "$runtime_check" = 1 ]; then
        echo "Checking the Steam Linux Runtime for updates ..."
    else
        return 1
    fi
}

# The notice: a zenity or kdialog window (SteamOS ships both), plus a
# notification in Game Mode (gamescope) or when neither is there. It closes
# when the game writes its log (its window is up), when umu-run ends, or
# after NOTICE_MAX seconds.
NOTICE_TEXT="Setting up Proton and the Steam Linux Runtime. This can take a few minutes; the game starts by itself."
notice_pids=()
notice_wait() {   # $1: umu-run's pid; prints the step each second
    local t=0
    while kill -0 "$1" 2>/dev/null && [ ! -s "$log" ] && [ "$t" -lt "${NOTICE_MAX:-1200}" ]; do
        echo "# $(setup_pending || echo "Starting the game ...")"
        sleep 1
        t=$((t + 1))
    done
}
notice_start() {   # $1: umu-run's pid
    local window=""
    if command -v zenity >/dev/null; then
        window=zenity
    elif command -v kdialog >/dev/null; then
        window=kdialog
    fi
    if command -v notify-send >/dev/null \
        && { [ -z "$window" ] || [ "${XDG_CURRENT_DESKTOP:-}" = gamescope ]; }; then
        notify-send -a "@NAME@" -t 30000 "@NAME@" "$NOTICE_TEXT" 2>/dev/null || true
    fi
    case "$window" in
    zenity)
        # zenity closes at the end of its input (--auto-close).
        notice_wait "$1" | zenity --progress --pulsate --auto-close --no-cancel \
            --title "@NAME@" --text "$NOTICE_TEXT" >/dev/null 2>&1 &
        notice_pids+=($!)
        ;;
    kdialog)
        kdialog --title "@NAME@" --passivepopup "$NOTICE_TEXT" "${NOTICE_MAX:-1200}" >/dev/null 2>&1 &
        notice_pids+=($!)
        { notice_wait "$1" >/dev/null; kill "${notice_pids[0]}" 2>/dev/null; } &
        notice_pids+=($!)
        ;;
    esac
}

# xbox_kernel.log opens in the working directory. umu-run's and Proton's own
# output (Steam shows none of it) goes to umu.log, the last launch's only.
# The launcher stays umu-run's parent, holding the lock until it ends;
# LAUNCH_NOTICE=0 in config/launch.env turns the notice off.
# Whether a notice is due is decided before umu-run starts making the
# runtime's directory.
cd "$ROOT/logs"
notice=0
if [ "${LAUNCH_NOTICE:-1}" != 0 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } \
    && setup_pending >/dev/null; then
    notice=1
fi
"$UMU" "$HERE/@EXE@.exe" "$@" >"$ROOT/logs/umu.log" 2>&1 9>&- &
umu_pid=$!
trap 'kill -TERM "$umu_pid" 2>/dev/null || true' TERM INT HUP
[ "$notice" = 0 ] || notice_start "$umu_pid" 9>&-
rc=0
wait "$umu_pid" || rc=$?
# A trapped signal ends the first wait early; the game is still ending.
if kill -0 "$umu_pid" 2>/dev/null; then
    rc=0
    wait "$umu_pid" || rc=$?
fi
kill ${notice_pids[@]+"${notice_pids[@]}"} 2>/dev/null || true
exit "$rc"
