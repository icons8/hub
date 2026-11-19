"""The hub's own GitHub sign-in (installer spec §9): `gh` with GH_CONFIG_DIR=<state>/gh, so it never mixes with the
person's own gh and goes away with the hub. The token lies in that folder's hosts.yml, 0600 (`--insecure-storage`, as
accounts.json; no keychain window from a background service).

Git access through it — only for the git the core runs (codestore.quiet_env: the private modules check and install,
the update): GIT_CONFIG_PARAMETERS (as `git -c`) adds `credential.https://github.com.helper = !<gh> auth git-credential`. The
person's ~/.gitconfig is never changed (`gh auth setup-git` is not called; a `gh` that would write its git settings
writes them into the folder above, GIT_CONFIG_GLOBAL). Once the hub is signed in, its sign-in is the only helper for
https://github.com in these git processes (the person's own helpers are cleared for that host there, not in their
settings): their keychain neither stands in front of it nor receives its token. A hub without its own sign-in — as
before, the machine's access.

The stdlib only (and core/runtime): codestore imports this in every process that reaches the network with git, old page servers too."""
import os
import re
import shutil

DIR = "gh"
GH_ENV = "HUB_GH"   # the gh program; by default the one in PATH (the app puts its own there)
HELPER_KEY = "credential.https://github.com.helper"
HOST = re.compile(r"^github\.com:\s*$", re.M)
TOKEN = re.compile(r"^\s+oauth_token:\s*\S", re.M)


def state_dir():
    from core import runtime
    return runtime.state_dir()


def config_dir(state):
    return os.path.join(state, DIR)


def gh_program():
    """The gh to run, or None: HUB_GH (a path), else gh from PATH."""
    own = os.environ.get(GH_ENV)
    if own:
        return own if os.path.isfile(own) else None
    return shutil.which("gh")


def signed_in(state):
    """Whether the hub's own gh holds a github.com sign-in (hosts.yml with a token: we always sign in with
    --insecure-storage, so the token is in the file)."""
    if not state:
        return False
    try:
        with open(os.path.join(config_dir(state), "hosts.yml"), encoding="utf-8") as f:
            text = f.read(65536)
    except (OSError, UnicodeDecodeError):
        return False
    return bool(HOST.search(text) and TOKEN.search(text))


def login(state):
    """The GitHub login of the hub's sign-in (the `user:` field of hosts.yml), or None."""
    try:
        with open(os.path.join(config_dir(state), "hosts.yml"), encoding="utf-8") as f:
            text = f.read(65536)
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"^github\.com:\s*\n(?:[ \t]+.*\n)*?[ \t]+user:\s*([A-Za-z0-9-]{1,39})\s*$", text, re.M)
    return m.group(1) if m else None


def gh_env(state, base=None):
    """The environment for running the hub's gh itself: its folder, its git settings file, no update notices."""
    env = dict(os.environ if base is None else base)
    folder = config_dir(state)
    env.update(GH_CONFIG_DIR=folder, GIT_CONFIG_GLOBAL=os.path.join(folder, "gitconfig"), GH_NO_UPDATE_NOTIFIER="1",
               GH_NO_EXTENSION_UPDATE_NOTIFIER="1", GH_SPINNER_DISABLED="1", NO_COLOR="1")
    return env


def helper_value(gh):
    """`!'<gh>' auth git-credential` — git runs a `!` helper with sh (Git for Windows too: forward slashes there)."""
    path = gh.replace("\\", "/") if re.match(r"[A-Za-z]:\\", gh) else gh
    return "!'" + path.replace("'", "'\\''") + "' auth git-credential"


def _sq(text):
    """git's own quoting for GIT_CONFIG_PARAMETERS (sq_quote): '...' with ' as '\\''."""
    return "'" + text.replace("'", "'\\''") + "'"


def git_env(state, base=None, gh=None):
    """Extra environment for a git the core runs, or {} when the hub has no sign-in of its own (or no gh). The settings
    go the way `git -c` passes them (GIT_CONFIG_PARAMETERS, appended to what `base` has): an empty helper first clears
    the list git built for github.com so far — the person's osxkeychain or manager, and a github.com helper the caller
    set before — then the hub's sign-in answers alone: a login saved there for another account does not stand in front
    of it, and git does not hand the hub's token to their store after a fetch («store» goes to every helper on the
    list). Not GIT_CONFIG_COUNT/VALUE: the clearing value is an empty string, and an empty environment variable may
    read back as unset on Windows (git: «missing config value»)."""
    if not signed_in(state):
        return {}
    gh = gh or gh_program()
    if not gh:
        return {}
    base = os.environ if base is None else base
    ours = " ".join(_sq(f"{HELPER_KEY}={v}") for v in ("", helper_value(gh)))
    have = (base.get("GIT_CONFIG_PARAMETERS") or "").strip()
    return {"GH_CONFIG_DIR": config_dir(state), "GH_NO_UPDATE_NOTIFIER": "1",
            "GIT_CONFIG_PARAMETERS": f"{have} {ours}" if have else ours}
