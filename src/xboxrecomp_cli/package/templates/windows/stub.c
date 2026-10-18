/*
 * @APP@.exe at the install root: the stub the shortcuts point at.
 *
 * It reads current.txt (one line: a folder name under versions\) and
 * starts versions\<that>\@APP@.exe, the version's own launcher, with the
 * same arguments, then returns its exit code. Everything that can change
 * between versions (the environment, the game files, the backend) is in
 * that launcher, so this stub stays the same across updates: a patch
 * never replaces it, and a new STUB_ABI means a full installer (D8).
 *
 * Built by `<game> package windows` with llvm-mingw:
 *   x86_64-w64-mingw32-clang -municode -mwindows -O2 -Wl,--no-insert-timestamp
 *     '-DAPP_NAME=L"<product name>"' stub.c <app>.res.o -o <app>.exe
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The contract with every installed version's launcher: this stub runs
 * versions\<current.txt>\@APP@.exe from the root, with the arguments it
 * was given. The CLI records it in the manifest's root block. */
#define STUB_ABI 1

#ifndef APP_NAME
#error "build with -DAPP_NAME=L\"<product name>\" (<game> package does)"
#endif

#define PATH_CHARS 2048

static void fail(const wchar_t *msg)
{
    MessageBoxW(NULL, msg, APP_NAME, MB_ICONERROR);
    ExitProcess(1);
}

/* current.txt's first line; refuses a name that would leave versions\. */
static void read_current(const wchar_t *root, wchar_t *out, size_t cap)
{
    wchar_t p[PATH_CHARS];
    char buf[512];
    FILE *f;
    size_t n;
    swprintf(p, PATH_CHARS, L"%ls\\current.txt", root);
    f = _wfopen(p, L"rb");
    if (!f)
        fail(L"current.txt is missing beside @APP@.exe. Run the setup program again.");
    n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r' || buf[n - 1] == ' '))
        buf[--n] = 0;
    if (!n || buf[0] == '.' || strpbrk(buf, "\\/:\n\r") ||
        !MultiByteToWideChar(CP_UTF8, 0, buf, -1, out, (int)cap))
        fail(L"current.txt does not name a version. Run the setup program again.");
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show)
{
    wchar_t root[PATH_CHARS], ver[512], exe[PATH_CHARS], dir[PATH_CHARS], msg[PATH_CHARS];
    wchar_t *slash, *cmd;
    STARTUPINFOW si = {0};
    PROCESS_INFORMATION pi;
    DWORD code = 1;
    size_t len;
    (void)inst;
    (void)prev;
    (void)show;
    si.cb = sizeof(si);

    if (!GetModuleFileNameW(NULL, root, PATH_CHARS))
        fail(L"Cannot find the program's own folder.");
    slash = wcsrchr(root, L'\\');
    if (slash)
        *slash = 0;
    read_current(root, ver, 512);
    swprintf(dir, PATH_CHARS, L"%ls\\versions\\%ls", root, ver);
    swprintf(exe, PATH_CHARS, L"%ls\\@APP@.exe", dir);
    if (GetFileAttributesW(exe) == INVALID_FILE_ATTRIBUTES) {
        swprintf(msg, PATH_CHARS,
                 L"Version %ls is not installed (versions\\%ls\\@APP@.exe is missing). "
                 L"Run the setup program again.",
                 ver, ver);
        fail(msg);
    }
    len = wcslen(exe) + wcslen(cmdline) + 8;
    cmd = malloc(len * sizeof(wchar_t));
    if (!cmd)
        fail(L"Out of memory.");
    swprintf(cmd, len, L"\"%ls\"%ls%ls", exe, *cmdline ? L" " : L"", cmdline);
    if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        swprintf(msg, PATH_CHARS, L"Cannot start %ls (error %lu).", exe, GetLastError());
        fail(msg);
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    free(cmd);
    return (int)code;
}
