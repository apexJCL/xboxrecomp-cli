/* A host driver over apply_plan.c for tests/test_windows_apply.py: each
 * command prints what apply.exe would decide, one item per line.
 *
 *   steps PATCH.LST            "<act> <path>" per step, or "error <line> <msg>"
 *   cover PATCH.LST SUMS       "ok", or the first path that differs
 *   prune HISTORY KEEP CUR NAME...
 *                              "previous <v>|-", "order <names>", then
 *                              "remove <name>" per pruned version
 *   path PATH...               "<0|1> <path>" (plan_path_ok)
 *   version NAME...            "<0|1> <name>" (plan_version_ok)
 *   override CONFIG DEFAULTS   "<none|hides|stale> <value>" (plan_override;
 *                              "-" for a missing file)
 */
#include "apply_plan.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *ACTS[] = {"check-program", "check-game", "link", "copy", "game-write",
                             "game-delete"};

static char *slurp(const char *p)
{
    FILE *f = fopen(p, "rb");
    long n;
    char *b;
    if (!f)
        return NULL;
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    b = malloc((size_t)n + 1);
    if (fread(b, 1, (size_t)n, f) != (size_t)n)
        n = 0;
    b[n] = 0;
    fclose(f);
    return b;
}

int main(int argc, char **argv)
{
    if (argc >= 3 && !strcmp(argv[1], "steps")) {
        char *t = slurp(argv[2]);
        struct plan_patch p;
        struct plan_step *s;
        const char *err;
        int line;
        long n;
        if (!t || plan_parse_patch(t, &p, &err, &line)) {
            printf("error %d %s\n", t ? line : 0, t ? err : "unreadable");
            return 0;
        }
        printf("app %s\nversion %s\nbase %s\nstub_abi %d\n", p.app, p.version, p.base, p.stub_abi);
        n = plan_steps(&p, &s);
        for (long i = 0; i < n; i++)
            printf("%s %s\n", ACTS[s[i].act], s[i].e->path);
        free(s);
        plan_free_patch(&p);
        free(t);
        return 0;
    }
    if (argc >= 4 && !strcmp(argv[1], "cover")) {
        char *t = slurp(argv[2]), *u = slurp(argv[3]);
        struct plan_patch p;
        struct plan_entry *e;
        size_t n;
        const char *err, *bad;
        int line;
        if (plan_parse_patch(t, &p, &err, &line) || plan_parse_sums(u, &e, &n, &err, &line)) {
            printf("error %d %s\n", line, err);
            return 0;
        }
        bad = plan_cover(&p, e, n);
        printf("%s\n", bad ? bad : "ok");
        return 0;
    }
    if (argc >= 5 && !strcmp(argv[1], "prune")) {
        char *t = slurp(argv[2]);
        struct plan_hist *h = NULL;
        size_t nh = t ? plan_parse_history(t, &h) : 0, n = (size_t)(argc - 5);
        const char *cur = strcmp(argv[4], "-") ? argv[4] : NULL, *prev;
        char **names = calloc(n + 1, sizeof(char *));
        int *rm = calloc(n + 1, sizeof(int));
        for (size_t i = 0; i < n; i++)
            names[i] = argv[5 + i];
        prev = plan_previous(h, nh, cur);
        printf("previous %s\n", prev ? prev : "-");
        plan_order_versions(names, n, h, nh);
        printf("order");
        for (size_t i = 0; i < n; i++)
            printf(" %s", names[i]);
        printf("\n");
        plan_prune(names, n, atoi(argv[3]), cur, prev, rm);
        for (size_t i = 0; i < n; i++)
            if (rm[i])
                printf("remove %s\n", names[i]);
        return 0;
    }
    if (argc >= 2 && (!strcmp(argv[1], "path") || !strcmp(argv[1], "version"))) {
        int path = !strcmp(argv[1], "path");
        for (int i = 2; i < argc; i++)
            printf("%d %s\n", path ? plan_path_ok(argv[i]) : plan_version_ok(argv[i]), argv[i]);
        return 0;
    }
    if (argc >= 4 && !strcmp(argv[1], "override")) {
        static const char *KINDS[] = {"none", "hides", "stale"};
        char *c = strcmp(argv[2], "-") ? slurp(argv[2]) : NULL;
        char *d = strcmp(argv[3], "-") ? slurp(argv[3]) : NULL, val[1024] = "";
        enum plan_note k = plan_override(c, d, val, sizeof(val));
        printf("%s %s\n", KINDS[k], k == NOTE_NONE ? "" : val);
        free(c);
        free(d);
        return 0;
    }
    fprintf(stderr, "usage: see the comment\n");
    return 2;
}
