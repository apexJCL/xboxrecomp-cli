#@ scripts/benchlib/host/gen_digest.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (integrate). After the prologue; prints the sha256 over gen/.
cd "$REMOTE_GAME/$GEN_DIR" && sha256sum -- * | LC_ALL=C sort -k2 | sha256sum | cut -d' ' -f1
