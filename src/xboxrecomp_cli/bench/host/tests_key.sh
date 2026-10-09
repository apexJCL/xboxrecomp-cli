#@ bench/host/tests_key.sh: a host script `<game> bench` ships over ssh.
#@ The #@ lines this file starts with are this note and are not sent.
#@ Runs as: remote (golden, integrate, tests). After the prologue and
#@ PROTONPATH. Prints the host's parts of the tests key (tests_skip.py),
#@ "name: value" lines; a part it cannot read is "unknown".
set +e
# tests_key: the toolkit tree as sync sends it (no .git, build trees, venvs).
if cd "$BENCH_DIR/xboxrecomp" 2>/dev/null; then
    printf 'toolkit: %s\n' "$(find . \( -name .git -o -name build -o -name 'build-*' -o -name .venv \
        -o -name __pycache__ -o -name .DS_Store \) -prune -o -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
else
    echo "toolkit: unknown"
fi
hash_of() { if [ -f "$1" ]; then sha256sum "$1" | cut -d' ' -f1; else echo unknown; fi; }
cd "$REMOTE_GAME" 2>/dev/null || { echo "cmake: unknown"; exit 0; }
echo "toolchain: $(hash_of "$TOOLCHAIN")"
echo "cmake: $(hash_of CMakeLists.txt)"
# The Proton umu-run resolves: PROTONPATH itself when it is a directory,
# else the newest install of that name (GE-Proton: the latest it fetched).
if [ -d "$PROTONPATH" ]; then
    pd=$PROTONPATH
else
    pd=$(ls -d "$HOME/.local/share/Steam/compatibilitytools.d/$PROTONPATH"*/ 2>/dev/null | sort -V | tail -1)
fi
if [ -n "$pd" ] && [ -f "$pd/version" ]; then
    echo "proton: $(head -c 200 "$pd/version" | tr -d '\n')"
else
    echo "proton: unknown"
fi
# Proton stamps the prefix with the version that last updated it.
if [ -f "$BENCH_PREFIX/version" ]; then
    echo "prefix: $(head -c 200 "$BENCH_PREFIX/version" | tr -d '\n')"
else
    echo "prefix: none"
fi
