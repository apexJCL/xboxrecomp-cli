#!/usr/bin/env bash
# Before a public push: what the push would publish, checked for what must
# never leave the maintainer's machines.
#
#   scripts/audit-public.sh            the range origin/main..main
#   scripts/audit-public.sh RANGE      another range (A..B)
#   scripts/audit-public.sh --all      every tracked file at HEAD, and the
#                                      whole history's messages and identities
#
# Checked: added lines, the paths of the files the range adds and commit
# messages for private paths (home directories on macOS, Linux, Windows and
# MSYS, an immutable host's /var/home, without case; only a Wine prefix's
# own drive_c/users/steamuser/ and drive_c/users/public/ are nobody's), links into
# Claude's web app, tracker ids
# (with or without the hyphen, as run and worktree paths spell them), the review
# model's name, the test Deck's ssh alias (as an ssh, scp or rsync host) and
# the private patterns below; the files the range adds for binaries and
# game data (*.xbe *.iso *.xiso *.bmp *.png *.wav *.sav, and *.pack, a
# texture pack made from game data); every author and
# committer address, which must be the noreply one. Exit 1 on any hit.
# Commits listed in scripts/audit-public-allowed-messages were public before
# a pattern that matches their message was added; only their messages are
# skipped.
#
# Private patterns (the bench host's name, the personal email, anything
# else that must not appear) are not in this repository, since naming them
# here would publish them: one extended regex per line, matched without
# case, in $XBR_PUBLIC_DIR/audit-private (the file every game's
# `public audit` reads too), else $XBR_AUDIT_PRIVATE, else
# <git dir>/info/audit-private.
# Without one the audit refuses to pass.
#
# The regexes below bracket a letter (/[U]sers/) so this file does not
# match itself, and spell word edges as character classes because BSD
# regcomp has no \b.
set -euo pipefail

NOREPLY="4037632+apexJCL@users.noreply.github.com"
# The tracker ids and the Deck's ssh alias are private infrastructure
# details, yet their patterns live here rather than in the private file:
# each is a generic word (blx, deck), so the regex reveals nothing.
# Home paths are read through not_homes(), which masks what is nobody's
# home (src/xboxrecomp_cli/public.py's BUILTIN exempts the same). MSYS's
# paths (a drive letter as the first folder) are /[U]sers/'s matches.
HOME_PATTERNS=(
    '/[U]sers/'
    '[d]rive_c[\\/]+[U]sers[\\/]'
    '/[h]ome/'
    '/var/[h]ome/'
    '[A-Za-z]:[\\/]+[U]sers[\\/]'
)
PUBLIC_PATTERNS=(
    '[c]laude\.ai'
    '(^|[^a-z0-9])[b]lx(-?[0-9]|-$)'
    '(^|[^a-z])[f]ables?([^a-z]|$)'
    'ssh( +-[a-z0-9]+( +[^- ][^ ]*)?)* +[d]eck([^a-z0-9_.-]|$)'
    '(^|[^a-z0-9_.-])[d]eck:[~/$.a-z0-9_{-]'
    '@[d]eck([^a-z0-9_.-]|$)'
)
DATA_EXT='\.(xbe|iso|xiso|bmp|png|wav|sav|pack)$'
# Files that may be binary or carry a data extension (extended regexes on
# the path): the wizard's window icon, rendered from the committed SVG and
# checked against it by TestIcongen. The tests build their fixtures in
# temp dirs.
ALLOW=('^wizard/internal/gamecfg/icon\.png$')

cd "$(git rev-parse --show-toplevel)"
ALLOW_MESSAGES=scripts/audit-public-allowed-messages
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

# The first file with a pattern wins; one with only comments and blanks
# counts as missing, as in `<game> public audit`.
has_patterns() { [ -f "$1" ] && grep -qvE '^[[:space:]]*(#|$)' "$1"; }
private=""
looked=""
for f in "${XBR_PUBLIC_DIR:+$XBR_PUBLIC_DIR/audit-private}" "${XBR_AUDIT_PRIVATE:-}" \
    "$(git rev-parse --git-path info/audit-private)"; do
    [ -n "$f" ] || continue
    looked="$looked $f"
    if has_patterns "$f"; then
        private=$f
        break
    fi
done
if [ -z "$private" ]; then
    echo "audit-public: no private patterns (looked in$looked), so it cannot check for them." >&2
    echo "Write one extended regex per line (the bench host's name, the personal email)," >&2
    echo "once per clone:" >&2
    echo "    printf '%s\n' 'hostname' 'name@example[.]com' > \"\$(git rev-parse --git-path info/audit-private)\"" >&2
    echo "or set XBR_PUBLIC_DIR to a directory with an audit-private file (or XBR_AUDIT_PRIVATE" >&2
    echo "to the file). See docs/maintainers.md, Before a public push." >&2
    exit 2
fi
patterns=("${PUBLIC_PATTERNS[@]}")
while IFS= read -r p || [ -n "$p" ]; do
    p="${p#"${p%%[![:space:]]*}"}"
    p="${p%"${p##*[![:space:]]}"}"
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

# Drops the messages of the commits in $ALLOW_MESSAGES from a log whose
# entries start with \x01 and the full sha.
skip_allowed() {
    local list=""
    # Space-separated: BSD awk rejects a newline in a -v value.
    [ -f "$ALLOW_MESSAGES" ] && list=$(grep -Eo '^[0-9a-f]{40}' "$ALLOW_MESSAGES" | tr '\n' ' ' || true)
    awk -v list="$list" '
        BEGIN { n = split(list, a, " "); for (i = 1; i <= n; i++) skip[a[i]] = 1 }
        /^\001/ { sub(/^\001/, ""); drop = (substr($0, 1, 40) in skip) }
        !drop'
}

# What is checked: the text (added lines, or the whole tree), the messages,
# the files added, the identities.
# Each check takes the regex, then a filter for the text it reads (cat, or
# not_homes for the home paths); every pattern matches without case.
if [ $all = 1 ]; then
    text() { git grep -I -n -E -i -e "$1" HEAD -- . || true; }
    messages=$(git log --format=$'\x01%H %B' HEAD | skip_allowed)
    files=$(git ls-files)
    idents=$(git log --format='%h %ae %ce' HEAD)
    empty=$(git hash-object -t tree /dev/null)
    binaries=$(git diff --numstat "$empty" HEAD | awk -F'\t' '$1 == "-" {print $3}')
else
    text() {
        git diff "$range" --unified=0 --no-color | grep -E '^\+' | grep -v '^+++ ' |
            grep -n -E -i -e "$1" || true
    }
    messages=$(git log --format=$'\x01%H %B' "$range" | skip_allowed)
    files=$(git diff --name-only --diff-filter=AMR "$range")
    idents=$(git log --format='%h %ae %ce' "$range")
    binaries=$(git diff --numstat --diff-filter=AMR "$range" | awk -F'\t' '$1 == "-" {print $3}')
fi

# What is nobody's home: a Wine prefix's own steamuser and public users
# (under drive_c/ or C:, either separator, only with the separator after
# the name) and GitHub's API path github.com/users/. BSD sed has no case
# flag, hence the classes.
not_homes() {
    local users='[Uu][Ss][Ee][Rr][Ss]'
    local own='([Ss][Tt][Ee][Aa][Mm][Uu][Ss][Ee][Rr]|[Pp][Uu][Bb][Ll][Ii][Cc])'
    sed -E \
        -e "s#[Dd][Rr][Ii][Vv][Ee]_[Cc][\\/]+$users[\\/]+$own[\\/]#drive_c-wine-#g" \
        -e "s#([A-Za-z]:)[\\/]+$users[\\/]+$own[\\/]#\\1-wine-#g" \
        -e "s#([Gg][Ii][Tt][Hh][Uu][Bb]\\.[Cc][Oo][Mm])/$users/#\\1-api-#g"
}
# The lines of stdin whose filtered form matches, printed as they are, so
# a hit shows the line as committed and not the mask.
matching() {
    local p=$1 filter=$2 line seen
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "$filter" != cat ]; then
            seen=$($filter <<<"$line")
            grep -q -E -i -e "$p" <<<"$seen" || continue
        fi
        printf '%s\n' "$line"
    done < <(grep -E -i -e "$p" || true)
}
check() {
    local p=$1 filter=$2 line
    while IFS= read -r line; do
        hit "text /$p/: $line"
    done < <(text "$p" | matching "$p" "$filter")
    while IFS= read -r line; do
        hit "message /$p/: $line"
    done < <(matching "$p" "$filter" <<<"$messages")
    while IFS= read -r line; do
        hit "path /$p/: $line"
    done < <(matching "$p" "$filter" <<<"$files")
}
for p in "${HOME_PATTERNS[@]}"; do
    check "$p" not_homes
done
for p in "${patterns[@]}"; do
    check "$p" cat
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
