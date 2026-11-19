"""The hub's terminal commands: hub and hub-telegram in ~/.local/bin, wrappers that run them with the hub's Python
(deploy/install.sh, deploy/mac/link-commands.sh). The name may already belong to another program (GitHub's `hub`, a
link from a dotfiles repo): it is left as it is, the file a link points to too, with a line on how to reach the hub.
Ours: a hub's wrapper (of any copy of the code), a link to a hub's bin/<command> (the Linux install before 06.10 made
one), a broken link into some bin/<command>. The wrapper is written next to the name and renamed over it, so a link is
replaced, never written through (tests research 03.11: the write went through the link into the other program's file).
  commands.py REPO PYTHON [PATH_FIRST]   PATH_FIRST — folders the commands put first in PATH (the Mac app's programs)
  commands.py --instance NAME REPO PYTHON STATE CONFIG   hub-NAME of an instance (core/instance.py) and the
                                         <state>/bin/hub its sessions find first in PATH"""
import os
import re
import sys

NAMES = ("hub", "hub-telegram")


def wrapper(repo, py, cmd, first=""):
    if first:
        return f'#!/bin/sh\nPATH="{first}:$PATH" exec "{py}" "{repo}/bin/{cmd}" "$@"\n'
    return f'#!/bin/sh\nexec "{py}" "{repo}/bin/{cmd}" "$@"\n'


# the first line of the `hub` that core/uninstall.py leaves when the main hub is removed and other hubs stay (it names
# them): the main hub's install puts its own `hub` over it
STUB = "# agents-hub: the main hub of this computer is removed"


def instance_wrapper(repo, py, state, config, guard=True, cmd="hub"):
    """`hub-<name>` of an instance (core/instance.py): the hub with this state and settings folder. From a session of
    another hub it refuses (guard): the core of this hub would take a process without its HUB_SESSION for the owner —
    the sessions of another hub reach it through the bus. Without the guard — <state>/bin/hub of its own sessions."""
    check = (f'if [ -n "$HUB_SESSION" ] && [ "$HUB_STATE_DIR" != "{state}" ]; then\n'
             f'  echo "this is another hub of this computer: its sessions are reached through the bus (hub bus)" >&2\n'
             f'  exit 2\nfi\n') if guard else ""
    return (f'#!/bin/sh\n# agents-hub instance\n{check}'
            f'HUB_STATE_DIR="{state}" HUB_CONFIG_DIR="{config}" exec "{py}" "{repo}/bin/{cmd}" "$@"\n')


def leads_to(path):
    """The state folder a hub's command names (HUB_STATE_DIR="…" in it), or None."""
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'HUB_STATE_DIR="([^"\n]+)" ', f.read(4096))
    except (OSError, UnicodeDecodeError):
        return None
    return m.group(1) if m else None


def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def of_another(path, state):
    """The file at this name is a hub's command that leads to another state folder: another hub's, never replaced by
    this one (hub-<a>-<b> of one instance vs hub-<a> of another). The same folder spelled otherwise is this one."""
    got = leads_to(path)
    return got is not None and not _same_dir(got, state)


def _is_wrapper(path, cmd):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(4096)
    except (OSError, UnicodeDecodeError):
        return False
    if text.startswith("#!/bin/sh\n" + STUB + "\n") and cmd == "hub":
        return True
    if text.startswith("#!/bin/sh\n# agents-hub instance\n") and cmd.startswith("hub"):
        return True
    if re.fullmatch(r'#!/bin/sh\nHUB_STATE_DIR="[^"\n]*" HUB_CONFIG_DIR="[^"\n]*" exec "[^"\n]*" "[^"\n]*/bin/hub" '
                    r'"\$@"\n', text) and cmd.startswith("hub-"):
        return True   # the Mac installer's hub-<name> before the instances
    return re.fullmatch(r'#!/bin/sh\n(PATH="[^"\n]*" )?exec "[^"\n]*" "[^"\n]*/bin/' + re.escape(cmd) + r'" "\$@"\n',
                        text) is not None


def ours(path, cmd):
    """The file at this name is the hub's to replace."""
    if os.path.islink(path):
        target = os.path.realpath(path)
        if not os.path.lexists(target):
            return target.endswith(f"/bin/{cmd}")
        if os.path.basename(target) == cmd and os.path.isfile(os.path.join(os.path.dirname(target), "hub-up")):
            return True
        return _is_wrapper(target, cmd)
    return os.path.isfile(path) and _is_wrapper(path, cmd)


def put(folder, cmd, text, say=print):
    """-> True: the wrapper is at folder/cmd; False: the name is another program's, left alone."""
    dst = os.path.join(folder, cmd)
    if os.path.lexists(dst) and not ours(dst, cmd):
        say(dst)
        return False
    tmp = os.path.join(folder, f".{cmd}.{os.getpid()}.hub-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o777)   # executable, as far as the umask lets
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(argv):
    folder = os.path.join(os.path.expanduser("~"), ".local", "bin")
    if argv[:1] == ["--instance"] and len(argv) == 6:   # --instance NAME REPO PYTHON STATE CONFIG
        name, repo, py, state, config = argv[1:]
        os.makedirs(folder, exist_ok=True)
        # the sessions of the instance find their own hub first in PATH: <state>/bin/hub, without the guard
        os.makedirs(os.path.join(state, "bin"), exist_ok=True)
        put(os.path.join(state, "bin"), "hub", instance_wrapper(repo, py, state, config, guard=False))
        ok = True
        for cmd, at in (("hub", f"hub-{name}"), ("hub-telegram", f"hub-telegram-{name}")):
            if os.path.lexists(os.path.join(folder, at)) and of_another(os.path.join(folder, at), state):
                print(f"{os.path.join(folder, at)} is the command of another hub: leaving it", file=sys.stderr)
                ok = False
                continue
            ok = put(folder, at, instance_wrapper(repo, py, state, config, cmd=cmd),
                     say=lambda dst, cmd=cmd: print(
                         f"{dst} is another program with this name, leaving it as it is; the hub from a terminal: "
                         f'HUB_STATE_DIR="{state}" HUB_CONFIG_DIR="{config}" "{py}" "{repo}/bin/{cmd}"',
                         file=sys.stderr)) and ok
        return 0 if ok else 3
    repo, py, first = argv[0], argv[1], (argv[2] if len(argv) > 2 else "")
    os.makedirs(folder, exist_ok=True)
    for cmd in NAMES:
        put(folder, cmd, wrapper(repo, py, cmd, first),
            say=lambda dst, cmd=cmd: print(f"{dst} is another program with this name, leaving it as it is; the hub "
                                           f'from a terminal: "{py}" "{repo}/bin/{cmd}"', file=sys.stderr))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
