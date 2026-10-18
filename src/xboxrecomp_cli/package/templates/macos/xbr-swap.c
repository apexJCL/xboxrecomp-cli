/* xbr-swap: the one step of a macOS patch that sh cannot do safely.
 *
 *   xbr-swap swap NEW OLD   flush NEW to the disk, then exchange the two
 *                           paths in one call: there is never a moment
 *                           with no app at OLD, which two renames leave
 *   xbr-swap sync PATH      flush PATH (every file and folder under it)
 *
 * The flush is F_FULLFSYNC, not fsync: on macOS fsync leaves the data in
 * the drive's cache, so a power loss after the swap could find the new
 * app's name pointing at blocks never written.
 *
 * Exit 0, or 3 on EPERM (macOS 13 and later: the App Management
 * permission is missing for the app that runs this), 4 on EACCES (the
 * folder is not writable by this user), 1 on anything else. apply.command
 * explains each.
 */
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef RENAME_SWAP
#define RENAME_SWAP 0x00000002
#endif
int renamex_np(const char *from, const char *to, unsigned int flags);

static int fail(const char *what, const char *path)
{
    int e = errno;
    fprintf(stderr, "xbr-swap: %s %s: %s\n", what, path, strerror(e));
    if (e == EPERM)
        return 3;
    if (e == EACCES)
        return 4;
    return 1;
}

static int full_sync_fd(int fd)
{
    /* Some filesystems (an SMB share, FAT) lack F_FULLFSYNC: fsync then. */
    if (fcntl(fd, F_FULLFSYNC) == 0)
        return 0;
    return fsync(fd);
}

static int sync_path(const char *path)
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) {
        /* A symlink in a framework is flushed with its folder. */
        return errno == ELOOP ? 0 : -1;
    }
    int r = full_sync_fd(fd);
    close(fd);
    return r;
}

static const char *bad_path;

static int sync_one(const char *path, const struct stat *st, int flag, struct FTW *ftw)
{
    (void)st;
    (void)ftw;
    if (flag == FTW_SL || flag == FTW_SLN)
        return 0;
    if (sync_path(path) != 0) {
        bad_path = path;
        return -1;
    }
    return 0;
}

static int sync_tree(const char *path)
{
    /* FTW_DEPTH: each folder after what is in it. */
    return nftw(path, sync_one, 32, FTW_PHYS | FTW_DEPTH);
}

static int sync_parent(const char *path)
{
    char buf[4096];
    if (strlen(path) >= sizeof buf)
        return 0;
    strcpy(buf, path);
    return sync_path(dirname(buf));
}

int main(int argc, char **argv)
{
    if (argc == 4 && strcmp(argv[1], "swap") == 0) {
        if (sync_tree(argv[2]) != 0)
            return fail("flush", bad_path ? bad_path : argv[2]);
        if (renamex_np(argv[2], argv[3], RENAME_SWAP) != 0)
            return fail("swap", argv[3]);
        if (sync_parent(argv[3]) != 0)
            return fail("flush", argv[3]);
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "sync") == 0) {
        if (sync_tree(argv[2]) != 0)
            return fail("flush", bad_path ? bad_path : argv[2]);
        return sync_parent(argv[2]) != 0 ? fail("flush", argv[2]) : 0;
    }
    fprintf(stderr, "usage: xbr-swap swap NEW OLD | xbr-swap sync PATH\n");
    return 2;
}
