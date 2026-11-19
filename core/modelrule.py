"""Executor model and effort by kind of work (owner decision 2026-10-13: sessions name the kind, the model comes from settings).
A session that brings up an executor names only the kind — `--work normal|risky`; the model/effort pair is taken by the core from
`executors.<kind>` in hub.json (Settings → Main → Advanced → Executors). Empty — the flag is not passed, and claude uses the model of
its endpoint (for people with custom models behind their own address the opus alias may not be found). A session without --work is
refused with the rule (agent-dev run 2026-10-13: without a refusal, whoever did not read the help never learns the rule)."""
import os
import re

from core import catalog
from core.catalog import EFFORTS

MODELS = list(catalog.MODELS)
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")   # a Codex model name (also core/codexcli: model/list)
# kind, how it is labelled, when it applies; risky goes first and with "even a precise edit" (a point edit of a live
# session's permissions used to be counted as simple work). A reader gets them through the catalog (rule: risky_when)
LABELS = [
    ("risky", "risky work", "prod, money, clients, access, deleting data, architecture, editing instructions, "
                            "a non-obvious bug, long autonomous work, even an exact edit in them"),
    ("normal", "everything else", "the hub and the sessions' rules are checked on Opus with effort medium"),
]


def codex_only(cfg):
    """Codex (experimental): the hub is entirely on Codex (hub.json "host": "codex") — executor pairs are
    Codex ones: a model is codex/<model> or empty (the hub's Codex model, "codex.model"); Claude names do not apply."""
    return (cfg or {}).get("host") == "codex"


def model_ok(model, cfg):
    """Whether a pair may name this model on this hub (empty too: no model given)."""
    if model == "":
        return True
    if codex_only(cfg):
        return isinstance(model, str) and model.startswith("codex/") and bool(MODEL_ID.match(model[6:]))
    return catalog.known_model(model, catalog.codex_host(cfg))


def DEFAULTS(cfg=None):   # noqa: N802 — read on every access: the hub environment may change
    """Default pairs: own endpoint (ANTHROPIC_BASE_URL, except the account gateway into the API) or model (ANTHROPIC_MODEL) — empty, otherwise normal sonnet/high, risky opus/medium
    (owner decision 2026-10-13). The environment is the same as that of the sessions' claude: its own plus the Claude Code settings env (catalog.user_env) —
    otherwise the page and the owner's terminal would compute by one, and the sessions by another (review 2026-10-13).
    A hub entirely on Codex: the hub's Codex model, normal medium, risky high (risky reasons deeper on the same model)."""
    if codex_only(cfg):
        return {"normal": ("", "medium"), "risky": ("", "high")}
    env = {**os.environ, **catalog.user_env()}
    own = env.get("ANTHROPIC_BASE_URL") and not _gateway_to_api(env)
    if own or env.get("ANTHROPIC_MODEL"):   # the person's own model — leave it alone
        return {"normal": ("", ""), "risky": ("", "")}
    return {"normal": ("sonnet", "high"), "risky": ("opus", "medium")}


def own_endpoint(env):
    """The environment has an own ANTHROPIC_BASE_URL: not the hub's account gateway into the API — the test DEFAULTS
    makes inline (core/catalog.own_provider for the core's own model calls)."""
    return bool(env.get("ANTHROPIC_BASE_URL")) and not _gateway_to_api(env)


def _gateway_to_api(env):
    """ANTHROPIC_BASE_URL is the hub's account gateway (built-in or from a login module: the session environment is one,
    core/profiles through), and it sends to the Anthropic API: this is not the person's own endpoint (23.10: normal
    executors from sessions on the gateway went to the session's model). Recognised by the mark that through sets along
    with the address; an address from Claude Code settings that overrode the gateway address is the person's own."""
    from core import profiles
    on = (env.get("ANTHROPIC_BASE_URL") == os.environ.get("ANTHROPIC_BASE_URL") and
          profiles.SECRET_HEADER in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", ""))
    return on and os.environ.get(profiles.UPSTREAM_ENV, "").rstrip("/") == profiles.UPSTREAM


def pair(work, cfg):
    """(model, effort) for a kind of work: as set in settings (empty is also an answer: do not pass), otherwise the default."""
    every = (cfg or {}).get("executors")
    own = every.get(work) if isinstance(every, dict) else None
    own = own if isinstance(own, dict) else {}
    model, effort = DEFAULTS(cfg)[work]
    if "model" in own and model_ok(own["model"], cfg):   # and the full claude-… id
        model = own["model"]
    if "effort" in own and (own["effort"] in EFFORTS or own["effort"] == ""):
        effort = own["effort"]
    return model, effort


def any_pairs(cfg):
    """Whether any agent has its own pair (`executors.agents` holds anything): without it nothing below is looked up."""
    every = (cfg or {}).get("executors")
    held = every.get("agents") if isinstance(every, dict) else None
    return bool(held) and isinstance(held, dict)


def own(raiser, cfg):
    """The pair of the executors of one agent: `executors.agents.<agent>` in hub.json, e.g. {"model": "opus", "effort":
    "high"}. It outranks the kind of work (an agent whose executors should always run on one pair). None: not set."""
    every = (cfg or {}).get("executors")
    agents = every.get("agents") if isinstance(every, dict) else None
    mine = agents.get(raiser) if isinstance(agents, dict) and raiser else None
    if not isinstance(mine, dict):
        return None
    model, effort = mine.get("model") or "", mine.get("effort") or ""
    model = model if model_ok(model, cfg) else ""
    effort = effort if effort in EFFORTS else ""
    return {**({"model": model} if model else {}), **({"effort": effort} if effort else {})} or None


def agent_of(st, name):
    """Whose executors the session `name` raises with hub task run — the key of own(): the session itself, or, when it
    is an executor (role executor without a backlog), the lead on top of its chain (raised_by up to an agent, a
    direction or the hub). The owner (name None) and an executor the owner raised are the hub's. A chain that breaks (a
    raiser gone or removed) or loops stops at `name` itself."""
    from core import minihub   # lazily: the pair rules do not pull the mini-hub module in for everyone who imports them
    sessions = (st or {}).get("sessions") or {}
    hub = next((n for n, x in sessions.items()   # as reducer._hub
                if x.get("kind") == "hub" and x.get("state") not in ("closed", "failed")), None)
    if not name:
        return hub
    seen, at = {name}, name
    while True:
        sess = sessions.get(at)
        if not minihub.runner(sess):
            return at
        up = sess.get("raised_by")
        if not up:
            return hub or name
        if up in seen or (sessions.get(up) or {}).get("state", "removed") == "removed":
            return name
        seen.add(up)
        at = up


def _rank(model, effort):
    """Price of a pair for comparison: model family, then effort; empty (endpoint model, Claude Code effort) — the most expensive."""
    m = str(model or "")
    family = next((i for i, f in enumerate(("haiku", "sonnet", "opus", "fable")) if f in m), 9)
    return family, EFFORTS.index(effort) if effort in EFFORTS else len(EFFORTS)


def economy(cfg, mine=None):
    """Limit-saving pair for `--work normal` (spec "mini-hub", §4; owner decision 2026-10-15) — {model, effort} or
    None: no downgrade. From "Settings → Main → Advanced → Executors" (executors.economy), sonnet/medium by default; own endpoint
    or model — empty (the sonnet alias may not exist there), only the queue. If the normal pair is already no more expensive, it stays.
    mine is the agent's own pair (own()): what it sets is the agent's choice and is not lowered, the rest is."""
    every = (cfg or {}).get("executors")
    own = every.get("economy") if isinstance(every, dict) else None
    own = own if isinstance(own, dict) else {}
    model, effort = ECONOMY(cfg)
    if "model" in own and model_ok(own["model"], cfg):
        model = own["model"]
    if "effort" in own and (own["effort"] in EFFORTS or own["effort"] == ""):
        effort = own["effort"]
    if not (model or effort) or _rank(model, effort) >= _rank(*pair("normal", cfg)):
        return None
    mine = mine or {}
    return {**({"model": model} if model and "model" not in mine else {}),
            **({"effort": effort} if effort and "effort" not in mine else {})} or None


def ECONOMY(cfg=None):   # noqa: N802 — like DEFAULTS: the hub environment may change
    """Default saving pair: own endpoint or model — empty (no downgrade), otherwise sonnet/medium. A hub entirely on
    Codex — empty: only what "Settings" name (the Claude pair does not apply there)."""
    return ("", "") if codex_only(cfg) or DEFAULTS()["normal"] == ("", "") else ("sonnet", "medium")


def pin(work, cfg, mine=None, task=None):
    """`--work` → the executor's model and effort (empty is not passed); an unknown kind — None. mine is the raiser's
    own pair (own()): what it sets replaces the kind's, and what it lacks (or has wrong) stays from the kind. Without
    --work the base is the pair the task already has (task: a risky task keeps its opus), else the kind the task has
    (its pair may be empty), `normal` if it has none."""
    if work is not None and work not in ("normal", "risky"):
        return None   # a typo is refused for an agent with its own pair too
    named = work is None and any((task or {}).get(k) for k in ("model", "effort"))
    had = (task or {}).get("work")
    had = had if had in ("normal", "risky") else None   # the kind a task was raised with, when --work is not given
    if work is None and not mine and (named or not had):
        return {} if named else None   # the task already names its pair: nothing to add; no pair and no kind: refused
    model, effort = ((task or {}).get("model") or "", (task or {}).get("effort") or "") if named else pair(work or had or "normal", cfg)
    model, effort = (mine or {}).get("model", model), (mine or {}).get("effort", effort)
    return {**({"model": model} if model else {}), **({"effort": effort} if effort else {})}


def rule(cfg, width=118):
    """The rule as one paragraph wrapped by words; risky goes first. In the reader's language (`hub task run` help and
    refusal: a session's process reads English, the owner's terminal his language)."""
    from core import i18n

    def show(work):
        return ", ".join(x for x in pair(work, cfg) if x) or i18n.t("core.modelrule.same")
    text = i18n.t("core.modelrule.rule", risky=i18n.t("core.modelrule.risky_when"), risky_pair=show("risky"),
                  normal_pair=show("normal"))
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = ""
        line = f"{line} {word}" if line else word
    return "\n".join(lines + [line])
