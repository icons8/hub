"""Agent Developer available from the start (owner decision 2026-10-02): an agent that creates new hub agents through
evals (role, cases, runs, prompt, CLAUDE.md, tools) and hands them over to the hub. System-level, like Hub Developer
(core/system.py, owner decision 2026-10-08): always present, the core restores it on every start. Its folder is
`agent-dev` in the state directory: drafts of roles and cases."""
import os

ABOUT = ("Agent Developer: creates new hub agents through evals (role → eval cases → runs → prompt, CLAUDE.md, tools) and "
         "tunes them from outside; then assigns them and hands them over to the hub")


def spec(cfg, state_dir):
    """(name, session.requested data): what it should look like (core/system.py restores it if missing)."""
    name = (cfg.get("agent_dev") or {}).get("name") or "agent-dev"
    cwd = os.path.join(state_dir, "agent-dev")
    os.makedirs(cwd, exist_ok=True)
    return name, {"cwd": cwd, "role": "agent", "about": ABOUT, "icon": "🧪", "tools": ["core:agent-developer"]}
