"""Settings that change how the core behaves, watched for edits made outside the page (dev-guard spec §5.2, slice 1b).

A colleague's hub moved every session and every service call of the core to the most expensive model by a line in the
owner's settings.json, and nothing showed it. The core keeps the last accepted state of a fixed list of keys — quietly:
nothing of it is on the page yet (the owner put the block "changed outside this page" and a price note on hold, 06.11), it
is the base for a report and for what comes next:

- hub.json: models and pairs (`model`, `effort`, `executors.*`, `critic.claude_model`), what sessions read and where
  (`session_env_values`, `setting_sources`, `claude_config_dir`), who reaches the page (`tailnet.*`, `access.*`) and the
  names of `user_env`;
- the owner's Claude Code settings.json: the `env` values the hub passes to sessions (catalog.USER_ENV and the names of
  hub.json `user_env`); when `setting_sources` has `user` (sessions read that file whole), also its `model` and every
  model name of its `env`. A model name is kept as it is; any other value (a token, an address, a proxy) — only as a
  fingerprint. So is a hub.json value under a name that looks secret and a value too long to show; a page that shows rows
  must drop even the fingerprint (a guessable address must not be tried against it) and show only "changed".

How it goes (hubd, daemon.watch_config; the fold, reducer):
- no `config_seen` in the state (the first check after an update): everything is accepted as it is — `config.seen`
  with `first`, no rows;
- the page saved a watched key (`settings.changed` from the page: channel web, the owner): the fold marks it, the next
  check accepts the file's value silently (`config.seen`), clears the rows the marks cover and spends every mark;
- any other change (the file by hand, a session, a command in a terminal) that holds for SETTLE seconds — the page writes
  the file a moment before its event — gives `config.changed_outside {key, old, new[, hidden]}` and a row in `st["config_seen"]["rows"]`;
  the row keeps the first old value and leaves when the value comes back or the page sets the key; a row older than
  ROW_DAYS goes at the next outside change.
Nothing is held: the new value works. Both facts are hubd's own: daemon.spooled refuses them from the mailbox. Only the
stdlib here: the reducer imports this file."""
import hashlib
import json
import math
import os
import re

FACTS = ("config.seen", "config.changed_outside")   # written by hubd only (daemon.spooled refuses them from the mailbox)

# hub.json keys that change how the core works (spec §5.2); the page's `executors.parallel` is under executors
HUB_KEYS = ("model", "effort", "executors", "critic.claude_model", "session_env_values", "setting_sources",
            "claude_config_dir", "tailnet", "access", "user_env")
FLAT = ("executors", "session_env_values", "tailnet", "access")   # sections watched leaf by leaf: one row per value
SETTINGS = "settings."   # the key prefix of a value from the owner's settings.json
ENV = SETTINGS + "env."   # ... of its env name
SECRET_NAME = re.compile(r"token|secret|passw|api_?key|auth_?key|credential|private", re.I)
SHOWN_MAX = 200   # a longer value (as JSON) goes into the journal as a fingerprint
DEPTH = 6         # how deep a section is unfolded; deeper — the whole value
SETTLE = 5        # seconds a change must hold before it counts as outside the page (the page's event comes after its write)
EVERY = 60        # seconds between checks when neither file changed (a file system that keeps the mtime)
MAX_ROWS = 30     # rows "changed outside the page" kept; the oldest goes first
ROW_DAYS = 7      # days a row is kept; an older one goes at the next outside change


def env_names(cfg, base):
    """The settings.json env names the hub passes to sessions: base (catalog.USER_ENV) and the strings of hub.json
    `user_env` (any other shape — none)."""
    more = cfg.get("user_env") if isinstance(cfg, dict) else None
    return set(base) | ({n for n in more if isinstance(n, str)} if isinstance(more, list) else set())


def _model(name):
    """An env name whose value is a model name (catalog.model_remap, the one rule): kept as it is. Imported here, not at
    the top: a process of the previous release loads this file with the catalog it already has in memory."""
    from core import catalog
    return catalog.model_remap(name)


def user_sources(cfg):
    """hub.json `setting_sources` names `user`: sessions read the owner's settings.json whole."""
    value = cfg.get("setting_sources") if isinstance(cfg, dict) else None
    return isinstance(value, str) and "user" in [p.strip() for p in value.split(",")]


def read_settings(path, names, user=False):
    """{key: value} of a Claude Code settings.json the hub's sessions get: the listed `env` names as catalog.user_env
    passes them (text), and with user (user_sources) its `model` and every model name of `env` too. No file — {}; a file
    that cannot be read or parsed — None (its values stay as they were: a typo is not a removal of every value)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    data = data if isinstance(data, dict) else {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    out = {ENV + k: str(v) for k, v in env.items() if v is not None and (k in names or user and _model(k))}
    if user and data.get("model") is not None:
        out[SETTINGS + "model"] = data["model"]
    return out


def fingerprint(value):
    return {"fp": hashlib.sha256(_dump(value).encode()).hexdigest()[:12]}


def hidden(value):
    return isinstance(value, dict) and set(value) == {"fp"}


def _dump(value):
    """JSON text of a value from a settings file (its length and its fingerprint)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _clean(value):
    """The value as the journal can hold it: plain JSON, a float that is not finite — as text."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _shown(name, value, secret=False):
    if secret or SECRET_NAME.search(name) or len(_dump(value)) > SHOWN_MAX:
        return fingerprint(value)
    return _clean(value)


def _leaves(key, value, out, depth=0):
    if isinstance(value, dict) and depth < DEPTH:
        for k, v in value.items():
            _leaves(f"{key}.{k}", v, out, depth + 1)
    elif value is not None:
        out[key] = _shown(key, value)   # the whole path: a secret under a secret-named section is hidden too


def snapshot(cfg, settings):
    """{key: value as kept} of the watched keys now; an absent key or null — no entry. cfg — hub.json as written (any
    shape), settings — read_settings' {key: value}."""
    out = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    for key in HUB_KEYS:
        box, parts = cfg, key.split(".")
        for part in parts[:-1]:
            box = box.get(part) if isinstance(box, dict) else None
        value = box.get(parts[-1]) if isinstance(box, dict) else None
        if key in FLAT:
            _leaves(key, value, out)
        elif value is not None:
            out[key] = _shown(key, value)
    for key, value in (settings or {}).items():
        name = key[len(ENV):] if key.startswith(ENV) else None
        out[key] = _shown(key, value, secret=name is not None and not _model(name))
    return out


def watched(what):
    """A page key (settings.changed `what`) that is one of the watched keys or inside one."""
    return isinstance(what, str) and any(what == k or what.startswith(k + ".") for k in HUB_KEYS)


def covered(key, marks):
    """The key was set from the page: a mark is the key, a part of it or a section above it; a settings.json value comes
    and goes with `setting_sources` (user_sources), so the page's `setting_sources` covers those too."""
    return any(key == m or key.startswith(m + ".") or m.startswith(key + ".") for m in marks) or \
        key.startswith(SETTINGS) and "setting_sources" in marks


def selected(key, names, user):
    """A settings.json key read_settings would take now (with these names and user_sources)."""
    if key == SETTINGS + "model":
        return user
    name = key[len(ENV):] if key.startswith(ENV) else None
    return name is not None and (name in names or user and _model(name))


def changed(seen, now):
    """The keys whose value differs between the accepted state and now, sorted."""
    return sorted(k for k in set(seen) | set(now) if seen.get(k) != now.get(k))


# --- the fold (core/reducer.py) ------------------------------------------------------------------------------------

def page(st, ev, data):
    """settings.changed from the page (the web channel, the owner's word) of a watched key: the next check accepts the
    file's value without a fact. The same fact from anyone else (a session, a module) marks nothing: its edit would go
    unseen."""
    what = data.get("what")
    if st.get("config_seen") is not None and watched(what) and data.get("channel") == "web" and data.get("owner") is True:
        st["config_seen"].setdefault("page", {})[what] = ev["ts"]


class Bad(ValueError):
    """A config fact of the wrong shape (the reducer rejects it)."""


def fold(st, ev, data):
    if ev["type"] == "config.seen":
        values = data.get("values")
        if not isinstance(values, dict):
            raise Bad("config.seen: values must be an object")
        if data.get("first") or st.get("config_seen") is None:
            st["config_seen"] = {"values": dict(values), "since": ev["ts"]}
            return
        seen, marks = st["config_seen"], [m for m in data.get("page") or [] if isinstance(m, str)]
        for key, value in values.items():
            _put(seen["values"], key, value)
            (seen.get("rows") or {}).pop(key, None)
        for key in [k for k in seen.get("rows") or {} if covered(k, marks)]:
            seen["rows"].pop(key)   # the page set it: the value the owner chose, whatever it was before
        seen.pop("page", None)   # every mark is spent: a later edit by hand of the same key is outside
        if not seen.get("rows"):
            seen.pop("rows", None)
        return
    key = data.get("key")
    if not isinstance(key, str) or not key:
        raise Bad("config.changed_outside: key must be a string")
    seen = st.setdefault("config_seen", {"values": {}, "since": ev["ts"]})
    old, new = data.get("old"), data.get("new")
    _put(seen["values"], key, new)
    rows = seen.setdefault("rows", {})
    for k in [k for k, r in rows.items() if ev["ts"] - r["ts"] >= ROW_DAYS * 86400]:
        rows.pop(k)   # gone from the page: a later edit starts from the value the hub has lived with since
    row = rows.get(key)
    first = row["old"] if row else old
    if first == new:
        rows.pop(key, None)   # back to what it was before the first outside edit: nothing to show
    else:
        rows[key] = {"old": first, "new": new, "ts": ev["ts"],
                     **({"hidden": True} if data.get("hidden") or hidden(first) or hidden(new) else {})}
        for k in sorted(rows, key=lambda k: rows[k]["ts"])[:-MAX_ROWS]:
            rows.pop(k)
    if not rows:
        seen.pop("rows", None)


def _put(values, key, value):
    if value is None:
        values.pop(key, None)
    else:
        values[key] = value
