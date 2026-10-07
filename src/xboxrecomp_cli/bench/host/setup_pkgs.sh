#@ bench/host/setup_pkgs.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box (setup). After the prologue; no other variables.
sudo dnf install -y cmake ninja-build curl xz tar
