"""The two small things `agents-hub` needs, done with the package's own Python (no curl, no xdg-utils required):

    open_page.py wait <url> <seconds>   exit 0 when the page answers within the time, 1 when it does not
    open_page.py open <url>             the person's browser, when there is a screen; exit 0 when one was asked to open
                                        (with the page key on — through a file of this account, see target)

The stdlib only, no proxy: the page is on this machine."""
import http.client
import os
import shutil
import subprocess
import sys
import time
import urllib.parse


def answers(url, timeout=3):
    u = urllib.parse.urlsplit(url)
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request("GET", u.path or "/")
            return conn.getresponse().status < 500
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return False


def wait(url, seconds, sleep=time.sleep, clock=time.monotonic):
    end = clock() + seconds
    while True:
        if answers(url):
            return True
        if clock() >= end:
            return False
        sleep(1)


def has_screen(env=os.environ):
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def open_browser(url, env=os.environ, which=shutil.which, run=subprocess.run):
    if not has_screen(env):
        return False
    for program in ("xdg-open", "sensible-browser", "gio"):
        found = which(program)
        if found:
            argv = [found, "open", url] if program == "gio" else [found, url]
            try:
                return run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
    return False


def target(url, env=os.environ):
    """What the browser gets: the page; with the sign-in by a key on (core/pagekey.py — <config>/page-key, read the
    same way: the package's helper imports nothing of the hub) a file of this account (0600, in the state folder) that
    goes on to the link with the key — not the link itself: every account of the machine reads command lines (ps). A
    hub with the page's password (core/pagepass.py — page-password next to it) asks for it on the page: its key is the
    one a password keeps for a rollback, never opened."""
    config = env.get("HUB_CONFIG_DIR") or os.path.join(env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                                                       "hub-core")
    if os.path.exists(os.path.join(config, "page-password")):
        return url
    try:
        with open(os.path.join(config, "page-key"), "rb") as f:
            key = f.read(200).decode("ascii").strip()
    except (OSError, ValueError):
        return url
    if not (len(key) == 64 and all(c in "0123456789abcdef" for c in key)):
        return url
    link = f"{url}?key={key}"
    state = env.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core")
    path = os.path.join(state, "page-open.html")   # core/pagekey.opener writes the same file
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Hub by Icons8'
                    f'</title><meta http-equiv="refresh" content="0;url={link}"><script>location.replace("{link}")'
                    f'</script>\n')
    except OSError:   # no state folder there (another place of it): the page itself, the person signs in by hub ui
        return url
    return "file://" + urllib.parse.quote(path)


def main(argv):
    if len(argv) == 4 and argv[1] == "wait" and argv[3].isdigit():
        return 0 if wait(argv[2], int(argv[3])) else 1
    if len(argv) == 3 and argv[1] == "open":
        return 0 if open_browser(target(argv[2])) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
