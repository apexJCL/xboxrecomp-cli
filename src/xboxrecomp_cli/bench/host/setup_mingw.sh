#@ scripts/benchlib/host/setup_mingw.sh: a host script `blinx2 bench` ships over ssh, moved
#@ byte for byte from a scripts/bench.sh heredoc. The #@ lines this file starts
#@ with are this note and are not sent. Runs as: in_box (setup). After the prologue and TAG (the llvm-mingw release tag).
cc="$LLVM_MINGW_ROOT/bin/x86_64-w64-mingw32-clang"
if [ -x "$cc" ] && [ "$(cat "$LLVM_MINGW_ROOT/.tag" 2>/dev/null)" = "$TAG" ]; then
    echo "already installed: $TAG, $("$cc" --version | sed -n 1p)"
    exit 0
fi
url=$(curl -fsSL "https://api.github.com/repos/mstorsjo/llvm-mingw/releases/tags/$TAG" \
      | grep -o 'https://[^"]*-ucrt-ubuntu-[0-9.]*-x86_64\.tar\.xz' | sed -n 1p)
[ -n "$url" ] || { echo "no llvm-mingw $TAG Linux x86_64 release found" >&2; exit 1; }
# Only ever replace an llvm-mingw install, so a mis-set LLVM_MINGW_ROOT
# cannot wipe anything else.
if [ -e "$LLVM_MINGW_ROOT" ]; then
    case "$LLVM_MINGW_ROOT" in
        */llvm-mingw*) ;;
        *) [ -x "$cc" ] || { echo "refusing to replace $LLVM_MINGW_ROOT: not an llvm-mingw install" >&2; exit 1; } ;;
    esac
    rm -rf "$LLVM_MINGW_ROOT"
fi
echo "fetching $url"
tmp=$(mktemp -d)
curl -fL "$url" -o "$tmp/llvm-mingw.tar.xz"
mkdir -p "$LLVM_MINGW_ROOT"
tar -xJf "$tmp/llvm-mingw.tar.xz" -C "$LLVM_MINGW_ROOT" --strip-components=1
rm -rf "$tmp"
echo "$TAG" > "$LLVM_MINGW_ROOT/.tag"
"$cc" --version | sed -n 1p
