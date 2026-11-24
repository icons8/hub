"""Codex host (the hub's second engine): a session host on Codex CLI — `codex app-server`, JSON-RPC 2.0 as lines
over stdio, without a proxy. A session with the model `codex/<model>` (hub session model) goes here, the others to claude
(MuxHost). Turned on by the hub environment HUB_CODEX=1; without it the host is not created. A hub entirely on Codex is
hub.json `"host": "codex"` and `"codex": {"model": …, "effort": …}`: claude is not needed at all, every session is Codex.
The hub's Codex sessions have their own folder — CODEX_HOME = <state folder>/codex-home (sign-in, settings,
conversations), not the owner's personal ~/.codex: its rules, MCP, connectors and memory do not reach a hub session.

Codex has no hooks for the core — the core's facts come from the app-server event stream:
- session.started — the answer to thread/start | thread/resume (a Codex conversation is a thread; the host keeps its id);
- turn.started with the label [hub:mN] — the userMessage item (the input text as Codex took it): delivery by fact;
- session.said — an agentMessage item; turn.tool / turn.tool_result — commandExecution, fileChange, mcpToolCall…;
- turn.stopped + turn.result — turn/completed; the result text is all the answer's messages of the turn (phase
  final_answer), not only the last one (a question to the owner is sometimes in an earlier one);
- a question to the owner (like AskUserQuestion) — the request item/tool/requestUserInput (the turn waits for the answer)
  or an agentMessage with a questions field — the tool request_user_input_async (the turn does not wait: the answer
  comes as the next message).
A message to an idle session is turn/start, mid-turn turn/steer (Codex inserts it into the running turn).

The process is a StreamProcess (release waits for the exit in
the host's own thread, StreamHost._let_go), the environment goes through the modules' launch.env like claude's, the
session files get the user's umask (core/perms.py). MCP status and sign-in
(codex_mcp), the bus liaison (codex_liaison), subagents (codex_items) and permission modes (codex_rules).

Seams: the host's parts live in files of their own, so that different parts can be edited
independently — codex_limits (sign-in, limits), codex_rules (bans, modes, permissions), codex_cmds (commands typed into
a session), codex_mcp (MCP), codex_items (steps, texts, subagents), codex_life (recap, orphans, after a compaction),
codex_liaison (the bus liaison), codex_set (config.toml, the catalog's set); each file lists its own seams. Common ones,
called in SEAMS order on every module that has them:
- start(proc) — the thread is ready (thread/start | thread/resume answered);
- info(proc) -> dict — added to the session.info of the start (a later module's key wins);
- notice(proc, name, method, params) -> facts — a notification the host does not parse itself;
- response(proc, name, method, params, msg) -> facts | None — the answer to a request: the first non-None wins, the host
  does not touch it further;
- request(proc, name, rid, method, params) -> facts | None — a Codex request the host does not know itself; nobody took
  it — an error -32601 to Codex;
- answer_request(proc, method, data, response) -> result | None — the owner's answer (respond) to a card such a request
  put in proc.asks: what goes to Codex;
- before_turn(proc) — right before every turn/start: a slice may fix what it carries (proc.model, proc.effort);
- turn_done(proc, name, turn) -> facts — after a session's turn (not a compaction turn), before the next input goes;
- compacted(proc, name) -> facts — after a compaction, before the next input goes;
- item(proc, name, method, item) -> facts — every item/started | item/completed of the session's own thread, after the
  host's own facts of it (steps that outlive the turn, subagents a step spawned);
- codex_items.child(proc, name, method, params) — everything of another thread of the process (a subagent);
  after it and after item the host flushes the input queue: a slice may queue input (proc.backlog) there;
- interrupt(proc, name) — the session is interrupted (hub session interrupt, Esc), after the host's turn/interrupt of its
  own turn, whether or not a turn is running;
- compacted_inside(proc, name) -> facts — Codex compacted by itself inside a session's turn (the turn goes on).
proc.session_info is the session.info the host gave the core last (the core replaces info whole): a slice that changes a
field (a model, an effort) gives ("session.info", name, {**proc.session_info, field: …}) and stores that dict back.
proc.turn_extra["additionalContext"] is {source: text}: a slice writes its own source key and does not read the others;
the next turn/start carries them all as additionalContext ({source: {"kind": "application", "value": text}}, the
schema's AdditionalContextEntry). A good answer to that turn/start clears what it carried (a key rewritten meanwhile
stays), a refusal keeps it for the retry (_again); turn/steer does not carry it.
"""
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import platforms
from core import codexcli, i18n, perms
from hosts import (codex_cmds, codex_items, codex_liaison, codex_life, codex_limits, codex_mcp, codex_rules,
                   codex_set)
from hosts.codex_items import TOOL_NAMES, answer as _answer, tool as _tool, tool_result as _tool_result  # noqa: F401
from hosts.codex_set import CONFIG, KEEP_TABLES, write_config  # noqa: F401 — their place is codex_set
from hosts.common import existing_cwd
from hosts.streamjson import OOM_SESSION, RELEASED, SKILLS, SKILLS_CODEX, StreamHost, StreamProcess, _clip, _close, _result_text

PREFIX = "codex/"
# Claude Code's sign-in and gateway are not for Codex (nor for its commands): the token would go to a foreign process and its steps
CLAUDE_ENV = ("CLAUDE", "ANTHROPIC", "HUB_ACCOUNT", "HUB_REAL_CLAUDE", "HUB_NESTED", "HUB_OWNER_CLAUDE", "MCP_TIMEOUT")
# Codex asks the owner itself with the request_user_input tool only with this feature (0.162: "under development")
FEATURES = ("default_mode_request_user_input",)
# the "ask me" sentence — pilot 2026-10-19: to a direct "ask me" Codex answered "by your rule no question is needed" (it
# reads the cards rule's "decide yourself" literally); the last one — prompt evals 2026-11-05 (evals/prompts.py --host
# codex, the same cases as Claude, x8): Codex chose itself between variants that differ in what leaves (cards-owner
# rollout-prep 0/8 against Claude 5/8: "preparing is not the rollout"); "what users get" in it carried the cards of
# technical edges to the owner too (role-removal-edge 0/3) — only what leaves. Only for Codex sessions — the Claude
# sessions' prompt does not change
NOTE = ("You work in Codex CLI under the hub. The hub's commands (`hub send`, `hub task note`, `hub waiting`…) are in the "
        "shell; how to use them — the hub-base:hub skill. A question to the owner — with the request_user_input tool. You "
        "do not have Claude Code's tools (AskUserQuestion, TaskStop, run_in_background) — read the rules about them as "
        "about your own counterparts. The owner directly asks you to ask them — ask, even if the choice is technical: the "
        "\"decide yourself\" rule is about the cases when they did not ask. If the choice came down to options that "
        "differ in what goes outside (data to third-party services, money), it is their decision at the "
        "preparation step too: preparing for one option is already choosing it.")
GONE = "codex.gone"   # the reader's mark after a process's last line (reader -> _line)
RESUME_AGAIN = "codex.resume_again"   # thread/resume once more: the previous process still writes the thread (_line)
WRITER_WAIT, WRITER_TRIES = 2.0, 15   # s between tries, how many: release waits for the exit, this is the margin
RETRIES = 3   # turn/start|steer refused with the same input — a race (the turn just started or ended), not more
UNSUPPORTED_MCP = codex_mcp.UNSUPPORTED
SEAMS = (codex_limits, codex_cmds, codex_mcp, codex_items, codex_life, codex_rules, codex_liaison)


def seams(name):
    """The functions of the common seam `name` (see the module's docstring), in SEAMS order."""
    return [f for f in (getattr(m, name, None) for m in SEAMS) if f is not None]


def codex_bin(state_dir=None):
    """The codex binary: HUB_CODEX_BIN, the hub's own copy (<state>/bin/codex: another hub of the machine put it there,
    not into PATH — core/codexinstall.own_place; state_dir — that hub's folder, this process's hub by default), PATH,
    ~/.local/bin (a service started without the user's PATH) — one search for the host, the sign-in, hub doctor, the
    core's model, the critic and the voice."""
    from core import codexinstall, runtime
    state = state_dir or runtime.state_dir()
    own = codexinstall.own_place(state) if state else ""
    found = os.environ.get("HUB_CODEX_BIN") or (own if own and os.path.isfile(own) else None) or shutil.which("codex")
    # which() in the folder, not the bare name: Windows' is codex.exe there, as PATHEXT names it
    return found or shutil.which("codex", path=os.path.expanduser(os.path.join("~", ".local", "bin"))) or "codex"


def _found_codex(state_dir):
    return [codex_bin(state_dir)]


def is_codex(model):
    return str(model or "").startswith(PREFIX)


def codex_env(env):
    """The environment of the codex process: without Claude Code's sign-in and gateway."""
    return {k: v for k, v in env.items() if not k.startswith(CLAUDE_ENV)}


# the mark of a Codex session for the hook (hooks/deny_hint.on_codex): its CODEX_HOME may be an account folder of the pool
ON_CODEX_ENV = {"HUB_ON_CODEX": "1"}

# the Codex account pool (core/codexslot.py): what an account folder shares with the hub folder — the threads' store,
# its writer locks (without the shared one Codex lets a second writer in: probe 2026-11-05) and the MCP sign-ins' locks
# (folders: a link), the MCP sign-ins themselves (a file: a hard link — Codex refuses to write it through a symlink,
# "Symbolic link loop", and writes it in place)
SHARED_DIRS = ("sessions", "archived_sessions", "thread-writer-locks", "mcp-oauth-locks")
SHARED_FILE = ".credentials.json"
PREPARE_AGAIN = 60   # an account folder whose preparation failed: tried again no sooner, its reason meanwhile


def prepare_account(state_dir, home):
    """An account folder of the Codex pool, ready for a session: the core's settings, the links to the hub folder, the
    MCP sign-ins file shared. -> None, or why the folder is not fit (a real folder with something in it where a link
    must be: never removed, never merged). The MCP sign-ins diverged (the file was made anew in one of them): a log
    line, not a refusal — the session works, its MCP may ask to sign in again."""
    hub = os.path.abspath(codexcli.home_of(state_dir))
    try:
        write_config(home)
        for d in SHARED_DIRS:
            target, link = os.path.join(hub, d), os.path.join(home, d)
            os.makedirs(target, exist_ok=True)
            if os.path.lexists(link):
                if os.path.realpath(link) == os.path.realpath(target):
                    continue
                if os.path.isdir(link) and not os.path.islink(link) and not os.listdir(link):
                    os.rmdir(link)   # an empty one Codex made before the link (a sign-in in the fresh folder)
                else:
                    return i18n.t("core.codex.not_a_hub_link", folder=d)
            platforms.current.link_dir(target, link)
        cred, mine = os.path.join(hub, SHARED_FILE), os.path.join(home, SHARED_FILE)
        if not os.path.exists(cred):
            fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("{}")
        if not os.path.lexists(mine):
            os.link(cred, mine)
        elif not os.path.samefile(cred, mine):
            print(f"codex: the MCP logins of folder {home} diverged from the hub folder — the account's MCP may ask to log in again",
                  file=sys.stderr, flush=True)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


class CodexProcess(StreamProcess):
    """A session's `codex app-server`: the StreamProcess fields (release, stop, tree) plus the thread and turn state."""

    def __init__(self, popen, session_id, resume):
        super().__init__(popen)
        self.session_id = session_id   # the session id in the core (Codex has its own thread id)
        self.resume = resume
        self.thread = None             # the Codex thread id, once thread/start|resume answered
        self.turn = None               # the id of the running turn
        self.starting = False          # turn/start sent, no answer yet
        self.backlog = []              # input until the thread is ready or while a turn is starting
        self.calls = {}                # our request id -> (method, params)
        self.next_id = 1
        self.asks = {}                 # the core's request_id -> (Codex request id, method, data)
        self.last_text = ""            # the turn's last agentMessage (or an error) — the result if there are no phases
        self.texts = []                # (phase, text) of the turn's messages
        self.usage = None              # tokenUsage of the last model call (last — it, total — since the thread began)
        self.spent = [0, 0]            # tokens of the running turn: the sum of last over all its calls (in, out)
        self.compacting = None         # id of the compaction turn
        self.compact_before = None     # conversation length before compaction
        self.model = None
        self.info = {}
        self.start_params = None
        self.refused = {}              # id of an input list -> how many times Codex refused turn/start|steer with it
        self.message_of = {}           # id of an input list -> the core's message id it carries (type)
        self.had_input = False         # the running turn took input (userMessage): not a compaction turn
        self.turn_extra = {"additionalContext": {}}   # the slices' parameters of the next turn/start (module docstring)
        self.items = {}                # fileChange steps of the running turn by id: their approval names no files (codex_rules)
        self.session_info = {}         # the last session.info given to the core: a slice changing a field gives {**it, …}
        self.held = set()              # slices' tokens: input waits while any is here (codex_life: a compaction's context)
        self.host = None               # its host (spawn): settings for a slice (codex_rules), lines and input flushes (codex_life)

    def call(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.calls[rid] = (method, params)
        self.write({"id": rid, "method": method, "params": params})
        return rid


def _questions(item):
    """The request_user_input_async questions of an agentMessage (AsyncUserInputQuestion: title, options) with a title."""
    qs = item.get("questions")
    return [q for q in qs if isinstance(q, dict) and q.get("title")] if isinstance(qs, list) else []


def _file_steps(proc, method, p):
    """proc.items: fileChange steps by id while they can be asked about — their approval request carries only the
    step's id (codex_rules). A step's own end, its thread's turn's start or end drops it: a subagent's steps outlive the
    session's turn and go with their own thread's turn."""
    items, threads = vars(proc).setdefault("items", {}), vars(proc).setdefault("item_threads", {})
    thread = p.get("threadId") or proc.thread
    item = p.get("item") or {}
    if method in ("item/started", "item/completed") and item.get("type") == "fileChange" and item.get("id"):
        if method == "item/started":
            items[item["id"]], threads[item["id"]] = item, thread
        else:
            items.pop(item["id"], None)
            threads.pop(item["id"], None)
    elif method in ("turn/started", "turn/completed", "thread/closed"):
        # and with the session's own turn, the steps of a thread no longer its or its subagents' (a /clear's old thread)
        live = {proc.thread, *getattr(getattr(proc, "cx_items", None), "parent_of", {})}
        own = thread == proc.thread and method != "thread/closed"
        for done in [i for i in items if threads.get(i, proc.thread) == thread or (own and threads.get(i) not in live)]:
            items.pop(done, None)
            threads.pop(done, None)


def _detached(message):
    """The core's answer "the question is shown as a card" — exactly reducer.ASK_DETACHED: not forwarded to the session."""
    from core.reducer import ASK_DETACHED
    pattern = re.escape(ASK_DETACHED).replace(re.escape("{qid}"), r"q\w+").replace(re.escape("{hours}"), r"\d+")
    return bool(re.fullmatch(pattern, message))


class CodexHost(StreamHost):
    kind = "stream"   # for the core — like stream-json: the same actions
    codex_models = True   # codex/… models are known to the fold (core/daemon.codex_tick -> codex.host)
    liaison_settings = codex_liaison.SETTINGS   # the liaison is raised on Codex too (daemon.ensure_dev checks it is callable)

    def __init__(self, state_dir, codex=None, env=None, all_sessions=False, default_model=None,
                 default_effort=None):
        super().__init__(state_dir, env=env)
        self.codex = codex   # None: codex_bin at every use (the property below)
        self._fresh = True   # we assemble the suffix ourselves (command), `claude --help` is not needed
        self.all_sessions = all_sessions        # the hub entirely on Codex: sessions without a codex/… model too
        self.default_model = default_model      # the hub's model for them (empty — Codex's default model)
        self.default_effort = default_effort
        self.home = os.path.join(state_dir, "codex-home")
        write_config(self.home)
        codex_rules.prepare_home(self.home, codex=self.codex,
                                 env={**codex_env({**os.environ, **self.env}), "CODEX_HOME": self.home},
                                 find=self.finder())
        self.trusted_homes = {os.path.abspath(self.home)}   # the Codex pool's account folders whose hook trust is written
        self.readying = set()            # account folders being prepared in the background (prepare_codex)
        self.prepare_failed = {}         # a folder whose preparation failed -> (when, why)
        self.readying_lock = threading.Lock()
        self.prepare_lock = threading.Lock()   # one preparation at a time: its app-servers share the hub folder's store

    @property
    def codex(self):
        """The codex command: the one given, or codex_bin at every use — a hub switched to Codex on the band comes up
        before Codex CLI is in place (or the person installs it later): a name taken once at the start would stay a
        name to nothing, the hook's trust would never be written and every spawn refused (2026-11-19)."""
        return list(self._codex) if self._codex is not None else _found_codex(self.state_dir)

    @codex.setter
    def codex(self, value):
        self._codex = None if value is None else list(value)   # an empty one: no codex (no trust check, no sandbox)

    def finder(self):
        """codex_rules' search of the command anew (its trust retries): codex_bin of this hub's folder, not the host."""
        return None if self._codex is not None else functools.partial(_found_codex, self.state_dir)

    def prepare_codex(self, home):
        """An account folder of the Codex pool for sessions -> (ready, why). Not fit (codexslot.fit, its links:
        prepare_account) — (False, why) at once, while it is being prepared too. Fit: its hook's trust and, on Windows,
        Codex's sandbox are app-server runs — seconds, a minute when Codex does not answer — so they go in the
        background, one folder at a time: (False, None) while it runs, (True, None) after — while the core's hook file is
        there (a folder made anew is prepared again); a preparation that failed — (False, its reason) for PREPARE_AGAIN,
        then it is tried again. hubd's tick never waits for it:
        the core starts it when it takes the rows (codex.pool), and a session's spawn into the folder waits as a delayed
        action (core/daemon wait_codex_folder). A trust that failed is the hook's (features() refuses, trusted() retries)."""
        from core import codexslot
        key = os.path.abspath(home)
        with self.readying_lock:
            if key in self.readying:   # its own files are being written: only the quick check meanwhile
                return False, codexslot.fit(self.state_dir, home)
            why = codexslot.fit(self.state_dir, home) or prepare_account(self.state_dir, home)
            if why:
                return False, why
            if key in self.trusted_homes:
                if os.path.isfile(os.path.join(home, "hooks.json")):
                    return True, None
                self.trusted_homes.discard(key)   # made anew: without the core's hook its sessions would have no bans
            failed = self.prepare_failed.get(key)
            if failed and time.monotonic() - failed[0] < PREPARE_AGAIN:
                return False, failed[1]
            self.readying.add(key)
        threading.Thread(target=self._prepare, args=(home,), name="codex-folder", daemon=True).start()
        return False, None

    def _prepare(self, home):
        key, why = os.path.abspath(home), "the folder's preparation was cut short"   # not ready unless it ran to the end
        try:
            with self.prepare_lock:
                codex_rules.prepare_home(home, codex=self.codex,
                                         env={**codex_env({**os.environ, **self.env}), "CODEX_HOME": home},
                                         args=codexcli.args(self.state_dir, home), find=self.finder())
            why = None
        except Exception as e:  # noqa: BLE001 — the background is not silent: the spawns get the reason, a retry later
            why = f"the folder's preparation failed: {type(e).__name__}: {e}"[:300]
            print(f"codex: the account folder {home} cannot be prepared: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        finally:
            with self.readying_lock:
                if why:
                    self.prepare_failed[key] = (time.monotonic(), why)
                else:   # a trust that failed is the hook's: features() refuses, trusted() retries
                    self.trusted_homes.add(key)
                    self.prepare_failed.pop(key, None)
                self.readying.discard(key)

    def folder(self, name, account, home, hub_account=None):
        """(CODEX_HOME, account) of a launch: the account's folder from the core's state (codex.pool) — the hub folder
        for its row; a folder that is not fit now (gone, a link, open to others, its links cannot be made) or still being
        prepared — the hub folder as the hub row's account (hub_account; none — unmarked), with a log line: the core
        moves the pool off a folder that is not fit."""
        if not account or not home or os.path.abspath(home) == os.path.abspath(self.home):
            return self.home, account if account and home else None
        ready, why = self.prepare_codex(home)
        if not ready:   # not fit, or still being prepared after the spawn's wait (core/daemon CODEX_FOLDER_WAIT)
            print(f"codex: {name}: account {account} ({home}) — {why or 'the folder is still being prepared'}; starting in the hub folder",
                  file=sys.stderr, flush=True)
            return self.home, hub_account
        return home, account

    def pick(self, model, effort):
        """(model, effort) of Codex for a launch: codex/<model> — that one; on a hub on Codex any other (sonnet, opus — the
        executor pairs of --work, the settings' model) — the hub's model: those are Claude names Codex does not know; the
        session's own effort (hub session effort, the page, an executor pair) holds, otherwise the hub's."""
        if is_codex(model):
            return model[len(PREFIX):], effort or self.default_effort
        if self.all_sessions:
            return self.default_model, effort or self.default_effort
        return None, effort

    # --- the Codex thread at the host: core session id -> thread id ---------------------------------------------------
    def _threads_path(self, name):
        return os.path.join(self.state_dir, "sessions", name, "codex-threads.json")

    def _thread_of(self, name, session_id):
        try:
            with open(self._threads_path(name), encoding="utf-8") as f:
                return json.load(f).get(session_id)
        except (OSError, ValueError, AttributeError):
            return None

    def _remember(self, name, session_id, thread):
        path = self._threads_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                known = json.load(f)
        except (OSError, ValueError):
            known = {}
        known = known if isinstance(known, dict) else {}
        known[session_id] = thread
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(known, f)
        os.replace(path + ".tmp", path)

    # --- launch -----------------------------------------------------------------------------------------------------
    def instructions(self, name, session_id, agent_file, **kw):
        """The core's suffix (what claude gets: language, rules, the agent list, mini-hub…) + the agent's own CLAUDE.md +
        the line about Codex — the thread's developerInstructions."""
        cmd = self.command(name, session_id, agent_file=agent_file, **kw)
        text = ""
        for i, a in enumerate(cmd[:-1]):
            if a == "--append-system-prompt-file":
                with open(cmd[i + 1], encoding="utf-8") as f:
                    text = f.read()
            elif a == "--append-system-prompt":
                text = cmd[i + 1]
        text = text.replace(SKILLS, SKILLS_CODEX)   # no Skill tool in Codex: read the SKILL.md
        return (text.rstrip() + "\n\n" + NOTE).strip()

    def _env(self, name, agent_file, info, home=None):
        """The process environment: as claude's (owner's environment, the hub's variables, the modules' launch.env —
        secrets by folder and the like), without Claude's sign-in, gateway and the claude wrapper, with the hub's CODEX_HOME.
        A Claude sign-in module (account, session_providers) has nothing to give Codex — not asked."""
        from core import modlaunch
        added = modlaunch.adds(self.modules, {"session": name, **info}) if self.modules else []
        env = self._launch_env(name, None, agent_file, added)
        return {**codex_env(env), "CODEX_HOME": home or self.home, **ON_CODEX_ENV}

    def spawn(self, name, session_id, resume=False, cwd=None, mode=None, tools=(), project="all", kind="work",
              profile=None, about=None, agents=(), effort=None, model=None, executor_of=None, lang=None,
              minihub=False, uncommitted=None, bus=None, ui_lang=None, liaison=False, account=None, home=None,
              hub_account=None, spec=None, spec_boss=False, **_):
        codex_liaison.check(liaison, name=name, kind=kind)
        if self.alive(name):
            return "already running"
        if name in self.procs:
            _close(self.procs.pop(name))
        cwd = existing_cwd(cwd)
        agent_file = self.agent_file(profile, name, about, ui_lang) if profile else None
        where, account = self.folder(name, account, home, hub_account)   # the Codex account pool: its CODEX_HOME
        env = self._env(name, agent_file, {"cwd": cwd, "kind": kind, "model": model, "account": None, "agent": profile,
                                            "resume": bool(resume)}, home=where)
        text = self.instructions(name, session_id, agent_file, executor_of=executor_of, agents=agents, cwd=cwd,
                                 lang=lang, minihub=minihub, uncommitted=uncommitted, bus=bus, liaison=liaison,
                                 spec=spec, spec_boss=spec_boss)   # the spec line and the raiser's rule: as with claude
        roots, mcp = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "hub-base",
                                   "skills")], {}
        flags = self.tool_flags(name=name, tools=tools, kind=kind, cwd=cwd, project=project, effort=effort, model=None)
        # the liaison reads another hub's words: no repo of its folder — neither its hooks and skills nor its rules
        extra = codex_set.collect(flags, roots, mcp, env, cwd=None if liaison else cwd,
                                  trust_at=codex_rules.flags_path(self.home))
        session = dict(name=name, kind=kind, mode=mode, minihub=minihub, bus=bus, executor_of=executor_of, cwd=cwd,
                       state_dir=self.state_dir, agent_file=agent_file, liaison=bool(liaison),
                       sources=codex_set.setting_sources(flags), home=where)
        enabled = codex_rules.features(FEATURES + tuple(codex_items.features(**session)), **session)
        cmd = self.codex + ["app-server", *codexcli.args(self.state_dir, where), *codex_set.args(enabled),
                            *[x for f in enabled for x in ("--enable", f)]]
        err = open(os.path.join(self.logs, f"{name}.stderr.log"), "a")
        # session files in projects get the user's umask, not the hub's closed one (core/perms.py)
        popen = subprocess.Popen(platforms.current.below_hub(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=err, text=True, encoding="utf-8", bufsize=1, cwd=cwd, env=env,
                                 umask=perms.user_umask(),
                                 **platforms.current.background())   # below the hub parts, as claude (streamjson)
        err.close()
        platforms.current.oom_first(popen.pid, OOM_SESSION)
        thread = self._thread_of(name, session_id) if resume else None
        proc = CodexProcess(popen, session_id, resume=bool(thread))
        proc.host = self   # the slices' seams that need the host's settings (codex_rules: permissions.auto_allow)
        proc.model, proc.effort = self.pick(model, effort)
        proc.state_dir = self.state_dir   # the hub's folder: a slice's own files of the session (codex_items: subagents)
        proc.sources = session["sources"]   # the repo's settings the session reads (codex_rules: its allow rules)
        proc.account, proc.home = account, where   # the Codex account pool: its facts are this account's (codex_limits)
        self.procs[name] = proc
        self.note_prompt(name)
        proc.tree = platforms.current.session_tree(popen)
        threading.Thread(target=self.reader(name, popen), daemon=True).start()
        # experimentalApi: 0.162.1 refuses turn/start.additionalContext (proc.turn_extra) without it, and
        # thread/backgroundTerminals/terminate (codex_items: an interrupted turn's command) needs it (live probes 2026-11-05)
        proc.write({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "agents-hub", "version": "exp"},
                                                                 "capabilities": {"experimentalApi": True}}})
        proc.write({"method": "initialized"})
        proc.call("skills/extraRoots/set", {"extraRoots": roots})
        # the core's bypassPermissions is Codex's "never ask" without a sandbox (the hub's sessions work like that anyway)
        config = codex_set.thread_config(mcp, env, **extra)
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access",
                  "developerInstructions": text, **({"model": proc.model} if proc.model else {}),
                  **({"config": config} if config else {})}
        params = codex_liaison.thread(codex_rules.thread(params, **session), **session)
        proc.start_params = params
        if thread:
            proc.call("thread/resume", {**params, "threadId": thread})
        else:
            proc.call("thread/start", params)
        return "spawned"

    # --- output -----------------------------------------------------------------------------------------------------
    def _put_inbox(self, name, msg):
        with self.lock:
            self.inbox.put((name, msg, self.clock()))

    def reader(self, name, popen):
        def read():
            for line in popen.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    with self.lock:
                        self.inbox.put((name, msg, self.clock()))
            with self.lock:   # after its last line: the process is gone, no step of it will be asked about
                self.inbox.put((name, {"type": GONE, "popen": popen}, self.clock()))
        return read

    def _line(self, name, msg):
        if msg.get("type") == RELEASED:   # the exit of a released process (StreamHost._let_go): host.done release
            return super()._line(name, msg)
        proc = self.procs.get(name)
        if proc is None:
            return []
        if msg.get("type") == RESUME_AGAIN:   # the timer of an "active writer" refusal (below): the same process only
            if proc.popen is msg.get("popen") and proc.thread is None:
                proc.call("thread/resume", msg["params"])
            return []
        if msg.get("type") == GONE:   # the reader saw the end of its stdout (reader): its file steps go
            if proc.popen is msg.get("popen"):   # not a later process of the same name
                getattr(proc, "items", {}).clear()
                getattr(proc, "item_threads", {}).clear()
            return []
        if "method" not in msg and "id" in msg:
            return self._response(name, proc, msg)
        method, p = msg.get("method"), msg.get("params") or {}
        if "id" in msg:
            return self._request(name, proc, msg["id"], method, p)
        facts = []
        _file_steps(proc, method, p)   # any thread's: a subagent's file step is asked about by its id too (codex_rules)
        if p.get("threadId") and proc.thread and p["threadId"] != proc.thread:
            facts = codex_items.child(proc, name, method, p)   # a Codex subagent (its own thread) — not the session's remarks
            self._flush(proc)   # the slice may have queued input (a subagent ended while the session was idle)
            return facts
        if method == "turn/started":
            proc.turn = (p.get("turn") or {}).get("id")
            proc.starting = False
            proc.last_text = ""
            proc.texts = []
            proc.compacting = None
            proc.had_input = False
            proc.spent = [0, 0]
            self._flush(proc)
        elif method in ("item/started", "item/completed"):
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "contextCompaction" and (method == "item/started" or not (proc.compacts and not proc.had_input)):
                # the core's or Codex's own: the page says it is not a lag; the core's compaction turn (no input, below)
                # ends with session.compacted alone
                facts.append(("session.compacting", name, {"on": method == "item/started"}))
            if kind == "contextCompaction" and proc.compacts and not proc.had_input:
                # a turn without input is the compaction turn of thread/compact/start; Codex's own compaction inside
                # a turn that took input is not — its turn/completed is the session's result
                proc.compacting = p.get("turnId")   # the compaction turn (thread/compact/start) — not the session's turn
            elif kind == "contextCompaction" and method == "item/completed":
                # Codex compacted by itself inside a session's turn (0.162.1: before the turn's input or after a step,
                # no thread/compacted) — the slices' compacted_inside(proc, name) -> facts
                facts += [f for done in seams("compacted_inside") for f in done(proc, name)]
            elif kind == "userMessage" and method == "item/started":
                proc.had_input = True
                prompt = "\n".join(c.get("text", "") for c in item.get("content") or [] if c.get("type") == "text")
                from hooks.hub_hook import translate   # not at import: the hosts package imports without hooks/ (smoke step)
                facts.append(translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt}))
            elif kind == "agentMessage" and method == "item/completed" and _questions(item):
                # a question — as a card; its text goes neither into the feed as a remark nor into the turn's result (as
                # AskUserQuestion with Claude): the feed would show it twice, the watcher would mark "awaits you" a second time
                facts.append(self._async_ask(name, proc, item))
            elif kind == "agentMessage" and method == "item/completed" and (item.get("text") or "").strip():
                proc.last_text = item["text"]
                proc.texts.append((item.get("phase"), item["text"]))
                facts.append(("session.said", name, codex_items.said(item)))
            elif method == "item/started" and codex_items.tool(item):
                tname, inp = codex_items.tool(item)
                facts.append(("turn.tool", name, {"id": item.get("id"), "name": tname, "input": _clip(inp)}))
            elif method == "item/completed" and codex_items.tool(item):
                error, text = codex_items.tool_result(item)
                facts.append(("turn.tool_result", name, {"id": item.get("id"), "error": bool(error),
                                                         "text": _result_text(text), **codex_items.result_fields(item)}))
            facts = [(f[0], name, f[1]) if len(f) == 2 else f for f in facts]
            facts += [f for seen in seams("item") for f in seen(proc, name, method, item)]
            self._flush(proc)
        elif method == "thread/tokenUsage/updated":
            # an update on every model call of a turn (probe 19.10: two calls — two updates); the turn is the sum of last
            proc.usage = p.get("tokenUsage") or {}
            last = proc.usage.get("last") or {}
            proc.spent = [proc.spent[0] + (last.get("inputTokens") or 0), proc.spent[1] + (last.get("outputTokens") or 0)]
        elif method == "turn/completed" and proc.compacting and proc.compacting == (p.get("turn") or {}).get("id"):
            # compaction is over — like Claude Code's answer to /compact: no turn, only the fact
            proc.compacts -= 1
            proc.turn = proc.compacting = None
            facts.append(("session.compacted", name, {"ok": (p.get("turn") or {}).get("status") == "completed",
                                                      "before": proc.compact_before, "after": None}))
            facts += [f for done in seams("compacted") for f in done(proc, name)]
            self._flush(proc)
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            proc.turn, proc.starting = None, False
            last = (proc.usage or {}).get("last") or {}
            status = turn.get("status")
            err = (turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else None
            facts.append(("turn.stopped", name, {}))
            facts.append(("turn.result", name, {
                **codex_items.result_text(proc, status, err),
                "subtype": "success" if status == "completed" else f"error_{status}",
                "is_error": status != "completed", "duration_ms": turn.get("durationMs"), "turns": 1,
                "cost_usd": None, "tokens_in": proc.spent[0], "tokens_out": proc.spent[1],
                **({"context": last["inputTokens"]} if last.get("inputTokens") else {}),   # the length — the last call
                **codex_limits.result(proc, turn)}))
            proc.spent = [0, 0]
            facts += [f for done in seams("turn_done") for f in done(proc, name, turn)]
            self._flush(proc)
        elif method == "thread/compacted" and proc.compacts and not proc.compacting:
            # without a compaction turn; with one, its turn/completed above gives the fact (not both)
            proc.compacts -= 1
            facts.append(("session.compacted", name, {"ok": True, "before": None, "after": None}))
            facts += [f for done in seams("compacted") for f in done(proc, name)]
        elif method == "error" and not p.get("willRetry"):
            proc.last_text = str((p.get("error") or {}).get("message") or "Codex error")
        else:   # the rest goes to the slices: limits and sign-in (codex_limits), rules and plan (codex_rules), …
            facts = [f for notice in seams("notice") for f in notice(proc, name, method, p)]
        return facts

    def _response(self, name, proc, msg):
        """The answer to our request: the thread is ready — the session is up; a turn refused — a retry another way."""
        method, params = proc.calls.pop(msg.get("id"), (None, None))
        # the answer to a slice's request (limits, sign-in…) goes to the slices first, in SEAMS order
        for response in seams("response"):
            extra = response(proc, name, method, params, msg)
            if extra is not None:
                return extra
        if msg.get("id") == 0 and "result" in msg:
            proc.info["version"] = str(msg["result"].get("userAgent", "")).split(" ")[0].split("/")[-1]
            return []
        if method in ("thread/start", "thread/resume"):
            if "error" in msg:
                # no conversation on disk — a new thread, like claude without history; another refusal (network, sign-in,
                # quota) must not lose the conversation: the process is terminated, the core raises the session again
                if method == "thread/resume" and "no rollout found" in str((msg["error"] or {}).get("message")):
                    if getattr(proc, "account", None) and os.path.abspath(getattr(proc, "home", self.home)) != \
                            os.path.abspath(self.home):
                        print(f"codex: {name}: the conversation is not found from the folder of account {proc.account} — a new thread",
                              file=sys.stderr, flush=True)
                    proc.resume = False
                    proc.call("thread/start", proc.start_params)
                    return []
                tries = vars(proc).get("writer_tries", 0)
                if method == "thread/resume" and "active writer" in str((msg["error"] or {}).get("message")) \
                        and tries < WRITER_TRIES:
                    # the Codex account pool: the session came up in another account's folder while its previous process
                    # still writes the thread — the same thread again in a moment, not a new one (a lost conversation)
                    proc.writer_tries = tries + 1
                    timer = threading.Timer(WRITER_WAIT, lambda: self._put_inbox(
                        name, {"type": RESUME_AGAIN, "popen": proc.popen, "params": params}))
                    timer.daemon = True
                    timer.start()
                    return []
                print(f"codex: {method} {name}: {msg['error']}", file=sys.stderr, flush=True)
                proc.popen.terminate()
                return []
            res = msg.get("result") or {}
            proc.thread = (res.get("thread") or {}).get("id")
            self._remember(name, proc.session_id, proc.thread)
            info = {"host": "codex", "model": res.get("model"), "mode": "bypassPermissions", "cwd": res.get("cwd"),
                    "effort": proc.effort, "version": proc.info.get("version"), "mcp": [], "plugins": [], "builtins": [],
                    "backbone": []}
            for more in seams("info"):
                info.update(more(proc))
            proc.session_info = info   # the core replaces info whole: a slice changing a field later starts from it
            # a thread/start with sessionStartSource "clear" is /clear's new conversation (codex_cmds)
            source = "resume" if method == "thread/resume" else (params or {}).get("sessionStartSource") or "startup"
            facts = [("session.started", name, {"session_id": proc.session_id, "source": source}),
                     ("session.info", name, info)]
            for start in seams("start"):
                start(proc)
            self._flush(proc)
            return facts
        if method == "turn/start":
            proc.starting = False
            if "error" in msg:   # a turn is already running (a race with its start) — the same input as an insertion
                self._again(name, proc, method, params["input"], msg)
            else:
                proc.turn = ((msg.get("result") or {}).get("turn") or {}).get("id") or proc.turn
                proc.refused.pop(id(params["input"]), None)
                proc.message_of.pop(id(params["input"]), None)
                extra = proc.turn_extra["additionalContext"]
                for key, entry in (params.get("additionalContext") or {}).items():
                    if extra.get(key) == entry.get("value"):   # carried — gone; rewritten meanwhile — for the next turn
                        extra.pop(key)
            self._flush(proc)
        elif method == "turn/steer" and "error" in msg:   # the turn managed to end — a new turn with the same input
            proc.turn = None
            self._again(name, proc, method, params["input"], msg)
            self._flush(proc)
        elif method == "turn/steer":
            proc.refused.pop(id(params["input"]), None)
            proc.message_of.pop(id(params["input"]), None)
        return []

    def _again(self, name, proc, method, inp, msg):
        """A refused input goes back to the front of the queue — a race with a turn's start or end; at most RETRIES
        times: an input Codex refuses always (an effort the model does not know, a vanished image) is dropped with a log
        line and its message id forgotten (proc.sent): the core's delivery check retypes it (CONFIRM_SECONDS later — a
        long race resolves by then) and then marks the message undelivered, instead of a loop."""
        n = proc.refused.get(id(inp), 0) + 1
        if n > RETRIES:
            proc.refused.pop(id(inp), None)
            proc.sent.discard(proc.message_of.pop(id(inp), None))
            print(f"codex: {method} {name} refused {n - 1} times in a row, the input is dropped: {msg.get('error')}", file=sys.stderr,
                  flush=True)
            return
        proc.refused[id(inp)] = n
        proc.backlog.insert(0, inp)

    def _flush(self, proc):
        """Input from the queue: to an idle one — one turn/start (the rest as insertions once the turn starts), to a running
        one — turn/steer."""
        if not proc.thread or proc.starting or proc.held:
            return
        while proc.backlog:
            inp = proc.backlog.pop(0)
            if proc.turn:
                proc.call("turn/steer", {"threadId": proc.thread, "expectedTurnId": proc.turn, "input": inp})
            else:
                for before in seams("before_turn"):   # the last word on what this turn/start carries (the effort…)
                    before(proc)
                extra = {k: {"kind": "application", "value": v} for k, v in proc.turn_extra["additionalContext"].items()}
                proc.call("turn/start", {"threadId": proc.thread, "input": inp,
                                         **({"model": proc.model} if proc.model else {}),   # /model (codex_cmds)
                                         **({"effort": proc.effort} if proc.effort else {}),
                                         **({"additionalContext": extra} if extra else {})})
                proc.starting = True
                return

    def _async_ask(self, name, proc, item):
        """request_user_input_async (schema 0.162: AsyncUserInputQuestion {title, options as strings}) — as a card; Codex
        does not wait for the answer (the tool has already been answered {"accepted": true}), the owner's answer comes to
        the session as a message."""
        key = f"cx-{item.get('id') or uuid.uuid4().hex[:12]}"
        qs = _questions(item)
        proc.asks[key] = (None, "async", qs)
        return ("permission.requested", name, {
            "request_id": key, "tool": "AskUserQuestion", "interactive": True, "why": None,
            "input": {"questions": [{"question": q.get("title"), "header": "", "multiSelect": False,
                                     "options": [{"label": str(o), "description": ""} for o in q.get("options") or []]}
                                    for q in qs]}})

    def _request(self, name, proc, rid, method, p):
        """A Codex request to the client: a question to the owner and permissions — to the core as permission.requested."""
        key = f"cx-{rid}"
        if method == "item/tool/requestUserInput":
            qs = p.get("questions") or []
            proc.asks[key] = (rid, method, qs)
            return [("permission.requested", name, {
                "request_id": key, "tool": "AskUserQuestion", "interactive": True, "why": None,
                "input": {"questions": [{"question": q.get("question"), "header": q.get("header"),
                                         "multiSelect": False,
                                         "options": [{"label": o.get("label"), "description": o.get("description")}
                                                     for o in q.get("options") or []]} for q in qs]}})]
        for request in seams("request"):   # elicitation (codex_mcp), permissions (codex_rules)…
            facts = request(proc, name, rid, method, p)
            if facts is not None:
                return facts
        proc.write({"id": rid, "error": {"code": -32601, "message": f"the hub does not support {method}"}})
        return []

    # --- actions ----------------------------------------------------------------------------------------------------
    def type(self, name, text, message=None, attachments=(), **_):
        proc = self.procs.get(name)
        if not self.alive(name):
            raise RuntimeError("no process")
        if message and message in proc.sent:
            return "already sent"
        done = codex_cmds.intercept(self, name, proc, text, message=message, attachments=attachments)
        if done is not None:
            return done
        text = self.mod_input(name, text, message)
        inp = [{"type": "text", "text": text}]
        inp += [{"type": "localImage", "path": a["path"]} for a in attachments or []
                if str(a.get("mime", "")).startswith("image/") and a.get("path")]
        inp = codex_cmds.input(proc, inp, text)
        if message:
            proc.sent.add(message)
            proc.message_of[id(inp)] = message
        proc.backlog.append(inp)
        self._flush(proc)
        return "sent"

    def respond(self, name, request_id, response, **_):
        proc = self.procs.get(name)
        if not self.alive(name) or request_id not in proc.asks:
            raise RuntimeError("no process" if not self.alive(name) else "no such request")
        rid, method, data = proc.asks.pop(request_id)
        if method == "async":
            # Codex does not wait for an answer: the question is a card, the owner's answer comes as a message. A core
            # refusal with its own text (a mini-hub's executor may not, the owner is away — decide yourself) goes to the
            # session as an insertion; "shown as a card" — not
            why = response.get("message") or ""
            if response.get("behavior") != "allow" and why and not _detached(why):
                proc.backlog.append([{"type": "text", "text": why}])
                self._flush(proc)
            return "answered"
        allow = response.get("behavior") == "allow"
        if method == "item/tool/requestUserInput":
            given = (response.get("updatedInput") or {}).get("answers") or {}
            why = response.get("message") or "the owner did not answer"
            result = {"answers": {q.get("id"): {"answers": [str(given.get(q.get("question")) or why)]
                                                if allow else [why]} for q in data}}
        else:
            result = next((r for r in (answer(proc, method, data, response) for answer in seams("answer_request"))
                           if r is not None), None)
            if result is None:
                result = {"decision": "accept" if allow else "decline"}
        proc.write({"id": rid, "result": result})
        return "answered"

    def interrupt(self, name, **_):
        proc = self.procs.get(name)
        if not self.alive(name):
            raise RuntimeError("no process")
        if proc.turn and proc.thread:
            proc.call("turn/interrupt", {"threadId": proc.thread, "turnId": proc.turn})
        for stop in seams("interrupt"):   # the session's subagents, what its turn left running (codex_items)
            stop(proc, name)
        return "interrupted"

    def compact(self, name, text=None, **_):
        proc = self.procs.get(name)
        if not self.alive(name):
            raise RuntimeError("no process")
        if not proc.thread:   # still starting: nothing to compact yet (threadId null would be refused, the count would stick)
            raise RuntimeError("thread not ready")
        done = codex_cmds.compact(self, name, proc, text)
        if done is not None:
            return done
        proc.call("thread/compact/start", {"threadId": proc.thread})
        proc.compacts += 1
        proc.compact_before = ((proc.usage or {}).get("last") or {}).get("inputTokens")
        return "sent"

    def recap(self, name, **_):
        return codex_life.recap(self, name, self.procs.get(name))

    # MCP — hosts/codex_mcp.py
    def mcp_status(self, name, **_):
        return codex_mcp.status(self, name, self.procs.get(name))

    def mcp_reconnect(self, name, server, **_):
        return codex_mcp.reconnect(self, name, self.procs.get(name), server)

    def mcp_login(self, name, server, attempt, **_):
        return codex_mcp.login(self, name, self.procs.get(name), server, attempt)

    def mcp_callback(self, name, server, attempt, url, **_):
        return codex_mcp.callback(self, name, self.procs.get(name), server, attempt, url)

    def mcp_login_poll(self, name, server, attempt, **_):
        return codex_mcp.login_poll(self, name, self.procs.get(name), server, attempt)

    def reap(self, pid, session_id):
        return codex_life.reap(self, pid, session_id)

    def resumable(self, name, sess):
        """Whether the session's next process continues its conversation here (core/system.returning); None — not ours."""
        return codex_life.resumable(self, name, sess)


class MuxHost:
    """Two hosts under one: a session on a codex/… model — CodexHost, the others claude (StreamHost). A model change is a
    new process on the other host (the conversation does not move: each has its own)."""
    codex_models = True

    def __init__(self, stream, codex):
        object.__setattr__(self, "stream", stream)
        object.__setattr__(self, "codex", codex)
        object.__setattr__(self, "where", {})   # session name -> "stream" | "codex"
        object.__setattr__(self, "held", [])    # Codex lines read after the poll moment (stamped): to the next poll

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def __setattr__(self, attr, value):   # hubd plugs in modules, tool_flags, session_env — into both
        setattr(self.stream, attr, value)
        setattr(self.codex, attr, value)

    def host_of(self, name):
        return self.where.get(name)

    def prepare_codex(self, home):   # the Codex account pool's folders are the Codex half's (CodexHost.prepare_codex)
        return self.codex.prepare_codex(home)

    def _h(self, name):
        return self.codex if self.where.get(name) == "codex" else self.stream

    def spawn(self, name, session_id, **kw):
        to = "codex" if is_codex(kw.get("model")) and not kw.get("liaison") else "stream"
        was, old = self.where.get(name), self._h(name)
        proc = old.procs.get(name)
        if was and was != to and proc is not None and not proc.leaving:
            # a model change normally comes after release (the spawn waits for the exit, leaving); a live one is stopped here.
            # A leaving one finishes in its host's thread: its host.done release comes through stamped below
            old.kill(name)
            old.procs.pop(name, None)
        self.where[name] = to
        if to == "codex":
            return self.codex.spawn(name, session_id, **kw)
        return self.stream.spawn(name, session_id, **kw)

    def alive(self, name):
        return self._h(name).alive(name)

    def info(self, name):
        return self._h(name).info(name)

    def leaving(self, name, session_id=None):
        """A released process of this session on either host (daemon.wait_release reads it from the class)."""
        return self.stream.leaving(name, session_id) or self.codex.leaving(name, session_id)

    def liaison_settings(self):
        return self.stream.liaison_settings()   # the liaison always goes to claude (spawn above)

    def sessions(self):
        return self.stream.sessions() + self.codex.sessions()

    def stamped(self):
        """One poll moment for both: the stream host is polled first, so its moment is the earlier one; Codex lines read
        after it wait for the next poll — daemon.collect takes the mailbox hooks up to the moment, and a hook before a
        line must not come after it."""
        until, lines = self.stream.stamped()
        _, more = self.codex.stamped()
        lines = sorted(self.held + lines + more, key=lambda f: f[0])
        object.__setattr__(self, "held", [f for f in lines if f[0] > until])
        return until, [f for f in lines if f[0] <= until]

    def poll(self):
        return [f[1:] for f in self.stamped()[1]]

    def reap(self, pid, session_id):
        # each host knows only its own processes (claude: the session id in the command line, Codex: the tree marker)
        return self.stream.reap(pid, session_id) or self.codex.reap(pid, session_id)

    def resumable(self, name, sess):
        # as spawn routes: a codex/… model goes to Codex, the liaison always to claude
        return self.codex.resumable(name, sess) if is_codex(sess.get("model")) and not sess.get("liaison") else None

    def shutdown(self, wait=8.0, final=False):
        self.stream.shutdown(wait, final)
        self.codex.shutdown(wait, final)

    def kill_server(self):
        self.stream.kill_server()
        self.codex.kill_server()


for _do in ("type", "respond", "recap", "compact", "interrupt", "release", "kill", "end_tree", "mcp_status",
            "mcp_reconnect", "mcp_login", "mcp_callback", "mcp_login_poll"):
    setattr(MuxHost, _do, (lambda do: lambda self, name, *a, **kw: getattr(self._h(name), do)(name, *a, **kw))(_do))
