"""The Settings tab: hub settings that the page may change.

An allowlist: a hub.json key (dotted for nested ones) or a watch.json key, the value type and bounds, the group, and
whether it is basic or under the "Advanced" fold. Anything not listed here the page never writes: secrets, paths,
commands, lists (session rules, MCP, browsers, plugins) — otherwise the page would amount to running any command on
the machine. The core and modules read hub.json themselves by mtime, so no restart is needed; the model and effort of
sessions take effect from their next launch."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import admit, catalog, i18n, minihub, modelrule
from core.catalog import EFFORTS

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")    # a model or voice name: not a command-line option, no spaces
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")  # a git branch: does not start with "-" (otherwise it would be a git option)

# key, where (hub | watch), type, default, group, under the fold, label, whether there is a hint, bounds/choices[, toggle
# only]. The label is the row name in texts(): there are its text and the hint from the catalog (i18n/<language>.json);
# the group is a code, and its name is in texts() too: the text is in the page language (view).
# A module's group is its name, as on the Modules tab; enabling and disabling a module happens there, on Modules (not in
# the settings), while here are only its settings: basic ones and those under "Advanced".
HUB, SESSIONS, EXECUTORS, TELEGRAM = "hub", "sessions", "executors", "telegram"
CRITIC, VOICE, UPDATE, PUSH = "critic", "voice_reply", "update", "push"
S = [
    ("title", "hub", "title", "", HUB, False, "title", True, None),   # the default is web.hub.default_title (view)
    ("timezone", "hub", "tz", "UTC", HUB, False, "timezone", True, None),
    # interface language (core/i18n.py human): empty — automatic (the language of your messages, of the browser); the choices are the ready languages
    ("language", "hub", "enum", "", HUB, False, "language", True, [""] + [x for x in i18n.LANGS if x in i18n.ready()]),
    ("journal.rotate_mb", "hub", "num", 50, HUB, True, "rotate_mb", True, (10, 100)),
    ("web.upload_max_mb", "hub", "num", 200, HUB, True, "upload_max_mb", True, (1, 2048)),
    ("restart_wait_minutes", "hub", "num", 20, HUB, True, "restart_wait", True, (0, 1440)),
    # the mailbox sender check (core/admit.py): auto refuses after a clean window; flag — the way out if a legitimate
    # writer of this hub got refused
    ("admit.mode", "hub", "enum", "auto", HUB, True, "admit_mode", True, list(admit.SETTINGS)),
    # model and effort are fallbacks (the hub picks them per task): under "Advanced", not among the main settings
    ("model", "hub", "enum", "", SESSIONS, True, "model", True, ["", "opus", "sonnet", "haiku", "fable"]),
    ("effort", "hub", "enum", "medium", SESSIONS, True, "effort", True, ["low", "medium", "high", "xhigh", "max"]),
    ("park_after_hours", "hub", "num", 2, SESSIONS, False, "park_after_hours", True, (0, 168)),
    ("compact_idle.on_wake", "hub", "bool", True, SESSIONS, False, "compact_on_wake", True, None),
    ("compact_idle.after_minutes", "hub", "num", 30, SESSIONS, True, "compact_after_minutes", True, (0, 1440)),
    ("compact_idle.min_tokens", "hub", "num", 200000, SESSIONS, True, "compact_min_tokens", False, (50000, 1000000)),
    ("executors.remove_after_hours", "hub", "num", 0, SESSIONS, True, "executors_remove_after", True, (0, 720)),
    # the size cap of an agent's own CLAUDE.md (core/profcap.py): over it the core moves the oldest unpinned items out
    ("agents.profile_max_kb", "hub", "num", 250, SESSIONS, True, "profile_max_kb", True, (0, 4096)),
    ("recap_steps", "watch", "num", 12, SESSIONS, True, "recap_steps", True, (1, 50)),
    ("recap_task", "watch", "num", 500, SESSIONS, True, "recap_task", False, (100, 3000)),
    ("connectors", "hub", "bool", False, SESSIONS, True, "connectors", True, None),
    ("plugin_update_restart", "hub", "bool", False, SESSIONS, True, "plugin_update_restart", True, None),
    # Claude Code asks for confirmation even apart from permission requests; when enabled, the core answers "yes" by
    # itself, for all confirmation requests; off by default
    ("permissions.auto_allow", "hub", "bool", False, SESSIONS, False, "auto_allow", True, None),
    ("fork_subagents", "hub", "bool", True, SESSIONS, True, "fork_subagents", True, None),
    ("setting_sources", "hub", "enum", "project,local", SESSIONS, True, "setting_sources", True,
     ["project,local", "project", "user,project,local"]),
    ("critic.engine", "hub", "enum", "codex", CRITIC, False, "critic_engine", True, ["codex", "claude"]),
    ("critic.effort", "hub", "enum", "high", CRITIC, False, "critic_effort", False, ["low", "medium", "high"]),
    ("critic.model", "hub", "name", "gpt-6-astra", CRITIC, True, "critic_model", False, None),
    ("critic.claude_model", "hub", "enum", "sonnet", CRITIC, True, "critic_claude_model", True,
     ["opus", "sonnet", "haiku", "fable"]),
    # questions and tool requests only on the page (users asked for it); the bot reads it on the fly
    ("telegram.quiet", "hub", "bool", False, TELEGRAM, False, "telegram_quiet", True, None),
    # an answered card is erased from the chat rather than having the outcome appended to it; the bot reads it on the fly
    ("telegram.drop_answered", "hub", "bool", False, TELEGRAM, False, "telegram_drop_answered", True, None),
    # a long session reply — first line shown and the rest folded (blockquote expandable); the bot reads it on the fly
    ("telegram.fold_long", "hub", "bool", False, TELEGRAM, False, "telegram_fold_long", True, None),
    # a file and a result the core counts to the owner go to Telegram whatever the chain began with, also by his page message; the bot reads it on the fly
    ("telegram.page_results", "hub", "bool", False, TELEGRAM, False, "telegram_page_results", True, None),
    # the "while you were away" summary on return (/back, "I'm here", a message while away, end of the period); the bot reads it on the fly
    ("telegram.back_summary", "hub", "bool", True, TELEGRAM, False, "telegram_back_summary", True, None),
    # one message with the owner's open tasks, every N minutes, the previous one erased (modules/telegram/digest.py); off by
    # default: it writes to the chat on its own schedule; the bot reads both on the fly
    ("telegram.digest", "hub", "bool", False, TELEGRAM, False, "telegram_digest", True, None),
    ("telegram.digest_minutes", "hub", "num", 30, TELEGRAM, False, "telegram_digest_minutes", False, (5, 1440)),
    # the core's own 👀 (taken) and 👍 (answered) under the owner's messages; off — the bot does not set them (the page has its own
    # "view" checkbox); the owner's reactions stay; the bot reads it on the fly
    ("telegram.receipts", "hub", "bool", True, TELEGRAM, False, "telegram_receipts", True, None),
    # push topic and decision made by the model (push spec): off until compared with the fallback path; the question
    # text goes to the hub model's provider
    ("push.summarize", "hub", "bool", False, PUSH, False, "push_summarize", True, None),
    ("voice_reply.voice", "hub", "name", "cove", VOICE, False, "voice_reply_voice", False, None),
    ("update.every", "hub", "num", 3600, UPDATE, False, "update_every", False, (300, 86400)),
    # a hub on a code layer installs only on the person's press (modules/update plan); the row is drawn for that hub only
    ("update.manual", "hub", "bool", False, UPDATE, False, "update_manual", True, None),
    ("update.branch", "hub", "branch", "main", UPDATE, True, "update_branch", False, None),
    # the limit of the update self-check (modules/update tests_timeout): a slow machine with its own core edits needs more
    ("update.tests_timeout", "hub", "num", 1200, UPDATE, True, "update_tests_timeout", True, (1200, 14400)),
    # reports of the app's failed updates to the developers (modules/update/report.py): the switch beside its line in
    # "About the hub" — only an app whose build names where reports go has it
    ("update.report", "hub", "bool", True, UPDATE, False, "update_report", False, None),
    # only the toggles of the Modules cards (POST /api/modules/toggle) — they are not on the Main tab
    ("critic.enabled", "hub", "bool", False, "", False, "critic_enabled", False, None, True),
    ("video.enabled", "hub", "bool", False, "", False, "video_enabled", False, None, True),
    ("voice_reply.enabled", "hub", "bool", False, "", False, "voice_reply_enabled", False, None, True),
    ("stats.enabled", "hub", "bool", False, "", False, "stats_enabled", False, None, True),
    ("update.enabled", "hub", "bool", True, "", False, "update_enabled", False, None, True),
    ("secrets.enabled", "hub", "bool", True, "", False, "secrets_enabled", False, None, True),   # on: an empty vault does nothing
    ("push.enabled", "hub", "bool", False, "", False, "push_enabled", False, None, True),
    ("reactions.enabled", "hub", "bool", False, "", False, "reactions_enabled", False, None, True),
    ("gateway.enabled", "hub", "bool", False, "", False, "gateway_enabled", False, None, True),   # Settings → Accounts
    # the page on the owner's other devices over Tailscale (modules/tailnet): off until turned on here or in hub.json
    ("tailnet.enabled", "hub", "bool", False, "", False, "tailnet_enabled", False, None, True),
    ("off", "watch", "boolnot", True, "", False, "watch_enabled", False, None, True),
    ("surrender", "watch", "bool", False, "", False, "surrender_enabled", False, None, True),
    ("promise", "watch", "bool", False, "", False, "promise_enabled", False, None, True),
]
# model and effort of an executor by the kind of work the session names (--work normal|risky): sessions name the kind,
# the model comes from the settings; empty — the core passes no flag, and claude uses the model of its own endpoint
_AT = next(i for i, row in enumerate(S) if row[4] == CRITIC)   # right after "Sessions", before the module groups
for _work, _what, _when in reversed(modelrule.LABELS):
    _model, _effort = modelrule.DEFAULTS()[_work]
    S[_AT:_AT] = [
        (f"executors.{_work}.model", "hub", "enum", _model, EXECUTORS, False, f"executors_{_work}_model", True,
         [""] + modelrule.MODELS),
        (f"executors.{_work}.effort", "hub", "enum", _effort, EXECUTORS, False, f"executors_{_work}_effort", False,
         [""] + list(EFFORTS))]
# limit economy (only with mini-hubs, core/budget.py): the pair of routine executors and from which share of the window —
# under "Advanced", followed by "Total session executors per hub"
_eco_model, _eco_effort = modelrule.ECONOMY()
_AT = next(i for i, row in enumerate(S) if row[4] == CRITIC)
S[_AT:_AT] = [
    ("executors.economy.model", "hub", "enum", _eco_model, EXECUTORS, True, "executors_economy_model", True,
     [""] + modelrule.MODELS),
    ("executors.economy.effort", "hub", "enum", _eco_effort, EXECUTORS, True, "executors_economy_effort", False,
     [""] + list(EFFORTS)),
    ("budget.economy_at", "hub", "num", 0.85, EXECUTORS, True, "budget_economy_at", True, (0.4, 1)),
    # budget gate (core/budget.gate): whether to wait for the owner's word at the limit — yes by default
    ("budget.owner_gate", "hub", "bool", True, EXECUTORS, True, "budget_owner_gate", True, None)]
# parallelism is the single knob of mini-hubs (the key task is simply to steer parallelism); the core learns it from
# the owner.settings fact (core/minihub.parallel_of); the overall ceiling is under "Advanced"
_AT = next(i for i, row in enumerate(S) if row[4] == CRITIC)
S[_AT:_AT] = [("executors.max_live", "hub", "num", 0, EXECUTORS, True, "executors_max_live", True, (0, 200))]
_AT = next(i for i, row in enumerate(S) if row[4] == EXECUTORS)   # the main knob goes first in the group
S[_AT:_AT] = [("executors.parallel", "hub", "num", minihub.DEFAULT, EXECUTORS, False, "executors_parallel_scope", True,
               (0, 20))]
# whole numbers only: there is no such thing as 2.5 executors; the updater hands its limit to tools/test.py as a whole number
WHOLE = {"executors.parallel", "executors.max_live", "update.tests_timeout", "agents.profile_max_kb"}
KEYS = {row[0] if row[1] == "hub" else f"watch.{row[0]}": row for row in S}
# the pair of one agent's executors (core/modelrule.own): the agents are not known in advance, so these keys are taken by
# shape; the name is a session name (core/reducer.NAME)
AGENT_KEY = re.compile(r"^executors\.agents\.([a-z0-9][a-z0-9._-]{0,39})\.(model|effort)$")
# a module's group is visible only while the module is enabled (settings of disabled modules are hidden)
GROUP_ON = {CRITIC: "critic.enabled", VOICE: "voice_reply.enabled", UPDATE: "update.enabled", PUSH: "push.enabled"}
# where the row appears on the page (simplified): the first screen of "Main" holds only what people change; a module's
# settings are in its card on "Modules"; whatever is removed from the screen remains a hub.json key and stays in the
# allowlist (the hub changes it on request)
MAIN = {"title", "timezone", "language", "executors.parallel", "permissions.auto_allow"}
HIDDEN = {"update.every", "update.branch",   # machine operations; they do not change session behavior
          "update.report"}   # drawn by "About the hub" beside the line of what a report holds (js/updates.js)
MODULE_OF = {CRITIC: "critic", VOICE: "voice_reply", UPDATE: "update", TELEGRAM: "telegram", PUSH: "push"}


def texts(lang):
    """Row labels and hints (by the label name from S) and group names in the language lang (core/i18n.py)."""
    def t(key):
        return i18n.tr(lang, key)
    rows = {
        "title": (t("web.settings.title"), t("web.settings.title_hint")),
        "timezone": (t("web.settings.timezone"), t("web.settings.timezone_hint")),
        "language": (t("web.settings.language"), t("web.settings.language_hint")),
        "rotate_mb": (t("web.settings.rotate_mb"), t("web.settings.rotate_mb_hint")),
        "upload_max_mb": (t("web.settings.upload_max_mb"), t("web.settings.upload_max_mb_hint")),
        "restart_wait": (t("web.settings.restart_wait"), t("web.settings.restart_wait_hint")),
        "admit_mode": (t("web.settings.admit_mode"), t("web.settings.admit_mode_hint")),
        "model": (t("web.settings.model"), t("web.settings.model_hint")),
        "effort": (t("web.settings.effort"), t("web.settings.effort_hint")),
        "park_after_hours": (t("web.settings.park_after_hours"), t("web.settings.park_after_hours_hint")),
        "compact_on_wake": (t("web.settings.compact_on_wake"), t("web.settings.compact_on_wake_hint")),
        "compact_after_minutes": (t("web.settings.compact_after_minutes"), t("web.settings.compact_after_minutes_hint")),
        "compact_min_tokens": (t("web.settings.compact_min_tokens"), ""),
        "executors_remove_after": (t("web.settings.executors_remove_after"),
                                   t("web.settings.executors_remove_after_hint")),
        "profile_max_kb": (t("web.settings.profile_max_kb"), t("web.settings.profile_max_kb_hint")),
        "recap_steps": (t("web.settings.recap_steps"), t("web.settings.recap_steps_hint")),
        "recap_task": (t("web.settings.recap_task"), ""),
        "connectors": (t("web.settings.connectors"), t("web.settings.connectors_hint")),
        "plugin_update_restart": (t("web.settings.plugin_update_restart"), t("web.settings.plugin_update_restart_hint")),
        "auto_allow": (t("web.settings.auto_allow"), t("web.settings.auto_allow_hint")),
        "fork_subagents": (t("web.settings.fork_subagents"), t("web.settings.fork_subagents_hint")),
        "setting_sources": (t("web.settings.setting_sources"), t("web.settings.setting_sources_hint")),
        "critic_engine": (t("web.settings.critic_engine"), t("web.settings.critic_engine_hint")),
        "critic_effort": (t("web.settings.critic_effort"), ""),
        "critic_model": (t("web.settings.critic_model"), ""),
        "critic_claude_model": (t("web.settings.critic_claude_model"), t("web.settings.critic_claude_model_hint")),
        "telegram_quiet": (t("web.settings.telegram_quiet"), t("web.settings.telegram_quiet_hint")),
        "telegram_drop_answered": (t("web.settings.telegram_drop_answered"),
                                   t("web.settings.telegram_drop_answered_hint")),
        "telegram_fold_long": (t("web.settings.telegram_fold_long"), t("web.settings.telegram_fold_long_hint")),
        "telegram_page_results": (t("web.settings.telegram_page_results"),
                                  t("web.settings.telegram_page_results_hint")),
        "telegram_back_summary": (t("web.settings.telegram_back_summary"),
                                  t("web.settings.telegram_back_summary_hint")),
        "telegram_digest": (t("web.settings.telegram_digest"), t("web.settings.telegram_digest_hint")),
        "telegram_digest_minutes": (t("web.settings.telegram_digest_minutes"), ""),
        "telegram_receipts": (t("web.settings.telegram_receipts"), t("web.settings.telegram_receipts_hint")),
        "push_summarize": (t("web.settings.push_summarize"), t("web.settings.push_summarize_hint")),
        "voice_reply_voice": (t("web.settings.voice_reply_voice"), ""),
        "update_every": (t("web.settings.update_every"), ""),
        "update_manual": (t("web.settings.update_manual"), t("web.settings.update_manual_hint")),
        "update_report": (t("web.updates.report_toggle"), ""),
        "update_branch": (t("web.settings.update_branch"), ""),
        "update_tests_timeout": (t("web.settings.update_tests_timeout"), t("web.settings.update_tests_timeout_hint")),
        "critic_enabled": (t("web.settings.critic_enabled"), ""),
        "video_enabled": (t("web.settings.video_enabled"), ""),
        "voice_reply_enabled": (t("web.settings.voice_reply_enabled"), ""),
        "stats_enabled": (t("web.settings.stats_enabled"), ""),
        "update_enabled": (t("web.settings.update_enabled"), ""),
        "secrets_enabled": (t("web.settings.secrets_enabled"), ""),
        "push_enabled": (t("web.settings.push_enabled"), ""),
        "reactions_enabled": (t("web.settings.reactions_enabled"), ""),
        "gateway_enabled": (t("web.settings.gateway_enabled"), ""),
        "tailnet_enabled": (t("web.settings.tailnet_enabled"), ""),
        "watch_enabled": (t("web.settings.watch_enabled"), ""),
        "surrender_enabled": (t("web.settings.surrender_enabled"), ""),
        "promise_enabled": (t("web.settings.promise_enabled"), ""),
        "executors_risky_model": (t("web.settings.executors_risky_model"), t("web.settings.executors_risky_model_hint")),
        "executors_risky_effort": (t("web.settings.executors_risky_effort"), ""),
        "executors_normal_model": (t("web.settings.executors_normal_model"),
                                   t("web.settings.executors_normal_model_hint")),
        "executors_normal_effort": (t("web.settings.executors_normal_effort"), ""),
        "executors_economy_model": (t("web.settings.executors_economy_model"),
                                    t("web.settings.executors_economy_model_hint")),
        "executors_economy_effort": (t("web.settings.executors_economy_effort"), ""),
        "budget_economy_at": (t("web.settings.budget_economy_at"), t("web.settings.budget_economy_at_hint")),
        "budget_owner_gate": (t("web.settings.budget_owner_gate"), t("web.settings.budget_owner_gate_hint")),
        "executors_max_live": (t("web.settings.executors_max_live"), t("web.settings.executors_max_live_hint")),
        "executors_parallel_scope": (t("web.settings.executors_parallel"),
                                     t("web.settings.executors_parallel_scope_hint")),
    }
    groups = {HUB: t("web.settings.group_hub"), SESSIONS: t("web.settings.group_sessions"),
              EXECUTORS: t("web.settings.group_executors"), CRITIC: t("web.settings.group_critic"),
              TELEGRAM: t("web.settings.group_telegram"), VOICE: t("web.settings.group_voice_reply"),
              UPDATE: t("web.settings.group_update"), PUSH: t("web.settings.group_push"), "": ""}
    return rows, groups


def agent_row(key):
    """The allowlist row of one agent's pair (executors.agents.<agent>.model | .effort), None for any other key."""
    m = AGENT_KEY.match(key)
    if not m:
        return None
    choices = [""] + (modelrule.MODELS if m.group(2) == "model" else list(EFFORTS))
    return (key, "hub", "enum", "", EXECUTORS, True, f"executors_agent_{m.group(2)}", False, choices)


def row_of(key):
    """The allowlist row of a key: a listed one, or one agent's pair."""
    return KEYS.get(key) or agent_row(key)


def label(row, lang):
    """The label of a settings row in the language lang (core/i18n.py); an agent's pair names the agent."""
    m = AGENT_KEY.match(row[0])
    if m:   # the keys are spelled out: the check of unused catalog keys looks for them in the code by their text
        return i18n.tr(lang, "web.settings.executors_agent_model" if m.group(2) == "model"
                       else "web.settings.executors_agent_effort", agent=m.group(1))
    return texts(lang)[0][row[6]][0]


def place(key, group):
    if key in HIDDEN:
        return "hidden"
    if group in MODULE_OF:
        return "module:" + MODULE_OF[group]
    return "main" if key in MAIN else "more"


def _parts(path):
    """The keys of a dotted path; the name of an agent in executors.agents.<agent>.model may itself contain dots."""
    m = AGENT_KEY.match(path)
    return ["executors", "agents", m.group(1), m.group(2)] if m else path.split(".")


def _get(cfg, path):
    for part in _parts(path):
        if not isinstance(cfg, dict) or part not in cfg:
            return None
        cfg = cfg[part]
    return cfg


def reactions_on(cfg):
    """The "Reactions and folding of answered items" module: off by default — it changes the look of the feed and what sessions receive."""
    return _get(cfg, "reactions.enabled") is True


def validate(key, value, lang=None, cfg=None):
    """(value, None) or (None, what is wrong — in the language lang, or without it in the process language, core/i18n.py
    current). A key missing from the allowlist is refused whatever its type. cfg — the hub's settings: on a hub entirely
    on Codex an executor model is codex/<model> (core/modelrule.model_ok)."""
    lang = lang or i18n.current()
    row = row_of(key)
    t = lambda k, **p: i18n.tr(lang, k, **p)   # noqa: E731
    if not row:
        return None, t("web.settings.err_not_allowed")
    kind, limits = row[2], row[8]
    if kind in ("bool", "boolnot"):
        return (value, None) if isinstance(value, bool) else (None, t("web.settings.err_bool"))
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not limits[0] <= value <= limits[1] \
                or key in WHOLE and value != int(value):
            if key in WHOLE:
                return None, t("web.settings.err_whole_number", min=limits[0], max=limits[1])
            return None, t("web.settings.err_number", min=limits[0], max=limits[1])
        return int(value) if key in WHOLE else value, None
    if not isinstance(value, str):
        return None, t("web.settings.err_string")
    if kind == "enum" and listed_model(key, cfg):
        return (value, None) if modelrule.model_ok(value, cfg) else (None, t("web.settings.cx_e_codex_model"))
    if kind == "enum" and key.endswith(".model") and key.startswith("executors.") and catalog.known_model(value):
        return value, None   # and a full claude-… id (core/modelrule.pair accepts it)
    if kind == "enum":
        return (value, None) if value in limits else \
            (None, t("web.settings.err_choice", choices=", ".join(v or t("web.settings.default") for v in limits)))
    if kind == "title":
        title = " ".join(value.split())
        return (title, None) if 1 <= len(title) <= 40 else (None, t("web.settings.err_title"))
    if kind == "tz":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return None, t("web.settings.err_timezone")
        return value, None
    if kind == "name":
        return (value, None) if NAME.match(value) else (None, t("web.settings.err_name"))
    if kind == "branch":
        return (value, None) if BRANCH.match(value) else (None, t("web.settings.err_branch"))
    return None, t("web.settings.err_type")


def drop(cfg, parts):
    """Remove the key at parts and the parents it leaves empty (an agent with no half of a pair left goes too). A path the
    file does not hold (or holds not as dictionaries) is left as it is. Returns the previous value."""
    trail, box = [], cfg
    for part in parts[:-1]:
        if not isinstance(box.get(part), dict):
            return None
        trail.append((box, part))
        box = box[part]
    old = box.pop(parts[-1], None)
    for parent, part in reversed(trail):
        if parent[part]:
            break
        del parent[part]
    return old


def put(cfg, path, value):
    """Write into cfg by a dotted path; an empty model removes the key (Claude Code's default), and so does an empty
    half of an agent's pair. Returns the previous value."""
    parts = _parts(path)
    if value == "" and (path == "model" or AGENT_KEY.match(path)):
        return drop(cfg, parts)
    box = cfg
    for part in parts[:-1]:
        if not isinstance(box.get(part), dict):
            box[part] = {}
        box = box[part]
    old = box.get(parts[-1])
    box[parts[-1]] = value
    return old


def admit_names(lang, now):
    """Choices of admit.mode: auto says which mode the check is in now."""
    auto = i18n.tr(lang, "web.settings.admit_auto_refuse") if now == "refuse" else \
        i18n.tr(lang, "web.settings.admit_auto_flag")
    return {"auto": auto, "flag": i18n.tr(lang, "web.settings.admit_flag"),
            "refuse": i18n.tr(lang, "web.settings.admit_refuse")}


def listed_model(key, cfg):
    """An executor model row on a hub entirely on Codex: it offers the Codex models (codex_choices)."""
    return modelrule.codex_only(cfg) and key.startswith("executors.") and key.endswith(".model")


def codex_choices(cfg, row, codex_models):
    """Codex (experimental): the choices of an executor model row on a hub entirely on Codex — its Codex models
    (core/codexcli.models), not Claude's; any other row or hub — its own."""
    if not listed_model(row[0], cfg):
        return row[8]
    return [""] + [f"codex/{m}" for m in codex_models or []]


def agent_rows(cfg, agents, lang, group, codex_models=None):
    """Two rows (model, effort) per agent for the "Executors" group: the agents of the hub (agents) and those the file
    already holds a pair for (otherwise a pair of an agent that has gone could not be cleared from the page). Empty —
    the kind of work decides, as without the setting."""
    held = _get(cfg, "executors.agents")
    held = held if isinstance(held, dict) else {}
    names = list(dict.fromkeys([*agents, *(n for n, v in held.items() if isinstance(v, dict))]))
    out = []
    for name in names:
        mine = held.get(name) if isinstance(held.get(name), dict) else {}
        for field in ("model", "effort"):
            key = f"executors.agents.{name}.{field}"
            row = agent_row(key)
            if not row:   # not a session name: a hand-written file entry the page cannot address
                continue
            raw = mine.get(field)
            value = raw if isinstance(raw, str) else ""
            if field == "model" and not modelrule.model_ok(value, cfg):   # a Claude model left on a Codex hub: not in effect
                value = ""
            listed = codex_choices(cfg, row, codex_models)
            choices = listed if value in listed else listed + [value]
            out.append({"key": key, "kind": "enum", "value": value, "group": group, "advanced": True, "place": "more",
                        "label": label(row, lang), "hint": "", "agent": name, "field": field, "choices": choices})
    return out


def view(cfg, watch, lang=None, admit_now=None, agents=(), codex_models=None):
    """Rows for the page: the current value (the default if absent from the file), the group, whether under the fold;
    the label, hint and group name are in the language lang (or without it in the process language, core/i18n.py
    current). admit_now — the mode the mailbox check is in now (core/admit.mode): the "auto" choice names it. agents —
    the hub's agents: each gets the row pair of its executors. codex_models — the model ids of a hub entirely on Codex
    (core/codexcli.models): its executor rows offer them; the Claude session model and effort rows are not shown there
    (the host runs on the hub's Codex model, "codex.model")."""
    lang = lang or i18n.current()
    rows, groups = texts(lang)
    out = []
    for key, row in KEYS.items():
        if len(row) > 9 and row[9]:   # the module toggle is on the Modules tab
            continue
        if key in ("model", "effort") and modelrule.codex_only(cfg):
            continue
        on = GROUP_ON.get(row[4])
        if on and not (lambda v: KEYS[on][3] if v is None else v)(_get(cfg, on)):
            continue
        where, kind, default = row[1], row[2], row[3]
        work = key.split(".")
        if len(work) == 3 and work[0] == "executors" and work[1] in ("normal", "risky"):
            # the default is computed from the environment now (own endpoint — empty), not at module import
            default = modelrule.DEFAULTS(cfg, codex_models or [])[work[1]][work[2] == "effort"]
        if key in ("executors.economy.model", "executors.economy.effort") and modelrule.codex_only(cfg):
            default = modelrule.ECONOMY(cfg)[key.endswith("effort")]
        if key == "title":
            default = i18n.tr(lang, "web.hub.default_title")
        raw = _get(watch if where == "watch" else cfg, row[0])
        value = default if raw is None else raw
        if listed_model(key, cfg) and not modelrule.model_ok(value, cfg):   # a Claude model left on a Codex hub
            value = default
        if kind == "boolnot":   # the watchdog stores "off" — the page shows "on"
            value = not bool(raw)
        listed = codex_choices(cfg, row, codex_models) if kind == "enum" else row[8]
        choices = listed if kind != "enum" or value in listed or not isinstance(value, str) else listed + [value]
        out.append({"key": key, "kind": "bool" if kind == "boolnot" else kind, "value": value,
                    "group": groups[row[4]], "advanced": row[5], "place": place(key, row[4]),
                    "label": rows[row[6]][0], "hint": rows[row[6]][1],
                    **({"choices": choices} if kind == "enum" else {}),
                    **({"names": {"": i18n.tr(lang, "web.settings.language_auto"), **i18n.SELF}} if key == "language" else {}),
                    **({"names": admit_names(lang, admit_now)} if key == "admit.mode" else {}),
                    **({"min": row[8][0], "max": row[8][1]} if kind == "num" else {})})
    return out + agent_rows(cfg, agents, lang, groups[EXECUTORS], codex_models)
