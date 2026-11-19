"""The environment's installer (one-line install spec §4.5): what deploy/oneline/install.sh and install.ps1 hand over
to once the environment is unpacked (contract 1, deploy/oneline/README.md). It lies in the environment folder next to
launch.py and, like it, is frozen at build time; an environment stays on a machine across many releases, so this file
only gets the environment ready and hands the work to the newest installer there is: deploy/oneline/setup.py of the
hub's current code layer when that is at least the environment's own seed and unmodified, else of the seed. That one
reads the channel's latest release and, when it is newer than itself, hands over again to the latest one's — the logic
of an install is always the release's, never the environment's.

What is the environment's, done here once (each step is harmless again):
- Windows: the links the factory took out of the archive (links.json: hard links, msys's own links — app.py);
- the bytecode of the environment's Python standard library (the folder is read-only next, its Python would compile it
  in memory at every start);
- the environment read-only (files and folders: the sign "not to be edited", as a code layer's).

    <env>/runtime/python/bin/python3 -I -B <env>/install.py --contract 1 --env <env> … (the README's contract 1)
    <env>/runtime/python/bin/python3 -I -B <env>/install.py prepare --env <env>   (the environment's steps alone: the
                                                                                     update's new environment)
The arguments go on to setup.py as they are; its exit code is this one's. The stdlib only; nothing of the hub."""
import json
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = ("1",)
WINDOWS = os.name == "nt"
SETUP = os.path.join("deploy", "oneline", "setup.py")
SEALED = ".agents-hub-sealed"   # in the environment's folder: the steps above are done


def say(text):
    print(f"Hub by Icons8 install: {text}", file=sys.stderr, flush=True)


def log(text):
    path = os.environ.get("HUB_INSTALL_LOG")
    if not path:
        return
    try:
        import time
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} install.py: {text}\n")
    except OSError:
        pass


def value(argv, flag):
    """The value after flag, or None."""
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def state_dir(argv, environ=None):
    """The hub's state folder as the core finds it (core/daemon.default_state_dir): --state, HUB_STATE_DIR, the
    installers' mark, the OS's default. Only to find the current layer: setup.py decides with the core itself."""
    env = os.environ if environ is None else environ
    if value(argv, "--state"):
        return value(argv, "--state")
    if env.get("HUB_STATE_DIR"):
        return env["HUB_STATE_DIR"]
    if WINDOWS:
        return os.path.join(env.get("LOCALAPPDATA") or os.path.expanduser("~"), "hub-core")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for mark in (os.path.join(xdg, "hub-core", "state-dir"), os.path.expanduser("~/.config/hub-core/state-dir")):
        try:
            with open(mark, encoding="utf-8") as f:
                got = f.readline().strip()
        except OSError:
            continue
        if got:
            return got
    return os.path.expanduser("~/.hub-core")


def _manifest(root):
    try:
        with open(os.path.join(root, "MANIFEST.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return None
    return man if isinstance(man, dict) and isinstance(man.get("version"), str) else None


def _appcode():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hub_shell_appcode", os.path.join(HERE, "appcode.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def code_to_run(state, env=HERE):
    """The code whose setup.py installs: the current layer of this state folder when it has one, is not older than the
    environment's seed and is as released (an edited layer is the installer's to put back, not to run), else the seed."""
    seed = os.path.join(env, "code-seed")
    link = os.path.join(state, "code")
    ac = _appcode()
    try:
        name = ac.current(link, os.path.join(state, "layers"))
    except OSError:
        name = None
    if not name:
        return seed
    layer = os.path.join(state, "layers", name)
    have, mine = _manifest(layer), _manifest(seed)
    if not have or not os.path.isfile(os.path.join(layer, SETUP)):
        return seed
    if mine and ac.version_key(have["version"]) < ac.version_key(mine["version"]):
        return seed
    try:
        if ac.diff(layer, ac.load(layer)):
            return seed
    except (OSError, ac.Bad):
        return seed
    return layer


def read_only(root):
    """Files and folders of the environment without the write bit (Windows: the read-only attribute on files; a folder's
    means nothing there). Links are not followed."""
    for d, dirs, names in os.walk(root, topdown=False):
        for n in names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                continue
            mode = os.lstat(p).st_mode
            os.chmod(p, stat.S_IMODE(mode) & ~0o222)
        if not WINDOWS:
            mode = os.lstat(d).st_mode
            os.chmod(d, stat.S_IMODE(mode) & ~0o222)


def stdlib(env):
    """The folder of the environment's Python standard library."""
    if WINDOWS:
        return os.path.join(env, "runtime", "python", "Lib")
    lib = os.path.join(env, "runtime", "python", "lib")
    names = [n for n in os.listdir(lib) if n.startswith("python3")] if os.path.isdir(lib) else []
    return os.path.join(lib, sorted(names)[-1]) if names else lib


def prepare(env, run=subprocess.run):
    """The environment's own steps, once (SEALED marks them done). -> None, or why it is not ready."""
    if os.path.exists(os.path.join(env, SEALED)):
        return None
    if WINDOWS and os.path.isfile(os.path.join(env, "app.py")):
        sys.path.insert(0, env)
        try:
            import app
            made = app.restore_links(env)
        except Exception as e:  # noqa: BLE001 — a person reads it
            return f"the links of the environment are not restored: {e}"
        finally:
            sys.path.remove(env)
        log(f"links of the environment: {made} restored")
    lib = stdlib(env)
    if os.path.isdir(lib):
        r = run([sys.executable, "-I", "-m", "compileall", "-q", "-j", "0", lib], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, errors="replace", timeout=900)
        if r.returncode:   # a module of the tests that does not compile is not a failure of the environment
            log(f"compileall: exit {r.returncode}: {(r.stdout or '')[-300:]}")
    with open(os.path.join(env, SEALED), "w", encoding="utf-8") as f:
        f.write("1\n")
    read_only(env)
    return None


def main(argv, run=subprocess.run):
    if argv[:1] == ["prepare"]:   # only the environment's own steps: the update's new environment (modules/update/layer.py)
        try:
            why = prepare(os.path.abspath(value(argv, "--env") or HERE), run)
        except OSError as e:
            why = f"{type(e).__name__}: {e}"
        if why:
            say(why)
        return 1 if why else 0
    if value(argv, "--contract") not in CONTRACTS:
        say(f"this environment's installer speaks contract {', '.join(CONTRACTS)}, the script asked for "
            f"{value(argv, '--contract')!r}: run the install line again (it brings a matching script)")
        return 64
    env = os.path.abspath(value(argv, "--env") or HERE)
    try:
        why = prepare(env, run)
    except OSError as e:
        why = f"{type(e).__name__}: {e}"
    if why:
        say(why)
        log(why)
        return 1
    code = code_to_run(state_dir(argv))
    setup = os.path.join(code, SETUP)
    if not os.path.isfile(setup):
        say(f"the environment's code has no installer ({setup}): the delivery server has an older build")
        return 1
    log(f"setup.py of {code}")
    cmd = [sys.executable, "-I", "-B", *(["-X", "utf8"] if WINDOWS else []), setup, *argv]
    try:
        return run(cmd).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
