#@ scripts/benchlib/host/build.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box_locked, shared (build). After the prologue and EXTRA_ARGS (an array of
#@ extra cmake arguments).
cd "$REMOTE_GAME"
[ -f cmake/llvm-mingw-x86_64.cmake ] || { echo "cmake/llvm-mingw-x86_64.cmake missing -- not synced yet?" >&2; exit 1; }
export PATH="$LLVM_MINGW_ROOT/bin:$PATH"
if [ ! -f build-win/CMakeCache.txt ]; then
    cmake -S . -B build-win -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE=cmake/llvm-mingw-x86_64.cmake \
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
ls -la build-win/*.exe
