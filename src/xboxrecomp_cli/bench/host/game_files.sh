#@ bench/host/game_files.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (sync --game-files, a tree that does not own BENCH_GAME_FILES). After the
#@ prologue and src (the host's single game_files copy), dst (this tree's
#@ game files folder) and mode (link or reflink); src keeps a leading ~/
#@ unquoted so ~ expands on the host, the rest is quoted.
[ -f "$src/${XBE##*/}" ] || { echo "sync: $src has no ${XBE##*/}; run sync --game-files from the tree that owns it (BENCH_DIR=${src%/*/"$GAME_FILES"}) first" >&2; exit 1; }
if [ -L "$dst" ]; then
    rm "$dst"
elif [ -e "$dst" ]; then
    echo "sync: $dst is a real directory; left as is (remove it to share $src)"
    exit 0
fi
if [ "$mode" = reflink ]; then
    cp -a --reflink=always "$src" "$dst"
    chmod -R u+w "$dst"
    echo "sync: $dst is a reflink copy of $src"
else
    ln -s "$src" "$dst"
    echo "sync: $dst -> $src"
fi
