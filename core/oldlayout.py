"""Former Linux service layout → a single service with hub-up. Before 06.10 the installer set up a systemd --user
service per part (hubd, hub-web, hub-scheduler…) without hub-up; a code update does not change it. It lacks the parts
that exist only under hub-up (statistics, conduit, update, watchdog…), and nobody can execute `hub restart`: the core
stays on old code, and the system agents (core/system.py) do not appear (09.10: this was the case on Linux hubs).

The migration is deploy/install.sh in its own unit (systemd-run): it installs agents-hub.service and removes the part
services, including the core's service; the installer would be killed together with it if it stayed in its process
group. Two entries:
- the core came up under such a service (platforms old_service) — immediately;
- `hub restart` (the whole hub; the command always runs from disk, with new code, even if the core is still old) — when
  the core decides it is time (puts the restart file, sessions are free): the installer waits for that file."""
import os
import subprocess
import time

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = "service-migrate"   # when the core last migrated the hub (no more than once an hour: the service will bring it up again)
WAIT_S = 24 * 3600          # the installer waits for a restart no longer than this
SINCE = "service-migrate-since"   # marker: when a restart was requested (its time is the request time)


def installer():
    return os.path.join(REPO, "deploy", "install.sh")


def command(state_dir, wait=False, now=None):
    """systemd-run with the installer: the same state directory and PATH (the installer will find the same claude)."""
    from core.restart import FLAG
    env = {"HUB_STATE_DIR": state_dir, "HUB_INSTALL_NO_WAIT": "1", "PATH": os.environ.get("PATH", ""),
           **{k: os.environ[k] for k in ("HOME", "XDG_CONFIG_HOME", "HUB_CLAUDE") if os.environ.get(k)}}
    head = ["systemd-run", "--user", "--collect", "--quiet",
            "--unit=agents-hub-migrate" + ("-wait" if wait else f"-{int(now or time.time())}"),
            *(f"--setenv={k}={v}" for k, v in env.items())]
    if not wait:
        return head + ["sh", installer()]
    # the restart file must be no older than the request (SINCE marker): one left from an earlier request does not count, sessions may be busy
    loop = (f'i=0; until [ -f "$1" ] && [ ! "$1" -ot "$2" ]; do i=$((i+1)); [ $i -gt {WAIT_S // 2} ] && exit 1; '
            'sleep 2; done; exec sh "$3"')
    return head + ["sh", "-c", loop, "migrate", os.path.join(state_dir, FLAG), os.path.join(state_dir, SINCE),
                   installer()]


def _start(cmd, run, say):
    try:
        r = run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        say(f"the migration did not start: {type(e).__name__} — run sh {installer()}")
        return False
    if r.returncode != 0 and "already exists" not in (r.stderr or ""):   # one is already waiting — a second is not needed
        say(f"the migration did not start (code {r.returncode}): {(r.stderr or '').strip()[:300]} — run sh {installer()}")
        return False
    return True


def migrate(state_dir, run=subprocess.run, now=None):
    """The core runs under a former-layout service — the installer now. True — started."""
    unit = platforms.current.old_service(REPO)
    if not unit:
        return False
    now = time.time() if now is None else now
    stamp = os.path.join(state_dir, STAMP)
    try:
        with open(stamp) as f:
            if now - float(f.read().strip() or 0) < 3600:
                return False
    except (OSError, ValueError):
        pass
    with open(stamp, "w") as f:
        f.write(str(now))
    say = lambda text: print(f"hubd: {text}", flush=True)  # noqa: E731
    say(f"the former service layout ({os.path.basename(unit)} without hub-up) — moving the hub to a single service "
        "agents-hub.service: deploy/install.sh")
    return _start(command(state_dir, now=now), run, say)


def on_restart(state_dir, since, run=subprocess.run):
    """`hub restart` of the whole hub on the former layout (since — request time): the installer will wait until the core
    decides to restart. A line for whoever asked, or None (the layout is new)."""
    if not platforms.current.old_units(REPO, state_dir):
        return None
    mark = os.path.join(state_dir, SINCE)
    open(mark, "w").close()
    os.utime(mark, (since, since))
    from core import i18n
    if not _start(command(state_dir, wait=True), run, lambda text: None):
        return i18n.t("cli.oldlayout.nobody", installer=installer())
    return i18n.t("cli.oldlayout.waiting")
