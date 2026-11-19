"""One run of a case on one code version (started by evals/run.py as a separate process).

  driver.py <version worktree> <run folder> <case.json>

The core and host code is taken from the version's worktree (sys.path), not from the current repo: "before" and "after" are honestly different cores.
The session under test is a real claude with its own CLAUDE_CONFIG_DIR (a copy of the login and a shortened conversation) and
none of the machine's CLAUDE.md files (common.machine_memory: home and above, above the run and the session's repo), the
others are a fake claude from tests/fakes (turns cost nothing). The result is events.jsonl: everything the core wrote after the cut.

Codex (experimental): a case with "host": "codex" (evals/run.py on a hub on Codex) — the core is a hub on Codex,
the session under test is a real Codex (CodexHost: CODEX_HOME = <run>/state/codex-home, the run's own; EVAL_CODEX — the
codex command); a cut_seq case resumes the copy of its thread (codex_threads), the others are the same fake claude.
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WT, RUN, CASE = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, WT)
from core import journal, spool  # noqa: E402
from core.daemon import Hub  # noqa: E402
from hosts.streamjson import StreamHost  # noqa: E402

FAKE = os.path.join(WT, "tests", "fakes", "fake_claude_stream.py")
case = json.load(open(CASE))
state = os.path.join(RUN, "state")
ccfg = os.path.join(RUN, "claude-config")
real = set(case["real"])
cwds = case.get("cwd") or {}  # session -> folder for the duration of the run (hub: its own hub-home with the version's CLAUDE.md)


CLAUDE = os.environ.get("EVAL_CLAUDE") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
sys.path.append(HERE)   # after the version's tree: its core is the one under test, the run's helpers come from here
from common import machine_memory  # noqa: E402


def without_machine_memory(cmd, name, cwd):
    """claude's command with the core's --settings plus claudeMdExcludes of the machine's own CLAUDE.md files
    (common.machine_memory): a copy of the settings per session in the run, the core's file is not touched; written
    whole and then put in place, a claude of the session starting at that moment reads the old copy or the new one."""
    at = cmd.index("--settings") + 1 if "--settings" in cmd[:-1] else None
    settings = {}
    if at:
        try:
            with open(cmd[at], encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            pass
    settings = settings if isinstance(settings, dict) else {}
    own = settings.get("claudeMdExcludes")   # the core's own, if a version under test has them, stay
    settings["claudeMdExcludes"] = [*(own if isinstance(own, list) else []), *machine_memory(os.path.expanduser("~"), RUN, cwd)]
    path = os.path.join(RUN, f"settings-{name}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return cmd[:at] + [path] + cmd[at + 1:] if at else cmd + ["--settings", path]


class EvalHost(StreamHost):
    """A real claude only for the sessions under test; the hub folder comes from the run."""

    def command(self, name, *a, **kw):
        cmd = super().command(name, *a, **kw)
        # the fake reads no CLAUDE.md; cwd: the folder the session starts in (an older core's command may not get it)
        return without_machine_memory(cmd, name, kw.get("cwd")) if name in real else cmd

    def spawn(self, name, session_id, resume=False, cwd=None, **kw):
        self.claude = CLAUDE.split() if name in real else [sys.executable, FAKE]
        # the fake needs no history (and on --resume without it it fails, like the real claude)
        return super().spawn(name, session_id, resume=resume and name in real, cwd=cwds.get(name, cwd), **kw)

    def reap(self, pid, session_id):
        return False  # a run next to a live hub — do not touch the processes of live sessions


class EvalHub(Hub):
    """No side effects outside the run — no git worktree in live repos, no forks, no accounts."""
    def ensure_dev(self):
        pass

    def fork(self):
        pass

    def switch_accounts(self):
        pass


cut = dict(case.get("codex_threads") or {})   # session -> the cut thread, until the core resumes it


def codex_host(stream):
    """The session under test on Codex, the others on the stream host (fake claude) — by name, not by model."""
    from hosts.codex import CodexHost, MuxHost, codex_bin

    class EvalCodexHost(CodexHost):
        def _thread_of(self, name, session_id):
            # a case cut from a Codex thread: its copy in the run's CODEX_HOME, whatever session id the core resumes —
            # once: a later resume inside the run goes by the core's own map (a new conversation is not sent back)
            return cut.pop(name, None) or super()._thread_of(name, session_id)

        def reap(self, pid, session_id):
            return False

    class EvalMux(MuxHost):
        def spawn(self, name, session_id, **kw):
            self.where[name] = "codex" if name in real else "stream"
            # the hub folder comes from the run (its CLAUDE.md of the version under test), as in EvalHost
            return self._h(name).spawn(name, session_id, **{**kw, "cwd": cwds.get(name, kw.get("cwd"))})

    own = json.load(open(env["HUB_CONFIG"])).get("codex") or {}
    codex = EvalCodexHost(state, codex=os.environ["EVAL_CODEX"].split() if os.environ.get("EVAL_CODEX") else (codex_bin(),),
                          env=env, all_sessions=True, default_model=own.get("model"), default_effort=own.get("effort"))
    return EvalMux(stream, codex)


# The run's own settings folder (core/catalog.config_dir: contacts, accounts, bus keys) for every case, not the owner's
# live one; a case's config_files (contacts and the like) are laid there by run.py. The rest of HUB_* is the hub session
# that started the run (its state, agent file, settings paths): dropped; HUB_NO_* only turn things off and HUB_LANG is a
# language, not a place; the gateway marks (core/profiles STUB_ENV, UPSTREAM_ENV) are the sign-in of a run started from a
# session on a pool account: the nested claude (hosts/nested) puts the token stub back from them, without them "Not logged in"
KEEP = ("HUB_LANG", "HUB_GATEWAY_STUB", "HUB_GATEWAY_UPSTREAM")
config = os.path.join(RUN, "config")
os.makedirs(config, exist_ok=True)
for name in [k for k in os.environ if k.startswith("HUB_") and not k.startswith("HUB_NO_") and k not in KEEP]:
    os.environ.pop(name)
env = {"CLAUDE_CONFIG_DIR": ccfg, "PATH": os.path.join(WT, "bin") + os.pathsep + os.environ.get("PATH", ""),
       "HUB_CONFIG": os.path.join(RUN, "hub.json"), "HUB_CONFIG_DIR": config, "HUB_STATE_DIR": state}
os.environ.update({k: env[k] for k in ("HUB_CONFIG", "HUB_CONFIG_DIR", "HUB_STATE_DIR")})
host = EvalHost(state, claude=["claude"], env=env)
if case.get("host") == "codex":
    host = codex_host(host)
hub = EvalHub(state, host, registry_dir=os.path.join(RUN, "registry"))
prefix = hub.state["seq"]
hub.start()
started = time.time()
try:   # the "before" version may lack the inbox check (core/admit.py)
    from core.admit import CORE_ONLY  # noqa: E402
except ImportError:
    CORE_ONLY = frozenset()
for ev in case["inject"]:  # what the session answered in the original
    data = {**ev.get("data", {}), "eval_inject": True}
    if ev["type"] in CORE_ONLY:   # only the core writes these (turn result, session words, question): the inbox rejects them —
        hub.ingest()              # straight into the journal, like the core; what was earlier in the inbox goes before it, the case order
        hub.record(ev["type"], ev.get("entity"), data, src="eval")
    else:
        spool.put(os.path.join(state, "spool"), ev["type"], ev.get("entity"), data, src="eval")
deadline = started + case.get("timeout", 900)
done_at = None
target = case["real"][0]
while time.time() < deadline:
    hub.tick()
    evs = [e for e in journal.read(hub.journal.path) if e["seq"] > prefix]
    injected = [e for e in evs if (e.get("data") or {}).get("eval_inject")]
    # the decision is visible: the turn is finished or the session asked the owner a question (no answer will come in a run — nothing to wait for)
    # the turn that began with the fed message (mN label in turn.started), not just any turn after it —
    # otherwise a turn for someone else's message from the queue is counted as the reply; several fed messages (a
    # built-in /compact, then a message) — the turn of the last one, once all of them are in the journal
    mark = f"m{injected[-1]['seq']}" if injected and len(injected) >= len(case["inject"]) else None
    began = [e["seq"] for e in evs if e["type"] == "turn.started" and e["entity"] == target and mark
             and mark in ((e.get("data") or {}).get("marker") or "")]
    if injected and done_at is None and began and any(e["type"] in ("turn.result", "permission.requested")
                                                      and e["entity"] == target and e["seq"] > began[0] for e in evs):
        done_at = time.time()
    if done_at and time.time() - done_at > case.get("settle", 8):
        break
    if any(e["type"] == "host.error" and e["entity"] == target and (e.get("data") or {}).get("do") == "spawn" for e in evs):
        break  # the session under test did not start — the run does not count (n/a), nothing to wait for
    time.sleep(0.3)
host.kill_server()
with open(os.path.join(RUN, "events.jsonl"), "w") as f:
    for e in journal.read(hub.journal.path):
        if e["seq"] > prefix:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
json.dump({"prefix": prefix, "finished": done_at is not None, "seconds": round(time.time() - started),
           **({"cut_unused": sorted(cut)} if cut else {})},
          open(os.path.join(RUN, "driver.json"), "w"))
if case.get("keep_compact_summary"):   # the summary Claude Code wrote on a compaction of the session under test (a case's
    # own synthetic conversation only, never a copy of the owner's): what the language checks after a compaction read
    import glob
    with open(os.path.join(RUN, "compact-summary.txt"), "w", encoding="utf-8") as out:
        for path in glob.glob(os.path.join(ccfg, "projects", "*", "*.jsonl")):
            for line in open(path, encoding="utf-8", errors="replace"):
                if '"isCompactSummary"' in line:
                    msg = json.loads(line).get("message") or {}
                    body = msg.get("content")
                    out.write((body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)) + "\n\n")
shutil.rmtree(os.path.join(ccfg, "projects"), ignore_errors=True)  # a copy of the owner's conversation — do not keep
