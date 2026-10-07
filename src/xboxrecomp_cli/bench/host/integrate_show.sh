#@ scripts/benchlib/host/integrate_show.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh echo line. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (integrate). After the prologue.
cd "$REMOTE_GAME"; sha256sum "$EXE_REL"; cat build-win/provenance.txt
