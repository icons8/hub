/* The executable of "Agents Hub.app" (deploy/mac/app.py): runs argv[1..] as a child and waits for it.
 * A child, not exec: macOS attributes background work and folder access to this bundle process, not to python3.
 * SIGTERM/SIGINT/SIGHUP go to the child; the exit code is the child's.
 * The downloaded app (tools/package/macapp.py) carries its shell in Contents/Resources/hub: started without a program
 * (by Finder, maybe with -psn_…) it runs the shell's "open", with --serve (the launchd agent) its "serve" —
 * deploy/mac/appshell.py, by the app's own Python — after taking the download's quarantine off the app's files. */
#include <errno.h>
#include <fts.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

#define QUARANTINE "com.apple.quarantine"

extern char **environ;
static pid_t child;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static int run(char **args) {
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    int err = posix_spawnp(&child, args[0], NULL, NULL, args, environ);   /* a name without "/" — from PATH */
    if (err) {
        errno = err;
        perror(args[0]);
        return 127;
    }
    int status;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR) return 1;
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : WEXITSTATUS(status);
}

/* The person allowed the app in Gatekeeper; its own files still carry the download's quarantine, and macOS kills a
 * quarantined, unapproved program the app starts (the Mac bridge, 01.11: Python, node, git — SIGKILL). The mark goes
 * from every file of the app before anything inside it runs; the app's own folder last, so a cut walk is redone. Best
 * effort: what cannot be changed is left. */
static void unquarantine(const char *app) {
    if (getxattr(app, QUARANTINE, NULL, 0, 0, XATTR_NOFOLLOW) < 0) return;
    char *roots[] = {(char *)app, NULL};
    FTS *fts = fts_open(roots, FTS_PHYSICAL | FTS_NOCHDIR, NULL);
    if (!fts) return;
    FTSENT *e;
    while ((e = fts_read(fts)))
        if (e->fts_info != FTS_D && e->fts_level > 0) removexattr(e->fts_path, QUARANTINE, XATTR_NOFOLLOW);
    fts_close(fts);
    removexattr(app, QUARANTINE, XATTR_NOFOLLOW);
}

/* Opened from the disk image or from App Translocation (macOS runs a quarantined app that was not moved by Finder — out
 * of a downloaded archive, say — from a random read-only copy): the path goes away, nothing is installed — a dialog
 * asks to drag the app to Applications.
 * Here and not in the shell: inside such a copy the files keep their quarantine, and the shell's Python may not run. */
static const char *MOVE[] = {
    "on run argv",
    "tell me to activate",
    "set r to display dialog (item 1 of argv) with title \"Agents Hub\" buttons {item 2 of argv, \"OK\"} "
    "default button \"OK\" with icon caution",
    "if button returned of r is not \"OK\" then do shell script \"open /Applications\"",
    "end run", NULL};

static int moved_away(const char *app) {
    struct statfs fs;   /* the image is mounted read-only, so is a translocated copy; an external disk is not */
    int image = statfs(app, &fs) == 0 && (fs.f_flags & MNT_RDONLY);
    if (!image && strncmp(app, "/Volumes/", 9) == 0) {   /* an image mounted writable: its Applications link */
        char link[PATH_MAX];
        const char *end = strchr(app + 9, '/');
        struct stat st;
        if (end && snprintf(link, sizeof link, "%.*s/Applications", (int)(end - app), app) < (int)sizeof link)
            image = lstat(link, &st) == 0 && S_ISLNK(st.st_mode);
    }
    if (!image && !strstr(app, "/AppTranslocation/")) return 0;
    char *args[32];
    int n = 0;
    args[n++] = "osascript";
    for (int i = 0; MOVE[i]; i++) {
        args[n++] = "-e";
        args[n++] = (char *)MOVE[i];
    }
    args[n++] = "Agents Hub is running straight from the disk image or the downloaded archive. Drag it to the "
                "Applications folder and open it from there — otherwise the hub cannot start by itself.";
    args[n++] = "Open Applications";
    args[n] = NULL;
    run(args);
    return 1;
}

/* -1: this bundle has no shell (the hub from git starts it with a program) */
static int shell(char *action) {
    char exe[PATH_MAX], real[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) || !realpath(exe, real)) return -1;
    for (int i = 0; i < 2; i++) {   /* <app>/Contents/MacOS/Agents Hub → <app>/Contents */
        char *slash = strrchr(real, '/');
        if (!slash) return -1;
        *slash = 0;
    }
    snprintf(python, sizeof python, "%s/Resources/hub/runtime/python/bin/python3", real);
    snprintf(script, sizeof script, "%s/Resources/hub/appshell.py", real);
    if (access(script, R_OK) || access(python, X_OK)) return -1;
    char app[PATH_MAX];
    snprintf(app, sizeof app, "%s", real);
    char *slash = strrchr(app, '/');   /* <app>/Contents → <app> */
    if (slash) {
        *slash = 0;
        if (strcmp(action, "open") == 0 && moved_away(app)) return 2;
        unquarantine(app);
    }
    char *args[] = {python, "-I", "-B", script, action, NULL};
    return run(args);
}

int main(int argc, char **argv) {
    int opened = argc < 2 || strncmp(argv[1], "-psn_", 5) == 0;
    if (opened || strcmp(argv[1], "--serve") == 0) {
        int code = shell(opened ? "open" : "serve");
        if (code >= 0) return code;
    }
    if (argc < 2 || opened) {
        fprintf(stderr, "usage: %s program [args...]\n", argv[0]);
        return 64;
    }
    return run(argv + 1);
}
