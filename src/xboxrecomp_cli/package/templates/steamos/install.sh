#!/usr/bin/env bash
#
# @NAME@ installer for a SteamOS PC (a Steam Deck, or a gaming distribution
# that ships umu-run). Run it from the unpacked bundle folder, in Desktop Mode:
#
#   ./install.sh --steam        first time: install to @DEFAULT_ROOT@, add to Steam
#   ./install.sh                every later version: install on top (saves kept)
#   ./install.sh --fetch-umu    also download the pinned umu-launcher (a stock Deck)
#   ./install.sh rollback | status | list | uninstall
#   ./install.sh --help         all options
#
# If the file lost its executable bit on the way (a copy through Windows),
# run it as: bash install.sh
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "install: python3 is needed (SteamOS ships it)" >&2; exit 1; }
exec "$PY" "$HERE/install_lib.py" --bundle "$HERE" "$@"
