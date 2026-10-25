#@ benchdb/host/clean_apply.sh: the deletes `benchdb clean` makes on a POSIX
#@ host, over `ssh host bash -s`. The #@ lines this file starts with are
#@ this note and are not sent. After ROOTS (the plan's root paths), NEVER
#@ (paths no rule deletes, nor anything over them), NEVER_SELF (paths kept
#@ with all over them), BENCH (bench bases: only <game>/runs/<x> goes) and, per path, P (the path), R (its root's index
#@ in ROOTS), PAT (the root's pattern, an ERE), M (the planned newest mtime,
#@ whole seconds), AGE (the root's days, in seconds) and OWN (1: must be the user's).
#@ Prints one line per path, as given: deleted|skipped|failed <tab> path [<tab> why].
# Under the run lock, exclusively, as bench gc took it: no run writes a tree
# while it goes, and a run queued behind this lock re-checks its dir after.
set -u
exec 9>>~/.recomp-run.lock
if ! flock -x -n 9; then
    echo "benchdb: clean waiting for the run lock" >&2
    flock -x -w 3600 -E 75 9 || exit 75
fi
now=$(date +%s)
out() { printf '%s\t%s%s\n' "$1" "$2" "${3:+$'\t'$3}"; }
under() { # $1 is $2 or below it
    [ "$1" = "$2" ] || [ "${1#"${2%/}"/}" != "$1" ]
}
fails=0
for i in "${!P[@]}"; do
    p=${P[i]}
    case $p in */ | *'/../'* | *'/..' | '../'* | '..')
        out skipped "$p" "a trailing / or a .. in the path"
        continue
        ;;
    esac
    if ! root=$(realpath -- "${ROOTS[${R[i]}]}" 2>/dev/null); then
        out skipped "$p" "its root is gone"
        continue
    fi
    if ! rp=$(realpath -- "$(dirname -- "$p")" 2>/dev/null); then
        out skipped "$p" "gone"
        continue
    fi
    # Every check below and the delete itself use the resolved path, never
    # the text given: a link in the parent's text is seen for what it is.
    res="${rp%/}/$(basename -- "$p")"
    if [ "$res" = "$root" ] || ! under "$res" "$root"; then
        out skipped "$p" "$res is not below the root $root"
        continue
    fi
    rel=${res#"${root%/}"/}
    # The whole path below the root, as plan matched it: a pattern that
    # names only some children (a scratchpad, never its parent) holds.
    if [[ ! "$rel" =~ ^(${PAT[i]})$ ]]; then
        out skipped "$p" "$rel does not match the root's pattern"
        continue
    fi
    bad=
    for n in "${NEVER[@]}"; do
        nr=$(realpath -- "$n" 2>/dev/null) || nr=$n
        if under "$res" "$nr" || under "$nr" "$res"; then
            bad=$n
            break
        fi
    done
    # The built-in floor: a home, /tmp and the like only as themselves (what
    # is below them goes by the roots); in a bench base only <game>/runs/<x>.
    for n in "${NEVER_SELF[@]}"; do
        nr=$(realpath -- "$n" 2>/dev/null) || nr=$n
        if under "$nr" "$res" || under "$n" "$res"; then
            bad=$n
            break
        fi
    done
    for n in "${BENCH[@]}"; do
        [ -n "$bad" ] && break
        nr=$(realpath -- "$n" 2>/dev/null) || nr=$n
        if under "$nr" "$res"; then
            bad=$n
        elif under "$res" "$nr" && [[ ! "${res#"${nr%/}"/}" =~ ^[^/]+/runs/[^/]+ ]]; then
            bad=$n
        fi
    done
    if [ -n "$bad" ]; then
        out skipped "$p" "never-path $bad"
        continue
    fi
    if [ ! -e "$res" ] && [ ! -L "$res" ]; then
        out skipped "$p" "gone"
        continue
    fi
    # The leaf's own owner (stat does not follow a link; -O would).
    if [ "${OWN[i]}" = 1 ] && [ "$(stat -c %u -- "$res" 2>/dev/null)" != "$(id -u)" ]; then
        out skipped "$p" "not owned by $(id -un)"
        continue
    fi
    if [ -L "$res" ]; then
        # A link is only ever unlinked, wherever it points.
        if rm -f -- "$res"; then out deleted "$p"; else out failed "$p" "rm"; fails=$((fails + 1)); fi
        continue
    fi
    if [ "$(realpath -- "$res")" != "$res" ]; then
        out skipped "$p" "its real path is not $res"
        continue
    fi
    # Guard 5 again, under the lock: a dir that ship touched since the
    # plan, or that is now within the days, stays.
    mt=$(find -P "$res" -xdev -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
    mt=${mt%.*}
    if [ "$mt" != "${M[i]}" ]; then
        out skipped "$p" "changed since the plan (newest mtime $mt, planned ${M[i]})"
        continue
    fi
    if [ $((now - mt)) -lt "${AGE[i]}" ]; then
        out skipped "$p" "written within $((${AGE[i]} / 86400)) days"
        continue
    fi
    # Out of a ship's way before the delete: ship does not take the run
    # lock, so one could start on this dir mid-rm. The rename is atomic: a
    # ship after it makes a fresh dir, and one that touched the dir between
    # the check above and the rename shows in the re-read, and it goes back.
    gone="${rp%/}/.$(basename -- "$res").deleting.$$"
    if [ -e "$gone" ] || [ -L "$gone" ] || ! mv -- "$res" "$gone"; then
        out failed "$p" "rename to $gone"
        fails=$((fails + 1))
        continue
    fi
    mt=$(find -P "$gone" -xdev -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
    mt=${mt%.*}
    if [ "$mt" != "${M[i]}" ]; then
        if [ ! -e "$res" ] && [ ! -L "$res" ] && mv -- "$gone" "$res"; then
            out skipped "$p" "shipped to during the clean (newest mtime $mt)"
        else
            out failed "$p" "shipped to during the clean; left as $gone"
            fails=$((fails + 1))
        fi
        continue
    fi
    # Read-only trees (old game_files copies) are made writable without
    # following a link: never chmod -R, which follows one given as its
    # argument. --one-file-system keeps rm out of a bind mount inside.
    find -P "$gone" -xdev ! -type l -exec chmod u+w {} + 2>/dev/null
    if rm -rf --one-file-system -- "$gone" && [ ! -e "$gone" ] && [ ! -L "$gone" ]; then
        out deleted "$p"
    else
        out failed "$p" "rm of $gone"
        fails=$((fails + 1))
    fi
done
[ "$fails" = 0 ]
