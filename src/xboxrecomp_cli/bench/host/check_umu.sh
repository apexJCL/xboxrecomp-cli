#@ bench/host/check_umu.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh echo line. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (setup). No prologue.
command -v umu-run >/dev/null || { echo "host has no umu-run" >&2; exit 1; }
