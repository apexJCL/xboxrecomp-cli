#@ benchdb/host/clean_list.sh: what `benchdb plan` reads of a POSIX host's
#@ deletable roots, in one round trip over `ssh host bash -s`. The #@ lines
#@ this file starts with are this note and are not sent. After ROOTS (the
#@ root paths), SWEEP (1: list the root's children) and PATHS (explicit
#@ paths). Read-only. Tab-separated lines:
#@   H uid now                     the user and the host's clock
#@   T i resolved-root kb avail-kb a root that exists (G i: one that does not)
#@   E given resolved real type uid newest-mtime kb   per entry (type l|d|f)
#@   S resolved stamp run-info-sha256|- exit-code(1|0)   each bench-logs/<stamp>
#@   F resolved stamp relpath      each of its files outside frames/
#@   M given                       an entry that is gone
#@   B given                       a name with a tab or newline (refused)
# The resolved path is realpath(parent)/name: the one that is checked and
# deleted, never a link's target. Nothing here follows a link (-P), and
# -xdev keeps the walks out of mounts inside a tree.
set -u
printf 'H\t%s\t%s\n' "$(id -u)" "$(date +%s)"
for i in "${!ROOTS[@]}"; do
    if rr=$(realpath -- "${ROOTS[i]}" 2>/dev/null) && [ -d "$rr" ]; then
        printf 'T\t%s\t%s\t%s\n' "$i" "$rr" "$(df -Pk -- "$rr" | awk 'NR==2 {print $2 "\t" $4}')"
    else
        printf 'G\t%s\n' "$i"
    fi
done
entry() {
    local p=$1 parent leaf rp res ty uid mt kb real s sha ex f st
    case $p in *$'\t'* | *$'\n'*)
        printf 'B\t%s\n' "${p//[$'\t\n']/?}"
        return
        ;;
    esac
    parent=$(dirname -- "$p")
    leaf=$(basename -- "$p")
    if ! rp=$(realpath -- "$parent" 2>/dev/null); then
        printf 'M\t%s\n' "$p"
        return
    fi
    res="${rp%/}/$leaf"
    if [ -L "$res" ]; then
        ty=l
    elif [ -d "$res" ]; then
        ty=d
    elif [ -e "$res" ]; then
        ty=f
    else
        printf 'M\t%s\n' "$p"
        return
    fi
    uid=$(find -P "$res" -maxdepth 0 -printf '%U')
    mt=$(find -P "$res" -xdev -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
    kb=$(du -skP -- "$res" 2>/dev/null | cut -f1)
    real=-
    [ "$ty" = l ] || real=$(realpath -- "$res")
    printf 'E\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$p" "$res" "$real" "$ty" "$uid" "${mt%.*}" "${kb:-0}"
    [ "$ty" = d ] || return 0
    while IFS= read -r s; do
        st=$(basename -- "$s")
        sha=-
        [ -f "$s/run-info.txt" ] && sha=$(sha256sum <"$s/run-info.txt" | cut -c1-64)
        ex=0
        [ -f "$s/exit-code" ] && ex=1
        printf 'S\t%s\t%s\t%s\t%s\n' "$res" "$st" "$sha" "$ex"
        while IFS= read -r f; do
            printf 'F\t%s\t%s\t%s\n' "$res" "$st" "${f#./}"
        done < <(cd "$s" && find -P . -path ./frames -prune -o -type f -print)
    done < <(find -P "$res" -xdev -maxdepth 4 -type d -path '*/bench-logs/*' \
        -regextype posix-extended -regex '.*/bench-logs/[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?' \
        -print 2>/dev/null)
}
for p in "${PATHS[@]}"; do
    entry "$p"
done
for i in "${!ROOTS[@]}"; do
    [ "${SWEEP[i]}" = 1 ] && [ -d "${ROOTS[i]}" ] || continue
    for n in "${ROOTS[i]%/}"/* "${ROOTS[i]%/}"/.[!.]*; do
        [ -e "$n" ] || [ -L "$n" ] || continue
        entry "$n"
    done
done
exit 0
