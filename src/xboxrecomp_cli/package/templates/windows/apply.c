/*
 * apply.exe: installs, switches, patches and rolls back the versioned
 * Windows layout (D8):
 *
 *   <root>\current.txt          the version folder name
 *   <root>\versions\<v>\        program files, the launcher, manifest.json, SHA256SUMS
 *   <root>\game_files\          unversioned
 *   <root>\@APP@.exe            the stub (stub.c): runs versions\<current>\@APP@.exe
 *   <root>\apply.exe            this program
 *   <root>\state\history.txt    "<utc> <what> <old> -> <new>", one line per switch
 *
 *   apply.exe [patch] [--root DIR] [--keep N] [--yes]
 *       from a patch folder (patch.lst, files\): the patch's version
 *       becomes current. Kept program files are hard-linked from the
 *       current version, the patch's files copied and flushed, the game
 *       files synced, then current.txt replaced in one move.
 *   apply.exe rollback [VERSION] [--root DIR]
 *   apply.exe switch VERSION [--root DIR] [--keep N]
 *       the installer's last step: versions\VERSION.partial (what NSIS
 *       extracted) becomes versions\VERSION and current.
 *   apply.exe migrate [--root DIR]
 *       the installer's first step over an in-place install (no
 *       current.txt): its program files move into versions\<old>.
 *
 * The root is --root, else the uninstall key's InstallLocation, else this
 * program's own folder when it holds current.txt. A power loss at any
 * point leaves a version that runs: every file is flushed before the
 * current.txt move that makes it current. The game holds the mutex
 * Local\@APP@-running (the launcher takes it); nothing here runs while it
 * exists. Saves and settings (the data folder) are never touched.
 *
 * The decisions (what to check, link, copy, sync and prune) are in
 * apply_plan.c, which builds and is tested on any host.
 *
 * Built by `<game> package windows` with llvm-mingw:
 *   x86_64-w64-mingw32-clang -municode -O2 -Wall -Wl,--no-insert-timestamp
 *     apply.c apply_plan.c -o apply.exe -lbcrypt -ladvapi32
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <winioctl.h>
#include <stdarg.h>
#include <stddef.h>
#include <wchar.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "apply_plan.h"

#define APP L"@APP@"
#define STUB_ABI 1
#define PATH_CHARS 4096
#define UNINST_KEY L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" APP
#define GAME_DATA_WARNING L"this update also changes game data; rolling back will not undo that"

static wchar_t g_root[PATH_CHARS];

static void out_w(HANDLE h, const wchar_t *s);

/* Double-clicked from Explorer, the console is this program's alone and
 * closes with it: wait for Enter so the player can read the result. */
static __attribute__((noreturn)) void finish(int code)
{
    DWORD procs[2];
    if (GetConsoleProcessList(procs, 2) == 1) {
        char b[8];
        DWORD n;
        out_w(GetStdHandle(STD_OUTPUT_HANDLE), L"Press Enter to close.\n");
        ReadFile(GetStdHandle(STD_INPUT_HANDLE), b, sizeof(b), &n, NULL);
    }
    ExitProcess((UINT)code);
}

/* ---- output ---------------------------------------------------------- */

static void out_w(HANDLE h, const wchar_t *s)
{
    DWORD mode, n;
    if (GetConsoleMode(h, &mode)) {
        WriteConsoleW(h, s, (DWORD)wcslen(s), &n, NULL);
    } else {
        /* A pipe (the wizard, the installer's log): UTF-8. */
        int len = WideCharToMultiByte(CP_UTF8, 0, s, -1, NULL, 0, NULL, NULL);
        char *b = malloc(len > 0 ? (size_t)len : 1);
        if (b && len > 0) {
            WideCharToMultiByte(CP_UTF8, 0, s, -1, b, len, NULL, NULL);
            WriteFile(h, b, (DWORD)(len - 1), &n, NULL);
        }
        free(b);
    }
}

static void say(const wchar_t *fmt, ...)
{
    wchar_t buf[PATH_CHARS * 2];
    va_list ap;
    va_start(ap, fmt);
    vswprintf(buf, sizeof(buf) / sizeof(buf[0]) - 1, fmt, ap);
    va_end(ap);
    wcscat(buf, L"\n");
    out_w(GetStdHandle(STD_OUTPUT_HANDLE), buf);
}

static __attribute__((noreturn)) void die(const wchar_t *fmt, ...)
{
    wchar_t buf[PATH_CHARS * 2];
    va_list ap;
    va_start(ap, fmt);
    wcscpy(buf, L"apply: ");
    vswprintf(buf + 7, sizeof(buf) / sizeof(buf[0]) - 9, fmt, ap);
    va_end(ap);
    wcscat(buf, L"\n");
    out_w(GetStdHandle(STD_ERROR_HANDLE), buf);
    finish(1);
}

static const wchar_t *errtext(DWORD e)
{
    static wchar_t buf[512];
    if (!FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL, e, 0,
                        buf, 512, NULL))
        swprintf(buf, 512, L"error %lu", e);
    for (size_t n = wcslen(buf); n && (buf[n - 1] == L'\n' || buf[n - 1] == L'\r'); n--)
        buf[n - 1] = 0;
    return buf;
}

/* ---- strings and paths ----------------------------------------------- */

static wchar_t *wide(const char *s)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    wchar_t *w = malloc((size_t)(n > 0 ? n : 1) * sizeof(wchar_t));
    if (!w || n <= 0 || !MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n))
        die(L"a name that is not UTF-8");
    return w;
}

/* dir\rel, with rel's '/' as '\'. */
static void join(wchar_t *out, const wchar_t *dir, const char *rel)
{
    wchar_t *w = wide(rel);
    for (wchar_t *c = w; *c; c++)
        if (*c == L'/')
            *c = L'\\';
    if (swprintf(out, PATH_CHARS, L"%ls\\%ls", dir, w) < 0)
        die(L"a path too long under %ls", dir);
    free(w);
}

static int exists(const wchar_t *p)
{
    return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES;
}

static int is_dir(const wchar_t *p)
{
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

/* Every folder above p. */
static void make_parents(const wchar_t *p)
{
    wchar_t d[PATH_CHARS];
    wcscpy(d, p);
    for (wchar_t *c = d + 3; *c; c++) {
        if (*c == L'\\') {
            *c = 0;
            CreateDirectoryW(d, NULL);
            *c = L'\\';
        }
    }
}

static char *read_all(const wchar_t *p, size_t *len)
{
    HANDLE h = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    LARGE_INTEGER sz;
    char *b;
    DWORD n;
    if (h == INVALID_HANDLE_VALUE)
        return NULL;
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart > (1 << 28)) {
        CloseHandle(h);
        return NULL;
    }
    b = malloc((size_t)sz.QuadPart + 1);
    if (!b || !ReadFile(h, b, (DWORD)sz.QuadPart, &n, NULL) || n != (DWORD)sz.QuadPart) {
        CloseHandle(h);
        free(b);
        return NULL;
    }
    CloseHandle(h);
    b[n] = 0;
    if (len)
        *len = n;
    return b;
}

/* ---- sha256 (BCrypt: NSIS has none) ---------------------------------- */

struct sha {
    BCRYPT_ALG_HANDLE alg;
    BCRYPT_HASH_HANDLE h;
};

static void sha_begin(struct sha *s)
{
    if (BCryptOpenAlgorithmProvider(&s->alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) != 0 ||
        BCryptCreateHash(s->alg, &s->h, NULL, 0, NULL, 0, 0) != 0)
        die(L"no SHA-256 provider");
}

static void sha_add(struct sha *s, const void *b, DWORD n)
{
    BCryptHashData(s->h, (PUCHAR)b, n, 0);
}

static void sha_end(struct sha *s, char hex[PLAN_SHA_CHARS])
{
    unsigned char d[32];
    BCryptFinishHash(s->h, d, 32, 0);
    BCryptDestroyHash(s->h);
    BCryptCloseAlgorithmProvider(s->alg, 0);
    for (int i = 0; i < 32; i++)
        sprintf(hex + 2 * i, "%02x", d[i]);
}

/* The sha256 of a file and its size; 0, or -1 when it cannot be read. */
static int sha_file(const wchar_t *p, char hex[PLAN_SHA_CHARS], long long *size)
{
    static unsigned char buf[1 << 20];
    HANDLE h = CreateFileW(p, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                           FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    struct sha s;
    long long total = 0;
    DWORD n;
    if (h == INVALID_HANDLE_VALUE)
        return -1;
    sha_begin(&s);
    while (ReadFile(h, buf, sizeof(buf), &n, NULL) && n) {
        sha_add(&s, buf, n);
        total += n;
    }
    CloseHandle(h);
    sha_end(&s, hex);
    if (size)
        *size = total;
    return 0;
}

static long long file_size(const wchar_t *p)
{
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExW(p, GetFileExInfoStandard, &d) ||
        (d.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
        return -1;
    return ((long long)d.nFileSizeHigh << 32) | d.nFileSizeLow;
}

/* By size first (cheap), then sha256. */
static int matches(const wchar_t *p, const struct plan_entry *e)
{
    char hex[PLAN_SHA_CHARS];
    if (e->size >= 0 && file_size(p) != e->size)
        return 0;
    return sha_file(p, hex, NULL) == 0 && strcmp(hex, e->sha) == 0;
}

/* ---- writes that survive a power loss -------------------------------- */

static int flush_file(const wchar_t *p)
{
    HANDLE h = CreateFileW(p, GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    int ok;
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    ok = FlushFileBuffers(h);
    CloseHandle(h);
    return ok;
}

/* src to dst (made afresh), flushed; its sha256 must be want's. */
static void copy_checked(const wchar_t *src, const wchar_t *dst, const char *want)
{
    static unsigned char buf[1 << 20];
    HANDLE in, o;
    struct sha s;
    char hex[PLAN_SHA_CHARS];
    DWORD n, w;
    make_parents(dst);
    in = CreateFileW(src, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                     FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (in == INVALID_HANDLE_VALUE)
        die(L"cannot read %ls: %ls", src, errtext(GetLastError()));
    o = CreateFileW(dst, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (o == INVALID_HANDLE_VALUE)
        die(L"cannot write %ls: %ls", dst, errtext(GetLastError()));
    sha_begin(&s);
    while (ReadFile(in, buf, sizeof(buf), &n, NULL) && n) {
        sha_add(&s, buf, n);
        if (!WriteFile(o, buf, n, &w, NULL) || w != n)
            die(L"cannot write %ls: %ls", dst, errtext(GetLastError()));
    }
    if (!FlushFileBuffers(o))
        die(L"cannot flush %ls: %ls", dst, errtext(GetLastError()));
    CloseHandle(o);
    CloseHandle(in);
    sha_end(&s, hex);
    if (want && strcmp(hex, want) != 0)
        die(L"%ls does not match its sha256 (a damaged patch?)", src);
}

/* A hard link to a kept file (no data copied); a checked copy where the
 * volume has no links. */
static void link_or_copy(const wchar_t *src, const wchar_t *dst, const char *want)
{
    make_parents(dst);
    DeleteFileW(dst);
    if (CreateHardLinkW(dst, src, NULL))
        return;
    copy_checked(src, dst, want);
}

/* tmp then one write-through move over p: atomic on NTFS. */
static void write_atomic(const wchar_t *p, const char *text)
{
    wchar_t tmp[PATH_CHARS];
    HANDLE h;
    DWORD n;
    swprintf(tmp, PATH_CHARS, L"%ls.tmp", p);
    make_parents(p);
    h = CreateFileW(tmp, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE || !WriteFile(h, text, (DWORD)strlen(text), &n, NULL) ||
        !FlushFileBuffers(h))
        die(L"cannot write %ls: %ls", tmp, errtext(GetLastError()));
    CloseHandle(h);
    if (!MoveFileExW(tmp, p, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        die(L"cannot replace %ls: %ls", p, errtext(GetLastError()));
}

static void replace_file(const wchar_t *src, const wchar_t *dst)
{
    flush_file(src);
    if (!MoveFileExW(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        die(L"cannot replace %ls: %ls", dst, errtext(GetLastError()));
}

/* Removes p and what is under it. A junction (the game_files link in a
 * migrated version) or any other reparse point is removed itself, never
 * followed: following it would delete the root game_files. */
static void rm_tree(const wchar_t *p)
{
    DWORD a = GetFileAttributesW(p);
    if (a == INVALID_FILE_ATTRIBUTES)
        return;
    if (a & FILE_ATTRIBUTE_REPARSE_POINT) {
        if (a & FILE_ATTRIBUTE_DIRECTORY)
            RemoveDirectoryW(p);
        else
            DeleteFileW(p);
        return;
    }
    if (a & FILE_ATTRIBUTE_DIRECTORY) {
        wchar_t pat[PATH_CHARS], c[PATH_CHARS];
        WIN32_FIND_DATAW fd;
        HANDLE h;
        swprintf(pat, PATH_CHARS, L"%ls\\*", p);
        h = FindFirstFileW(pat, &fd);
        if (h != INVALID_HANDLE_VALUE) {
            do {
                if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L".."))
                    continue;
                swprintf(c, PATH_CHARS, L"%ls\\%ls", p, fd.cFileName);
                rm_tree(c);
            } while (FindNextFileW(h, &fd));
            FindClose(h);
        }
        RemoveDirectoryW(p);
        return;
    }
    if (a & FILE_ATTRIBUTE_READONLY)
        SetFileAttributesW(p, a & ~FILE_ATTRIBUTE_READONLY);
    DeleteFileW(p);
}

/* Flushes every file under p (the installer's extracted version). */
static void flush_tree(const wchar_t *p)
{
    wchar_t pat[PATH_CHARS], c[PATH_CHARS];
    WIN32_FIND_DATAW fd;
    HANDLE h;
    swprintf(pat, PATH_CHARS, L"%ls\\*", p);
    h = FindFirstFileW(pat, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return;
    do {
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L".."))
            continue;
        swprintf(c, PATH_CHARS, L"%ls\\%ls", p, fd.cFileName);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)
            continue;
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            flush_tree(c);
        else if (!flush_file(c))
            die(L"cannot flush %ls: %ls", c, errtext(GetLastError()));
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

/* A directory junction (no elevation needed, unlike a symbolic link):
 * link -> target, an absolute path. */
struct mount_point_buffer {
    DWORD tag;
    WORD data_length, reserved;
    WORD sub_offset, sub_length, print_offset, print_length;
    WCHAR path[1];
};

static void make_junction(const wchar_t *link, const wchar_t *target)
{
    wchar_t sub[PATH_CHARS];
    size_t sub_len, print_len, size;
    struct mount_point_buffer *b;
    HANDLE h;
    DWORD n;
    swprintf(sub, PATH_CHARS, L"\\??\\%ls", target);
    sub_len = wcslen(sub) * sizeof(WCHAR);
    print_len = wcslen(target) * sizeof(WCHAR);
    size = offsetof(struct mount_point_buffer, path) + sub_len + print_len + 2 * sizeof(WCHAR);
    b = calloc(1, size);
    if (!b)
        die(L"out of memory");
    b->tag = IO_REPARSE_TAG_MOUNT_POINT;
    b->data_length = (WORD)(size - 8);
    b->sub_offset = 0;
    b->sub_length = (WORD)sub_len;
    b->print_offset = (WORD)(sub_len + sizeof(WCHAR));
    b->print_length = (WORD)print_len;
    memcpy(b->path, sub, sub_len);
    memcpy((char *)b->path + sub_len + sizeof(WCHAR), target, print_len);
    if (!CreateDirectoryW(link, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
        die(L"cannot make %ls: %ls", link, errtext(GetLastError()));
    h = CreateFileW(link, GENERIC_WRITE, 0, NULL, OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (h == INVALID_HANDLE_VALUE ||
        !DeviceIoControl(h, FSCTL_SET_REPARSE_POINT, b, (DWORD)size, NULL, 0, &n, NULL))
        die(L"cannot link %ls to %ls: %ls", link, target, errtext(GetLastError()));
    CloseHandle(h);
    free(b);
}

/* ---- the install root ------------------------------------------------ */

static void root_p(wchar_t *out, const wchar_t *rel)
{
    swprintf(out, PATH_CHARS, L"%ls\\%ls", g_root, rel);
}

static void version_dir(wchar_t *out, const char *v, const wchar_t *suffix)
{
    wchar_t *w = wide(v);
    swprintf(out, PATH_CHARS, L"%ls\\versions\\%ls%ls", g_root, w, suffix);
    free(w);
}

/* current.txt's version, or NULL. */
static char *current(void)
{
    wchar_t p[PATH_CHARS];
    char *t, *e;
    root_p(p, L"current.txt");
    t = read_all(p, NULL);
    if (!t)
        return NULL;
    for (e = t + strlen(t); e > t && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' '); e--)
        e[-1] = 0;
    if (!plan_version_ok(t)) {
        free(t);
        return NULL;
    }
    return t;
}

static void utcnow(char out[32])
{
    SYSTEMTIME t;
    GetSystemTime(&t);
    sprintf(out, "%04u-%02u-%02uT%02u:%02u:%02uZ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute,
            t.wSecond);
}

static void reg_display_version(const char *v)
{
    HKEY k;
    wchar_t *w;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, UNINST_KEY, 0, KEY_SET_VALUE, &k) != ERROR_SUCCESS)
        return;
    w = wide(v);
    RegSetValueExW(k, L"DisplayVersion", 0, REG_SZ, (const BYTE *)w,
                   (DWORD)((wcslen(w) + 1) * sizeof(WCHAR)));
    RegCloseKey(k);
    free(w);
}

/* current.txt names v, in one write-through move; then the history line
 * and the uninstall entry's version. */
static void switch_to(const char *v, const char *what)
{
    wchar_t p[PATH_CHARS];
    char *old = current(), line[1024], when[32], *hist;
    size_t len = 0;
    HANDLE h;
    DWORD n;
    root_p(p, L"current.txt");
    snprintf(line, sizeof(line), "%s\n", v);
    write_atomic(p, line);
    utcnow(when);
    snprintf(line, sizeof(line), "%s %s %s -> %s\n", when, what, old ? old : "-", v);
    root_p(p, L"state\\history.txt");
    make_parents(p);
    hist = read_all(p, &len);
    free(hist);
    h = CreateFileW(p, FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL,
                    NULL);
    if (h != INVALID_HANDLE_VALUE) {
        WriteFile(h, line, (DWORD)strlen(line), &n, NULL);
        FlushFileBuffers(h);
        CloseHandle(h);
    }
    reg_display_version(v);
    free(old);
}

static int version_installed(const char *v)
{
    wchar_t d[PATH_CHARS], m[PATH_CHARS];
    version_dir(d, v, L"");
    swprintf(m, PATH_CHARS, L"%ls\\manifest.json", d);
    return exists(m);
}

/* Keeps the newest `keep` versions plus current and previous (as on
 * steamos); a .partial or .old folder older than a day goes too. */
static void prune(int keep)
{
    wchar_t pat[PATH_CHARS], p[PATH_CHARS];
    WIN32_FIND_DATAW fd;
    HANDLE h;
    char **names = NULL, *cur = current(), *htext;
    const char *prev;
    struct plan_hist *hist = NULL;
    size_t n = 0, cap = 0, nh = 0;
    int *rm;
    FILETIME now;
    ULARGE_INTEGER nowu;
    GetSystemTimeAsFileTime(&now);
    nowu.LowPart = now.dwLowDateTime;
    nowu.HighPart = now.dwHighDateTime;
    root_p(p, L"state\\history.txt");
    htext = read_all(p, NULL);
    if (htext)
        nh = plan_parse_history(htext, &hist);
    prev = plan_previous(hist, nh, cur);
    root_p(pat, L"versions\\*");
    h = FindFirstFileW(pat, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            int len;
            char *u;
            ULARGE_INTEGER t;
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || fd.cFileName[0] == L'.')
                continue;
            len = WideCharToMultiByte(CP_UTF8, 0, fd.cFileName, -1, NULL, 0, NULL, NULL);
            u = malloc((size_t)len);
            WideCharToMultiByte(CP_UTF8, 0, fd.cFileName, -1, u, len, NULL, NULL);
            t.LowPart = fd.ftLastWriteTime.dwLowDateTime;
            t.HighPart = fd.ftLastWriteTime.dwHighDateTime;
            if (!plan_version_ok(u)) {
                /* 100 ns units: a day. */
                if (nowu.QuadPart - t.QuadPart > 864000000000ULL) {
                    swprintf(p, PATH_CHARS, L"%ls\\versions\\%ls", g_root, fd.cFileName);
                    rm_tree(p);
                    say(L"pruned %ls (an interrupted run's)", fd.cFileName);
                }
                free(u);
                continue;
            }
            if (!version_installed(u)) {
                free(u);
                continue;
            }
            if (n == cap) {
                cap = cap ? cap * 2 : 16;
                names = realloc(names, cap * sizeof(*names));
            }
            names[n++] = u;
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    plan_order_versions(names, n, hist, nh);
    rm = calloc(n + 1, sizeof(int));
    plan_prune(names, n, keep, cur, prev, rm);
    for (size_t i = 0; i < n; i++) {
        if (rm[i]) {
            version_dir(p, names[i], L"");
            rm_tree(p);
            say(L"pruned %hs", names[i]);
        }
        free(names[i]);
    }
    free(rm);
    free(names);
    free(hist);
    free(htext);
    free(cur);
}

static void refuse_while_running(void)
{
    HANDLE m = OpenMutexW(SYNCHRONIZE, FALSE, L"Local\\" APP L"-running");
    if (m) {
        CloseHandle(m);
        die(L"%ls is running; quit it first", APP);
    }
}

/* One apply at a time; held until exit. */
static void take_lock(void)
{
    HANDLE m = CreateMutexW(NULL, TRUE, L"Local\\" APP L"-apply");
    if (!m || GetLastError() == ERROR_ALREADY_EXISTS)
        die(L"another apply.exe is running");
}

static void find_root(const wchar_t *given)
{
    wchar_t self[PATH_CHARS], *slash, p[PATH_CHARS];
    HKEY k;
    DWORD type, size = sizeof(g_root) - sizeof(wchar_t);
    if (given) {
        if (!GetFullPathNameW(given, PATH_CHARS, g_root, NULL))
            die(L"bad --root %ls", given);
        return;
    }
    if (RegOpenKeyExW(HKEY_CURRENT_USER, UNINST_KEY, 0, KEY_QUERY_VALUE, &k) == ERROR_SUCCESS) {
        LONG r = RegQueryValueExW(k, L"InstallLocation", NULL, &type, (BYTE *)g_root, &size);
        RegCloseKey(k);
        /* A REG_SZ need not end in a NUL. */
        g_root[r == ERROR_SUCCESS ? size / sizeof(wchar_t) : 0] = 0;
        if (r == ERROR_SUCCESS && type == REG_SZ && g_root[0])
            return;
    }
    GetModuleFileNameW(NULL, self, PATH_CHARS);
    slash = wcsrchr(self, L'\\');
    if (slash)
        *slash = 0;
    swprintf(p, PATH_CHARS, L"%ls\\current.txt", self);
    if (exists(p)) {
        wcscpy(g_root, self);
        return;
    }
    die(L"%ls is not installed (no uninstall entry); use the full installer", APP);
}

/* ---- apply.exe [patch] ----------------------------------------------- */

static int ask_yes(const wchar_t *q)
{
    DWORD mode;
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    char ans[16] = {0};
    DWORD n = 0;
    if (!GetConsoleMode(in, &mode))
        return -1;
    say(L"%ls [y/N]", q);
    if (!ReadFile(in, ans, sizeof(ans) - 1, &n, NULL))
        return 0;
    return ans[0] == 'y' || ans[0] == 'Y';
}

static void self_dir(wchar_t *out)
{
    wchar_t *slash;
    GetModuleFileNameW(NULL, out, PATH_CHARS);
    slash = wcsrchr(out, L'\\');
    if (slash)
        *slash = 0;
}

/* Every patch leaves its own apply.exe at the root, so a rollback works
 * after the patch folder is deleted. */
static void install_self(void)
{
    wchar_t self[PATH_CHARS], dst[PATH_CHARS], tmp[PATH_CHARS];
    GetModuleFileNameW(NULL, self, PATH_CHARS);
    root_p(dst, L"apply.exe");
    if (_wcsicmp(self, dst) == 0)
        return;
    swprintf(tmp, PATH_CHARS, L"%ls.tmp", dst);
    copy_checked(self, tmp, NULL);
    replace_file(tmp, dst);
}

static void cmd_patch(int keep, int yes)
{
    wchar_t here[PATH_CHARS], lst[PATH_CHARS], files[PATH_CHARS], p[PATH_CHARS], q[PATH_CHARS];
    wchar_t base_dir[PATH_CHARS], part[PATH_CHARS], final_[PATH_CHARS];
    char *text, *sums_text, *cur, hex[PLAN_SHA_CHARS];
    const char *err, *bad;
    int line;
    struct plan_patch pt;
    struct plan_entry *sums;
    size_t nsums;
    struct plan_step *steps;
    long ns;
    int game_data = 0, written = 0, removed = 0;

    self_dir(here);
    swprintf(lst, PATH_CHARS, L"%ls\\patch.lst", here);
    swprintf(files, PATH_CHARS, L"%ls\\files", here);
    text = read_all(lst, NULL);
    if (!text)
        die(L"no patch here (%ls is missing); run the full installer instead", lst);
    if (plan_parse_patch(text, &pt, &err, &line))
        die(L"patch.lst line %d: %hs", line, err);
    if (strcmp(pt.app, "@APP@") != 0)
        die(L"this patch is for %hs, not %ls", pt.app, APP);
    if (pt.stub_abi != STUB_ABI)
        die(L"this patch needs launcher stub %d; use the full installer for %hs", pt.stub_abi,
            pt.version);
    refuse_while_running();
    take_lock();

    /* The patch itself: its manifest and sums, and the lists cover them. */
    swprintf(p, PATH_CHARS, L"%ls\\manifest.json", files);
    if (sha_file(p, hex, NULL) || strcmp(hex, pt.new_sha) != 0)
        die(L"the patch's manifest.json does not match patch.lst (a damaged patch?)");
    swprintf(p, PATH_CHARS, L"%ls\\SHA256SUMS", files);
    sums_text = read_all(p, NULL);
    if (!sums_text || plan_parse_sums(sums_text, &sums, &nsums, &err, &line))
        die(L"the patch's SHA256SUMS cannot be read (a damaged patch?)");
    bad = plan_cover(&pt, sums, nsums);
    if (bad)
        die(L"patch.lst and SHA256SUMS disagree on %hs (a damaged patch?)", bad);

    cur = current();
    if (cur && strcmp(cur, pt.version) == 0) {
        install_self();
        prune(keep);
        say(L"%ls %hs is current already", APP, pt.version);
        return;
    }
    /* D10: the current version must be the base, by its manifest's bytes. */
    if (!cur)
        die(L"This update is for %hs; this install is not installed. Use the full installer "
            L"for %hs.",
            pt.base, pt.version);
    version_dir(base_dir, cur, L"");
    swprintf(p, PATH_CHARS, L"%ls\\manifest.json", base_dir);
    if (strcmp(cur, pt.base) != 0 || sha_file(p, hex, NULL) || strcmp(hex, pt.base_sha) != 0)
        die(L"This update is for %hs; this install is %hs%ls. Use the full installer for %hs.",
            pt.base, cur, strcmp(cur, pt.base) == 0 ? L" with another manifest" : L"",
            pt.version);

    ns = plan_steps(&pt, &steps);
    if (ns < 0)
        die(L"out of memory");
    say(L"checking %ls ...", g_root);
    for (long i = 0; i < ns; i++) {
        const struct plan_entry *e = steps[i].e;
        if (steps[i].act == ACT_CHECK_PROGRAM)
            join(p, base_dir, e->path);
        else if (steps[i].act == ACT_CHECK_GAME)
            join(p, g_root, e->path);
        else
            continue;
        if (!matches(p, e))
            die(L"This update is for %hs, but %hs does not match it. Use the full installer "
                L"for %hs.",
                pt.base, e->path, pt.version);
    }
    /* Every file the patch ships, checked before anything is written: a
     * damaged game file found only at its copy would leave the game files
     * half patched. */
    for (long i = 0; i < ns; i++) {
        const struct plan_entry *e = steps[i].e;
        if (steps[i].act != ACT_COPY && steps[i].act != ACT_GAME_WRITE)
            continue;
        join(p, files, e->path);
        if (!matches(p, e))
            die(L"damaged patch; nothing was changed (%hs does not match patch.lst)", e->path);
    }
    for (long i = 0; i < ns; i++)
        if (steps[i].act == ACT_GAME_WRITE || steps[i].act == ACT_GAME_DELETE)
            game_data = 1;
    if (game_data && !yes) {
        int a;
        say(L"patch: %ls", GAME_DATA_WARNING);
        a = ask_yes(L"Continue?");
        if (a < 0)
            die(L"%ls: run with --yes to apply it", GAME_DATA_WARNING);
        if (!a)
            die(L"stopped; nothing was changed");
    }

    /* The new version, beside the current one: a complete one from an
     * interrupted run is reused. */
    version_dir(part, pt.version, L".partial");
    version_dir(final_, pt.version, L"");
    swprintf(p, PATH_CHARS, L"%ls\\manifest.json", final_);
    if (!(sha_file(p, hex, NULL) == 0 && strcmp(hex, pt.new_sha) == 0)) {
        rm_tree(part);
        rm_tree(final_);
        for (long i = 0; i < ns; i++) {
            const struct plan_entry *e = steps[i].e;
            if (steps[i].act == ACT_LINK) {
                join(p, base_dir, e->path);
                join(q, part, e->path);
                link_or_copy(p, q, e->sha);
            } else if (steps[i].act == ACT_COPY) {
                join(p, files, e->path);
                join(q, part, e->path);
                copy_checked(p, q, e->sha);
            }
        }
        for (int k = 0; k < 2; k++) {
            const char *name = k ? "SHA256SUMS" : "manifest.json";
            join(p, files, name);
            join(q, part, name);
            copy_checked(p, q, NULL);
        }
        if (!MoveFileExW(part, final_, MOVEFILE_WRITE_THROUGH))
            die(L"cannot rename %ls: %ls", part, errtext(GetLastError()));
    }

    /* The game files: each written beside itself, flushed, then moved
     * over the old one; only what the patch deletes is removed. */
    for (long i = 0; i < ns; i++) {
        const struct plan_entry *e = steps[i].e;
        if (steps[i].act == ACT_GAME_WRITE) {
            wchar_t tmp[PATH_CHARS];
            join(q, g_root, e->path);
            if (matches(q, e))
                continue; /* a rerun after this file */
            join(p, files, e->path);
            swprintf(tmp, PATH_CHARS, L"%ls.partial", q);
            copy_checked(p, tmp, e->sha);
            replace_file(tmp, q);
            written++;
        } else if (steps[i].act == ACT_GAME_DELETE) {
            join(q, g_root, e->path);
            if (DeleteFileW(q))
                removed++;
        }
    }

    switch_to(pt.version, "patch");
    say(L"current: %hs -> %hs", cur, pt.version);
    if (game_data)
        say(L"game files: %d written, %d removed", written, removed);
    install_self();
    prune(keep);
    free(steps);
    free(sums);
    free(sums_text);
    plan_free_patch(&pt);
    free(text);
    free(cur);
}

/* ---- apply.exe rollback ---------------------------------------------- */

static void cmd_rollback(const char *version)
{
    wchar_t p[PATH_CHARS];
    char *cur, *htext;
    const char *target = version;
    struct plan_hist *h = NULL;
    size_t nh = 0;
    refuse_while_running();
    take_lock();
    cur = current();
    root_p(p, L"state\\history.txt");
    htext = read_all(p, NULL);
    if (htext)
        nh = plan_parse_history(htext, &h);
    if (!target)
        target = plan_previous(h, nh, cur);
    if (!target)
        die(L"no previous version to roll back to");
    if (!plan_version_ok(target) || !version_installed(target))
        die(L"version %hs is not installed", target);
    if (cur && strcmp(cur, target) == 0) {
        say(L"current is already %hs", cur);
        return;
    }
    switch_to(target, "rollback");
    say(L"current: %hs -> %hs (saves untouched; game files are not rolled back)",
        cur ? cur : "-", target);
}

/* ---- apply.exe switch VERSION (the installer) ------------------------ */

/* 1 when every program file under dir matches its SHA256SUMS; else 0,
 * with the first bad path in bad. */
static int version_ok(const wchar_t *dir, wchar_t *bad)
{
    wchar_t p[PATH_CHARS];
    char *t;
    const char *err;
    int line, ok = 1;
    struct plan_entry *sums;
    size_t n;
    swprintf(p, PATH_CHARS, L"%ls\\SHA256SUMS", dir);
    wcscpy(bad, p);
    t = read_all(p, NULL);
    if (!t || plan_parse_sums(t, &sums, &n, &err, &line)) {
        free(t);
        return 0;
    }
    for (size_t i = 0; i < n && ok; i++) {
        if (plan_is_game(sums[i].path))
            continue;
        join(p, dir, sums[i].path);
        if (!matches(p, &sums[i])) {
            wcscpy(bad, p);
            ok = 0;
        }
    }
    free(sums);
    free(t);
    return ok;
}

static void verify_version(const wchar_t *dir)
{
    wchar_t bad[PATH_CHARS];
    if (!version_ok(dir, bad))
        die(L"%ls does not match SHA256SUMS (disk full?)", bad);
}

static void cmd_switch(const char *v, int keep)
{
    wchar_t part[PATH_CHARS], final_[PATH_CHARS], old[PATH_CHARS], stub[PATH_CHARS],
        exe[PATH_CHARS], a[PATH_CHARS], b[PATH_CHARS];
    char *ta, *tb;
    if (!plan_version_ok(v))
        die(L"bad version %hs", v);
    refuse_while_running();
    take_lock();
    version_dir(part, v, L".partial");
    version_dir(final_, v, L"");
    version_dir(old, v, L".old");
    if (is_dir(part)) {
        verify_version(part);
        flush_tree(part);
        swprintf(a, PATH_CHARS, L"%ls\\SHA256SUMS", part);
        swprintf(b, PATH_CHARS, L"%ls\\SHA256SUMS", final_);
        ta = read_all(a, NULL);
        tb = read_all(b, NULL);
        if (tb && ta && strcmp(ta, tb) == 0 && version_ok(final_, a)) {
            /* The same version again, the same bytes, and the installed
             * copy intact: kept. A damaged one is replaced below, so a
             * reinstall repairs it. */
            rm_tree(part);
        } else {
            /* Another build under the same name: a short moment without
             * versions\<v>; a rerun of the installer repairs it. */
            rm_tree(old);
            if (is_dir(final_) && !MoveFileExW(final_, old, MOVEFILE_WRITE_THROUGH))
                die(L"cannot move %ls aside: %ls", final_, errtext(GetLastError()));
            if (!MoveFileExW(part, final_, MOVEFILE_WRITE_THROUGH))
                die(L"cannot rename %ls: %ls", part, errtext(GetLastError()));
            rm_tree(old);
        }
        free(ta);
        free(tb);
    }
    if (!version_installed(v))
        die(L"version %hs is not installed (no versions\\%hs)", v, v);
    switch_to(v, "install");
    /* The stub goes in after current.txt names a version it can run. */
    root_p(stub, APP L".exe.stub");
    root_p(exe, APP L".exe");
    if (exists(stub))
        replace_file(stub, exe);
    say(L"current: %hs", v);
    prune(keep);
}

/* ---- apply.exe migrate (the installer, over an in-place install) ----- */

static char *old_version(void)
{
    HKEY k;
    wchar_t w[512];
    DWORD type, size = sizeof(w);
    char *u;
    int n;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, UNINST_KEY, 0, KEY_QUERY_VALUE, &k) != ERROR_SUCCESS)
        return NULL;
    if (RegQueryValueExW(k, L"DisplayVersion", NULL, &type, (BYTE *)w, &size) != ERROR_SUCCESS ||
        type != REG_SZ) {
        RegCloseKey(k);
        return NULL;
    }
    RegCloseKey(k);
    n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    u = malloc((size_t)n);
    WideCharToMultiByte(CP_UTF8, 0, w, -1, u, n, NULL, NULL);
    return u;
}

/* The old top-level program files go once the stub runs the migrated
 * version: each listed in the old SHA256SUMS, and the folders they leave
 * empty. */
static void remove_old_top(struct plan_entry *sums, size_t n)
{
    wchar_t p[PATH_CHARS];
    for (size_t i = 0; i < n; i++) {
        if (plan_is_game(sums[i].path) || strcmp(sums[i].path, "@APP@.exe") == 0)
            continue;
        join(p, g_root, sums[i].path);
        DeleteFileW(p);
        for (wchar_t *s = wcsrchr(p, L'\\'); s && s > p + wcslen(g_root);
             s = wcsrchr(p, L'\\')) {
            *s = 0;
            if (!RemoveDirectoryW(p))
                break;
        }
    }
    root_p(p, L"manifest.json");
    DeleteFileW(p);
    root_p(p, L"SHA256SUMS");
    DeleteFileW(p);
}

static void cmd_migrate(void)
{
    wchar_t p[PATH_CHARS], q[PATH_CHARS], part[PATH_CHARS], final_[PATH_CHARS], gf[PATH_CHARS],
        stub[PATH_CHARS], exe[PATH_CHARS];
    char *t, *v, *cur;
    const char *err;
    int line;
    struct plan_entry *sums;
    size_t n;
    refuse_while_running();
    take_lock();
    root_p(p, L"SHA256SUMS");
    t = read_all(p, NULL);
    cur = current();
    if (!t) {
        say(cur ? L"migrate: the versioned layout already" : L"migrate: nothing to migrate");
        return;
    }
    if (plan_parse_sums(t, &sums, &n, &err, &line))
        die(L"the old SHA256SUMS line %d: %hs", line, err);
    root_p(stub, APP L".exe.stub");
    root_p(exe, APP L".exe");
    if (cur) {
        /* Resumed after current.txt: only the stub and the old files. */
        if (exists(stub))
            replace_file(stub, exe);
        remove_old_top(sums, n);
        say(L"migrate: finished (%hs)", cur);
        return;
    }
    v = old_version();
    if (!v || !plan_version_ok(v))
        die(L"cannot tell the installed version (no DisplayVersion); uninstall it first");
    say(L"migrate: %hs into versions\\%hs", v, v);
    version_dir(part, v, L".partial");
    version_dir(final_, v, L"");
    if (!version_installed(v)) {
        rm_tree(part);
        /* 1. the old program files, linked (no copy) and checked. */
        for (size_t i = 0; i < n; i++) {
            if (plan_is_game(sums[i].path))
                continue;
            join(p, g_root, sums[i].path);
            join(q, part, sums[i].path);
            if (!matches(p, &sums[i]))
                die(L"%hs does not match the installed version; uninstall it and run the full "
                    L"installer",
                    sums[i].path);
            link_or_copy(p, q, sums[i].sha);
        }
        for (int k = 0; k < 2; k++) {
            const char *name = k ? "SHA256SUMS" : "manifest.json";
            join(p, g_root, name);
            join(q, part, name);
            link_or_copy(p, q, NULL);
        }
        /* 2. its launcher reads game_files beside itself: a junction to the
         * root's. */
        swprintf(q, PATH_CHARS, L"%ls\\game_files", part);
        root_p(gf, L"game_files");
        make_junction(q, gf);
        if (!MoveFileExW(part, final_, MOVEFILE_WRITE_THROUGH))
            die(L"cannot rename %ls: %ls", part, errtext(GetLastError()));
    }
    /* 3. current.txt; 4. the stub; 5. the old top-level files. */
    switch_to(v, "migrate");
    if (exists(stub))
        replace_file(stub, exe);
    remove_old_top(sums, n);
    say(L"migrate: done; current is %hs", v);
    free(v);
    free(sums);
    free(t);
}

/* ---- main ------------------------------------------------------------ */

static char *utf8(const wchar_t *w)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    char *u = malloc((size_t)(n > 0 ? n : 1));
    WideCharToMultiByte(CP_UTF8, 0, w, -1, u, n, NULL, NULL);
    return u;
}

int wmain(int argc, wchar_t **argv)
{
    const wchar_t *cmd = L"patch", *root = NULL;
    char *arg = NULL;
    int keep = 3, yes = 0, i = 1;
    if (i < argc && argv[i][0] != L'-')
        cmd = argv[i++];
    for (; i < argc; i++) {
        if (!wcscmp(argv[i], L"--root") && i + 1 < argc)
            root = argv[++i];
        else if (!wcscmp(argv[i], L"--keep") && i + 1 < argc)
            keep = _wtoi(argv[++i]);
        else if (!wcscmp(argv[i], L"--yes"))
            yes = 1;
        else if (!wcscmp(argv[i], L"-h") || !wcscmp(argv[i], L"--help")) {
            say(L"apply.exe [patch] [--root DIR] [--keep N] [--yes]");
            say(L"apply.exe rollback [VERSION] [--root DIR]");
            say(L"apply.exe switch VERSION [--root DIR] [--keep N]");
            say(L"apply.exe migrate [--root DIR]");
            return 0;
        } else if (argv[i][0] != L'-' && !arg)
            arg = utf8(argv[i]);
        else
            die(L"unknown argument %ls (see --help)", argv[i]);
    }
    if (keep < 1)
        keep = 1;
    find_root(root);
    if (!wcscmp(cmd, L"patch"))
        cmd_patch(keep, yes);
    else if (!wcscmp(cmd, L"rollback"))
        cmd_rollback(arg);
    else if (!wcscmp(cmd, L"switch") && arg)
        cmd_switch(arg, keep);
    else if (!wcscmp(cmd, L"migrate"))
        cmd_migrate();
    else
        die(L"unknown command %ls (see --help)", cmd);
    free(arg);
    finish(0);
}
