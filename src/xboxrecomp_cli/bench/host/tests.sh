#@ bench/host/tests.sh: a host script `<game> bench` ships over ssh, first
#@ moved from a blinx2-recomp scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box_locked -x tests (tests). After the prologue; no other variables.
cd "$REMOTE_GAME"
[ -f "$TOOLCHAIN" ] || { echo "$TOOLCHAIN missing -- not synced yet?" >&2; exit 1; }
export PATH="$LLVM_MINGW_ROOT/bin:$PATH"
emu=$(cd ../xboxrecomp 2>/dev/null && pwd -P || true)/tests/proton_run.sh
[ -x "$emu" ] || { echo "tests: $emu missing (toolkit older than 5564c43?)" >&2; exit 1; }
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
[ -f build-win/CMakeCache.txt ] || { echo "tests: no build-win (run bench.sh build first)" >&2; exit 1; }
cmake -B build-win -DCMAKE_CROSSCOMPILING_EMULATOR="$emu" >/dev/null
cmake --build build-win --target d3d8_hlsl_split d3d11_backend_smoke input_map_test input_keyboard_test
# tests/nv2a_zbuf, apu_irq, kernel_irql_abi, fp_precision, x87_trig, vblank_ack,
# vblank_schedule, spin_wait, rt_alias, irq_safe_points, kernel_missing_report,
# kernel_file_status, kernel_guest_cpu, dpc_order, kernel_events,
# kernel_regressions and memory_regressions are projects of their own
# (not in the game build): configure each beside build-win with the same
# toolchain.
standalone="nv2a_zbuf apu_irq kernel_irql_abi fp_precision x87_trig vblank_ack vblank_schedule spin_wait rt_alias irq_safe_points kernel_missing_report kernel_file_status kernel_guest_cpu dpc_order kernel_events kernel_regressions memory_regressions"
for t in $standalone; do
    src=../xboxrecomp/tests/$t
    [ -f "$src/CMakeLists.txt" ] || { echo "tests: $src missing (toolkit too old?)" >&2; exit 1; }
    fresh_if_stale "build-win/tests/$t"
    cmake -S "$src" -B "build-win/tests/$t" -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
        -DLLVM_MINGW_ROOT="$LLVM_MINGW_ROOT" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CROSSCOMPILING_EMULATOR="$emu" >/dev/null
    cmake --build "build-win/tests/$t"
done
unset PROTON_RUN_LOCK
export WINEPREFIX="$BENCH_PREFIX"
fail=0
for d in src/d3d/d3d8_hlsl_split src/d3d/d3d11_backend_smoke src/input/input_map src/input/input_keyboard; do
    t=${d##*/}
    if ctest --test-dir "build-win/xboxrecomp/$d" --output-on-failure --no-tests=error; then
        echo "tests: $t pass"
    else
        echo "tests: FAIL $t"; fail=1
    fi
done
for t in $standalone; do
    if ctest --test-dir "build-win/tests/$t" --output-on-failure --no-tests=error; then
        echo "tests: $t pass"
    else
        echo "tests: FAIL $t"; fail=1
    fi
done
exit $fail
