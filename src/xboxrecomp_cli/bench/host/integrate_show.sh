#@ bench/host/integrate_show.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh echo line. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (integrate). After the prologue.
cd "$REMOTE_GAME"; sha256sum "$EXE_REL"; cat build-win/provenance.txt
