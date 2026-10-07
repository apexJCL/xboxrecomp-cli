#@ bench/host/build.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box_locked, shared (build). After the prologue and EXTRA_ARGS (an array of
#@ extra cmake arguments).
cd "$REMOTE_GAME"
[ -f "$TOOLCHAIN" ] || { echo "$TOOLCHAIN missing -- not synced yet?" >&2; exit 1; }
export PATH="$LLVM_MINGW_ROOT/bin:$PATH"
# A tree configured with a toolchain file that is gone (the game's own copy,
# deleted once the CLI's became the default) cannot reconfigure:
# CMakeSystem.cmake includes the old path on every run. One configured with
# another file keeps it: CMake ignores a new -DCMAKE_TOOLCHAIN_FILE. Either
# way, start that tree afresh.
fresh_if_stale() {
    local dir=$1 old
    old=$(sed -n 's/^include("\(.*\)")$/\1/p' "$dir"/CMakeFiles/*/CMakeSystem.cmake 2>/dev/null | head -1 || true)
    if [ -z "$old" ] || [ -f "$old" ]; then
        old=$(sed -n 's/^CMAKE_TOOLCHAIN_FILE:[A-Z]*=//p' "$dir/CMakeCache.txt" 2>/dev/null | head -1 || true)
        if [ -z "$old" ] || [ "$old" -ef "$TOOLCHAIN" ]; then
            return 0
        fi
    fi
    echo "$dir: configured with the toolchain file $old, not $TOOLCHAIN; configuring afresh"
    rm -rf "$dir/CMakeCache.txt" "$dir/CMakeFiles"
}
fresh_if_stale build-win
if [ ! -f build-win/CMakeCache.txt ]; then
    cmake -S . -B build-win -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
        -DLLVM_MINGW_ROOT="$LLVM_MINGW_ROOT" \
        -DCMAKE_BUILD_TYPE=Release \
        "${EXTRA_ARGS[@]}"
elif [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    cmake -B build-win "${EXTRA_ARGS[@]}"
fi
# Wall time of cmake --build alone (not ssh, distrobox or the lock wait),
# with ninja's last line: "no work to do" or the final [N/M] step.
start=$(date +%s.%N)
cmake --build build-win 2>&1 | tee build-win/last-build.log
echo "build: $(awk -v a="$start" -v b="$(date +%s.%N)" 'BEGIN {printf "%.2f", b - a}')s; last line: $(sed -n '$p' build-win/last-build.log | cut -c1-100)"
if [ -f bench-provenance.txt ]; then
    cp bench-provenance.txt build-win/provenance.txt
else
    echo "build: unknown sources (no bench-provenance.txt; synced by an older bench.sh?)" > build-win/provenance.txt
fi
ls -la "$EXE_REL"
