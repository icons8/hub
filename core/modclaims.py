"""What a core module claims from clients (Module API 2.4): its own `/api/…` paths on the page port and its own
top-level keys and features in the snapshot. A client written for another hub uses its own paths and reads its own
snapshot keys, not `/api/mod/<name>/…` and `mods` — the module answers it there, the core does not change.

    "http": {"paths": ["/api/files"], "views": ["/api/visual"]}       handlers (like /api/mod/<name>/…) and HTML views
    "snapshot": {"keys": ["todo"], "features": ["session.rename"]}    key = mods.<name>.<key>; a feature goes in features

The core always wins: a module does not get a core path, key or feature (CORE_*) — neither now nor when the core
adds the same one. Two loaded modules with the same path or key — both lose it until `modules.use.paths` /
`modules.use.keys` names one. The module keeps working: it loses only the disputed. What it lost is shown by `hub modules`
and a `module.claims` line in the journal (hubd, once per change). hubd decides (resolve) and puts the result in the want file
(`claims`); the page (modules/web/modproxy.py, core/modstate.py) reads only that.
"""
import re

from core import i18n, modules

MAX_PATHS = 32   # paths and views together
MAX_KEYS = 32    # snapshot keys; features — the same number
PATH = re.compile(r"/api(?:/[a-z0-9][a-z0-9_.-]{0,63}){1,8}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
FEATURE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

# Paths the page answers (modules/web/server.py): a module does not get them or anything under them. The list is kept
# complete by a test (tests/test_modapi_claims.CoreList): every /api/… path in the page code must be here.
CORE_PATHS = frozenset("/api/" + p for p in (
    "about", "accounts/probe", "app/restore", "app/undo", "accounts/use", "answer", "codex/add", "codex/login", "codex/logout", "away", "backlog", "bus/act", "bus/job", "bus/start", "dismiss", "feed", "feed/post", "file", "history", "history/search", "hold", "inject", "instances",
    "interrupt", "layout", "marketplaces", "marketplaces/icon", "mcp/callback", "mcp/dismiss", "mcp/link", "mcp/login", "mod", "module-model", "modules", "modules/toggle", "modules/enable", "modules/model", "modules/reset", "modules/forget", "owner/back", "page-key", "signin", "signout", "paths", "permission", "plugins/fix",
    "plugins/icon", "plugins/install", "plugins/remove", "plugins/review", "plugins/toggle", "plugins/update",
    "push/register", "question/dismiss", "queue/cancel", "react", "restart", "schedule", "secrets", "secrets/fill", "seen", "send",
    "send/receipt", "session/close", "session/compact", "session/effort", "session/fire", "session/icon", "session/kit", "session/model",
    "session/parallel", "session/reload", "session/remove", "session/resume", "session/solo", "session/spec", "session/stop", "session/train", "settings/set", "spec",
    "setup", "setup/bundle", "setup/cancel", "setup/host", "setup/input", "setup/run",
    "settings/title", "snapshot", "stats", "stream", "subagent", "themes", "themes/art", "themes/file", "timeline",
    "tools", "tools/grant", "update/check", "update/fix", "update/install", "update/local-edits", "update/rollback", "upload", "upload/raw", "usage", "voice", "voice/failed", "voice/local", "voice/raw"))
# the page compares these by string prefix: /api/flowers, /api/schedule/x are also its own
CORE_PREFIXES = ("/api/flow", "/api/plugins/updates", "/api/plugins/search", "/api/schedule/", "/api/mod/", "/api/secrets/")
# Core snapshot keys (core/views.py, Web.with_clock) — including those that are not always present; `_gone`, `_delta`
# and `_n` are the stream patch's. A test checks them against the snapshot.
CORE_KEYS = frozenset((
    "seq", "sessions", "catalog", "delegations", "owner", "messages", "schedule", "browsers", "queue", "parallel",
    "budget", "queue_paused", "mcp_waits", "mcp_logins", "reactions", "layout", "limits", "account", "updates", "update_check", "min_claude",
    "update_testing", "private_modules", "search_fields", "timelines", "epoch", "timeline_rev", "feeds", "mods", "mod_versions", "core_written", "clock", "voice", "jira",
    "files_port", "hub_title", "hub_name", "directions", "direction_tasks", "version", "features", "upload", "context_limit",
    "extensions", "plugins", "plugin_api", "accounts", "codex", "settings", "commands", "code", "_gone", "_delta", "_n", "alerts",
    "alerts_rev", "bus", "app_code", "app_move"))
CORE_FEATURES = frozenset(("push", "push.alerts", "reactions", "modules", "upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "setup.app", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "voice.raw", "file.thumb"))


def core_path(path):
    """The path is the core's: the page recognises it itself (CORE_PATHS whole, CORE_PREFIXES by prefix). A path under
    a core path (/api/usage/cycles, /api/voice/stream) is not its own: it compares whole."""
    return path in CORE_PATHS or path.startswith(CORE_PREFIXES)


def declared(man):
    """{paths, views, keys, features} from the manifest (already validated)."""
    http = man.get("http") if isinstance(man.get("http"), dict) else {}
    snap = man.get("snapshot") if isinstance(man.get("snapshot"), dict) else {}
    return {"paths": list(http.get("paths") or []), "views": list(http.get("views") or []),
            "keys": list(snap.get("keys") or []), "features": list(snap.get("features") or [])}


# --- module.json ----------------------------------------------------------------------------------------------------

def http_errors(http):
    """http.paths and http.views: paths like /api/a/b — lowercase Latin letters, digits, "_.-", no query and no trailing "/";
    /api/mod/… — module handlers by name, not here."""
    errs, seen = [], set()
    for field in ("paths", "views"):
        if field not in http:
            continue
        items = http[field]
        if not (isinstance(items, list) and all(isinstance(x, str) for x in items)):
            errs.append(f"http.{field}: a list of paths, e.g. [\"/api/files\"]")
            continue
        for p in items:
            if not PATH.fullmatch(p):
                errs.append(f"http.{field}: \"{p}\" — a /api/… path of lowercase Latin letters, digits and \"_.-\", with no query "
                            "and no trailing \"/\"")
            elif p == "/api/mod" or p.startswith("/api/mod/"):
                errs.append(f"http.{field}: \"{p}\" — /api/mod/<name>/… a module gets anyway")
            elif p in seen:
                errs.append(f"http.{field}: \"{p}\" is named twice")
            seen.add(p)
    if len(seen) > MAX_PATHS:
        errs.append(f"http: paths and views together — at most {MAX_PATHS}")
    return errs


def snapshot_errors(snap):
    """snapshot: {keys, features} — names of snapshot keys and features."""
    if not isinstance(snap, dict):
        return ['snapshot: an object {"keys": [...], "features": [...]}']
    errs = [f"snapshot.{k}: unknown field (known: keys, features)" for k in sorted(set(snap) - {"keys", "features"})]
    for field, rx, what in (("keys", KEY, "lowercase Latin letters, digits and \"_\", starting with a letter, up to 64 characters"),
                            ("features", FEATURE, "lowercase Latin letters, digits and \"_.-\", starting with a letter, up to 64 characters")):
        items = snap.get(field, [])
        if not (isinstance(items, list) and all(isinstance(x, str) and rx.fullmatch(x) for x in items)):
            errs.append(f"snapshot.{field}: a list of names — {what}")
        elif len(set(items)) != len(items) or len(items) > MAX_KEYS:
            errs.append(f"snapshot.{field}: no repeats, at most {MAX_KEYS}")
    return errs


# --- who gets what --------------------------------------------------------------------------------------------------

def _choice(cfg, field):
    use = modules.settings(cfg).get("use") or {}
    got = use.get(field)
    return got if isinstance(got, dict) else {}


def resolve(plan, cfg):
    """From the load plan (modules.resolve) -> ({name: {paths?, views?, keys?, features?}}, {name: [what it lost]}).
    Only loaded ones (state ok) compete; empty is not returned."""
    live = {r["name"]: declared(r["manifest"]) for r in plan if r.get("state") == "ok"}
    owners = {}   # (kind, name) -> modules: paths and views are one path space
    for n, d in live.items():
        for field, kind in (("paths", "path"), ("views", "path"), ("keys", "key")):
            for x in d[field]:
                owners.setdefault((kind, x), []).append(n)
    use = {"path": _choice(cfg, "paths"), "key": _choice(cfg, "keys")}
    claims, notes = {}, {}
    for n, d in live.items():
        got, lost = {}, []
        for field, kind, label in (("paths", "path", "core.modclaims.label_path"),
                                   ("views", "path", "core.modclaims.label_view"),
                                   ("keys", "key", "core.modclaims.label_key")):
            for x in d[field]:
                why = None
                if kind == "path" and core_path(x) or kind == "key" and x in CORE_KEYS:
                    why = i18n.t("core.modclaims.core_wins")
                elif len(owners[(kind, x)]) > 1:
                    rivals = owners[(kind, x)]
                    chosen = use[kind].get(x)
                    if chosen in rivals:
                        why = None if chosen == n else i18n.t("core.modclaims.given_to", module=chosen, kind=kind)
                    else:
                        why = i18n.t("core.modclaims.rivals", others=", ".join(r for r in rivals if r != n), kind=kind)
                if why:
                    lost.append(i18n.t("core.modclaims.lost", label=i18n.t(label), name=x, why=why))
                else:
                    got.setdefault(field, []).append(x)
        for f in d["features"]:
            if f in CORE_FEATURES:
                lost.append(i18n.t("core.modclaims.lost_feature", name=f, why=i18n.t("core.modclaims.core_wins")))
            else:
                got.setdefault("features", []).append(f)
        if got:
            claims[n] = got
        if lost:
            notes[n] = lost
    return claims, notes
