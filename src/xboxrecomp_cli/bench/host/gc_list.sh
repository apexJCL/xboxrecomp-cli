#@ bench/host/gc_list.sh: a host script `<game> bench gc --host` ships over
#@ ssh. The #@ lines this file starts with are this note and are not sent.
#@ Runs as: remote (gc). After the prologue. Lists the host's store in one
#@ round trip, tab-separated: per run dir (a stamp name)
#@   R name kb run-info-sha256|- exit-code(1|0) frames-kb
#@   I name line      the run-info.txt lines gc reads (env, exe, built)
#@   F name relpath   each file outside frames/
#@ and N name kb for every other entry (never removed).
cd "$REMOTE_GAME"
[ -d bench-logs ] || exit 0
cd bench-logs
for n in * .[!.]*; do
    [ -e "$n" ] || [ -L "$n" ] || continue
    if [ -d "$n" ] && [ ! -L "$n" ] && [[ "$n" =~ ^[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?$ ]]; then
        kb=$(du -sk -- "$n" | cut -f1)
        fkb=0
        [ -d "$n/frames" ] && fkb=$(du -sk -- "$n/frames" | cut -f1)
        sha=-
        [ -f "$n/run-info.txt" ] && sha=$(sha256sum -- "$n/run-info.txt" | cut -d' ' -f1)
        ex=0
        [ -f "$n/exit-code" ] && ex=1
        printf 'R\t%s\t%s\t%s\t%s\t%s\n' "$n" "$kb" "$sha" "$ex" "$fkb"
        if [ -f "$n/run-info.txt" ]; then
            grep -E '^(env:|built:|[0-9a-f]{64}  )' -- "$n/run-info.txt" | sed "s/^/I\t$n\t/" || true
        fi
        (cd "$n" && find . -path ./frames -prune -o -type f -print) | sed "s|^\./||; s/^/F\t$n\t/"
    else
        printf 'N\t%s\t%s\n' "$n" "$(du -sk -- "$n" | cut -f1)"
    fi
done
