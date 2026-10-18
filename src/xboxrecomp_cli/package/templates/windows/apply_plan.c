/* apply_plan: see apply_plan.h. Standard C only. */
#include "apply_plan.h"

#include <stdlib.h>
#include <string.h>

#define GAME_PREFIX "game_files/"

int plan_is_game(const char *path)
{
    return strncmp(path, GAME_PREFIX, sizeof(GAME_PREFIX) - 1) == 0;
}

int plan_path_ok(const char *path)
{
    const char *s = path;
    if (!*path || *path == '/')
        return 0;
    for (const char *c = path; *c; c++) {
        if (*c == '\\' || *c == ':' || (unsigned char)*c < 0x20)
            return 0;
    }
    /* Each part: not empty, not "." or "..", no trailing '.' or ' '. */
    for (;;) {
        const char *end = strchr(s, '/');
        size_t len = end ? (size_t)(end - s) : strlen(s);
        if (len == 0 || (len == 1 && s[0] == '.') || (len == 2 && s[0] == '.' && s[1] == '.'))
            return 0;
        /* Windows drops a trailing '.' or ' ': "a." would be "a". */
        if (s[len - 1] == '.' || s[len - 1] == ' ')
            return 0;
        if (!end)
            return 1;
        s = end + 1;
    }
}

int plan_version_ok(const char *v)
{
    size_t n = strlen(v);
    if (!n || strchr(v, '/') || strchr(v, '\\') || strchr(v, ':') || v[0] == '.')
        return 0;
    if (n > 8 && strcmp(v + n - 8, ".partial") == 0)
        return 0;
    if (n > 4 && strcmp(v + n - 4, ".old") == 0)
        return 0;
    return 1;
}

static int is_hex64(const char *s)
{
    for (int i = 0; i < 64; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return 0;
    }
    return s[64] == 0;
}

/* The next line of text, cut at '\n' (a '\r' before it goes too); NULL at
 * the end. */
static char *next_line(char **cur)
{
    char *s = *cur, *nl;
    size_t len;
    if (!s || !*s)
        return NULL;
    nl = strchr(s, '\n');
    if (nl) {
        *nl = 0;
        *cur = nl + 1;
    } else {
        *cur = s + strlen(s);
    }
    len = strlen(s);
    if (len && s[len - 1] == '\r')
        s[len - 1] = 0;
    return s;
}

/* Cuts the first space-separated word off *s. */
static char *word(char **s)
{
    char *w = *s, *sp;
    if (!w)
        return NULL;
    sp = strchr(w, ' ');
    if (sp) {
        *sp = 0;
        *s = sp + 1;
    } else {
        *s = NULL;
    }
    return w;
}

static int push(struct plan_entry **e, size_t *n, size_t *cap, struct plan_entry v)
{
    if (*n == *cap) {
        size_t c = *cap ? *cap * 2 : 256;
        struct plan_entry *ne = realloc(*e, c * sizeof(**e));
        if (!ne)
            return -1;
        *e = ne;
        *cap = c;
    }
    (*e)[(*n)++] = v;
    return 0;
}

#define FAIL(msg)                                                                                  \
    do {                                                                                           \
        *err = (msg);                                                                              \
        *line = ln;                                                                                \
        plan_free_patch(p);                                                                        \
        return -1;                                                                                 \
    } while (0)

int plan_parse_patch(char *text, struct plan_patch *p, const char **err, int *line)
{
    char *cur = text, *l;
    size_t cap = 0;
    int ln = 0, header = 0;
    memset(p, 0, sizeof(*p));
    p->stub_abi = -1;
    while ((l = next_line(&cur)) != NULL) {
        char *rest = l, *k;
        ln++;
        if (!*l || *l == '#')
            continue;
        k = word(&rest);
        if (!header) {
            /* The first line names the format; a later one is refused,
             * not misread. */
            if (strcmp(k, "xbr-patch") != 0 || !rest || strcmp(rest, "1") != 0)
                FAIL("not a patch list this applier reads (xbr-patch 1)");
            header = 1;
            continue;
        }
        if (strlen(k) == 1 && strchr("CNDK", k[0])) {
            struct plan_entry e;
            char *size = word(&rest), *sha = word(&rest), *end;
            if (!size || !sha || !rest)
                FAIL("an entry needs a size, a sha256 and a path");
            e.kind = k[0];
            /* Digits only: strtoll alone would take "+1" or " 1". */
            if (!*size || strspn(size, "0123456789") != strlen(size) || strlen(size) > 18)
                FAIL("a bad size");
            e.size = strtoll(size, &end, 10);
            if (strlen(sha) != 64 || !is_hex64(sha))
                FAIL("a bad sha256");
            memcpy(e.sha, sha, PLAN_SHA_CHARS);
            e.path = rest;
            if (!plan_path_ok(e.path))
                FAIL("an unsafe path");
            if (push(&p->e, &p->n, &cap, e))
                FAIL("out of memory");
            continue;
        }
        if (!rest || !*rest)
            FAIL("a key without a value");
        if (strcmp(k, "app") == 0)
            p->app = rest;
        else if (strcmp(k, "version") == 0)
            p->version = rest;
        else if (strcmp(k, "base") == 0)
            p->base = rest;
        else if (strcmp(k, "base_sha") == 0)
            p->base_sha = rest;
        else if (strcmp(k, "new_sha") == 0)
            p->new_sha = rest;
        else if (strcmp(k, "stub_abi") == 0)
            p->stub_abi = atoi(rest);
        /* Other keys are a later CLI's; this applier ignores them. */
    }
    ln = 0;
    if (!p->app || !p->version || !p->base || !p->base_sha || !p->new_sha || p->stub_abi < 0)
        FAIL("the list lacks app, version, base, base_sha, new_sha or stub_abi");
    if (!plan_version_ok(p->version) || !plan_version_ok(p->base))
        FAIL("a bad version name");
    if (strlen(p->base_sha) != 64 || !is_hex64(p->base_sha) || strlen(p->new_sha) != 64 ||
        !is_hex64(p->new_sha))
        FAIL("a bad manifest sha256");
    return 0;
}

void plan_free_patch(struct plan_patch *p)
{
    free(p->e);
    p->e = NULL;
    p->n = 0;
}

long plan_steps(const struct plan_patch *p, struct plan_step **out)
{
    /* At most two steps per entry (a kept program file is checked, then
     * linked). */
    struct plan_step *s = malloc((p->n * 2 + 1) * sizeof(*s));
    long n = 0;
    if (!s)
        return -1;
    for (size_t i = 0; i < p->n; i++) {
        if (p->e[i].kind == 'K')
            s[n++] = (struct plan_step){
                plan_is_game(p->e[i].path) ? ACT_CHECK_GAME : ACT_CHECK_PROGRAM, &p->e[i]};
    }
    for (size_t i = 0; i < p->n; i++) {
        const struct plan_entry *e = &p->e[i];
        if (plan_is_game(e->path))
            continue;
        if (e->kind == 'K')
            s[n++] = (struct plan_step){ACT_LINK, e};
        else if (e->kind == 'C' || e->kind == 'N')
            s[n++] = (struct plan_step){ACT_COPY, e};
        /* A deleted program file is simply not in the new version. */
    }
    for (size_t i = 0; i < p->n; i++) {
        const struct plan_entry *e = &p->e[i];
        if (plan_is_game(e->path) && (e->kind == 'C' || e->kind == 'N'))
            s[n++] = (struct plan_step){ACT_GAME_WRITE, e};
    }
    for (size_t i = 0; i < p->n; i++) {
        const struct plan_entry *e = &p->e[i];
        if (plan_is_game(e->path) && e->kind == 'D')
            s[n++] = (struct plan_step){ACT_GAME_DELETE, e};
    }
    *out = s;
    return n;
}

#undef FAIL
#define FAIL(msg)                                                                                  \
    do {                                                                                           \
        *err = (msg);                                                                              \
        *line = ln;                                                                                \
        free(e);                                                                                   \
        return -1;                                                                                 \
    } while (0)

int plan_parse_sums(char *text, struct plan_entry **out, size_t *n, const char **err, int *line)
{
    char *cur = text, *l;
    struct plan_entry *e = NULL;
    size_t cap = 0, cnt = 0;
    int ln = 0;
    while ((l = next_line(&cur)) != NULL) {
        struct plan_entry v;
        ln++;
        if (!*l)
            continue;
        if (strlen(l) < 67 || l[64] != ' ' || l[65] != ' ')
            FAIL("not a SHA256SUMS line");
        l[64] = 0;
        if (!is_hex64(l))
            FAIL("a bad sha256");
        v.kind = 0;
        v.size = -1;
        memcpy(v.sha, l, PLAN_SHA_CHARS);
        v.path = l + 66;
        if (!plan_path_ok(v.path))
            FAIL("an unsafe path");
        if (push(&e, &cnt, &cap, v))
            FAIL("out of memory");
    }
    *out = e;
    *n = cnt;
    return 0;
}

const char *plan_cover(const struct plan_patch *p, const struct plan_entry *sums, size_t n)
{
    /* Quadratic, but on a few thousand entries once per patch. */
    size_t listed = 0;
    for (size_t i = 0; i < p->n; i++) {
        const struct plan_entry *e = &p->e[i];
        size_t j;
        if (e->kind == 'D')
            continue;
        listed++;
        for (j = 0; j < n; j++) {
            if (strcmp(sums[j].path, e->path) == 0)
                break;
        }
        if (j == n || strcmp(sums[j].sha, e->sha) != 0)
            return e->path;
    }
    if (listed != n) {
        for (size_t j = 0; j < n; j++) {
            size_t i;
            for (i = 0; i < p->n; i++) {
                if (p->e[i].kind != 'D' && strcmp(p->e[i].path, sums[j].path) == 0)
                    break;
            }
            if (i == p->n)
                return sums[j].path;
        }
    }
    return NULL;
}

size_t plan_parse_history(char *text, struct plan_hist **out)
{
    char *cur = text, *l;
    struct plan_hist *h = NULL;
    size_t n = 0, cap = 0;
    while ((l = next_line(&cur)) != NULL) {
        char *rest = l, *w[5];
        int k;
        for (k = 0; k < 5; k++) {
            while (rest && *rest == ' ')
                rest++;
            w[k] = rest && *rest ? word(&rest) : NULL;
            if (!w[k])
                break;
        }
        while (rest && *rest == ' ')
            rest++;
        /* Exactly five words, the fourth "->", as on steamos. */
        if (k < 5 || (rest && *rest) || strcmp(w[3], "->") != 0)
            continue;
        if (n == cap) {
            size_t c = cap ? cap * 2 : 32;
            struct plan_hist *nh = realloc(h, c * sizeof(*h));
            if (!nh)
                break;
            h = nh;
            cap = c;
        }
        h[n++] = (struct plan_hist){w[0], w[1], w[2], w[4]};
    }
    *out = h;
    return n;
}

const char *plan_previous(const struct plan_hist *h, size_t n, const char *cur)
{
    if (!cur)
        return NULL;
    for (size_t i = n; i-- > 0;) {
        if (strcmp(h[i].new_, cur) == 0 && strcmp(h[i].old, "-") != 0 &&
            strcmp(h[i].old, cur) != 0)
            return h[i].old;
    }
    return NULL;
}

static long last_switch(const char *v, const struct plan_hist *h, size_t nh)
{
    for (size_t i = nh; i-- > 0;) {
        if (strcmp(h[i].new_, v) == 0)
            return (long)i;
    }
    return -1;
}

void plan_order_versions(char **names, size_t n, const struct plan_hist *h, size_t nh)
{
    /* Insertion sort: a handful of versions. */
    for (size_t i = 1; i < n; i++) {
        char *v = names[i];
        long kv = last_switch(v, h, nh);
        size_t j = i;
        while (j > 0) {
            long kj = last_switch(names[j - 1], h, nh);
            if (kj < kv || (kj == kv && strcmp(names[j - 1], v) <= 0))
                break;
            names[j] = names[j - 1];
            j--;
        }
        names[j] = v;
    }
}

void plan_prune(char **names, size_t n, int keep, const char *cur, const char *prev, int *remove)
{
    for (size_t i = 0; i < n; i++) {
        int newest = keep > 0 && i + (size_t)keep >= n;
        int protect = newest || (cur && strcmp(names[i], cur) == 0) ||
                      (prev && strcmp(names[i], prev) == 0);
        remove[i] = !protect;
    }
}

/* One line of text at *cur, trimmed of blanks and a trailing CR, into
 * buf (truncated); advances *cur past it. Returns 0 at the end. */
static int scan_line(const char **cur, char *buf, size_t cap)
{
    const char *s = *cur, *e;
    size_t n;
    if (!*s)
        return 0;
    e = strchr(s, '\n');
    *cur = e ? e + 1 : s + strlen(s);
    if (!e)
        e = s + strlen(s);
    while (s < e && (*s == ' ' || *s == '\t'))
        s++;
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r'))
        e--;
    n = (size_t)(e - s) < cap - 1 ? (size_t)(e - s) : cap - 1;
    memcpy(buf, s, n);
    buf[n] = 0;
    return 1;
}

static int is_textures_header(const char *line)
{
    char h[16];
    size_t n = 0;
    for (const char *c = line; *c && *c != '#'; c++) {
        if (*c == ' ' || *c == '\t')
            continue;
        if (n + 1 >= sizeof(h))
            return 0;
        h[n++] = *c;
    }
    h[n] = 0;
    return strcmp(h, "[textures]") == 0;
}

static void copy_value(const char *s, size_t len, char *out, size_t cap)
{
    while (len > 0 && (s[len - 1] == ' ' || s[len - 1] == '\t'))
        len--;
    if (len > cap - 1)
        len = cap - 1;
    memcpy(out, s, len);
    out[len] = 0;
}

int plan_pack_key(const char *text, char *out, size_t cap)
{
    char line[1024];
    int inside = 0;
    while (scan_line(&text, line, sizeof(line))) {
        char *eq, *v, *k;
        if (line[0] == '[') {
            inside = is_textures_header(line);
            continue;
        }
        if (!inside || !(eq = strchr(line, '=')))
            continue;
        k = line;
        while (eq > k && (eq[-1] == ' ' || eq[-1] == '\t'))
            eq--;
        if ((size_t)(eq - k) != 4 || strncmp(k, "pack", 4) != 0)
            continue;
        v = strchr(line, '=') + 1;
        while (*v == ' ' || *v == '\t')
            v++;
        if (*v == '"' || *v == '\'') {
            char *end = strchr(v + 1, *v);
            copy_value(v + 1, end ? (size_t)(end - v - 1) : strlen(v + 1), out, cap);
        } else {
            char *hash = strchr(v, '#');
            copy_value(v, hash ? (size_t)(hash - v) : strlen(v), out, cap);
        }
        return 1;
    }
    return 0;
}

enum plan_note plan_override(const char *config, const char *defaults, char *val, size_t cap)
{
    char ours[1024];
    int have_ours;
    if (!config || !plan_pack_key(config, val, cap))
        return NOTE_NONE;
    have_ours = defaults && plan_pack_key(defaults, ours, sizeof(ours)) && ours[0];
    if (have_ours)
        return strcmp(val, ours) ? NOTE_HIDES : NOTE_NONE;
    return strcmp(val, "none") ? NOTE_STALE : NOTE_NONE;
}
