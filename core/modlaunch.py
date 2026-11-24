"""The launch.env point (Module API, spec §2.4): a module adds environment and claude arguments on every session launch.

Order at the host (hosts/streamjson.py: launch_env): core environment → module additions in load order (a later one
overrides an earlier one, an empty string removes the variable) → core removals and the pool account login → the login
rule as the last step, on the final environment.

Login rule: a module set a name that sends claude to another address (ADDRESS: ANTHROPIC_BASE_URL, provider
addresses, a socket, a login address, cloud region and address), and its value stayed in the environment — any login
key will go there too (checked with a real claude: without its own key it carries there the pool token, an inherited
key, the machine's saved login, and in provider mode the owner's cloud key; ANTHROPIC_CUSTOM_HEADERS too). So the core
removes all login keys and the provider mode (login_name) and leaves only those set by that module; a module without
its own key (AUTH_ENV) is a schema failure, its addition is not applied at all (headers without a key do not count as
a key). Two modules redirect claude by different names — the later one is rejected. The session network (proxy, CA,
TLS) and the provider choice are closed to modules entirely (modules.env_denied).
"""
import sys

from core import i18n, modrun, modules

BASE_URL = "ANTHROPIC_BASE_URL"
HEADERS = "ANTHROPIC_CUSTOM_HEADERS"   # headers of every claude request: they may carry a login (the owner's own gateway)
# claude address — words of the ANTHROPIC_*/CLAUDE_* name (ANTHROPIC_BEDROCK_BASE_URL, ANTHROPIC_UNIX_SOCKET,
# CLAUDE_CODE_CUSTOM_OAUTH_URL, ANTHROPIC_FOUNDRY_RESOURCE…); in provider mode — also any name of the cloud families
# (the region is part of the host name, AWS_ENDPOINT_URL*, AWS_CONFIG_FILE with endpoint_url): without the mode claude does not read them
ADDRESS = {"URL", "URI", "HOST", "HOSTS", "ENDPOINT", "REGION", "RESOURCE", "SOCKET"}
CLOUD = ("AWS_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_", "AZURE_", "CLOUD_ML_", "VERTEX_")
OWN = ("ANTHROPIC_", "CLAUDE_")
SECRET = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSPHRASE", "_CREDENTIALS", "_TOKEN_FILE", "_FILE_DESCRIPTOR")
# claude login keys (the API key outranks the login token): set by the session profile (core/profiles.py), removed by the login rule
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def adds(mods, payload):
    """[(module, {env?, args?})] from ready modules with the launch.env point, in load order; a failure — without it."""
    out = []
    for name in mods.answering("launch.env"):
        man = modrun.manifest_of(mods.state_dir, name)
        got = mods.call(name, "launch.env", payload, ctx={"env": man.get("env") or []})
        if got is not None:
            out.append((name, got))
    return out


def missing(mods, added):
    """Modules with launch.env this launch of a session goes without, in load order: coming up after a hub or module
    restart past the spawn's wait (one that never answered hello is known by its launch.env right), paused, stopped, or
    the call failed. added is what modules answered, before the login rule: a module it refuses is not here — its
    module.error says why, every session goes without it alike, and a relaunch would be refused the same way. hubd puts
    them into the spawn fact (host.done without); the core relaunches the free session once such a module answers hello
    again (reducer: env_stale)."""
    got = {name for name, _ in added}
    try:
        declared = mods.declared("launch.env", cap="launch.env")
    except Exception as e:  # noqa: BLE001 — the trace must not hold the launch itself (records unreadable)
        print(f"modlaunch: which modules the session goes without — {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []
    return [name for name in declared if name not in got]


def apply(env, add):
    """Module environment on top of env (modifies env): an empty value removes the variable."""
    for k, v in (add or {}).items():
        if v == "":
            env.pop(k, None)
        else:
            env[k] = v
    return env


def provider(env):
    """A claude provider (Bedrock, Vertex…) is enabled in the environment — the login is cloud."""
    return any(modules.provider_name(k) and str(v).strip().lower() in ("1", "true", "yes", "on")
               for k, v in env.items())


def address(key, cloud=False):
    """The name sends claude to another address (login rule); cloud — a provider is enabled."""
    key = key.upper()
    return (key.startswith(OWN) and bool(set(key.split("_")) & ADDRESS)) or (cloud and key.startswith(CLOUD))


def login_name(key):
    """Claude login: keys and headers (AUTH_ENV, HEADERS, ANTHROPIC_*/CLAUDE_* secrets) and provider mode — the cloud
    login is taken from machine files. The core also withholds these from module processes (modrun.launch)."""
    key = key.upper()
    return (key in AUTH_ENV or key == HEADERS or (key.startswith(OWN) and key.endswith(SECRET))
            or modules.provider_name(key))


def owners(env, added):
    """Modules whose claude address name stayed in the final environment (login rule; the account gateway does not
    take such sessions — modules/gateway)."""
    cloud = provider(env)
    return [name for name, add in added
            if any(v and address(k, cloud) and env.get(k) == v for k, v in (add.get("env") or {}).items())]


def login(env, added):
    """The login rule on the final environment (modifies env). -> (name of the module whose addition is rejected, why:
    "key" — address without its own key, "second" — a second module with an address, the modules with an address before it) or None."""
    taken = owners(env, added)
    if not taken:
        return None
    if len(taken) > 1:
        return taken[-1], "second", taken[:-1]
    own = {k: v for k, v in (dict(added)[taken[0]].get("env") or {}).items() if login_name(k) and v}
    if not set(own) & set(AUTH_ENV):
        return taken[0], "key", []
    for k in list(env):
        if login_name(k):   # Windows: environment names are case-insensitive
            env.pop(k)
    env.update(own)
    env.pop("HUB_ACCOUNT", None)   # the session is not on a pool account: do not attribute the account's limits to it
    return None


def refusal(why, others=()):
    if why == "second":
        return i18n.t("core.modlaunch.second", others=", ".join(others))
    return i18n.t("core.modlaunch.no_key", base_url=BASE_URL, keys=", ".join(AUTH_ENV))
