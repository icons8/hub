"""Hub files are readable only by its user.

- `private()` - the first line of every entry point (bin/hub, bin/hub-up, hubd, the hook): umask 077, new state and
  settings files are 0600, folders 0700. The user's previous umask goes into HUB_USER_UMASK for the processes the hub
  starts on their behalf (sessions, schedule jobs, executor working copies): `user_umask()` - their files in projects get
  the user's usual permissions.
- `secure()` - at core start: closes the state and settings folder that a previous hub left open (folders 0755, files
  0644). What changed is recorded as lines in the RECORD of that folder (paths and modes only); `restore()` puts it back.
  `perms.keep: true` in hub.json - do not close (set by `hub perms restore`: the state is legitimately read by another
  user); hub-up then passes HUB_PERMS_KEEP to the parts, and `private()` does not set 077 - new files are open too.

POSIX only: on Windows the profile folders are closed by ACLs, the functions do nothing.
"""
import json
import os
import stat
import threading

import platforms

UMASK_ENV = "HUB_USER_UMASK"
KEEP_ENV = "HUB_PERMS_KEEP"   # perms.keep: hub processes run with the user's umask (set by hub-up)
RECORD = "perms-before.jsonl"
# the state folder holds session work: its files keep the user's permissions (the closed state folder above them is enough)
SKIP = ("workspaces",)
# getattr: a process of v1.4.0 (the page server during the update's self-check) imports this file with its own
# platforms in memory, without file_modes: it does nothing with modes, as v1.4.0 did (tests/test_old_page_server)
POSIX = getattr(platforms.current, "file_modes", lambda: False)()
_deep = []   # background walk of the state folder (secure): wait() waits for it


def private():
    """umask 077 for this process; remember the previous one for the user's processes (the first entry of the chain
    remembers it: hubd under hub-up already starts with 077). perms.keep (KEEP_ENV) - the user's umask, if known (hub-up
    after a restart - exec - inherited the previous process's 077)."""
    if os.environ.get(KEEP_ENV):
        if user_umask() >= 0:
            platforms.current.umask(user_umask())
        return
    was = platforms.current.umask(0o077)
    if was is not None:
        os.environ.setdefault(UMASK_ENV, oct(was))


def user_umask():
    """The user's umask for a process the hub starts on their behalf (Popen(umask=...)); -1 - inherit
    (unknown)."""
    try:
        return int(os.environ[UMASK_ENV], 8) if POSIX else -1
    except (KeyError, ValueError):
        return -1


def give_to_user(paths):
    """Files the hub has just made where the user asked (`hub shot --out`): permissions as the user's own programs would
    give (their umask), not the hub's closed ones. umask unknown (Windows) - as made."""
    mask = user_umask()
    for path in paths if mask >= 0 else ():
        if os.path.isfile(path) and not os.path.islink(path):
            os.chmod(path, 0o666 & ~mask)


def _open(path):
    """What to close: (path, previous mode) - a folder or a regular file open to the group or others; a link - no."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return None
    old = stat.S_IMODE(st.st_mode)
    return (path, old) if old & 0o077 else None


def _close(found, record_dir):
    """Record the previous modes first, then close: a walk interrupted midway (hubd exit) leaves nothing closed
    without a record. -> how many were closed."""
    found = [f for f in found if f]
    if not found:
        return 0
    try:
        fd = os.open(os.path.join(record_dir, RECORD), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"path": p, "mode": m}, ensure_ascii=False) + "\n" for p, m in found)
    except OSError:
        return 0   # nowhere to record - do not close: there would be no way to restore
    done = 0
    for path, old in found:
        try:
            os.chmod(path, old & ~0o077)
            done += 1
        except OSError:
            pass   # not ours: whatever stays open will be reported by unsafe()
    return done


def _root():
    """The hub runs as root: it may change anything on the machine — a settings folder of /etc would close the
    system's files."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _too_wide(folder):
    """The home folder, the root, the shared settings root (~/.config): other people's things live there."""
    real = os.path.realpath(folder)
    return real in {os.path.realpath(p) for p in (os.path.expanduser("~"), os.sep, os.path.expanduser("~/.config"),
                                                   os.path.dirname(platforms.current.config_dir()))}


def tighten(folder, deep=True, record_dir=None):
    """Close the folder and everything in it to its user (group and others lose all rights); links are not followed,
    session work (SKIP) keeps its modes. deep=False - the folder and what lies directly in it (the settings folder:
    HUB_CONFIG_DIR may name a folder shared with other programs). The previous modes go into the RECORD of record_dir
    (by default - this folder). -> how many entries changed."""
    if not POSIX or not os.path.isdir(folder) or _too_wide(folder):
        return 0
    folder = os.path.realpath(folder)   # a state folder moved out via a link: the real one is closed
    record_dir = record_dir or folder
    n = _close([_open(folder)], record_dir)
    for top, dirs, files in os.walk(folder):
        if top == folder:
            for d in [d for d in dirs if d in SKIP]:
                dirs.remove(d)
                n += _close([_open(os.path.join(top, d))], record_dir)
        n += _close([_open(os.path.join(top, name)) for name in dirs + files], record_dir)
        if not deep:
            dirs[:] = []
    return n


def tighten_file(path, record_dir=None):
    """One settings file outside the folder (HUB_CONFIG): 0600; its record goes to record_dir (by default - next to it)."""
    if not POSIX or not os.path.isfile(path):
        return 0
    return _close([_open(path)], record_dir or os.path.dirname(path) or ".")


def restore(folder):
    """Put back the modes that tighten() changed in this folder. -> how many."""
    path = os.path.join(folder, RECORD)
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return 0
    done = 0
    for row in reversed(rows):   # for a path the oldest mode wins
        try:
            if not os.path.islink(row["path"]):
                os.chmod(row["path"], int(row["mode"]))
                done += 1
        except (OSError, KeyError, ValueError, TypeError):
            pass
    os.replace(path, path + ".restored")
    return done


def unsafe(state_dir):
    """Where another user can still write: the state folder, the mailbox, the hook settings (the hook command runs in
    every session). Not empty after closing - the files are not ours, the core does not start."""
    if not POSIX:
        return []
    out = []
    for path in (state_dir, os.path.join(state_dir, "spool"), os.path.join(state_dir, "hooks-settings.json")):
        try:
            st = os.stat(path)   # where a link leads: that is where writes go
        except FileNotFoundError:
            continue
        if st.st_mode & 0o022 or st.st_uid != os.getuid():
            out.append(path)
    return out


def kept(config_path):
    """`perms.keep: true` in hub.json - do not close permissions. The file cannot be read - close."""
    from core import catalog
    try:
        cfg = catalog.read_settings(config_path)
    except (OSError, ValueError):
        return False
    section = cfg.get("perms") if isinstance(cfg, dict) else None
    return isinstance(section, dict) and section.get("keep") is True


def secure(state_dir, config_path):
    """At core start: close the state folder and the settings (their folder or a single file, if HUB_CONFIG names one) ->
    what is still open to others (unsafe): then the core does not start. The state folder itself and what lies directly
    in it are closed at once; deeper - in the background (a big hub has hundreds of thousands of files, and outsiders
    cannot get below a closed folder anyway). perms.keep - touch nothing; nor a state folder without the hub's marks (a
    wrong HUB_STATE_DIR naming a project); a settings folder other than the standard one - only with hub.json in it
    and not as root."""
    if not POSIX or kept(config_path):
        return []
    from core import uninstall
    if not uninstall.marked(state_dir):
        return unsafe(state_dir)
    tighten(state_dir, deep=False)
    record_dir = os.path.realpath(state_dir)   # all records go in the state folder: hub.json may sit in a foreign folder
    if os.environ.get("HUB_CONFIG"):
        tighten_file(config_path, record_dir)
    elif os.path.isfile(config_path) and not _root() or \
            os.path.realpath(os.path.dirname(config_path)) == os.path.realpath(platforms.current.config_dir()):
        tighten(os.path.dirname(config_path), deep=False, record_dir=record_dir)
    deep = threading.Thread(target=tighten, args=(state_dir,), name="perms", daemon=True)
    deep.start()
    _deep.append(deep)
    return unsafe(state_dir)


def wait():
    """Wait for the background walk of secure() (tests)."""
    while _deep:
        _deep.pop().join()


def open_up(state_dir, config_path):
    """`hub perms restore`: put back the previous modes of the state and settings folders and stop closing them
    (perms.keep: true in hub.json; for hub processes - after a restart) -> how many paths were restored."""
    from core import catalog
    with catalog.edit_settings(config_path) as cfg:
        section = cfg.get("perms") if isinstance(cfg.get("perms"), dict) else {}
        cfg["perms"] = {**section, "keep": True}
    return restore(state_dir)
