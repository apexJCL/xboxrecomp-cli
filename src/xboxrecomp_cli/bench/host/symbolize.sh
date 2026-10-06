#@ scripts/benchlib/host/symbolize.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: remote (symbolize, logs). After the prologue and STAMP (empty: the newest run).
cd "$REMOTE_GAME"
[ -n "$STAMP" ] || STAMP=$(ls -1 bench-logs 2>/dev/null | sort | tail -1)
LOG="bench-logs/$STAMP"
[ -n "$STAMP" ] && [ -d "$LOG" ] || { echo "symbolize: no run $LOG" >&2; exit 1; }
stdio="$LOG/game-stdio.log" out="$LOG/crash-symbols.txt"
grep -q '^\[CRASH\]' "$stdio" 2>/dev/null || { echo "symbolize: no [CRASH] in $LOG"; exit 0; }

# The PDB has to be the one built with the exe that ran.
exe=build-win/cat_recomp.exe pdb=build-win/cat_recomp.pdb
ran=$(awk '/cat_recomp\.exe/ {print $1}' "$LOG/run-info.txt" 2>/dev/null || true)
now=$(sha256sum "$exe" | cut -d' ' -f1)
[ "$ran" = "$now" ] || { echo "symbolize: $exe was rebuilt since $STAMP ran; not symbolizing" >&2; exit 0; }

bin="$LLVM_MINGW_ROOT/bin"
hdr=$("$bin/llvm-readobj" --file-headers "$exe")
pref=$(awk '/ImageBase:/ {print $2}' <<<"$hdr")
size=$(awk '/SizeOfImage:/ {print $2}' <<<"$hdr")

# Where the exe was loaded: the crash report's own image range, else Proton's
# loader trace (PROTON_LOG=1).
base=$(grep -m1 -o 'return addrs in image [0-9A-Fa-fx]*' "$stdio" | awk '{print $NF}') || true
[ -n "$base" ] && [ $((16#${base#0x})) -ne 0 ] ||
    base=$(grep -m1 -o 'cat_recomp\.exe" at [0-9A-Fa-f]*' "$LOG/steam-default.log" 2>/dev/null | awk '{print $NF}') || true
[ -n "$base" ] || { echo "symbolize: no load address for the exe in $LOG" >&2; exit 1; }
base=$((16#${base#0x})) pref=$((pref)) size=$((size))

{
    printf 'exe %s  loaded at 0x%X  ImageBase 0x%X\n' "$now" "$base" "$pref"
    # RIP= on the [CRASH] line, then the "[i] 0x... in ..." native stack lines.
    grep -E '^\[CRASH\]|^    \[[0-9]+\] 0x' "$stdio" | while IFS= read -r line; do
        case "$line" in
            '[CRASH]'*) echo; echo "$line"
                        a=$(grep -oE 'RIP=0x[0-9A-Fa-f]+' <<<"$line" | cut -d= -f2) || true ;;
            *)          a=$(grep -oE '0x[0-9A-Fa-f]+' <<<"$line" | head -1) || true ;;
        esac
        [ -n "$a" ] || continue
        a=$((a))
        if [ "$a" -ge "$base" ] && [ "$a" -lt $((base + size)) ]; then
            link=$((pref + a - base))
            sym=$("$bin/llvm-symbolizer" --obj="$exe" --pdb="$pdb" --pretty-print \
                  "$(printf '0x%X' "$link")" | sed -e '/^$/d' -e 's/ at ??:0:0$//' | paste -sd'|' | sed 's/|/ <- /g')
            printf '  0x%X  (0x%X)  %s\n' "$a" "$link" "$sym"
        else
            printf '  0x%X  outside the exe\n' "$a"
        fi
    done
} > "$out"
echo "symbolize: $(grep -c '^\[CRASH\]' "$out") crash report(s) -> $out"
