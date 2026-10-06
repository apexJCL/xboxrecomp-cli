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
# (KEY=value lines, yours win).
#
# No lock: this is the game for play. A bench on the same machine checks for
# a running game itself (scripts/running_game.py).
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

load_env "$HERE/launch.env.default"
load_env "$ROOT/config/launch.env"

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
# Your enhance.toml is made once and never touched again; the .default
# beside it follows the installed version (refreshed here, not by the
# installer, which writes nothing in config/).
[ -e "$ROOT/config/enhance.toml" ] || cp "$HERE/enhance.toml.default" "$ROOT/config/enhance.toml"
cmp -s "$HERE/enhance.toml.default" "$ROOT/config/enhance.toml.default" \
    || cp "$HERE/enhance.toml.default" "$ROOT/config/enhance.toml.default"

stamp=$(date +%Y%m%d-%H%M%S)
log="$ROOT/logs/game-$stamp.log"
# The newest LOG_KEEP logs, this one included.
keep=${LOG_KEEP:-10}
ls -1t "$ROOT"/logs/game-*.log 2>/dev/null | tail -n +"$keep" | while IFS= read -r old; do rm -f -- "$old"; done || true

# Paths the game opens are Windows paths: Wine maps the host's / to Z:.
export RECOMP_GAME_FILES="Z:$ROOT/game_files"
export RECOMP_HDD_DIR="Z:$ROOT/hdd"
export RECOMP_ENHANCE_CONFIG="Z:$ROOT/config/enhance.toml"
export RECOMP_STDIO_LOG="Z:$log"
export WINEPREFIX="$ROOT/prefix" GAMEID="${GAMEID:-umu-default}" PROTONPATH="${PROTONPATH:-GE-Proton}"

# The first launch shows nothing for minutes while umu-run downloads its
# runtime and Proton and makes the Wine prefix. setup_pending says which
# step is still to come; none means a quick launch and no notice.
setup_pending() {
    local data="${XDG_DATA_HOME:-$HOME/.local/share}"
    if ! compgen -G "$data/umu/steamrt*" >/dev/null; then
        echo "Downloading the Steam Linux Runtime ..."
    elif [ "${PROTONPATH#/}" = "$PROTONPATH" ] \
        && ! compgen -G "$HOME/.local/share/Steam/compatibilitytools.d/$PROTONPATH*" >/dev/null; then
        echo "Downloading $PROTONPATH ..."
    elif [ ! -f "$ROOT/prefix/system.reg" ]; then
        echo "Creating the Wine prefix ..."
    else
        return 1
    fi
}

# The notice: a zenity or kdialog window (SteamOS ships both), plus a
# notification in Game Mode (gamescope) or when neither is there. It closes
# when the game writes its log (its window is up), when umu-run ends, or
# after NOTICE_MAX seconds.
NOTICE_TEXT="First launch: setting up Proton. This takes a few minutes, once; the game starts by itself."
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
# Without a notice the launcher becomes umu-run, as it always did;
# LAUNCH_NOTICE=0 in config/launch.env turns the notice off.
cd "$ROOT/logs"
if [ "${LAUNCH_NOTICE:-1}" = 0 ] || { [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; } \
    || ! setup_pending >/dev/null; then
    exec "$UMU" "$HERE/@EXE@.exe" "$@" >"$ROOT/logs/umu.log" 2>&1
fi
"$UMU" "$HERE/@EXE@.exe" "$@" >"$ROOT/logs/umu.log" 2>&1 &
umu_pid=$!
trap 'kill -TERM "$umu_pid" 2>/dev/null || true' TERM INT HUP
notice_start "$umu_pid"
rc=0
wait "$umu_pid" || rc=$?
# A trapped signal ends the first wait early; the game is still ending.
if kill -0 "$umu_pid" 2>/dev/null; then
    rc=0
    wait "$umu_pid" || rc=$?
fi
kill ${notice_pids[@]+"${notice_pids[@]}"} 2>/dev/null || true
exit "$rc"
