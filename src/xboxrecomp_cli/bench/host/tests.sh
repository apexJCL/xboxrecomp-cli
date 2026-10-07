#@ scripts/benchlib/host/tests.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box_locked -x tests (tests). After the prologue; no other variables.
cd "$REMOTE_GAME"
export PATH="$LLVM_MINGW_ROOT/bin:$PATH"
emu=$(cd ../xboxrecomp 2>/dev/null && pwd -P || true)/tests/proton_run.sh
[ -x "$emu" ] || { echo "tests: $emu missing (toolkit older than 5564c43?)" >&2; exit 1; }
[ -f build-win/CMakeCache.txt ] || { echo "tests: no build-win (run bench.sh build first)" >&2; exit 1; }
cmake -B build-win -DCMAKE_CROSSCOMPILING_EMULATOR="$emu" >/dev/null
cmake --build build-win --target d3d8_hlsl_split d3d11_backend_smoke input_map_test input_keyboard_test
# tests/nv2a_zbuf, apu_irq, kernel_irql_abi, fp_precision, vblank_ack, vblank_schedule
# spin_wait, rt_alias and irq_safe_points are projects of their own
# (not in the game build): configure each beside build-win with the same
# toolchain.
standalone="nv2a_zbuf apu_irq kernel_irql_abi fp_precision vblank_ack vblank_schedule spin_wait rt_alias irq_safe_points"
for t in $standalone; do
    src=../xboxrecomp/tests/$t
    [ -f "$src/CMakeLists.txt" ] || { echo "tests: $src missing (toolkit too old?)" >&2; exit 1; }
    cmake -S "$src" -B "build-win/tests/$t" -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE="$PWD/cmake/llvm-mingw-x86_64.cmake" \
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
