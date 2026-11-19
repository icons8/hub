"""Codex: the bus liaison (core/liaison.py) on the codex host
(hosts/codex.py). It reads another hub's words, so it gets only `hub bus …`, `hub gist`, `hub help` and the owner's form.

With Claude its bans are Claude Code's: tools refused by name (core/liaison.DENY) and its guard
(hooks/liaison_guard.py) on every call. On Codex (slice H, probe 2026-11-05 on codex 0.162.1 with a fake Responses
API) the same guard stands in the core's hook — hooks/deny_hint.py sees the liaison's mark in its codex-rules.json
(hosts/codex_rules.thread) and every call of it goes through liaison_guard: Codex runs the hook for each of its own
tools (Bash, apply_patch, view_image, the goals, request_user_input). Codex's sandbox is a second wall, which Claude
has no counterpart of: the liaison's thread writes only the core's spool (where `hub …` puts its facts), no /tmp, no
network; the web search (it runs at OpenAI, no hook sees it) is off, and so are MCP servers.

Seams the host calls:
- SETTINGS — CodexHost.liaison_settings: a function (host) -> the hook file the liaison's bans stand in (daemon.ensure_dev
  checks that it is callable; StreamHost.command puts the result where claude's --settings would go — unused on Codex);
- check(liaison, **session) — first in CodexHost.spawn: nothing a liaison start cannot hold today;
- thread(params, **session) — the parameters of thread/start | thread/resume after hosts/codex_rules.thread
  (session carries liaison=True for the liaison).
"""
import os


def settings(host):
    return os.path.join(host.home, "hooks.json")


SETTINGS = settings


def check(liaison, **session):
    return None


def thread(params, **session):
    """The liaison's thread: never ask (it has nobody to ask but the owner, by its form), its sandbox writes only the
    spool, nothing else reaches it."""
    if not session.get("liaison"):
        return params
    from hosts.codex_rules import spool_root
    spool = spool_root(session["state_dir"])
    config = {**(params.get("config") or {}),
              "sandbox_workspace_write": {"writable_roots": [spool], "network_access": False,
                                          "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True},
              "web_search": "disabled", "mcp_servers": {}}
    return {**params, "approvalPolicy": "never", "sandbox": "workspace-write", "config": config}
