"""A critic from another model before an important decision: an option (owner decision 2026-10-06: an option in the hub,
configured through the hub and json). Before a decision on prod, money, architecture, storage, or a result the session
calls final, it hands the draft to `hub critic <file>`: another model only reads it and the evidence and tries to
refute it; the session fixes what holds and writes the owner a line "critic: <what it disputed> -> <what changed>".

hub.json:
  "critic": {"enabled": true,                 # turn on (the rule reaches sessions at their next start)
             "engine": "codex" | "claude",     # who criticizes; codex by default, if present
             "model": "<Codex model>",         # always explicit: without it Codex takes whatever is in its config.toml
             "effort": "high",                 # low | medium | high
             "claude_model": "sonnet"}         # fallback critic (no codex, model unavailable, limit): Claude
The draft goes to the chosen model: Codex is OpenAI; the owner turns it on.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import platforms
from core import i18n

DEFAULTS = {"engine": "codex", "model": "gpt-6-astra", "effort": "high", "claude_model": "sonnet"}
TIMEOUT = 900
# the rule in every session's prompt while the critic is on; the command's form and example are the help's (core/hubhelp.txt):
# 13.11, the rule's own `[--evidence <paths>]` gave 39 runs in a week with paths separated by spaces
NOTE_TEXT = ("The critic is on. Before a decision on prod, money, architecture or storage, a proposal of a mass launch and "
             "a result you call final — the draft into a file and `{form}`, e.g. `{example}`: another model "
             "will try to refute it. Fix what holds, drop what doesn't with a reason; to the owner one line in their "
             "language \"critic: <what it disputed> → <what changed>\". Not needed: a yes/no answer, a routine status.")


FORM = 'hub critic <draft> [--task "<the task in one line>"] [--evidence path,path]'   # if the help cannot be read


def note():
    """The rule with the help's form and example; a help that cannot be read (a broken line) does not take the sessions'
    prompts down with it (hosts/streamjson imports this module for every session): the form without an example."""
    try:
        from core import clihelp
        e = clihelp.load().entry("critic")
        return NOTE_TEXT.format(form=e.forms[0], example=e.examples[0])
    except Exception:  # noqa: BLE001 — any fault of the help file
        return NOTE_TEXT.replace(", e.g. `{example}`", "").format(form=FORM)


NOTE = note()   # the name a running core's hosts/streamjson reads
BRIEF = """You are a critic. Try to refute the draft: {draft}
Evidence: {evidence}. Change nothing, only read.
Find: what is not measured; numbers without a measurement; a swap of the task's scope (an answer to a different question
than asked: {task}); wrong or outdated sources.
Return a list of objections: draft point → why it doesn't hold → how to check. No objections — say so."""
FALLBACK = ("not supported", "rate limit", "usage limit", "quota", "unauthorized", "401", "429")


class Error(Exception):
    pass


def settings(cfg):
    c = cfg.get("critic") or {}
    return {**DEFAULTS, **{k: v for k, v in c.items() if v not in (None, "")}}


def enabled(cfg):
    return bool((cfg.get("critic") or {}).get("enabled"))


EFFORTS = ("low", "medium", "high")


def codex_fit(cfg, state_dir=None):
    """settings(cfg) for the critic on a hub on Codex: the model is one the hub's sign-in has (its model/list, read for
    the page: core/codexcli.models) — a plan without the default model would otherwise get a refusal on every call.
    A model named in critic.model stays as the owner wrote it; the list not read yet — the default as is; the default
    model listed — it; else Codex's own default model of the list (or its first one). The effort — the highest the
    model knows up to the wanted one."""
    s = settings(cfg)
    if (cfg.get("critic") or {}).get("model"):
        return s
    from core import codexcli, codexmodel
    listed = codexcli.models(codexmodel.state_dir_of(state_dir))[0]
    if not listed:
        return s
    pick = next((m for m in listed if m["id"] == s["model"]), None) or next((m for m in listed if m["default"]), listed[0])
    s["model"] = pick["id"]
    if pick["efforts"] and s["effort"] not in pick["efforts"]:
        top = EFFORTS.index(s["effort"]) if s["effort"] in EFFORTS else len(EFFORTS) - 1
        fits = [e for e in EFFORTS[:top + 1] if e in pick["efforts"]] or [e for e in EFFORTS if e in pick["efforts"]][:1]
        if fits:   # up to the wanted one; none that low — the model's lowest it knows
            s["effort"] = fits[-1]
    return s


def brief(draft, evidence, task):
    return BRIEF.format(draft=draft, evidence=", ".join(evidence) or "none — the draft only",
                        task=task or "not given — judge by the draft")


def codex_cmd(s, text, codex=None):
    """Model and effort are always explicit; read-only; works outside git too; no input (otherwise it waits on stdin)."""
    return [codex or shutil.which("codex") or "codex", "exec", "-m", s["model"], "-c",
            f"model_reasoning_effort={s['effort']}", "-s", "read-only", "--skip-git-repo-check", text]


def real_claude():
    """The real claude, bypassing the session wrapper: the critic sets its own isolation (core/realclaude.py). Kept as a
    name: core/modelcall of the previous release, still in a running process's memory, imports it from this file."""
    from core import realclaude
    return realclaude.find()


def claude_cmd(s, dirs):
    """Claude read-only, without settings and MCP. The brief goes on stdin: --add-dir takes all arguments after it, so
    a brief at the end of the command line would be taken for a folder."""
    cmd = [real_claude(), "-p", "--model", s["claude_model"], "--tools", "Read,Grep,Glob",
           "--allowedTools", "Read,Grep,Glob", "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence"]
    for d in dirs:
        cmd += ["--add-dir", d]
    return cmd


ACCOUNT = {"name": None}   # pool account the sessions are on now (main takes it from the state)


def model_env(account):
    """The critic's environment: no hub variables, Claude on the pool account like the sessions (sign-in: core/profiles.py)
    and without their model remap: the core's model call environment (modules/watch/watch.py:model_env). Sign-in from
    the env of the Claude Code settings (a session's Bash does not have it: the nested claude used a stale sign-in,
    "OAuth session expired", 08.10 and 12.10); the pool account, if enabled, overrides it."""
    from modules.watch import watch
    return watch.model_env(account=account)


def _run(cmd, cwd, stdin=None, env=None):
    if os.environ.get("HUB_NO_MODEL"):   # tests: do not call a live model
        raise Error(i18n.t("cli.critic.model_off"))
    try:
        # as bytes: the model's answer is UTF-8, while a cmd.exe error on Windows is in the console encoding; platforms
        # turns them into text (a reader thread with text=True failed on it with UnicodeDecodeError and the cause was lost)
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, cwd=cwd, input=(stdin or "").encode("utf-8"),
                           env=env or model_env(ACCOUNT["name"]))
    except subprocess.TimeoutExpired:
        raise Error(i18n.t("cli.critic.timeout", minutes=TIMEOUT // 60))
    except OSError as e:
        raise Error(str(e))
    out = platforms.current.output_text(r.stdout or b"").strip()
    if r.returncode or not out:
        raise Error((platforms.current.output_text(r.stderr or b"") + out).strip()[-500:]
                    or i18n.t("cli.critic.exit_code", code=r.returncode))
    return out


def review(cfg, draft, evidence=(), task=None, run=_run):
    """(who criticized, objections). Codex unavailable (missing, wrong model, limit): fall back to Claude."""
    from core import codexmodel
    s = codex_fit(cfg) if codexmodel.on_codex(cfg=cfg) else settings(cfg)
    draft = os.path.abspath(draft)
    evidence = [os.path.abspath(e) for e in evidence]
    for p in [draft] + evidence:
        if not os.path.exists(p):
            raise Error(i18n.t("cli.critic.no_file", path=p))
    text = brief(draft, evidence, task)
    dirs = sorted({p if os.path.isdir(p) else os.path.dirname(p) for p in [draft] + evidence})
    cwd = dirs[0] if dirs else tempfile.gettempdir()
    if codexmodel.on_codex(cfg=cfg):
        # Codex (experimental): a hub on Codex — Codex on the hub's Codex folder (its sign-in, not the personal
        # ~/.codex), wherever it is called from; there is no claude to fall back to
        try:
            return f"Codex {s['model']} · {s['effort']}", run(codex_cmd(s, text, codexmodel.binary()), cwd,
                                                               env=codexmodel.env())
        except Error as e:
            raise Error(i18n.t("cli.critic.codex_refused",
                               why=str(e).splitlines()[-1][:200] if str(e) else i18n.t("cli.critic.no_reason")))
    # a Claude hub: the personal ~/.codex only on the shared Claude sign-in, as before; a hub with its own Claude
    # sign-in — its own Codex folder, or Claude when that holds no sign-in (the spec of several hubs, 2.12)
    side = codexmodel.side_home(cfg=cfg) if s["engine"] == "codex" else None
    exe = codexmodel.binary() if side else shutil.which("codex")
    if s["engine"] == "codex" and side == "":
        why = i18n.t("cli.critic.no_codex_login", cmd=codexmodel.hub_command())
    elif s["engine"] == "codex" and exe and (not side or os.path.isfile(exe) or shutil.which(exe)):
        try:
            return f"Codex {s['model']} · {s['effort']}", run(codex_cmd(s, text, exe), cwd,
                                                               **({"env": codexmodel.env(at=side)} if side else {}))
        except Error as e:
            if not any(w in str(e).lower() for w in FALLBACK):
                raise
            why = str(e).splitlines()[-1][:120]
    else:
        why = "engine: claude" if s["engine"] == "claude" else i18n.t("cli.critic.codex_missing")
    out = run(claude_cmd(s, dirs), cwd, text)
    return (f"Claude {s['claude_model']}" + ("" if s["engine"] == "claude" else " " + i18n.t("cli.critic.instead", why=why)),
            out)


def parse(argv):
    """(draft, evidence, task) of the command's words. The draft is the first word; --evidence takes the words up to the
    next flag — paths with commas, with spaces, the flag repeated: one list (13.11: 39 runs in a week with paths
    separated by spaces were refused); `~` in a path is the home folder (after a comma the shell leaves it: 16 more)."""
    draft, evidence, task, after = [], [], None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--evidence", "--task"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise Error(i18n.t("cli.hub.needs_value", flag=a))
            if a == "--task":
                task = argv[i + 1]
            else:
                evidence += [p for p in argv[i + 1].split(",") if p.strip()]
            after, i = a == "--evidence", i + 2
            continue
        if after:   # one more path after the evidence
            evidence += [p for p in a.split(",") if p.strip()]
        else:
            draft.append(a)
        i += 1
    if len(draft) != 1:
        raise Error(i18n.t("cli.critic.extra_word", word=draft[1]) if draft else i18n.t("cli.critic.no_draft"))
    home = os.path.expanduser
    return home(draft[0]), [home(p.strip()) for p in evidence], task


def main(argv, cfg, account=None):
    ACCOUNT["name"] = account
    if not enabled(cfg):
        print(i18n.t("cli.critic.disabled"), file=sys.stderr)
        return 2
    try:
        try:
            draft, evidence, task = parse(list(argv))
        except Error as e:   # the form and an example after what is wrong
            from core import clihelp
            raise Error(f"{e}\n{clihelp.load().advice('critic')}")
        who, out = review(cfg, draft, evidence, task)
    except Error as e:
        print(f"hub critic: {e}", file=sys.stderr)
        return 2
    # the last line is an instruction to the model that called the critic: English, like NOTE
    print(f"{i18n.t('cli.critic.header', who=who)}\n\n{out}\n\nNext: fix what holds; to the owner one line in their "
          "language: \"critic: <what it disputed> → <what changed>\".")
    return 0
