/*
 * apply_plan: the part of apply.exe that decides, with no Windows calls,
 * so it is built and tested on any host (tests/test_windows_apply.py
 * builds it with the host's cc and checks it against a Python model).
 *
 * It reads the patch's patch.lst, a version's SHA256SUMS and
 * state\history.txt, and says what apply.exe does with them: the checks
 * before any write, the files of the new version, the game-file sync,
 * and which versions a prune removes. apply.c carries it out.
 */
#ifndef APPLY_PLAN_H
#define APPLY_PLAN_H

#include <stddef.h>

#define PLAN_SHA_CHARS 65

/* patch.lst kinds: changed, new, deleted, kept. */
struct plan_entry {
    char kind; /* 'C', 'N', 'D', 'K' */
    long long size;
    char sha[PLAN_SHA_CHARS];
    char *path; /* '/' separated, relative to the version or the root */
};

struct plan_patch {
    char *app, *version, *base, *base_sha, *new_sha;
    int stub_abi;
    struct plan_entry *e;
    size_t n;
};

/* What apply.exe does, in this order: every check comes before the first
 * write, so a mismatch changes nothing (D10). */
enum plan_act {
    ACT_CHECK_PROGRAM, /* a kept program file in versions\<base>\, by size then sha256 */
    ACT_CHECK_GAME,    /* a kept game file in the root game_files\ */
    ACT_LINK,          /* a kept program file: hard link from versions\<base>\ */
    ACT_COPY,          /* a changed or new program file: copied from the patch */
    ACT_GAME_WRITE,    /* a changed or new game file: .partial, flush, replace */
    ACT_GAME_DELETE,   /* a deleted game file */
};

struct plan_step {
    enum plan_act act;
    const struct plan_entry *e;
};

/* The game files sit at the root (game_files\), unversioned; everything
 * else is the version's. */
int plan_is_game(const char *path);

/* A path from a list is safe to join under a folder: relative, '/'
 * separated, no '.' or '..' part, no drive, no backslash or colon. */
int plan_path_ok(const char *path);

/* Parses patch.lst (the text is modified and must outlive the patch).
 * Returns 0, or -1 with *err (a static message) and *line set. */
int plan_parse_patch(char *text, struct plan_patch *p, const char **err, int *line);
void plan_free_patch(struct plan_patch *p);

/* The steps for p, in order, into a malloc'd array; returns the count or
 * -1 (out of memory). */
long plan_steps(const struct plan_patch *p, struct plan_step **out);

/* "<sha256>  <path>" lines (SHA256SUMS). Entries carry kind 0 and size -1. */
int plan_parse_sums(char *text, struct plan_entry **out, size_t *n, const char **err, int *line);

/* The patch's lists and the new SHA256SUMS describe the same version:
 * every changed, new or kept entry is in the sums with its sha256, and
 * every sums entry is one of them. Returns NULL, or the first path that
 * differs. */
const char *plan_cover(const struct plan_patch *p, const struct plan_entry *sums, size_t n);

/* state\history.txt: "<utc> <what> <old> -> <new>" lines. */
struct plan_hist {
    char *time, *what, *old, *new_;
};
size_t plan_parse_history(char *text, struct plan_hist **out);

/* The version current was switched to from, the rollback target; NULL
 * when there is none (as install_lib.previous on steamos). */
const char *plan_previous(const struct plan_hist *h, size_t n, const char *cur);

/* Orders names (installed version folders) oldest first: by the last
 * switch to each, then by name, which starts with the build date. */
void plan_order_versions(char **names, size_t n, const struct plan_hist *h, size_t nh);

/* Marks remove[i] = 1 for each of the ordered names a prune to `keep`
 * removes: never the newest `keep`, current or previous. */
void plan_prune(char **names, size_t n, int keep, const char *cur, const char *prev, int *remove);

/* A version folder name apply.exe may make or remove: a plain name, not
 * a .partial or .old work folder. */
int plan_version_ok(const char *v);

/* textures.pack in a config.toml, by a line scan (a [textures] header,
 * then pack = before the next header), as install_lib and apply.command
 * read it; not a TOML parser, so an unusual layout only loses the note
 * (D8). Copies the value without quotes into out (cap bytes, truncated)
 * and returns 1, or returns 0 when there is none. */
int plan_pack_key(const char *text, char *out, size_t cap);

/* The note after a patch for a player's config.toml (it wins over the
 * version's config.default.toml): HIDES, it sets another pack than the
 * defaults' one; STALE, the defaults set none and it names a pack that
 * is not "none". val gets its value. */
enum plan_note { NOTE_NONE, NOTE_HIDES, NOTE_STALE };
enum plan_note plan_override(const char *config, const char *defaults, char *val, size_t cap);

#endif
