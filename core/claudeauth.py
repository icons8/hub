"""Whether Claude Code is signed in — its own `claude auth status` (JSON). The page's «Setup» (modules/setup) and the host
before a new hub's spawn (hosts/streamjson signed_in) ask the same. No model call and no network for the answer (probe on
2.1.309): no sign-in is exit 1 with loggedIn false; a saved login, a key in its settings or the environment, a cloud
provider — loggedIn true."""
import json
import re
import subprocess
import tempfile

KEYS = ("loggedIn", "authMethod", "email", "orgName", "subscriptionType")


def status(claude, env=None, cwd=None, timeout=60):
    """-> {loggedIn, authMethod, email?, …} or None when it gives no JSON (an older Claude Code, a crash, a timeout):
    unknown, not «signed out». cwd — the folder whose project settings count (a session's). The output goes to a file,
    not a pipe: on Windows a timeout kills the program, and a descendant holding a pipe (claude.cmd → node) would keep
    the read waiting with no limit — in hubd's tick."""
    import platforms
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(list(claude) + ["auth", "status"], stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.DEVNULL, env=env, cwd=cwd, **platforms.current.apart())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                platforms.current.kill_detached(p, hard=True)   # with its descendants (its own group: apart)
            except Exception:  # noqa: BLE001 — «unknown», never a failed spawn
                p.kill()
            p.wait()
            return None
        out.seek(0)
        text = out.read().decode("utf-8", "replace")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        got = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(got, dict) or not isinstance(got.get("loggedIn"), bool):
        return None
    return {k: got[k] for k in KEYS if isinstance(got.get(k), (str, bool))}
