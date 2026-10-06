#!/usr/bin/env bash
# Before a public push: what the push would publish, checked for what must
# never leave the maintainer's machines.
#
#   scripts/audit-public.sh            the range origin/main..main
#   scripts/audit-public.sh RANGE      another range (A..B)
#   scripts/audit-public.sh --all      every tracked file at HEAD, and the
#                                      whole history's messages and identities
#
# Checked: added lines and commit messages for private paths (home
# directories on macOS and Linux, an immutable host's /var/home), links into Claude's web app and
# the private patterns below; the files the range adds for binaries and
# game data (*.xbe *.iso *.xiso *.bmp *.png *.wav *.sav); every author and
# committer address, which must be the noreply one. Exit 1 on any hit.
#
# Private patterns (the bench host's name, the personal email, anything
# else that must not appear) are not in this repository, since naming them
# here would publish them: one extended regex per line, matched without
# case, in the file $XBR_AUDIT_PRIVATE or else <git dir>/info/audit-private.
# Without one the audit refuses to pass.
#
# The regexes below bracket a letter (/[U]sers/) so this file does not
# match itself.
set -euo pipefail

NOREPLY="4037632+apexJCL@users.noreply.github.com"
PUBLIC_PATTERNS=(
    '/[U]sers/'
    '/[h]ome/'
    '/var/[h]ome/'
    '[c]laude\.ai'
)
DATA_EXT='\.(xbe|iso|xiso|bmp|png|wav|sav)$'
# Synthetic fixtures that may be binary or carry a data extension (extended
# regexes on the path). None yet: the tests build theirs in temp dirs.
ALLOW=()

cd "$(git rev-parse --show-toplevel)"
all=0
range="origin/main..main"
case "${1:-}" in
    --all) all=1 ;;
    -h | --help)
        sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'
        exit 0
        ;;
    "") ;;
    *) range=$1 ;;
esac
if [ $all = 0 ] && ! git rev-parse -q --verify "${range%%..*}^{commit}" >/dev/null; then
    echo "audit-public: no ${range%%..*} (nothing pushed yet?): use --all" >&2
    exit 2
fi

private=${XBR_AUDIT_PRIVATE:-$(git rev-parse --git-path info/audit-private)}
if [ ! -s "$private" ]; then
    echo "audit-public: no private patterns in $private, so it cannot check for them." >&2
    echo "Write one extended regex per line (the bench host's name, the personal email)," >&2
    echo "once per clone:" >&2
    echo "    printf '%s\n' 'hostname' 'name@example[.]com' > \"\$(git rev-parse --git-path info/audit-private)\"" >&2
    echo "or set XBR_AUDIT_PRIVATE to a file that has them. See README.md, Releases and pins." >&2
    exit 2
fi
patterns=("${PUBLIC_PATTERNS[@]}")
while IFS= read -r p || [ -n "$p" ]; do
    [ -n "$p" ] && [ "${p:0:1}" != "#" ] && patterns+=("$p")
done <"$private"

hits=0
hit() {
    echo "HIT  $*"
    hits=$((hits + 1))
}
allowed() {
    local a
    for a in "${ALLOW[@]+"${ALLOW[@]}"}"; do
        [[ $1 =~ $a ]] && return 0
    done
    return 1
}

# What is checked: the text (added lines, or the whole tree), the messages,
# the files added, the identities.
if [ $all = 1 ]; then
    text() { git grep -I -n -E -i -e "$1" HEAD -- . || true; }
    messages=$(git log --format='%H %B' HEAD)
    files=$(git ls-files)
    idents=$(git log --format='%h %ae %ce' HEAD)
    empty=$(git hash-object -t tree /dev/null)
    binaries=$(git diff --numstat "$empty" HEAD | awk -F'\t' '$1 == "-" {print $3}')
else
    text() {
        git diff "$range" --unified=0 --no-color | grep -E '^\+' | grep -v '^+++ ' |
            grep -n -E -i -e "$1" || true
    }
    messages=$(git log --format='%H %B' "$range")
    files=$(git diff --name-only --diff-filter=AMR "$range")
    idents=$(git log --format='%h %ae %ce' "$range")
    binaries=$(git diff --numstat --diff-filter=AMR "$range" | awk -F'\t' '$1 == "-" {print $3}')
fi

for p in "${patterns[@]}"; do
    while IFS= read -r line; do
        [ -n "$line" ] && hit "text /$p/: $line"
    done < <(text "$p")
    while IFS= read -r line; do
        [ -n "$line" ] && hit "message /$p/: $line"
    done < <(grep -E -i -e "$p" <<<"$messages" || true)
done
while IFS= read -r f; do
    [ -z "$f" ] && continue
    allowed "$f" && continue
    [[ $f =~ $DATA_EXT ]] && hit "game data or capture: $f"
done <<<"$files"
while IFS= read -r f; do
    [ -z "$f" ] && continue
    allowed "$f" && continue
    hit "binary: $f"
done <<<"$binaries"
while read -r sha ae ce; do
    [ -z "${sha:-}" ] && continue
    [ "$ae" = "$NOREPLY" ] && [ "$ce" = "$NOREPLY" ] && continue
    hit "identity: $sha author $ae committer $ce (want $NOREPLY)"
done <<<"$idents"

if [ $hits -gt 0 ]; then
    echo "audit-public: $hits hits" >&2
    exit 1
fi
echo "audit-public: clean ($([ $all = 1 ] && echo "HEAD, whole history" || echo "$range"))"
