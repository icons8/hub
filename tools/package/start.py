"""The start of a hub of the one-line install (spec §4.5 step 7): what its service runs, from the environment folder
through the hub's pointer <state>/app — launchd (through "Agents Hub.app"), systemd --user, the scheduler task's
hub-up.cmd. Frozen at build time with the environment, like launch.py next to it: an update of the code cannot break it.

  start.py [--prepare]

Every start: the layer the hub runs (launch.py: the first layer, a later one an update left for the restart, PACKAGED of
the environment into it, the quick check and the watchdog of a modified layer; layers/deliver.json stays the
installer's — the key of the install line), a replacement of the environment and a trial of an update whose watchdog a
reboot ended (launch.py), then hub-up of <state>/code in place of this process with the environment's programs first in
PATH. --prepare: all of it but hub-up (Windows: hub-up.cmd starts hub-up itself, after the environment's variables).
The state folder is HUB_STATE_DIR (the service sets it), else the folder this file's pointer lies in. The stdlib only."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # <state>/app when the service runs it through the pointer
sys.path.insert(0, HERE)   # -I leaves the script's folder out of sys.path
import launch  # noqa: E402

WINDOWS = os.name == "nt"
# the environment's programs first in PATH (Mac: git through the wrapper in <state>/bin — dugite's git needs its
# GIT_EXEC_PATH; Linux takes the system's git; Windows: hub-up.cmd sets its own PATH)
BINS = ("runtime/node/bin", "runtime/gh/bin", "runtime/ffmpeg/bin")


def state_dir():
    if os.environ.get("HUB_STATE_DIR"):
        return os.environ["HUB_STATE_DIR"]
    return os.path.dirname(HERE) if os.path.basename(HERE) == "app" else os.path.expanduser("~/.hub-core")


def env_path(env, state):
    """PATH with <state>/bin and the environment's programs first (once, however often it is applied)."""
    first = [os.path.join(state, "bin")] + [os.path.join(HERE, b) for b in BINS if os.path.isdir(os.path.join(HERE, b))]
    rest = [p for p in (env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep) if p and p not in first]
    return os.pathsep.join(first + rest)


def python():
    return os.path.join(HERE, "runtime", "python", "python.exe" if WINDOWS else os.path.join("bin", "python3"))


def prepare(state):
    """-> the code folder, or None when it is not ready (said on stderr: the service's log)."""
    try:
        code = launch.ensure_layer(os.path.join(HERE, "code-seed"), os.path.join(HERE, "PACKAGED"), state,
                                   own_deliver=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"Hub by Icons8: the hub's code is not ready: {e}", file=sys.stderr, flush=True)
        return None
    except Exception as e:  # noqa: BLE001 — appcode.Bad and the like: the service tries again, the log says why
        print(f"Hub by Icons8: the hub's code is not ready: {e!r}", file=sys.stderr, flush=True)
        return None
    launch.resume_trial(state, python())
    return code


def main(argv, execve=os.execve):
    state = state_dir()
    if not WINDOWS:
        os.environ["PATH"] = env_path(os.environ, state)
    code = prepare(state)
    if code is None:
        return 1   # launchd and systemd start the service again
    if argv[:1] == ["--prepare"]:
        return 0
    os.chdir(code)
    py = python()
    execve(py, [py, os.path.join(code, "bin", "hub-up")], dict(os.environ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
